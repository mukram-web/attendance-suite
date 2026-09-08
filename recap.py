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

TWO DIFFERENT YARDSTICKS, ON PURPOSE
------------------------------------
**Attendance** awards use the residual, for the reason above. **Rating** awards
use the raw score, because a rating does not decay with cohort age — a 4.8 in a
batch's tenth week is the same claim as a 4.8 in its first. So "Session of the
week" is the best rated session and "Beat the curve" is the best residual, and
neither is pretending to be the other.

RETENTION IS REAL NOW
---------------------
This file used to say a "best retention" award was impossible because the weekly
run did not fetch the attendee reports for past sessions. Since 2026-09-06 it
fetches all 1,284 of them in about 76 seconds, and `sessionmeta.measure` sweeps
the join/leave intervals into a per-minute concurrency curve. Stickiness is the
mean of the closing 30 minutes over the session's peak — of the fullest the room
ever was, how much was still there at the end.
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


# ── one room, several batches ────────────────────────────────────────────────
# From B35 a POD webinar is shared by every batch at that point in the
# curriculum ('AI CAP B35 , B36 , B37 - Finance'), and older batches shared
# whole-batch rooms too ('AI CAP B17 + B21 11AM'). Attendance is rightly one row
# per batch — each is measured against its own roster — but the room, the
# trainer and the poll are ONE fact, and summing the rows counted them 2-3
# times: 46 such groups on the live store, 6.6% too many poll responses, and
# 35 of 70 trainers with inflated session counts. These three helpers are the
# single place that decides what "one session" is, for the weekly recap, the
# trainer rollups and the app alike.

def session_key(r: dict) -> tuple:
    """What makes two batch rows the same session.

    (date, pod, the batches L2 put in the room). A batch has at most one column
    per (date, pod) — the marker unions same-day webinars into it — so the key
    is unique by construction and needs no title. `shared_batches` comes from
    L2 (data.py), so rows of a shared room agree on it whether or not a poll
    ran; a row without it is its own session.
    """
    sb = r.get("shared_batches") or []
    return (r.get("date") or r.get("mm"), r.get("pod") or "",
            tuple(sb) if len(sb) > 1 else (r.get("batch"),))


_RATING_KEYS = ("rating", "rating_trainer", "rating_recommend", "rating_n",
                "nps", "dist")


def joint_rating(rows: list) -> dict:
    """The whole room's poll, from the batch rows of one session.

    Each batch row of a split poll carries only ITS students' answers and the
    joint figure in `rating_shared.joint`, so that is taken when present. Rows
    that were never split (a single batch, an older store, a poll with no
    emails) hold identical copies — take the fullest one, never a sum.
    """
    for r in rows:
        j = (r.get("rating_shared") or {}).get("joint")
        if j and j.get("responses"):
            return {"rating": j.get("session"), "rating_trainer": j.get("trainer"),
                    "rating_recommend": j.get("recommend"),
                    "rating_n": j.get("responses") or 0, "nps": j.get("nps"),
                    "dist": j.get("dist") or {}}
    best = max(rows, key=lambda r: r.get("rating_n") or 0) if rows else {}
    return {"rating": best.get("rating"),
            "rating_trainer": best.get("rating_trainer"),
            "rating_recommend": best.get("rating_recommend"),
            "rating_n": best.get("rating_n") or 0,
            "nps": best.get("nps", best.get("rating_nps")),
            "dist": best.get("dist") or best.get("rating_dist") or {}}


def group_sessions(rows: list) -> list:
    """Batch rows -> one dict per session, in first-seen order.

    Carries the same field names a batch row does, so `_agg`, `_awards` and
    `trainers.build` read a group exactly as they read a row: attendance pooled
    over the batches (the room was one room), the poll taken once from
    `joint_rating`, and every per-room measurement (duration, peak, retention,
    stickiness) from whichever row has it. `batch` reads 'B35, B36, B37' and
    `rows` keeps the per-batch lines for anyone who needs the split.
    """
    groups: dict = {}
    for r in rows or ():
        groups.setdefault(session_key(r), []).append(r)
    out = []
    for key, rs in groups.items():
        first = lambda k: next((x[k] for x in rs if x.get(k) is not None), None)
        present = sum(x.get("present") or 0 for x in rs)
        total = sum(x.get("total") or 0 for x in rs)
        idx = [(x["index"], x.get("total") or 0) for x in rs
               if x.get("index") is not None]
        wsum = sum(t for _i, t in idx)
        batches = sorted({x.get("batch") for x in rs if x.get("batch")},
                         key=lambda b: (len(b), b))
        g = {
            **{k: first(k) for k in ("date", "date_lbl", "week", "mm", "topic",
                                     "pod", "mentor", "trainer", "trainers",
                                     "trainer_type", "session_type", "l2_batch",
                                     "wk", "expected_pct", "duration_hrs", "peak",
                                     "unique_viewers", "retention", "stick10",
                                     "stick30", "poll_at_min", "rating_shared")},
            "key": key,
            "batch": ", ".join(batches),
            "batches": batches,
            "shared_batches": list(key[2]) if len(key[2]) > 1 else [],
            "rows": rs,
            "present": present,
            "total": total,
            "pct": round(present / total * 100, 1) if total else None,
            "index": (round(sum(i * t for i, t in idx) / wsum, 3) if wsum else None),
            **joint_rating(rs),
        }
        g["rating_dist"] = g["dist"]
        g["rating_nps"] = g["nps"]
        out.append(g)
    return out


def _agg(rows: list) -> dict:
    """Roll a set of session rows into one weekly line.

    Attendance is pooled (sum present / sum invited), never a mean of the
    per-session percentages: a whole-batch session of 3,000 and a pod session of
    59 are not two equal opinions about the week.

    Everything that is a fact about a ROOM — how many sessions, the poll, the
    stickiness — is taken over `group_sessions`, so a webinar three batches sat
    in is one session with one poll. Attendance and the index stay over the
    batch rows, because each batch is measured against its own roster.
    """
    present = sum(r["present"] for r in rows)
    total = sum(r["total"] for r in rows)
    idx = [r["index"] for r in rows if r["index"] is not None]
    # The index is weighted the same way, by invited headcount.
    wsum = sum(r["total"] for r in rows if r["index"] is not None)
    widx = (sum(r["index"] * r["total"] for r in rows if r["index"] is not None) / wsum
            if wsum else None)
    grp = group_sessions(rows)
    merged = _polls.merge_dists(g["dist"] for g in grp)
    rated = [g for g in grp if g["rating"] is not None]
    rn = sum(g["rating_n"] for g in rated)
    # Stickiness is a plain mean over sessions, NOT weighted by headcount: it
    # already is a ratio of a session to itself, so a big room's 40% and a small
    # room's 40% are the same fact about how well each held its audience.
    sticks = [g["stick30"] for g in grp if g.get("stick30") is not None]
    trated = [g for g in grp if g.get("rating_trainer") is not None]
    trn = sum(g["rating_n"] for g in trated)
    return {
        "sessions": len(grp),
        "batches": sorted({r["batch"] for r in rows}),
        "present": present,
        "invited": total,
        "pct": round(present / total * 100, 1) if total else None,
        "index": round(widx, 3) if widx else None,
        "n_indexed": len(idx),
        "rating": (round(sum(g["rating"] * g["rating_n"] for g in rated) / rn, 2)
                   if rn else None),
        "rating_n": rn,
        "nps": _polls.nps_from_dist(merged.get("recommend")),
        "rating_trainer": (round(sum(g["rating_trainer"] * g["rating_n"]
                                     for g in trated) / trn, 2) if trn else None),
        "stickiness": round(sum(sticks) / len(sticks), 1) if sticks else None,
        "n_sticky": len(sticks),
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

    Room awards — rating, retention, NPS, size — are judged over
    `group_sessions`, so a webinar three batches sat in competes once, with its
    whole poll and its whole room, and the card names every batch. "Beat the
    curve" stays on the batch rows: the residual is a claim about one batch at
    one age, and pooling it would blur exactly what it measures.
    """
    out = []
    grp = group_sessions(rows)
    # Session of the week is judged on the RATING, not the residual. Ratings do
    # not decay with cohort age the way attendance does, so a raw comparison is
    # fair here in a way it never is for attendance -- which is why the
    # attendance award below stays on the residual.
    scored = [r for r in grp
              if r["rating"] is not None and r["rating_n"] >= MIN_POLL_N]
    if len(scored) >= MIN_CONTENDERS:
        w = max(scored, key=lambda r: r["rating"])
        out.append({
            "award": "Session of the week",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"{w['rating']:.2f}",
            "why": f"rated by {w['rating_n']} learners",
        })
    sticky = [r for r in grp if r.get("stick30") is not None]
    if len(sticky) >= MIN_CONTENDERS:
        w = max(sticky, key=lambda r: r["stick30"])
        out.append({
            "award": "Best retention",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"{w['stick30']:.0f}%",
            "why": ("of its peak audience was still in the room over the "
                    "closing half hour"),
        })
    indexed = [r for r in rows if r["index"] is not None]
    if len(indexed) >= MIN_CONTENDERS:
        w = max(indexed, key=lambda r: r["index"])
        out.append({
            "award": "Beat the curve",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"{w['index']:.2f}x",
            "why": (f"drew {w['pct']:.1f}% where a {w['batch']} session "
                    f"{w['wk']} weeks in normally draws {w['expected_pct']:.1f}%"),
        })
    polled = [r for r in grp if r["nps"] is not None and r["rating_n"] >= MIN_POLL_N]
    if len(polled) >= MIN_CONTENDERS:
        w = max(polled, key=lambda r: r["nps"])
        out.append({
            "award": "Crowd favourite",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"NPS {w['nps']:+d}",
            "why": f"from {w['rating_n']} responses",
        })
    if len(grp) >= MIN_CONTENDERS:
        w = max(grp, key=lambda r: r["present"])
        out.append({
            "award": "Biggest room",
            "batch": w["batch"], "topic": w["topic"], "pod": w["pod"],
            "mentor": w["mentor"], "date_lbl": w["date_lbl"],
            "value": f"{w['present']:,}",
            "why": f"of {w['total']:,} invited",
        })
    return out


def build(DATA: dict, today: date, weeks: int = 8, rows: list | None = None) -> dict:
    """The recap payload: recent weeks, the latest week's detail, awards.

    `weeks` bounds how much history is summarised, not how much is scored —
    every session is still used to fit the curve the residuals are measured
    against, so a short window does not change anyone's index.
    """
    # `rows` lets the caller pass sessions that have already been enriched with
    # duration, peak and stickiness. Without it this recomputes from DATA alone,
    # which carries no retention data -- that is how the weekly stickiness KPI
    # came out empty while the per-session award had the numbers.
    rows = rows if rows is not None else collect_sessions(DATA, today)
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
            "rating_trainer": _delta(cur["rating_trainer"],
                                     prev and prev["rating_trainer"]),
            "stickiness": _delta(cur["stickiness"], prev and prev["stickiness"]),
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
        board.append({"mentor": name, "sessions": a["sessions"], "present": a["present"],
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
