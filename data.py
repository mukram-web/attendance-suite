"""
data.py — pure, Streamlit-free data layer for the AI CAP attendance dashboard.

It turns raw sheet rows (list-of-lists of strings, exactly what gspread's
get_all_values() returns, or what we synthesise from the .xlsx) into the
per-batch object the prototype renders, applying every business rule from the
spec. No I/O, no Streamlit — so the numbers are unit-testable on their own.

build(sheets, l2_lookup) -> (DATA, summary) where:
  sheets     = {tab_name: [[cell, ...], ...]}      # one entry per worksheet
  l2_lookup  = {(batch, mm_dd): topic, mm_dd: topic}  # from the L2 schedule (optional)

  DATA[code] = {
    code, strength, active, n_sessions, avg_pct, peak, low,
    sessions: [{date_lbl, mm, topic, present, absent, total, pct,
                present_only, no_l2}],
    closing:  [{type, count, pct, att}],
  }
  summary = {batches, enrolled, active, sessions}

Dynamic by design: batches, sessions, and closing-type values are all derived
from the data at read time — nothing about counts is hardcoded.
"""
from __future__ import annotations
import re
from collections import defaultdict

import pods
from attendance_core import _col_pod   # session column header -> its POD
from attendance_core import _mmdd  # proven date parser: "4th April", "31st may", "2026_05_31", datetimes
from attendance_core import _cell_email, extract_batches
import polls as _polls

# ── business-rule constants (the only hardcoded things, per spec) ─────────────
# Payment values that mean NOT active (covers the real misspellings in the data).
_REFUND_TOKENS = ("refund", "unidentified", "not paid", "cancel",
                  "undifined", "unidetified", "undefined")

# Colour bands for attendance pills/bars.
BAND_HIGH, BAND_MID = 45, 30  # ≥45 green, 30–45 amber, <30 red

# The L2 schedule is the register of what actually ran. A session column whose
# webinar has no L2 row is NOT shown on the dashboard (owner's rule, 2026-08-29):
# those are unscheduled/mislabeled folders, and they read as real sessions at 4-9%
# attendance, dragging a batch's average down by a third. They are still MARKED
# into the workbook - the roster download keeps the full record - they are just
# not counted or displayed. Fix a wrongly-hidden session by adding its row to L2,
# never by flipping this off.
REQUIRE_L2 = True

# Valid-session thresholds.
_MIN_MARKED_FRAC = 0.30   # a real session has Present/Absent for >30% of strength
_MIN_PRESENT_FRAC = 0.01  # …and present > 1% of strength (drops broken near-empty cols)

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_BATCH_RE = re.compile(r"^\s*ai\s*cap\s*(b\d+)\s*$", re.I)


# ── tiny helpers ──────────────────────────────────────────────────────────────
def band(pct: float) -> str:
    """'high' / 'mid' / 'low' — the UI maps these to the prototype's colours."""
    if pct >= BAND_HIGH:
        return "high"
    if pct >= BAND_MID:
        return "mid"
    return "low"


def is_active(payment) -> bool:
    """Active = Payment non-blank AND not a refund/unidentified/cancel/etc."""
    p = str(payment or "").strip().lower()
    if not p:
        return False
    return not any(t in p for t in _REFUND_TOKENS)


def normalize_closing(raw) -> str:
    """Closing-type buckets (applied before grouping). Unknown values are kept as
    their own bucket — never dropped."""
    s = str(raw or "").strip().lower()
    if not s:
        return "Unknown"
    if "collection" in s:
        return "BDA Collection"
    if "clos" in s:                 # catches the 'BDA Closimg' typo and all casings
        return "BDA Closing"
    if "system" in s:
        return "System"
    if "lwb" in s or "resume" in s:
        return "LWB Resume"
    return str(raw).strip()         # unrecognised → keep verbatim (own bucket)


def batch_label(tab: str) -> str | None:
    """Roster tab name -> batch label ('AI CAP B17' -> 'B17'); None for non-batch
    tabs and the Zoom '*Att' helper tabs."""
    if "att" in tab.lower():
        return None
    m = _BATCH_RE.match(tab)
    return m.group(1).upper() if m else None


def date_label(mm: str | None) -> str | None:
    """'04_27' -> '27 Apr' (clean, consistent display label)."""
    if not mm:
        return None
    try:
        m, d = mm.split("_")
        return f"{int(d)} {_MONTHS[int(m)]}"
    except (ValueError, IndexError):
        return None


def _cell(row, i):
    return row[i] if (i is not None and 0 <= i < len(row)) else None


def _nonempty(v) -> bool:
    return v is not None and str(v).strip() != ""


def _find_col(header, *needles):
    """Index of the first header cell containing ALL needles (case-insensitive)."""
    for i, c in enumerate(header):
        h = str(c or "").strip().lower()
        if all(n in h for n in needles):
            return i
    return None


def _find_header_row(rows) -> int:
    """Scan the first ~5 rows for the field-name row (don't assume row 1)."""
    keys = ("batch name", "country code", "contry code", "registered number", "payment")
    for i, row in enumerate(rows[:5]):
        joined = " | ".join(str(c or "").strip().lower() for c in row[:14])
        if any(k in joined for k in keys):
            return i
    return 0


def shared_batches(raw_label) -> list:
    """The batches L2's 'Batch Name' cell puts in ONE webinar, or [] for one.

    'AI CAP B35 , B36 , B37 - Finance' -> ['B35', 'B36', 'B37']
    'AI CAP B17 + B21 11AM'            -> ['B17', 'B21']
    'AI CAP B35 - Techies'             -> []

    This — not the poll — is what says a session was shared: the room was one
    room whether or not anyone answered a poll in it, and every rollup that
    counts sessions must count it once. Same spelling and order as the keys
    `polls.lookup_by_session_rows` writes, so the two can never disagree.
    """
    keys = extract_batches(str(raw_label or ""))
    labels = [_polls.batch_label(t, n) for t, n in sorted(keys)]
    return labels if len(labels) > 1 else []


def roster_emails(tabs: dict) -> dict:
    """{batch: frozenset(emails)} from the roster workbook's tabs.

    The one place a roster's mail column is read for identity outside
    `build_batch`, and it uses the same header rule ('registered' + 'mail') so a
    renamed column breaks both together and loudly rather than one silently.
    Emails are normalised with `attendance_core._cell_email`, exactly as the
    marker does when it matches Zoom attendees — so a poll respondent found here
    is the same person the attendance marker would have found.
    """
    out = {}
    for tab, rows in (tabs or {}).items():
        code = batch_label(tab)
        if not code or not rows:
            continue
        hr = _find_header_row(rows)
        mail_col = _find_col(rows[hr], "registered", "mail")
        if mail_col is None:
            continue
        out[code] = frozenset(e for r in rows[hr + 1:]
                              if (e := _cell_email(_cell(r, mail_col))))
    return out


def clean_l2_label(raw) -> str:
    """Tidy L2's raw 'Batch Name' cell for display without rewording it.

    The sheet is hand-typed, so the same batch appears as 'AI CAP B17 11AM',
    'AI CAP B17 @ 11AM', 'AI CAP B17  11AM' and even 'AI CAP B17 11:00 AM]'.
    Collapse whitespace and drop stray edge punctuation; keep everything else,
    including the time of day — an 11AM and a 7PM session are different
    sessions, which is the whole reason the label is shown.
    """
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    return s.strip("[](){},;:-").strip()


# ── per-batch build ─────────────────────────────────────────────────────────
def build_batch(rows: list[list], batch: str, l2_lookup: dict | None,
                l2_labels: dict | None = None, ratings: dict | None = None,
                mentors: dict | None = None) -> dict | None:
    """Compute one batch's dashboard object from its raw rows. None if the tab
    doesn't look like a roster (no mail/closing columns) or has no strength."""
    if not rows:
        return None
    hr = _find_header_row(rows)
    header = rows[hr]
    date_row = rows[hr - 1] if hr >= 1 else rows[hr]   # dates sit in the row above the header
    mail_col = _find_col(header, "registered", "mail")
    pay_col = _find_col(header, "payment")
    # "clos" not "closing" — newer sheets head the column 'Close Type'. If it's
    # missing entirely, fall back to the column right after Payment (same rule
    # dashboard_core uses), so a renamed header never drops a whole batch.
    close_col = _find_col(header, "clos")
    if close_col is None and pay_col is not None:
        close_col = pay_col + 1
    if mail_col is None or close_col is None:
        return None

    width = max((len(r) for r in rows), default=0)
    data = rows[hr + 1:]
    enrolled = [r for r in data if _nonempty(_cell(r, mail_col))]
    strength = len(enrolled)
    if strength == 0:
        return None
    active = sum(1 for r in enrolled if is_active(_cell(r, pay_col)))

    # POD membership (B35 onwards). The header drifts - 'POD pref', 'POD Pref',
    # 'POD Prefrence' - and its position moves (B38 has no 'batch name' column),
    # so it is found by substring like every other column. A batch without one
    # has no PODs and every session below is a whole-batch session, which is what
    # keeps every pre-B35 number identical to before.
    pod_col = _find_col(header, "pod")
    pod_strength: dict = defaultdict(int)
    pod_active: dict = defaultdict(int)
    row_pod: dict = {}
    pod_guessed = 0
    if pod_col is not None:
        for r in enrolled:
            name, multi = pods.from_roster_cell(_cell(r, pod_col))
            pod_guessed += bool(multi)
            name = name or pods.UNKNOWN
            pod_strength[name] += 1
            row_pod[id(r)] = name
            if is_active(_cell(r, pay_col)):
                pod_active[name] += 1

    l2_lookup = l2_lookup or {}
    l2_labels = l2_labels or {}
    ratings = ratings or {}
    mentors = mentors or {}

    # Which PODs actually met on each date. A date that ran a named POD room AND
    # an unlabelled room is a two-room weekend: the unlabelled one is the
    # COMPLEMENT, not the whole batch. Needs its own pass because column order
    # gives no guarantee the POD column comes first.
    #
    # Structure alone cannot say which it is: both look like one unlabelled
    # column plus one "<date> | POD" column, and in BOTH the unlabelled column
    # marks every student. What separates them is behaviour. If the POD met in
    # its own room INSTEAD of the main one, its members are marked Absent in the
    # unlabelled column while Present in their own - B40's 12 Sep has 347 of 561
    # Techies present in the Techies column and exactly 1 in the plain one. When
    # the whole batch genuinely met and the POD ALSO met, the same people are
    # Present in both.
    #
    # So: treat the unlabelled column as the complement only when the day's PODs
    # are essentially missing from it. The 10% floor is deliberately generous -
    # a handful of people wander into the wrong room, and that must not flip a
    # real whole-batch session into a complement one.
    _POD_LEAK_MAX = 0.10

    def _present_at(c, rows_):
        return sum(1 for r in rows_
                   if str(_cell(r, c) or "").strip().lower() == "present")

    cols_by_date: dict = defaultdict(lambda: {"pod": [], "plain": []})
    for c in range(close_col + 1, width):
        mm_ = _mmdd(_cell(date_row, c)) or _mmdd(_cell(header, c))
        if not mm_:
            continue
        p = _col_pod(_cell(date_row, c)) or _col_pod(_cell(header, c))
        cols_by_date[mm_]["pod" if p else "plain"].append((c, p))

    # PODs that ran a room of their OWN on a date - whatever else happened that
    # day. A whole-batch session's breakdown must skip them or they appear twice
    # for one date: once in their own room, once inside the All Domains split.
    pod_rooms: dict = {mm_: {p for _c, p in cc["pod"]}
                       for mm_, cc in cols_by_date.items() if cc["pod"]}

    date_pods: dict = defaultdict(set)
    for mm_, cc in cols_by_date.items():
        if not cc["pod"] or not cc["plain"]:
            continue                      # nothing to disambiguate
        met = {p for _c, p in cc["pod"]}
        members = [r for r in enrolled if row_pod.get(id(r), "") in met]
        if not members:
            continue
        in_own = sum(_present_at(c, members) for c, _p in cc["pod"])
        in_plain = max(_present_at(c, members) for c, _p in cc["plain"])
        if in_own and in_plain <= _POD_LEAK_MAX * in_own:
            date_pods[mm_] = met          # two parallel rooms

    def _real_split(raw, invited):
        """{domain: {present,total,pct}}, minus anything that is not a breakdown.

        Two ways a "split" can say nothing, and B17-B34 hit both depending on
        whether the tab has a POD column at all: with none, every student keys
        on "" and with one full of blanks they all key on "Unassigned". Either
        way it was ONE bucket covering everyone, restating the session's own
        total - 464 of 491 stored splits, and a row per session downstream.

        So: drop the nameless bucket, and drop a lone bucket that covers the
        entire invited population. A named domain smaller than the session is
        kept even when it is the only one left after the day's own-room PODs
        are excluded - Finance at 6 of a 10-person session still says
        something the session line does not.
        """
        out = {k: v for k, v in raw.items() if v["total"] and str(k).strip()}
        if len(out) == 1 and next(iter(out.values()))["total"] >= invited:
            return {}
        return {k: dict(v, pct=round(v["present"] / v["total"] * 100, 1))
                for k, v in sorted(out.items())}

    def _invited(rp, pod, excl):
        """Is a student with POD `rp` invited to this session?"""
        if excl:
            return rp not in excl          # the complement room
        if pod:
            return rp == pod               # a named POD's room
        return True                        # genuine whole-batch session

    # discover + validate session columns (everything after Closing Type)
    sessions = []
    for c in range(close_col + 1, width):
        hraw = _cell(header, c)
        # The marker writes "<date> | <POD>" for a domain session, so the POD has
        # to be known BEFORE the validity thresholds: a 59-member Data POD could
        # never clear "30% of the batch marked", and every small POD's session
        # would be discarded as a broken column.
        pod = _col_pod(_cell(date_row, c)) or _col_pod(hraw)
        mm = _mmdd(_cell(date_row, c)) or _mmdd(_cell(header, c))
        excl = frozenset(date_pods.get(mm, ())) if not pod else frozenset()
        if excl:
            # The complement room: everyone the day's POD rooms did not invite.
            pod = pods.COMMON
            denom = strength - sum(pod_strength.get(p, 0) for p in excl)
        else:
            denom = pod_strength.get(pod, 0) if pod else strength
        if not denom:
            continue        # a POD nobody in this batch belongs to

        # Count over the SAME population the denominator uses. Counting every
        # marked row against a POD-sized denominator is how B37's 22 Aug came to
        # report 1,631 present out of 571 - 285%.
        # ANY session that invited more than one domain can be broken down by
        # domain: an All Domains session (a batch's first two or three) just as
        # much as a complement room. L2 calls each of them one session, but the
        # roster records which domain every attendee belongs to, so Finance and
        # Data are recoverable instead of being folded into a single number
        # until the eleven-POD format starts in week three. A named POD's own
        # room needs none - it is one domain by construction.
        split: dict = defaultdict(lambda: {"present": 0, "total": 0})
        multi = bool(excl) or not pod
        own_rooms = pod_rooms.get(mm, frozenset()) if not excl else frozenset()

        present = absent = 0
        for r in enrolled:
            rp_ = row_pod.get(id(r), "")
            if not _invited(rp_, pod, excl):
                continue
            if multi and rp_ not in own_rooms:
                v_ = str(_cell(r, c) or "").strip().lower()
                split[rp_]["total"] += 1
                if v_ == "present":
                    split[rp_]["present"] += 1
            v = str(_cell(r, c) or "").strip().lower()
            if v == "present":
                present += 1
            elif v == "absent":
                absent += 1
        marked = present + absent
        if not (marked > _MIN_MARKED_FRAC * denom and present > _MIN_PRESENT_FRAC * denom):
            continue  # un-synced/empty formula column or broken near-empty column

        # a real roster header (topic) is one that ISN'T itself a date
        roster_topic = None if (hraw is None or _mmdd(hraw)) else str(hraw).strip()
        # POD-specific topic first: on a domain date the eleven sessions differ,
        # and (batch, date) would give them all the same name.
        l2_topic = (l2_lookup.get((batch, mm, pod))
                    or l2_lookup.get((batch, mm)) or l2_lookup.get(mm))
        topic = l2_topic or roster_topic or (f"Session on {date_label(mm)}" if mm else "Live session")
        # How L2 names this session's batch ("AI CAP B35 - Techies", "AI CAP B8 + B22").
        # The same topic runs across many batches, so the label is what tells two
        # otherwise identical session names apart. Batch-specific match only —
        # the date-only fallback would borrow another batch's label.
        raw_label = l2_labels.get((batch, mm, pod)) or l2_labels.get((batch, mm))
        l2_batch = clean_l2_label(raw_label)
        # Who taught it, from L2's Mentor column. Batch-specific only, matching
        # l2_batch: a date-only fallback would credit the wrong person.
        mentor = str(mentors.get((batch, mm, pod))
                     or mentors.get((batch, mm)) or "").strip()

        sessions.append({
            "col": c, "mm": mm,
            "date_lbl": date_label(mm) or (str(hraw).strip() if hraw else "—"),
            "topic": topic,
            "l2_batch": l2_batch,
            # Every batch that sat in this webinar ([] when only this one did).
            # Read from the SAME L2 cell as l2_batch, so the two cannot drift.
            # This is what lets trainer and weekly rollups count a room once.
            "shared_batches": shared_batches(raw_label),
            "pod": pod,
            # Which PODs this room did NOT invite. Empty for a named-POD or a
            # genuine whole-batch session; set only on a complement room, and
            # the three places that decide "was this student invited" all read
            # it through _invited so they cannot drift apart.
            "excl": sorted(excl),
            # {domain: {present, total, pct}} for any multi-domain session -
            # All Domains or a complement room; {} for a single POD's own room.
            #
            # A split needs at least TWO domains to say anything. B17-B34 have
            # no POD column, so every student fell into one bucket keyed on ""
            # and 464 of 491 "splits" were a single nameless entry restating
            # the session's own total - noise in the store and a row per
            # session in anything that renders it.
            "pod_split": _real_split(split, denom),
            "mentor": mentor,
            # The session's own feedback poll, joined on Webinar ID like the topic.
            # For a shared webinar these are THIS batch's students' answers only
            # (pipeline [5a] splits the poll by roster); `rating_shared` then
            # carries the whole room's figures and how the split went.
            "rating": (rt := ratings.get((batch, mm, pod)) or {}).get("session"),
            "rating_shared": rt.get("shared"),
            "rating_trainer": rt.get("trainer"),
            "rating_recommend": rt.get("recommend"),
            "rating_n": rt.get("responses", 0),
            # NPS and the 1-5 histograms come from the same poll payload. Both
            # are aggregates - counts, never a respondent - so they are safe in
            # the store and safe on the public static site.
            "rating_nps": rt.get("nps"),
            "rating_dist": rt.get("dist") or {},
            # When the room began answering the poll — used to mark the
            # retention curve. An absolute timestamp here; the pipeline turns it
            # into minutes-from-first-join once it knows the session's t0.
            "poll_at": rt.get("submitted_first"),
            "present": present, "absent": max(0, denom - present), "total": denom,
            "pct": round(present / denom * 100, 1),
            "present_only": absent == 0,
            "no_l2": l2_topic is None,
        })

    # Enforce the L2 rule (see REQUIRE_L2). Counted before dropping so the page can
    # say how many are hidden rather than silently showing a shorter list.
    #
    # Guarded on a NON-EMPTY lookup on purpose. With no L2 loaded at all - the id
    # unset, the fetch failed, a caller passing None - every session looks "not in
    # L2" and filtering would blank the entire dashboard. An absent schedule means
    # "cannot tell", not "nothing ran", so in that case show everything.
    hidden_no_l2 = sum(1 for s in sessions if s["no_l2"]) if l2_lookup else 0
    if REQUIRE_L2 and hidden_no_l2:
        sessions = [s for s in sessions if not s["no_l2"]]

    if not sessions:
        return None

    if all(s["mm"] for s in sessions):           # sort chronologically when fully dated
        sessions.sort(key=lambda s: s["mm"])
    valid_cols = [s["col"] for s in sessions]
    n_valid = len(sessions)

    # closing-type breakdown
    groups: dict[str, list] = defaultdict(list)
    for r in enrolled:
        groups[normalize_closing(_cell(r, close_col))].append(r)
    # A student can only attend their OWN POD's sessions plus the whole-batch
    # ones, so the denominator is per person. Dividing by every session in the
    # batch quartered these numbers the moment B35 ran eleven PODs a day.
    col_rule = {sx["col"]: (sx.get("pod", ""), frozenset(sx.get("excl") or ()))
                for sx in sessions}
    closing = []
    for ctype, grp in groups.items():
        pres = slots = 0
        for r in grp:
            rp = row_pod.get(id(r), "")
            for c in valid_cols:
                cp, cx = col_rule.get(c, ("", frozenset()))
                if not _invited(rp, cp, cx):
                    continue            # a different POD's session
                slots += 1
                if str(_cell(r, c) or "").strip().lower() == "present":
                    pres += 1
        att = (pres / slots * 100) if slots else 0.0
        closing.append({
            "type": ctype, "count": len(grp),
            "pct": round(len(grp) / strength * 100, 1),
            "att": round(att, 1),
        })
    closing.sort(key=lambda c: c["count"], reverse=True)   # biggest channel first

    # One date can now carry eleven sessions, so the batch headline is rolled up
    # per DATE and weighted by POD size: everyone present across every POD that
    # ran that date, over the whole batch. That reads as "what share of the batch
    # showed up this week", and where a date holds a single whole-batch session -
    # every batch before B35 - it is arithmetically the old number.
    by_date: dict = {}
    for sx in sessions:
        k = sx["mm"] or sx["date_lbl"]
        d = by_date.setdefault(k, {"mm": sx["mm"], "date_lbl": sx["date_lbl"],
                                   "present": 0, "total": 0, "n_pods": 0,
                                   "cols": [], "pods": set(), "whole": False})
        d["n_pods"] += 1
        d["cols"].append(sx["col"])
        if sx.get("excl"):
            # a complement room: everyone outside the day's POD rooms was invited
            d.setdefault("excl", set()).update(sx["excl"])
            d["complement"] = True
        elif sx.get("pod"):
            d["pods"].add(sx["pod"])
        else:
            d["whole"] = True

    # Counted per STUDENT, not by summing sessions, for two reasons:
    #  * you cannot be absent from a session you were never invited to - on a day
    #    when only the Generalist POD met, the other 2,291 students are not
    #    absentees, and dividing by the whole batch reported 12% for a session
    #    41% of its POD attended;
    #  * someone in a POD that met on a day the whole batch also met would
    #    otherwise be counted twice in the numerator.
    for d in by_date.values():
        pres = tot = 0
        for r in enrolled:
            rp = row_pod.get(id(r), "")
            if not (d["whole"] or rp in d["pods"]
                    or (d.get("complement") and rp not in d.get("excl", ()))):
                continue                     # not invited that day
            tot += 1
            if any(str(_cell(r, c) or "").strip().lower() == "present"
                   for c in d["cols"]):
                pres += 1                    # attended at least one of their sessions
        d["present"], d["total"] = pres, tot
        d["pct"] = round(pres / tot * 100, 1) if tot else 0.0
        # Scratch keys only - the payload is JSON-serialised into the store, so
        # anything left here that is a set takes the whole weekly run down at
        # [6/8], long after the numbers were right.
        d.pop("cols"); d.pop("pods"); d.pop("whole")
        d.pop("excl", None); d.pop("complement", None)
    dates = sorted(by_date.values(), key=lambda d: d["mm"] or "")
    dpcts = [d["pct"] for d in dates]

    return {
        "code": batch, "strength": strength, "active": active,
        "n_sessions": n_valid,
        "avg_pct": round(sum(dpcts) / len(dpcts), 1),
        "peak": max(dpcts), "low": min(dpcts),
        "sessions": sessions, "closing": closing,
        "hidden_no_l2": hidden_no_l2,
        "by_date": dates,
        "pods": {p: {"strength": pod_strength[p], "active": pod_active.get(p, 0)}
                 for p in sorted(pod_strength)},
        "pod_guessed": pod_guessed,
    }


def build(sheets: dict[str, list[list]], l2_lookup: dict | None = None,
          l2_labels: dict | None = None, ratings: dict | None = None,
          mentors: dict | None = None):
    """Build the full DATA dict + summary from all worksheets. Auto-discovers
    batch tabs; never hardcodes the batch or session list."""
    data = {}
    for tab, rows in sheets.items():
        b = batch_label(tab)
        if not b:
            continue
        bd = build_batch(rows, b, l2_lookup, l2_labels, ratings, mentors)
        if bd:
            data[b] = bd

    order = sorted(data, key=lambda b: int(re.sub(r"\D", "", b) or 0))
    data = {b: data[b] for b in order}
    summary = {
        "batches": len(data),
        "enrolled": sum(d["strength"] for d in data.values()),
        "active": sum(d["active"] for d in data.values()),
        "sessions": sum(d["n_sessions"] for d in data.values()),
    }
    return data, summary
