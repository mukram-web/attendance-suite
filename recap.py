"""
recap.py — the week just gone, scored against what should have happened.

Pure: DATA in, a JSON-safe dict out. No I/O, no Streamlit, so it is unit-tested
on its own and `pipeline.py` can drop the result straight into the store.

WHY THIS IS NOT A LEAGUE TABLE OF RAW PERCENTAGES
-------------------------------------------------
Attendance decays over a cohort's life — every batch since B17 opens at 46-59%
and falls about 10% a week. So a raw ranking answers "which batch is youngest?",
not "which session went well". Measured on the live store, B38 (2 sessions old)
averages 48.2% and B17 (38 sessions) averages 22.6%, yet B17 was AHEAD of B38 at
every comparable point in its life — 56.1% vs 52.1% at session one.

Rank raw and B38 wins session-of-the-week every single week until it ages out.
That is not an award, it is a calendar.

So every headline here is a RESIDUAL: what the session actually drew, over what
the decay curve says a batch of that age, that level and that pod should have
drawn. An index of 1.06 means "6% above where these cohorts normally are by
now", which is a claim about the session rather than about its age. The curve,
the per-batch offset and the pod multiplier all already exist in `forecast.py`
and are refitted every run, so the yardstick moves with the programme.

THE ONE HONEST CAVEAT
---------------------
The curve is fitted on every session INCLUDING the one being scored, so an
outlier partly raises its own expectation and its index moves less than its raw
attendance did. With 22 batches and 470 sessions the pull is small, but it is
never zero and it always flatters an outlier. That is the right trade for an
award — every session in a given week is measured against the same yardstick,
which is what makes them comparable — but the index is not a clean out-of-sample
score and must not be quoted as one. `test_recap` pins this behaviour so it
cannot be quietly forgotten.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No "best retention" award: retention needs per-attendee join/leave times, which
the weekly run does not fetch (see `live_data.fetch_new_attendees` — it pulls
only unmarked sessions). Inventing one from attendance would be a different
metric wearing the same name.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta

import forecast as _f
import polls as _polls

# A week runs Monday-Sunday, matching how the sessions are actually scheduled
# and how the Attendance tab's period picker already groups them.
_WEEK_START = 0          # Monday, per date.weekday()

# Below this many responses a poll is one opinionated table, not a signal. Used
# only to gate the AWARDS — the numbers themselves are always shown, because
# hiding a small sample makes it look like no poll ran at all.
MIN_POLL_N = 20

# An award needs a real field to win. One session in a category is a fact, not
# a competition, and calling it "of the week" reads as praise nobody earned.
MIN_CONTENDERS = 3


def week_start(d: date) -> date:
    """The Monday on or before `d`."""
    return d - timedelta(days=(d.weekday() - _WEEK_START) % 7)


def _expected_pct(fit: dict, pod_mult: dict, batch: str, wk: int, pod: str) -> float | None:
    """What the curve says this batch, at this age, in this pod should draw.

    None when the week is beyond anything ever observed — the curve has no point
    there and extrapolating an exponential tail invents a number. A session with
    no expectation is reported with a blank index rather than a fabricated one.
    """
    curve, offsets = fit.get("curve") or {}, fit.get("offsets") or {}
    if wk not in curve:
        return None
    rate = math.exp(curve[wk] + offsets.get(batch, 0.0))
    if pod:
        rate *= pod_mult.get(pod, 1.0)
    return rate if rate > 0 else None


def _dates_by_mm(DATA: dict, today: date) -> dict:
    """{batch: {mm: date}} using forecast's proven year-walker.

    DATA carries no year at all — every session is just {'mm': '08_22'} — so
    anything week-based has to attach one first. `batch_dates` anchors on the
    last row (which has already happened) and walks backwards, advancing the
    year whenever the month goes backwards.
    """
    out: dict = {}
    for code, pairs in _f.batch_dates(DATA, today).items():
        out[code] = {r["mm"]: d for d, r in pairs if r.get("mm")}
    return out


def collect_sessions(DATA: dict, today: date) -> list:
    """Every session in DATA as a flat, dated, residual-scored row.

    One row per (batch, date, pod) — the same grain the dashboard shows — so a
    date on which eleven pods met contributes eleven rows, each judged against
    its own pod's expectation rather than the batch's.
    """
    fit = _f.fit_curve(DATA, today)
    try:
        pod_mult, _detail = _f._pod_multipliers(DATA, today)
    except Exception:
        pod_mult = {}
    start = fit.get("start") or {}
    dmap = _dates_by_mm(DATA, today)
    rows = []
    for code, d in DATA.items():
        dates = dmap.get(code) or {}
        for s in d.get("sessions") or ():
            # Intro-call rows are synthesised by the pipeline from a separate
            # sheet and have no webinar, no poll and no place in the curve.
            if s.get("is_intro") or not s.get("mm"):
                continue
            dt = dates.get(s["mm"])
            if dt is None or dt > today:
                continue
            wk = round((dt - start[code]).days / 7) if code in start else None
            exp = (_expected_pct(fit, pod_mult, code, wk, s.get("pod") or "")
                   if wk is not None else None)
            pct = s.get("pct")
            rows.append({
                # PASS THE SESSION DICT THROUGH rather than re-listing fields.
                # Hand-copying is how `l2_batch` and `rating_trainer` went
                # missing: both were sitting in `s` and simply were not on the
                # list, so the Sessions tab showed a bare batch code and an
                # empty Trainer rating column on all 522 rows. Anything data.py
                # adds to a session from now on arrives here for free.
                **s,
                "batch": code,
                "date": dt.isoformat(),
                "week": week_start(dt).isoformat(),
                "topic": s.get("topic") or "",
                "l2_batch": s.get("l2_batch") or "",
                "pod": s.get("pod") or "",
                "mentor": (s.get("mentor") or "").strip(),
                "present": s.get("present") or 0,
                "total": s.get("total") or 0,
                "rating_n": s.get("rating_n") or 0,
                "wk": wk,
                "expected_pct": round(exp, 1) if exp else None,
                # The headline. 1.0 = exactly on curve for a cohort this old.
                "index": (round(pct / exp, 3) if (exp and pct is not None) else None),
                # Short aliases the recap and trainer rollups already use.
                "nps": s.get("rating_nps"),
                "dist": s.get("rating_dist") or {},
            })
    rows.sort(key=lambda r: (r["date"], r["batch"], r["pod"]))
    return rows


def _agg(rows: list) -> dict:
    """Roll a set of session rows into one weekly line.

    Attendance is pooled (sum present / sum invited), never a mean of the
    per-session percentages: a whole-batch session of 3,000 and a pod session of
    59 are not two equal opinions about the week.
    """
    present = sum(r["present"] for r in rows)
    total = sum(r["total"] for r in rows)
    idx = [r["index"] for r in rows if r["index"] is not None]
    # The index is weighted the same way, by invited headcount.
    wsum = sum(r["total"] for r in rows if r["index"] is not None)
    widx = (sum(r["index"] * r["total"] for r in rows if r["index"] is not None) / wsum
            if wsum else None)
    merged = _polls.merge_dists(r["dist"] for r in rows)
    rated = [r for r in rows if r["rating"] is not None]
    rn = sum(r["rating_n"] for r in rated)
    return {
        "sessions": len(rows),
        "batches": sorted({r["batch"] for r in rows}),
        "present": present,
        "invited": total,
        "pct": round(present / total * 100, 1) if total else None,
        "index": round(widx, 3) if widx else None,
        "n_indexed": len(idx),
        "rating": (round(sum(r["rating"] * r["rating_n"] for r in rated) / rn, 2)
                   if rn else None),
        "rating_n": rn,
        "nps": _polls.nps_from_dist(merged.get("recommend")),
        "dist": merged,
    }


def _delta(cur, prev):
    """Signed change, or None when either side is missing.

    None is not 0. A week with no comparable prior week must not render as
    "no change" — that is a claim, and it would be a false one.
    """
    if cur is None or prev is None:
        return None
    return round(cur - prev, 3)


def _awards(rows: list) -> list:
    """The week's honours, each judged on the residual — never on raw pct.

    Every award states its own basis in `why`, because an award with an unstated
    rule is just an assertion.
    """
    out = []
    indexed = [r for r in rows if r["index"] is not None]
    if len(indexed) >= MIN_CONTENDERS:
        w = max(indexed, key=lambda r: r["index"])
        out.append({
            "award": "Session of the week",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"{w['index']:.2f}x",
            "why": (f"drew {w['pct']:.1f}% where a {w['batch']} session "
                    f"{w['wk']} weeks in normally draws {w['expected_pct']:.1f}%"),
        })
    polled = [r for r in rows if r["nps"] is not None and r["rating_n"] >= MIN_POLL_N]
    if len(polled) >= MIN_CONTENDERS:
        w = max(polled, key=lambda r: r["nps"])
        out.append({
            "award": "Crowd favourite",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"NPS {w['nps']:+d}",
            "why": f"from {w['rating_n']} responses",
        })
    if len(rows) >= MIN_CONTENDERS:
        w = max(rows, key=lambda r: r["present"])
        out.append({
            "award": "Biggest room",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"{w['present']:,}",
            "why": f"of {w['total']:,} invited",
        })
    return out


def build(DATA: dict, today: date, weeks: int = 8) -> dict:
    """The recap payload: recent weeks, the latest week's detail, awards.

    `weeks` bounds how much history is summarised, not how much is scored —
    every session is still used to fit the curve the residuals are measured
    against, so a short window does not change anyone's index.
    """
    rows = collect_sessions(DATA, today)
    if not rows:
        return {"weeks": [], "latest": None, "awards": [], "leaderboard": [],
                "sessions": [], "warnings": ["no dated sessions to recap"]}

    by_week = defaultdict(list)
    for r in rows:
        by_week[r["week"]].append(r)
    ordered = sorted(by_week)

    weekly = []
    for i, wk in enumerate(ordered):
        cur = _agg(by_week[wk])
        prev = _agg(by_week[ordered[i - 1]]) if i else None
        cur["week"] = wk
        cur["delta"] = {
            "pct": _delta(cur["pct"], prev and prev["pct"]),
            "index": _delta(cur["index"], prev and prev["index"]),
            "nps": _delta(cur["nps"], prev and prev["nps"]),
            "rating": _delta(cur["rating"], prev and prev["rating"]),
            "present": _delta(cur["present"], prev and prev["present"]),
        }
        weekly.append(cur)

    latest_week = ordered[-1]
    latest_rows = by_week[latest_week]

    # Trainer leaderboard, latest week, on the residual. Co-taught cells are
    # left as the raw L2 string here on purpose - splitting them is trainers.py's
    # job, and doing it twice in two ways is how the two views start disagreeing.
    per_mentor = defaultdict(list)
    for r in latest_rows:
        if r["mentor"]:
            per_mentor[r["mentor"]].append(r)
    board = []
    for name, rs in per_mentor.items():
        a = _agg(rs)
        board.append({"mentor": name, "sessions": len(rs), "present": a["present"],
                      "invited": a["invited"], "pct": a["pct"], "index": a["index"],
                      "nps": a["nps"], "rating": a["rating"], "rating_n": a["rating_n"]})
    # Unindexed trainers sort last rather than being dropped - a name with no
    # curve point still taught, and vanishing from the board reads as "did not".
    board.sort(key=lambda b: (b["index"] is None, -(b["index"] or 0)))

    return {
        "weeks": weekly[-weeks:],
        "latest": next(w for w in weekly if w["week"] == latest_week),
        "awards": _awards(latest_rows),
        "leaderboard": board,
        "sessions": latest_rows,
        "warnings": [],
    }
