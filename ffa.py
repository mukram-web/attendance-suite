"""FFA sessions: marked ONLY from the exports the owner hands over.

FFA — the 4-Day Financial Freedom Accelerator — is a different programme that
AI CAP cohorts attend in the middle of their course. Each AI CAP batch meets it
exactly once, at roughly 2-5 weeks old, and L2 records that by writing `FFA` in
Topic Name for that weekend instead of a curriculum topic.

Two facts make FFA unlike every other session this pipeline marks:

1. **L2 never carries its Webinar ID.** Measured 2026-09-19: 0 of 104 FFA rows
   have one, across 20 monthly tabs. That is the sheet's convention, not an
   oversight, so the usual Webinar-ID join has nothing to join on and
   `attendance_core` skips the export as unregistered.

2. **One Zoom webinar spans the whole event.** FFA B20's days 1-4 are four
   dates on a single id (91643507137), and the room holds the entire FFA
   funnel — 8,000-11,000 people — of whom only the AI CAP students matter here.

So FFA is registered HERE, by hand, per (webinar, date), and the owner's rules
(2026-09-23) are enforced by the shape of this file:

* **Only what was handed over gets marked.** Nothing is inferred from a date,
  a folder name or an L2 row. A (webinar, date) absent from `_SESSIONS` is not
  an FFA session and `lookup` returns None — which is why the SAME webinar's
  days 1 and 2 stay unmarked even if their exports are dropped on the drive.
* **Whole batch, never domain-wise.** FFA is one room for everyone, so there is
  no POD to split it by. `attendance_core` forces the whole-batch column for
  anything this module claims, and the label below deliberately carries no
  ` - <domain>` tail for `pods.from_l2_label` to find.

To add an FFA weekend: append one entry per DATE (not per file — an invite call
or a weekday session that is not an AI CAP slot simply gets no entry), then run
the pipeline. `tests/test_ffa.py` checks the entries parse and that the days
left out stay out.
"""
import re

# One entry per (webinar, date) the owner has handed over and asked to count.
#
# `batches`  — the AI CAP batch numbers that sat in that room. FFA B20's room
#              was B33-B38; everything else in it belongs to other programmes.
#              Verified against the rosters 2026-09-23: B33-B38 matched 26-39%
#              of their students, every other batch 0.0-0.2%.
# `topic`    — what the dashboard shows. Matches L2's own word for it.
# `day`      — which day of the FFA event this was, for the Drive folder name
#              only. Note it says "Day 3", never "B20": see `folder_name`.
# `source`   — which export this came from, so a column can be traced back.
_SESSIONS = (
    dict(wid="91643507137", ymd="2026_09_12", batches=(33, 34, 35, 36, 37, 38),
         topic="FFA", day="Day 3", source="FFA B20 DAY-3.csv"),
    dict(wid="91643507137", ymd="2026_09_13", batches=(33, 34, 35, 36, 37, 38),
         topic="FFA", day="Day 4", source="FFA B20 DAY-4.csv"),
)

TRACK = "CAP"          # FFA is attended by AI CAP cohorts; the key space is theirs


def _norm_wid(wid) -> str:
    """Digits only — Zoom writes '916 4350 7137', filenames write '91643507137'."""
    return re.sub(r"\D", "", str(wid or ""))


def _norm_ymd(ymd) -> str:
    """`YYYY_MM_DD`, however the caller punctuated it."""
    return re.sub(r"[^0-9]", "_", str(ymd or "")).strip("_")


_BY_KEY = {(_norm_wid(s["wid"]), _norm_ymd(s["ymd"])): s for s in _SESSIONS}


def batch_label(batches) -> str:
    """The L2-style batch cell for these batches, e.g. 'AI CAP B33 , B34 , B38'.

    Written in L2's own comma form so `attendance_core.extract_batches` and
    `data.shared_batches` read it exactly as they read a real shared session.
    There is deliberately no ' - <domain>' tail: `pods.from_l2_label` treats a
    label with no hyphen as a whole-batch session, which is what FFA is.
    """
    return "AI CAP " + " , ".join(f"B{n}" for n in batches)


def folder_name(session) -> str:
    """The Drive session folder for one entry: `YYYY-MM-DD - <label> - <topic>`.

    Same convention as every Zoom export folder, and it must stay parseable:
    `live_data.fetch_new_attendees` decides whether to download a folder at all
    by running `attendance_core._folder_batches` over this name.

    KEEP THE FFA COHORT NUMBER OUT OF IT. The first upload was named
    `... B38 - FFA (FFA B20 DAY-3)` and `extract_batches` read `B38 … B20` as a
    RANGE, so the folder claimed nineteen batches, B20 through B38. Nothing
    broke — the registry above is what supplies the batches, and the folder name
    is only a fallback the L2 rule never reaches — but a name that says B20 to a
    reader and B20-B38 to the parser is a trap for whoever comes next. `Day 3`
    carries the same information and parses to exactly the six.
    """
    tail = " ".join(x for x in (session["topic"], session.get("day")) if x)
    return (f"{session['ymd'].replace('_', '-')} - "
            f"{batch_label(session['batches'])} - {tail}")


def lookup(wid, ymd):
    """(frozenset[(track, num)], topic, label) for a hand-registered FFA export.

    None for everything else — including another date of the SAME webinar. The
    date is half the key on purpose: FFA B20 ran days 1-4 on one id, only two
    of which are AI CAP slots, and keying on the id alone would mark all four
    the moment their exports reached the drive.
    """
    s = _BY_KEY.get((_norm_wid(wid), _norm_ymd(ymd)))
    if not s:
        return None
    keys = frozenset((TRACK, n) for n in s["batches"])
    return keys, s["topic"], batch_label(s["batches"])


def sessions() -> tuple:
    """Every registered (wid, ymd, batches, topic, source), for reporting."""
    return tuple(dict(s) for s in _SESSIONS)
