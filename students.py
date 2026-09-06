"""
students.py — one learner's record, out of the per-batch grid already in the store.

Pure: column names and row dicts in, per-student summaries out. No pandas, no
Streamlit, no I/O, so the arithmetic is unit-tested on its own.

WHAT A GRID ROW LOOKS LIKE
--------------------------
    Email | Phone | Active | Present | POD Prefrence | 2026_08_27 | 2026_09_05 | Techies | ...

The trailing columns are the sessions, each holding 'Present', 'Absent' or ''.
A column named '<date> | <POD>' is a pod session; a bare date is whole-batch.

FOUR TRAPS, ALL OF THEM MEASURED
--------------------------------
1. **Not every trailing column is a session.** The obvious rule — "everything
   except Email, Phone, Active and Present" — sweeps up the POD-preference
   column. Columns are matched with `attendance_core._mmdd`, which needs a real
   date, so a preference column cannot pass.

2. **Not every session column is ISO-dated.** Only the marker's own appended
   columns read `2026_09_05`; the older batch tabs carry whatever their owners
   typed — '9th May', '10thMay', '16th May'. An anchored `\\d{4}_\\d{2}_\\d{2}`
   pattern silently dropped 59 real, marked session columns across B17-B24 and
   halved those batches' attendance. `_mmdd` is the project's proven parser for
   exactly this drift, so it is used here rather than a fresh regex.

3. **The roster's pod cell and the column's pod are different vocabularies.**
   The header says `Techies`; the roster cell says
   `Techies - Ai Career Accelerator Program B35`. Comparing them raw matched
   ZERO of 3,236 B35 students, so every pod session was dropped for everybody —
   7,330 Present marks discarded across B35-B38. `pods.from_roster_cell`
   canonicalises the cell (and folds aliases: 'Ai Generalist' -> 'Generalist',
   'Operations/Supply Chain' -> 'Ops/Supply Chain'), which is what the marker
   itself uses.

4. **The grid holds sessions the dashboard hides.** `data.REQUIRE_L2` drops any
   session with no L2 row, but `roster_grid` keeps its column. Counting those
   put five phantom sessions in every B35 student's denominator and deflated
   attendance by 13-22 points against the dashboard's own figure for the same
   batch. Callers pass `visible` — the (mm, pod) pairs DATA actually shows — and
   anything outside it is ignored.

You cannot be absent from a session you were never invited to: a pod session
counts only for students in that pod, whole-batch sessions for everyone.

WHAT IT DOES NOT CLAIM
----------------------
No minutes, no attention, no engagement depth. Those need per-attendee join and
leave times, which the weekly run does not fetch. Everything here is derived
from Present/Absent marks that are already in the store.
"""
from __future__ import annotations

from collections import Counter

import attendance_core as _ac
import pods as _pods

# Columns the grid always carries that are facts about the person, not sessions.
_META_COLS = ("email", "phone", "active", "present")


def parse_col(name):
    """A grid column -> (mm, canonical pod) when it is a session, else None.

    Handles both the marker's `2026_09_05` / `2026_09_05 | Techies` and the
    legacy hand-typed headers on the older batch tabs.
    """
    s = str(name or "").strip()
    if not s or s.lower() in _META_COLS:
        return None
    pod_raw = _ac._col_pod(s)
    head = s.split(_ac.POD_SEP, 1)[0] if _ac.POD_SEP in s else s
    mm = _ac._mmdd(head)
    if not mm:
        return None
    return mm, (_pods.canon(pod_raw) or pod_raw.strip() if pod_raw else "")


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


def visible_keys(sessions) -> set:
    """{(mm, pod)} from a DATA batch's `sessions` list.

    Pass the result as `visible` to keep this module in step with the dashboard.
    An empty or missing list means "cannot tell", which callers must treat as
    "count everything" rather than "nothing ran" — the same rule REQUIRE_L2
    itself follows when no L2 is loaded.
    """
    out = set()
    for s in (sessions or ()):
        if s.get("mm"):
            out.add((s["mm"], s.get("pod") or ""))
    return out


def _truthy_present(v) -> bool:
    return str(v or "").strip().lower() == "present"


def _marked(v) -> bool:
    return str(v or "").strip().lower() in ("present", "absent")


def student_pod(row, pod_col) -> str:
    """The student's pod, canonicalised the way the marker canonicalises it."""
    if not pod_col:
        return ""
    name, _multi = _pods.from_roster_cell(row.get(pod_col))
    return name or ""


def profile(row: dict, columns, pod_col=None, visible=None) -> dict:
    """One student's record from their grid row.

    `invited` counts only sessions this student could attend AND that the
    dashboard shows: every whole-batch session plus the pod sessions for their
    own pod. A student with no pod recorded is treated as whole-batch only —
    never as a member of every pod, which would make them absent ten times a day.
    """
    pod_col = pod_col if pod_col is not None else pod_column(columns)
    mine = student_pod(row, pod_col)
    invited = attended = marked = 0
    timeline = []
    for c in columns:
        parsed = parse_col(c)
        if not parsed:
            continue
        mm, pod = parsed
        if visible is not None and (mm, pod) not in visible:
            continue                      # hidden from the dashboard: not a session
        if pod and pod != mine:
            continue                      # not their pod: not their session
        invited += 1
        v = row.get(c)
        if _marked(v):
            marked += 1
        hit = _truthy_present(v)
        attended += hit
        timeline.append({"mm": mm, "pod": pod, "present": bool(hit),
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


def summarise(columns, rows, visible=None, top=None) -> dict:
    """Every student in one batch, plus the shape of the batch behind them.

    `visible` should be `visible_keys(DATA[batch]["sessions"])`. Without it the
    numbers will not match the Dashboard tab for any batch that has an
    L2-unregistered session, so callers that can supply it must.

    `top` limits how many student records come back; the distribution and the
    counts are always computed over everyone, so a truncated list never changes
    a headline number.
    """
    cols = list(columns or ())
    pod_col = pod_column(cols)
    sess = session_columns(cols)
    counted = [c for c in sess
               if visible is None or parse_col(c) in visible]
    people = [profile(r, cols, pod_col, visible) for r in (rows or ())]

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
        "n_sessions": len(counted),
        "n_hidden": len(sess) - len(counted),
        "skipped_columns": non_session_columns(cols),
        "pod_column": pod_col,
        "pods": dict(Counter(p["pod"] or "(none)" for p in people)),
        "attended_any": len(attended_any),
        "never_attended": len(with_any) - len(attended_any),
        "buckets": dict(buckets),
        "students": people[:top] if top else people,
    }
