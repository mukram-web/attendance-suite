"""
students.py — one learner's record, out of the per-batch grid already in the store.

Pure: column names and row dicts in, per-student summaries out. No pandas, no
Streamlit, no I/O, so the arithmetic is unit-tested on its own.

WHAT A GRID ROW LOOKS LIKE
--------------------------
    Email | Phone | Active | Present | POD Prefrence | 2026_08_27 | 2026_09_05 | Techies | ...

The trailing columns are the sessions, each holding 'Present', 'Absent' or ''.
A column named '<date> | <POD>' is a pod session; a bare date is whole-batch.

TWO TRAPS THIS MODULE EXISTS TO AVOID
-------------------------------------
1. The obvious way to find session columns — "everything except Email, Phone,
   Active and Present" — is what the app does today, and it is wrong: it sweeps
   up the POD-preference column too. Measured across the live store, 44 of 478
   grid columns are not sessions. Counting them inflates every student's
   denominator by one and quietly drops their attendance percentage. Columns are
   therefore matched on the DATE PATTERN, which a preference column cannot fake.

2. You cannot be absent from a session you were never invited to. On a day when
   only the Techies pod met, the rest of the batch are not absentees. A pod
   session counts only for students in that pod; whole-batch sessions count for
   everyone. Dividing by every column in the grid is how a pod-heavy batch comes
   to look like it collapsed.

WHAT IT DOES NOT CLAIM
----------------------
No minutes, no attention, no engagement depth. Those need per-attendee join and
leave times, which the weekly run does not fetch. Everything here is derived
from Present/Absent marks that are already in the store.
"""
from __future__ import annotations

import re
from collections import Counter

# '2026_09_05' or '2026_09_05 | Techies'. Anchored, so 'POD Prefrence' and any
# future descriptive column can never be mistaken for a session.
_SESSION_COL = re.compile(r"^\s*(\d{4})_(\d{2})_(\d{2})\s*(?:\|\s*(.+?))?\s*$")

# Columns the grid always carries that are facts about the person, not sessions.
_META_COLS = ("email", "phone", "active", "present")


def parse_col(name):
    """A grid column -> (date_str, pod) when it is a session, else None."""
    m = _SESSION_COL.match(str(name or ""))
    if not m:
        return None
    y, mo, d, pod = m.groups()
    return f"{y}_{mo}_{d}", (pod or "").strip()


def session_columns(columns) -> list:
    """Just the session columns, in the order given (which is chronological)."""
    return [c for c in (columns or ()) if parse_col(c)]


def non_session_columns(columns) -> list:
    """Everything else — useful for showing what was skipped and why."""
    return [c for c in (columns or ()) if not parse_col(c)]


def pod_column(columns):
    """The POD-preference column, whatever it was spelled as this week.

    Seen in the live store as 'POD pref', 'POD Pref' and 'POD Prefrence'.
    Matched on substring for that reason, but only among NON-session columns so
    a pod session column can never be mistaken for the preference field.
    """
    for c in non_session_columns(columns):
        lc = str(c).strip().lower()
        if lc in _META_COLS:
            continue
        if "pod" in lc:
            return c
    return None


def _truthy_present(v) -> bool:
    return str(v or "").strip().lower() == "present"


def _marked(v) -> bool:
    return str(v or "").strip().lower() in ("present", "absent")


def profile(row: dict, columns, pod_col=None) -> dict:
    """One student's record from their grid row.

    `invited` counts only the sessions this student could attend: every
    whole-batch session, plus the pod sessions for their own pod. A student with
    no pod recorded is treated as whole-batch only — never as a member of every
    pod, which would make them absent from ten sessions a day.
    """
    pod_col = pod_col if pod_col is not None else pod_column(columns)
    mine = str(row.get(pod_col) or "").strip() if pod_col else ""
    invited = attended = marked = 0
    timeline = []
    for c in columns:
        parsed = parse_col(c)
        if not parsed:
            continue
        dt, pod = parsed
        if pod and pod.lower() != mine.lower():
            continue                      # not their pod: not their session
        invited += 1
        v = row.get(c)
        if _marked(v):
            marked += 1
        hit = _truthy_present(v)
        attended += hit
        timeline.append({"date": dt, "pod": pod, "present": bool(hit),
                         "marked": _marked(v)})
    return {
        "email": row.get("Email") or row.get("email") or "",
        "phone": row.get("Phone") or row.get("phone") or "",
        "pod": mine,
        "active": str(row.get("Active") or "").strip().lower() in ("yes", "true", "1", "active"),
        "invited": invited,
        "attended": attended,
        # marked < invited means the marker never wrote a value for a session
        # this student was invited to. Surfaced rather than folded into 'absent',
        # because the two mean different things and only one is the student's.
        "unmarked": invited - marked,
        "pct": round(attended / invited * 100, 1) if invited else None,
        "timeline": timeline,
    }


def summarise(columns, rows, top=None) -> dict:
    """Every student in one batch, plus the shape of the batch behind them.

    `top` limits how many student records come back; the distribution and the
    counts are always computed over everyone, so a truncated list never changes
    a headline number.
    """
    cols = list(columns or ())
    pod_col = pod_column(cols)
    sess = session_columns(cols)
    people = [profile(r, cols, pod_col) for r in (rows or ())]

    with_any = [p for p in people if p["invited"]]
    attended_any = [p for p in with_any if p["attended"] > 0]
    # Buckets rather than a mean: "average attendance 34%" hides whether that is
    # everyone showing up a third of the time or a third of them showing up
    # always, and the two call for completely different action.
    buckets = Counter()
    for p in with_any:
        pct = p["pct"] or 0
        if pct == 0:
            buckets["never"] += 1
        elif pct < 25:
            buckets["rare"] += 1
        elif pct < 50:
            buckets["occasional"] += 1
        elif pct < 75:
            buckets["regular"] += 1
        else:
            buckets["core"] += 1

    people.sort(key=lambda p: (-(p["pct"] or 0), -p["attended"], p["email"]))
    return {
        "n_students": len(people),
        "n_sessions": len(sess),
        "skipped_columns": non_session_columns(cols),
        "pod_column": pod_col,
        "attended_any": len(attended_any),
        "never_attended": len(with_any) - len(attended_any),
        "buckets": dict(buckets),
        "students": people[:top] if top else people,
    }
