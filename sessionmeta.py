"""
sessionmeta.py — duration and peak concurrency, from the attendee report's own header.

Pure: report text in, a small dict out. No I/O, unit-tested.

WHERE THESE NUMBERS COME FROM
-----------------------------
Every Zoom Attendee Report opens with a summary row that nothing in this repo
was reading:

    Topic, Webinar ID, Actual Start Time, Actual Duration (minutes),
    # Registrants, # Cancelled registrants, Unique Viewers, Total Users,
    Max Concurrent Views, Enable Registration

So `Duration (hrs)` and `Peak` need no interval arithmetic at all — they are two
fields on line 3. That is worth knowing before anyone reaches for a
minute-by-minute concurrency model to get them.

THREE THINGS TO KNOW BEFORE TRUSTING THEM
-----------------------------------------
1. **`Actual Duration` measures the MEETING, not the teaching.** It runs from
   the host starting to the host leaving, so a trainer who forgets to end the
   webinar inflates it. Measured across the corpus, nominally 3-hour classes
   report 112 to 279 minutes. Shown because it is the number Zoom reports and it
   is useful for spotting the extremes, but it is not lesson length.

2. **`Max Concurrent Views` is missing from about a quarter of reports**, and
   reads blank or 0 on the newer ones. `peak_source` says which of the two paths
   produced the value so the UI can distinguish "no peak recorded" from a real
   zero, and `peak` is None rather than 0 when it is absent.

3. **The Webinar ID is space-formatted** ('942 2996 0431'), unlike the digits in
   the filename. It is normalised here; comparing it raw to a filename id
   matches nothing.

A LIVE/SIMULIVE FLAG IS NOT AVAILABLE HERE
------------------------------------------
Checked the corpus: no attendee report carries a presenter or simulive marker.
The reference app finds 'Presenter' in the last 30 lines of ITS exports; ours do
not have it. `Enable Registration` IS in the header and is captured, because it
is the underlying cause of the flat-export shape that used to mark whole batches
absent — a webinar run without registration cannot produce a usable report.
"""
from __future__ import annotations

import csv
import io
import re

# The header labels we want, matched case-insensitively on a substring so a
# renamed-but-recognisable column still binds. Kept narrow on purpose: 'total'
# would also match 'Total Users' from '# Registrants'.
_WANT = {
    "duration_min": ("actual duration",),
    "peak": ("max concurrent",),
    "unique_viewers": ("unique viewers",),
    "total_users": ("total users",),
    "registrants": ("# registrants", "registrants"),
    "start": ("actual start time",),
    "registration": ("enable registration",),
}


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _num(v):
    """A number, or None. Blank and '0' are different facts for the peak."""
    s = str(v or "").strip().replace(",", "")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def parse_header(text) -> dict | None:
    """The report's summary row -> {webinar_id, duration_min, peak, …} or None.

    None means the file has no recognisable summary row — a poll export, an
    Overview export, or a shape nobody has taught this yet. Callers must treat
    that as "no metadata", never as zeros.
    """
    if not text:
        return None
    # csv.reader over splitlines(): the reports embed newlines inside quoted
    # fields, which trips a bare reader on the raw string.
    rows = list(csv.reader(text.splitlines()))
    hidx = None
    for i, r in enumerate(rows[:12]):
        low = [str(c or "").strip().lower() for c in r]
        if "topic" in low and any("webinar id" in c or "meeting id" in c for c in low):
            hidx = i
            break
    if hidx is None or hidx + 1 >= len(rows):
        return None
    hdr = [str(c or "").strip().lower() for c in rows[hidx]]
    val = rows[hidx + 1]

    def cell(*needles):
        for j, h in enumerate(hdr):
            if any(n in h for n in needles):
                return val[j] if j < len(val) else None
        return None

    wid = _digits(cell("webinar id", "meeting id"))
    if not wid:
        return None
    out = {"webinar_id": wid, "topic": (cell("topic") or "").strip()}
    for key, needles in _WANT.items():
        raw = cell(*needles)
        out[key] = raw.strip() if key in ("start", "registration") and raw else _num(raw)
    # 'no peak recorded' and 'peak of zero' are different claims.
    out["peak_source"] = "zoom" if out.get("peak") else None
    out["duration_hrs"] = (round(out["duration_min"] / 60, 1)
                           if out.get("duration_min") else None)
    return out


# A session left open overnight would otherwise store a thousand zeros. Six
# hours covers every real class in the corpus (longest measured span: 5.2 h).
MAX_MINUTES = 360

_TIME_FMTS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
              "%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M:%S")


def _ts(v):
    from datetime import datetime
    s = str(v or "").strip().strip('"')
    if not s:
        return None
    for f in _TIME_FMTS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def measure(text) -> dict:
    """Peak concurrency and the attended span, computed from the join/leave rows.

    Zoom's own `Max Concurrent Views` cannot fill a Peak column: it is absent
    from 64% of reports and reads 0 on some that had hundreds of viewers. So the
    peak is computed by sweeping the intervals — +1 at each join, -1 at each
    leave, take the running maximum. Validated against Zoom's figure on the
    reports that do carry it.

    `span_hrs` is first-join to last-leave. It differs from Zoom's
    `Actual Duration` (host start to host end) and is the better of the two for
    "how long was the room busy", but it is still inflated by one person who
    never clicked Leave.

    Rows with no usable timestamps are skipped rather than counted as
    instantaneous — a zero-length interval would drag the peak down.
    """
    events, first, last, rows = [], None, None, 0
    hdr, section = None, None
    for r in csv.reader((text or "").splitlines()):
        if not r:
            continue
        c0 = str(r[0] or "").strip().lower()
        # Section-aware, because 'Host Details' and 'Panelist Details' carry
        # their OWN 'Attended,...' header. Counting them made every computed
        # peak exactly 2 too high (the host and a co-host sit in the room for
        # the whole session), which showed up as a consistent +0.2 to +1.3%
        # against Zoom's own Max Concurrent Views.
        if c0 in ("attendee details", "other attended",
                  "host details", "panelist details"):
            section, hdr = c0, None
            continue
        if c0 == "attended":
            hdr = [str(c or "").strip().lower() for c in r]
            continue
        if hdr is None or section not in ("attendee details", "other attended"):
            continue

        def g(*needles):
            for j, h in enumerate(hdr):
                if any(n in h for n in needles):
                    return r[j] if j < len(r) else None
            return None

        j, l = _ts(g("join time")), _ts(g("leave time"))
        if not (j and l) or l <= j:
            continue
        rows += 1
        events.append((j, 1))
        events.append((l, -1))
        first = j if first is None or j < first else first
        last = l if last is None or l > last else last

    peak = cur = 0
    # Leaves before joins at the same instant, so a hand-off does not read as a
    # spurious extra viewer.
    for _t, delta in sorted(events, key=lambda e: (e[0], e[1])):
        cur += delta
        peak = max(peak, cur)
    span = (round((last - first).total_seconds() / 3600, 1)
            if first and last else None)

    # The per-minute concurrency series — the retention curve. It is the SAME
    # sweep as the peak above, recorded instead of maxed, so it costs one more
    # pass over events already in hand. Capped at MAX_MINUTES: a session left
    # open overnight would otherwise store a thousand zeros.
    curve = []
    if first and last:
        total = int((last - first).total_seconds() // 60) + 1
        buckets = [0] * (min(total, MAX_MINUTES) + 1)
        for t, delta in events:
            m = int((t - first).total_seconds() // 60)
            if 0 <= m < len(buckets):
                buckets[m] += delta
            elif m < 0:
                buckets[0] += delta
        run = 0
        for b in buckets[:-1]:
            run += b
            curve.append(max(0, run))
        # The last leave ALWAYS lands in the final bucket, so the sweep always
        # ends on an empty minute -- measured 2026-09-07: 521 of 521 curves in
        # the live store ended in a zero, exactly one of them. Keeping it made
        # every stickiness figure divide a real tail by one minute more than
        # existed: -5.4 points on the 10-minute number, -2.5 on the 30. It is
        # a trailing artefact of the bucketing, not a minute anyone sat in.
        while curve and curve[-1] == 0:
            curve.pop()

    def _tail_mean(n):
        """Mean concurrency over the last n minutes, as a share of peak.

        This is 'stickiness': of the fullest the room ever was, how much of it
        was still there at the end. A mean rather than the final value, because
        one person leaving on the last minute should not move it.

        Reads the TRIMMED curve, so the empty minute the bucketing leaves on
        the end is not counted as a minute the room was deserted.
        """
        if not curve or not peak:
            return None
        tail = curve[-n:] if len(curve) >= n else curve
        return round(sum(tail) / len(tail) / peak * 100, 1) if tail else None

    return {"peak_computed": peak or None, "span_hrs": span, "timed_rows": rows,
            "curve": curve or None,
            # t0 is minute zero of the curve — the first join, NOT the host's
            # start. Callers need it to place anything else on the same axis,
            # such as when the room began answering a poll.
            "t0": first.isoformat() if first else None,
            "stick10": _tail_mean(10), "stick30": _tail_mean(30)}


def name_key(name) -> tuple | None:
    """attendee filename -> (webinar_id, mm_dd), matching polls.name_key's shape."""
    n = str(name).rsplit("/", 1)[-1]
    m = re.match(r"attendee_(\d+)_((?:20\d\d)[_-]?(\d{2})[_-]?(\d{2}))", n, re.I)
    if not m:
        return None
    return m.group(1), f"{int(m.group(3)):02d}_{int(m.group(4)):02d}"


def collect(header_rows) -> dict:
    """[(name, header dict)] -> {webinar_id: best header}.

    A webinar sits on both Shared Drives and sometimes twice on one, and the
    copies differ. The copy reporting the most unique viewers wins — the same
    tie-break the marker and the poll reader already use, for the same reason.
    """
    best: dict = {}
    for name, h in (header_rows or ()):
        if not h:
            continue
        wid = h.get("webinar_id") or (name_key(name) or ("",))[0]
        if not wid:
            continue
        prev = best.get(wid)
        if prev and (prev.get("unique_viewers") or 0) >= (h.get("unique_viewers") or 0):
            continue
        best[wid] = h
    return best


def lookup_by_session(by_wid: dict, l2_bytes) -> dict:
    """{webinar_id: header} -> {(batch, mm_dd, pod): header}.

    Keyed exactly like polls.lookup_by_session and the topic lookup, so a
    dashboard session finds its own metadata and a shared session reaches every
    batch that sat in it. The join is the Webinar ID, never the date, so a
    number can never drift onto the wrong session.
    """
    import attendance_core as ac
    import pods as _pods

    if not by_wid or not l2_bytes:
        return {}
    wid_map, wid_labels = ac.parse_l2(l2_bytes, with_labels=True)
    out: dict = {}
    for wid, h in by_wid.items():
        info = wid_map.get(wid)
        if not info:
            continue
        keys, _topic = info
        raw = (wid_labels.get(wid) or "").strip()
        pod = _pods.from_l2_label(raw) if raw else None
        pod = "" if pod in (None, _pods.WHOLE_BATCH) else pod
        mm = h.get("_mm")
        if not mm:
            continue
        for track, num in keys:
            label = f"B{num}" if track == "CAP" else f"{track} B{num}"
            prev = out.get((label, mm, pod))
            if prev and (prev.get("unique_viewers") or 0) >= (h.get("unique_viewers") or 0):
                continue
            out[(label, mm, pod)] = h
    return out
