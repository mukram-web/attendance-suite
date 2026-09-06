"""
forecast.py — predicted attendance for sessions that have not run yet.

Pure and Streamlit-free, like data.py: it takes the dashboard `DATA` object the
pipeline has already built plus the Master Curriculum Schedule's tabs, and
returns aggregates only (no PII). Everything is unit-testable without Google.

    build(DATA, curric_tabs, today, horizon_weeks) -> dict

THE MODEL
---------
    predicted = denominator x curve(week) x batch_offset x pod_multiplier

  curve(week)     the pooled decay of attendance against weeks-since-batch-start.
                  Measured across 22 batches / 447 sessions: every batch B17-B38
                  opens at 46-59% and falls ~10% a week. This is the strong part
                  of the model - the shape barely moves between cohorts.
  batch_offset    that batch's own level against the curve, from its sessions so
                  far. This is what notices a batch running cold: B36 sat at
                  x0.863 (14% below typical) as of 30 Aug 2026.
  pod_multiplier  a pod's draw against its batch's average for the same day.
                  Data x1.13 and Ops/Supply Chain x1.12 pull best, Students
                  x0.89 worst. Shrunk toward 1.0 - see _pod_multipliers.

Measured accuracy (rolling-origin backtest over every batch, predicting each
batch's future sessions from only its own past): 8.3% MAPE on headcount one week
out, 10.4% at four weeks, 16.0% at eleven. Carrying the last observed percentage
forward instead scores 33.5%. `accuracy` in the returned dict re-measures this on
the live data every run, so a model that decays in the field says so itself.

WHAT IS WEAK, AND WHY IT IS SEPARATE
------------------------------------
The batch-level curve rests on 22 batches. The POD split only began 2026-08-23,
so the pod layer rests on a few dozen sessions and is the part to distrust. It is
kept as its own multiplicative term precisely so it can be inspected, shrunk, and
- when a pod has no history at all - dropped to 1.0 rather than silently guessed.

Batches that have not started have no roster, so their strength and pod shares
are ASSUMED from the recent-batch mean. Every such row is flagged
`assumed_strength: True`, and the app renders those differently. Never let an
assumed row read like a measured one.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, timedelta

# Sessions are per-pod only from this date on; before it a date is one
# whole-batch session. Used to explain, not to gate — the gate is _split_kind.
POD_ERA_START = date(2026, 8, 23)

# A (date, batch) is a pod split when the schedule lists at least this many
# DISTINCT normalised topics across the pod tabs. Measured against what actually
# ran: whole-batch dates show 1-3 distinct titles (pure spelling variants, e.g.
# "Prompt Engineering 2026" vs "Prompt Engineering in 2026"), real pod splits
# show 8-12. The gap is wide, so the threshold is not delicate.
SPLIT_MIN_TOPICS = 5

# Shrinkage weight for pod multipliers: mult = 1 + (raw - 1) * n / (n + K).
# With K=3 a pod needs ~3 observed sessions before it carries half its own
# measured effect. Set from how thin the pod era is, not tuned for fit.
POD_SHRINK_K = 3.0

# Batch strength / pod shares for a batch that has not started are the mean of
# this many most recent started batches.
RECENT_BATCHES = 4

# 80% interval. Errors are near-lognormal, so the band is multiplicative.
Z80 = 1.2816

_BATCH_RE = re.compile(r"^\s*(?:AI\s*CAP\s*)?(B\d+)\s*$", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["", "jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

# A trainer cell holds a PERSON'S NAME and nothing else. The column to the right
# of a topic is not reliably the trainer: measured on the real sheet it also
# carries session times ("11:00 AM", "7:30 PM"), a stray batch code ("B52"), and
# topic text bled in from a merged cell. Names have no digits, so accept only
# letters and ordinary name punctuation and drop everything else — showing a
# time under "Trainer" is worse than showing nothing, because it reads as data.
_TRAINER_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,39}$")

# Words that carry no meaning when deciding whether two titles are "the same
# session". Kept small on purpose: over-stripping merges genuinely different pod
# topics and would make a real split look like a whole-batch day.
_STOP = {"a", "an", "the", "of", "with", "for", "and", "in", "to", "your",
         "using", "&", "2026", "2027", "part", "1", "2"}


# ── small helpers ────────────────────────────────────────────────────────────
def topic_key(s) -> str:
    """Order-insensitive normalised form of a session title, for equality only."""
    words = re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).split()
    return " ".join(sorted({w for w in words if w not in _STOP}))


def pod_key(s) -> str:
    return re.sub(r"[^a-z]", "", str(s or "").lower())


def clean_trainer(cell) -> str:
    """The trainer's name, or "" when the cell holds something that is not one.

    openpyxl hands back a real time object for a time cell, which str()s to
    "19:30:00" — the digit rule catches that too, but reject it by type first so
    the intent is explicit rather than incidental."""
    if cell is None or hasattr(cell, "hour"):        # datetime / time cell
        return ""
    v = str(cell).strip()
    return v if _TRAINER_RE.match(v) else ""


def batch_code(s) -> str | None:
    m = _BATCH_RE.match(str(s or ""))
    return m.group(1).upper() if m else None


def _parse_daymonth(cell) -> tuple[int, int] | None:
    """'8 Aug', '8-Aug', 'Aug 8', a datetime — returns (month, day)."""
    if hasattr(cell, "month") and hasattr(cell, "day"):
        return int(cell.month), int(cell.day)
    s = str(cell or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})\s*[-/ ]?\s*([A-Za-z]{3,})", s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        return (mon, int(m.group(1))) if mon else None
    m = re.match(r"^([A-Za-z]{3,})\s*[-/ ]?\s*(\d{1,2})", s)
    if m:
        mon = _MONTHS.get(m.group(1)[:3].lower())
        return (mon, int(m.group(2))) if mon else None
    return None


def _walk_years(md_list, anchor: date, anchor_idx: int = 0) -> list[date]:
    """Attach years to a CHRONOLOGICAL list of (month, day) pairs.

    Neither the roster's session labels ('30 Aug') nor the curriculum sheet carry
    a year, so a schedule crossing new year would otherwise sort December after
    January. The list is known to be in order, so the year only ever advances,
    and it advances exactly when the month goes backwards. `anchor` fixes the
    year of entry `anchor_idx`; everything else is derived from it.
    """
    out: list[date | None] = [None] * len(md_list)
    y = anchor.year
    for i in range(anchor_idx, len(md_list)):          # forward: year advances
        mo, dy = md_list[i]
        if i > anchor_idx and (mo, dy) < (md_list[i - 1][0], md_list[i - 1][1]):
            y += 1
        out[i] = _safe_date(y, mo, dy)
    y = anchor.year
    for i in range(anchor_idx - 1, -1, -1):            # backward: year retreats
        mo, dy = md_list[i]
        if (mo, dy) > (md_list[i + 1][0], md_list[i + 1][1]):
            y -= 1
        out[i] = _safe_date(y, mo, dy)
    return [d for d in out if d is not None]


def _safe_date(y, m, d) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:                    # 30 Feb and friends in a typo'd cell
        return None


# ── 1. the decay curve and per-batch offsets ─────────────────────────────────
def batch_dates(DATA: dict, today: date) -> dict[str, list[tuple[date, dict]]]:
    """Each batch's by_date rows with real dates attached, oldest first.

    by_date is written in worksheet column order, which is chronological, so the
    last entry is the most recent session and cannot be in the future — that is
    the anchor _walk_years needs.
    """
    out = {}
    for code, d in DATA.items():
        rows = [r for r in (d.get("by_date") or []) if r.get("mm")]
        if not rows:
            continue
        mds = []
        for r in rows:
            try:
                mo, dy = str(r["mm"]).split("_")
                mds.append((int(mo), int(dy)))
            except (ValueError, KeyError):
                mds.append(None)
        if any(x is None for x in mds):
            continue
        # anchor the LAST row: it has already happened, so it is this year unless
        # that would put it in the future (a batch running across new year).
        last_mo, last_dy = mds[-1]
        anchor_year = today.year
        if (last_mo, last_dy) > (today.month, today.day):
            anchor_year -= 1
        anchor = _safe_date(anchor_year, last_mo, last_dy)
        if anchor is None:
            continue
        dates = _walk_years(mds, anchor, anchor_idx=len(mds) - 1)
        if len(dates) != len(rows):
            continue
        out[code] = list(zip(dates, rows))
    return out


def fit_curve(DATA: dict, today: date) -> dict:
    """Pooled log-attendance by week-since-start, plus each batch's own offset.

    Returns curve/offsets/sd_by_horizon/start/strength. A week with no history
    has no curve point; callers must treat that as "cannot forecast", never as
    zero — an unseen week is silence, not an empty room.
    """
    bd = batch_dates(DATA, today)
    start, strength, obs = {}, {}, defaultdict(list)
    for code, rows in bd.items():
        start[code] = rows[0][0]
        strength[code] = DATA[code].get("strength") or 0
        for dt_, r in rows:
            pct = r.get("pct")
            if not pct or pct <= 0:
                continue
            wk = round((dt_ - start[code]).days / 7)
            obs[code].append((wk, dt_, math.log(pct)))

    sums = defaultdict(float)
    counts = defaultdict(int)
    for code, xs in obs.items():
        for wk, _dt, lp in xs:
            sums[wk] += lp
            counts[wk] += 1
    curve = {wk: sums[wk] / counts[wk] for wk in sums}

    offsets = {}
    for code, xs in obs.items():
        res = [lp - curve[wk] for wk, _dt, lp in xs if wk in curve]
        offsets[code] = sum(res) / len(res) if res else 0.0

    return {"curve": curve, "curve_n": dict(counts), "offsets": offsets,
            "start": start, "strength": strength, "obs": obs,
            "sd_by_horizon": _backtest(obs, curve)}


def _backtest(obs: dict, curve: dict) -> dict:
    """Rolling-origin error spread by forecast horizon, in log space.

    For every batch and every cut-point, fit the offset on the sessions before
    the cut and score the ones after it. This is what the prediction intervals
    are made of, and it is re-measured each run rather than hardcoded — if the
    programme changes shape, the bands widen on their own.
    """
    err = defaultdict(list)
    for code, xs in obs.items():
        xs = sorted(xs, key=lambda t: t[1])
        for k in range(2, len(xs)):
            train, test = xs[:k], xs[k:]
            res = [lp - curve[wk] for wk, _d, lp in train if wk in curve]
            if not res:
                continue
            off = sum(res) / len(res)
            last = train[-1][1]
            for wk, dt_, lp in test:
                if wk not in curve:
                    continue
                h = max(1, round((dt_ - last).days / 7))
                err[h].append(lp - (curve[wk] + off))
    return {h: _sd(v) for h, v in err.items() if len(v) >= 8}


def _sd(v) -> float:
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def accuracy(fit: dict) -> dict:
    """Headline MAPE by horizon plus the carry-forward baseline, for display.

    Shown in the UI so nobody has to take the model's word for it."""
    obs, curve = fit["obs"], fit["curve"]
    mod, base = defaultdict(list), defaultdict(list)
    for code, xs in obs.items():
        xs = sorted(xs, key=lambda t: t[1])
        for k in range(2, len(xs)):
            train, test = xs[:k], xs[k:]
            res = [lp - curve[wk] for wk, _d, lp in train if wk in curve]
            if not res:
                continue
            off = sum(res) / len(res)
            last_dt, last_lp = train[-1][1], train[-1][2]
            for wk, dt_, lp in test:
                if wk not in curve:
                    continue
                h = max(1, round((dt_ - last_dt).days / 7))
                act = math.exp(lp)
                mod[h].append(abs(math.exp(curve[wk] + off) - act) / act * 100)
                base[h].append(abs(math.exp(last_lp) - act) / act * 100)

    def _avg(d, lo, hi):
        v = [x for h, xs in d.items() if lo <= h <= hi for x in xs]
        return round(sum(v) / len(v), 1) if v else None

    return {
        "mape_by_horizon": {h: round(sum(v) / len(v), 1) for h, v in sorted(mod.items())},
        "mape_1_4wk": _avg(mod, 1, 4),
        "mape_5_8wk": _avg(mod, 5, 8),
        "baseline_mape_1_4wk": _avg(base, 1, 4),
        "n_scored": sum(len(v) for v in mod.values()),
    }


# ── 2. pod strengths and multipliers ─────────────────────────────────────────
def _pod_multipliers(DATA: dict, today: date) -> tuple[dict, dict]:
    """How each pod draws against its own batch's average for the SAME day.

    Comparing within a day is what makes this a clean pod effect: batch level and
    week-of-life both cancel, so what is left is the pod. Shrunk toward 1.0 by
    POD_SHRINK_K because the pod era is young.
    """
    ratios = defaultdict(list)
    for code, d in DATA.items():
        day_pct = {r["mm"]: r["pct"] for r in (d.get("by_date") or [])
                   if r.get("pct")}
        for s in d.get("sessions", []):
            pod, mm, pct = s.get("pod"), s.get("mm"), s.get("pct")
            if not pod or not mm or not pct:
                continue
            base = day_pct.get(mm)
            if base:
                ratios[pod].append(pct / base)
    mult, detail = {}, {}
    for pod, v in ratios.items():
        raw = sum(v) / len(v)
        m = 1 + (raw - 1) * len(v) / (len(v) + POD_SHRINK_K)
        mult[pod] = m
        detail[pod] = {"n": len(v), "raw": round(raw, 3), "mult": round(m, 3)}
    return mult, detail


def _pod_strengths(DATA: dict, recent: list[str]) -> tuple[dict, dict]:
    """Per-batch pod headcounts, and the mean pod SHARE across recent batches.

    The share is the fallback for a batch that has not started. It is genuinely
    unstable — Generalist ran 18.9% of B37 and 48.6% of B38 — which is why every
    row built from it is flagged assumed_strength.
    """
    known = {}
    shares = defaultdict(list)
    for code, d in DATA.items():
        pods = d.get("pods") or {}
        stg = d.get("strength") or 0
        for pod, v in pods.items():
            known[(code, pod)] = v.get("strength") or 0
            if stg and code in recent:
                shares[pod].append((v.get("strength") or 0) / stg)
    mean_share = {p: sum(v) / len(v) for p, v in shares.items() if v}
    return known, mean_share


# ── 3. the curriculum schedule ───────────────────────────────────────────────
def parse_curriculum(tabs: dict, today: date, pod_names: list[str]) -> tuple[list, list]:
    """Rows of {date, batch, pod, topic, trainer} from the Master Curriculum tabs.

    Each tab is one pod and looks like:
        Date | Day | AI CAP B35 | Trainer | AI CAP B36 | Trainer | ...
    Tab names are matched to the dashboard's pod names ('General' -> 'Generalist').
    An unmatched tab is reported rather than dropped silently — a renamed tab
    would otherwise remove a whole pod from the forecast with no symptom.
    """
    warn = []
    by_key = {pod_key(p): p for p in pod_names}
    rows = []
    for tab_name, grid in (tabs or {}).items():
        pod = _match_pod(tab_name, by_key)
        if pod is None:
            warn.append(f"curriculum tab {tab_name!r} matches no pod — skipped")
            continue
        hdr_i, bcols = _find_header(grid)
        if hdr_i is None:
            warn.append(f"curriculum tab {tab_name!r} has no 'AI CAP B<n>' header row")
            continue
        raw = []
        for r in grid[hdr_i + 1:]:
            md = _parse_daymonth(r[0] if r else None)
            if md and md[0]:
                raw.append((md, r))
        if not raw:
            continue
        # anchor the FIRST row to the year that puts it nearest today
        (mo, dy), _ = raw[0]
        cands = [c for c in (_safe_date(today.year - 1, mo, dy),
                             _safe_date(today.year, mo, dy),
                             _safe_date(today.year + 1, mo, dy)) if c]
        anchor = min(cands, key=lambda c: abs((c - today).days))
        dates = _walk_years([md for md, _ in raw], anchor, anchor_idx=0)
        for dt_, (_md, r) in zip(dates, raw):
            for ci, bat in bcols:
                topic = str(r[ci] or "").strip() if ci < len(r) else ""
                trainer = clean_trainer(r[ci + 1]) if ci + 1 < len(r) else ""
                if not topic:
                    continue
                rows.append({"date": dt_, "batch": bat, "pod": pod,
                             "topic": topic, "trainer": trainer})
    return rows, warn


def _match_pod(tab_name: str, by_key: dict) -> str | None:
    k = pod_key(tab_name)
    if not k:
        return None
    if k in by_key:
        return by_key[k]
    for pk, pod in by_key.items():          # 'general' -> 'generalist'
        if pk.startswith(k) or k.startswith(pk):
            return pod
    # 'sales/ marketing/hr' vs 'Sales/Marketing/HR' survive pod_key already;
    # this catches order or spacing drift like 'ops supply chain'.
    toks = set(re.sub(r"[^a-z ]", " ", str(tab_name).lower()).split())
    best, score = None, 0
    for pk, pod in by_key.items():
        pt = set(re.sub(r"[^a-z ]", " ", pod.lower()).split())
        n = len(toks & pt)
        if n > score:
            best, score = pod, n
    return best if score else None


def _find_header(grid) -> tuple[int | None, list]:
    """The row carrying the 'AI CAP B<n>' column titles, and where they sit.

    ONE such column is enough. Requiring two would silently drop a tab that
    happens to schedule a single batch — which is exactly what the last weeks of
    the sheet look like, when only the newest batch is still running.
    """
    for i, row in enumerate(grid[:10]):
        cols = [(j, batch_code(c)) for j, c in enumerate(row)]
        cols = [(j, b) for j, b in cols if b]
        if cols:
            return i, cols
    return None, []


def _split_kind(topics: list[str]) -> str:
    return "pod" if len({topic_key(t) for t in topics}) >= SPLIT_MIN_TOPICS else "whole"


# ── 4. the forecast ──────────────────────────────────────────────────────────
def build(DATA: dict, curric_tabs: dict, today: date,
          horizon_weeks: int = 8) -> dict:
    """Predicted attendance for every scheduled session inside the horizon.

    Aggregates only — safe to ship in the store and to render publicly.
    """
    warnings: list[str] = []
    if not DATA:
        return empty_result("no dashboard data to fit the model on")

    fit = fit_curve(DATA, today)
    curve, offsets = fit["curve"], fit["offsets"]
    start, strength = dict(fit["start"]), dict(fit["strength"])
    if not curve:
        return empty_result("no session history to fit the decay curve on")

    started = sorted(start, key=lambda b: (start[b], b))
    recent = started[-RECENT_BATCHES:]
    pod_mult, pod_detail = _pod_multipliers(DATA, today)
    known_pod, mean_share = _pod_strengths(DATA, recent)
    pod_names = sorted({p for (_b, p) in known_pod})

    rows, cwarn = parse_curriculum(curric_tabs, today, pod_names)
    warnings += cwarn
    if not rows:
        return empty_result("the curriculum schedule produced no rows", warnings)

    # Batches in the schedule that have not started yet: assume the recent mean
    # cadence (one new batch each week) and the recent mean strength.
    mean_strength = (sum(strength[b] for b in recent) / len(recent)) if recent else 0
    mean_offset = (sum(offsets[b] for b in recent) / len(recent)) if recent else 0.0
    assumed: set[str] = set()
    for bat in sorted({r["batch"] for r in rows}):
        if bat in start:
            continue
        first = min(r["date"] for r in rows if r["batch"] == bat)
        start[bat] = first
        strength[bat] = mean_strength
        offsets[bat] = mean_offset
        assumed.add(bat)
    if assumed:
        warnings.append(
            f"{len(assumed)} batch(es) have not started ({', '.join(sorted(assumed))}) — "
            f"strength assumed at {round(mean_strength):,} and pod shares at the "
            f"{len(recent)}-batch mean. Pod shares vary widely between batches, so "
            "treat these as indicative.")

    horizon_end = today + timedelta(weeks=horizon_weeks)
    grouped = defaultdict(list)
    for r in rows:
        if today < r["date"] <= horizon_end:
            grouped[(r["date"], r["batch"])].append(r)

    out, skipped_weeks = [], set()
    for (dt_, bat), g in sorted(grouped.items()):
        wk = round((dt_ - start[bat]).days / 7)
        if wk < 0:
            continue
        if wk not in curve:
            skipped_weeks.add(wk)
            continue
        h = max(1, round((dt_ - today).days / 7))
        sd = _sd_for(fit["sd_by_horizon"], h)
        rate = math.exp(curve[wk] + offsets[bat])
        kind = _split_kind([r["topic"] for r in g])
        if kind == "whole":
            top = max(g, key=lambda r: len(r["topic"]))
            out.append(_row(dt_, bat, "ALL", top["topic"], top["trainer"], "whole",
                            wk, h, strength[bat], rate, sd, bat in assumed))
        else:
            seen = set()
            for r in sorted(g, key=lambda r: r["pod"]):
                if r["pod"] in seen:          # merged cells can repeat a pod
                    continue
                seen.add(r["pod"])
                den = known_pod.get((bat, r["pod"]))
                if den is None:
                    den = mean_share.get(r["pod"], 0) * strength[bat]
                if not den or den <= 0:
                    continue
                out.append(_row(dt_, bat, r["pod"], r["topic"], r["trainer"], "pod",
                                wk, h, den, rate * pod_mult.get(r["pod"], 1.0),
                                sd, bat in assumed or (bat, r["pod"]) not in known_pod))
    if skipped_weeks:
        warnings.append(
            f"{len(skipped_weeks)} scheduled week(s) sit beyond any batch's observed "
            f"lifetime (week {min(skipped_weeks)}+) — no curve point exists, so those "
            "sessions are not forecast rather than being extrapolated.")
    thin = sorted(p for p, d in pod_detail.items() if d["n"] < POD_SHRINK_K)
    if thin:
        warnings.append(f"{len(thin)} pod(s) have fewer than {int(POD_SHRINK_K)} "
                        f"observed sessions ({', '.join(thin)}) — their multipliers "
                        "are shrunk hard toward the batch average.")

    by_session = _roll_up_sessions(out)

    by_date = defaultdict(lambda: {"sessions": 0, "batches": set(),
                                   "pred": 0, "lo": 0, "hi": 0})
    for r in out:
        a = by_date[r["date"]]
        a["sessions"] += 1
        a["batches"].add(r["batch"])
        for k in ("pred", "lo", "hi"):
            a[k] += r[k]
    dates = [{"date": d, "sessions": v["sessions"], "batches": len(v["batches"]),
              "pred": v["pred"], "lo": v["lo"], "hi": v["hi"]}
             for d, v in sorted(by_date.items())]

    return {
        "generated_for": today.isoformat(),
        "horizon_weeks": horizon_weeks,
        "sessions": out,
        "by_session": by_session,
        "by_date": dates,
        "accuracy": accuracy(fit),
        "curve": {str(k): round(math.exp(v), 1) for k, v in sorted(curve.items())},
        "curve_n": {str(k): v for k, v in sorted(fit["curve_n"].items())},
        "batch_offset": {b: round(math.exp(o), 3) for b, o in sorted(offsets.items())},
        "pod_multiplier": pod_detail,
        "assumed_batches": sorted(assumed),
        "warnings": warnings,
    }


def _roll_up_sessions(rows: list) -> list:
    """One entry per SESSION — a (date, topic, pod) that several batches sit in —
    carrying the per-batch split and the total.

    This is how the programme is actually run and staffed: "Build Your Marketing
    OS on 6 Sep draws ~230 across B35/B36/B37", not three unrelated numbers. The
    per-batch rows in `sessions` stay the source of truth; this is a view of them.

    The total's interval SUMS each batch's lo/hi rather than combining them in
    quadrature. Batches sharing a date share their shocks — a festival weekend, a
    broken Zoom link, a holiday — so treating the errors as independent would
    quote a false precision on exactly the days most likely to surprise you.
    """
    g = defaultdict(list)
    for r in rows:
        g[(r["date"], topic_key(r["topic"]), r["pod"])].append(r)
    out = []
    for (dt_, _tk, pod), rs in g.items():
        rs = sorted(rs, key=lambda r: r["batch"])
        # display the longest title variant — the sheet's spellings differ and the
        # fullest one is the most informative
        topic = max((r["topic"] for r in rs), key=len)
        trainer = next((r["trainer"] for r in rs if r["trainer"]), "")
        out.append({
            "date": dt_, "date_lbl": rs[0]["date_lbl"], "topic": topic,
            "pod": pod, "trainer": trainer, "kind": rs[0]["kind"],
            "batches": [r["batch"] for r in rs],
            "n_batches": len(rs),
            "per_batch": {r["batch"]: {"pred": r["pred"], "lo": r["lo"],
                                       "hi": r["hi"], "denom": r["denom"],
                                       "pred_pct": r["pred_pct"], "wk": r["wk"],
                                       "assumed": r["assumed_strength"]}
                          for r in rs},
            "denom": sum(r["denom"] for r in rs),
            "pred": sum(r["pred"] for r in rs),
            "lo": sum(r["lo"] for r in rs),
            "hi": sum(r["hi"] for r in rs),
            "pred_pct": round(100 * sum(r["pred"] for r in rs)
                              / max(1, sum(r["denom"] for r in rs)), 1),
            "any_assumed": any(r["assumed_strength"] for r in rs),
        })
    return sorted(out, key=lambda s: (s["date"], s["pod"], s["topic"]))


def _row(dt_, bat, pod, topic, trainer, kind, wk, h, den, rate, sd, assumed) -> dict:
    n = den * rate / 100
    return {
        "date": dt_.isoformat(),
        "date_lbl": f"{dt_.day} {dt_.strftime('%b')}",
        "batch": bat, "pod": pod, "topic": topic, "trainer": trainer,
        "kind": kind, "wk": wk, "horizon_wks": h,
        "denom": round(den),
        "pred_pct": round(rate, 1),
        "pred": round(n),
        "lo": round(n * math.exp(-Z80 * sd)),
        "hi": round(n * math.exp(Z80 * sd)),
        "assumed_strength": bool(assumed),
    }


def _sd_for(sd_by_h: dict, h: int) -> float:
    """The measured spread at horizon h, or the nearest shorter one.

    Falling back to a SHORTER horizon understates the band, so when nothing is
    measured at all we use a deliberately wide default rather than a tight one.
    """
    if not sd_by_h:
        return 0.20
    avail = [x for x in sd_by_h if x <= h]
    return sd_by_h[max(avail)] if avail else sd_by_h[min(sd_by_h)]


def empty_result(reason: str, warnings=None) -> dict:
    """A well-formed but empty section, so callers never branch on None."""
    return {"generated_for": None, "horizon_weeks": 0, "sessions": [],
            "by_session": [], "by_date": [],
            "accuracy": {}, "curve": {}, "curve_n": {}, "batch_offset": {},
            "pod_multiplier": {}, "assumed_batches": [],
            "warnings": (warnings or []) + [reason]}
