"""
pods.py — one canonical name for a POD (domain), whatever source spelled it.

From B35 the programme splits each batch into domain PODs, and a single date can
carry eleven different sessions (B35, 23 Aug). Three sources name the same POD
three ways, so nothing joins until they are reconciled:

    roster  `POD pref` cell   'Operations/Supply Chain - AI Career Accelerator Program B35'
    L2      `Batch Name`      'AI CAP B35 - Ops/SC'
    Drive   folder name       '2026-08-23 - AI CAP B35 - Ops/Supply Chain - <topic>'

`canon()` folds all of them to `Ops/Supply Chain`.

Design notes:

* Matching is on a **squashed key** (lowercase, letters+digits only), so
  'BusinessOwners' == 'Business Owners' with no alias needed. Only genuinely
  different words — 'Ops/SC', 'S/M/HR', 'AI Generalist' — need an entry.
* An unrecognised value returns None and the caller WARNS. Silently bucketing a
  new POD as 'other' would quietly move a denominator, which is the one failure
  mode that is hard to notice; a warning gets the alias added instead.
* `All Domains` (and a label with no domain suffix at all) means the session is
  for the whole batch, not a POD — that is `WHOLE_BATCH`, not an unknown.
"""
from __future__ import annotations

import re

# The session covers every POD, so its denominator is the batch, not a POD.
WHOLE_BATCH = "*all*"

# Students whose POD cell is blank or literally 'Unidentified'. Kept as a real
# bucket rather than dropped - they are enrolled, and hiding them would shrink
# the batch total below its roster count.
UNKNOWN = "Unassigned"

# The COMPLEMENT cohort. A batch's early weekends run as two parallel rooms:
# one domain POD (in practice Techies) and one for everybody else. That second
# room has no POD name of its own anywhere - not in the roster, not in L2 - so
# it used to be read as a whole-batch session and divided by full strength,
# reporting B40's 1,611 attendees as 43% of 3,711 when they were 51% of the
# 3,150 people actually invited.
#
# It is deliberately NOT a value that can appear in a roster cell. Membership is
# "not in the PODs that met that day", which is a property of the SESSION, not
# of the student - the same person is Common on a two-room weekend and
# Generalist once the batch moves to eleven domain PODs.
COMMON = "Common"

# ONE room inviting TWO PODs. L2 wrote 'AI CAP B35 , B36 , B37 , B38 - S/M/HR
# + Content Creators' on 27 Sep 2026: one webinar, one attendee export, one
# column - for two domains. `from_l2_label` folds such a tail to a single
# canonical string in `ALL` order ('Sales/Marketing/HR + Content Creators'),
# and that exact string is the join key everywhere: the column header the
# marker writes, `_col_pod` reading it back, and the (batch, date, pod) keys
# polls, topics, labels, mentors and session metadata are filed under. A
# display-name / key mismatch once showed a rating with 424 answers as "no
# poll conducted", so the string is never re-spelled downstream - consumers
# that need the PODs themselves call `members()`.
COMPOUND_SEP = " + "

# canonical name -> every spelling seen in the roster, L2 or a folder name
_ALIASES: dict[str, tuple[str, ...]] = {
    # 'general' is NOT here: B36 uses BOTH 'AI CAP B36 - General' (22/23 Aug,
    # the whole batch) and 'AICAPB35, B36-Generalist' (30 Aug, the AI Generalist
    # POD). Folding them together scored a whole-batch session against the POD -
    # B37's 22 Aug reported 1,631 present out of 571.
    "Generalist":          ("generalist", "aigeneralist"),
    # 'techis' is a real spelling on Drive: "AI CAP B37 8PM-Techis"
    "Techies":             ("techies", "techie", "techis", "tech"),
    "Finance":             ("finance",),
    "Business Owners":     ("businessowners", "businessownersenterpreneurs",
                            "businessownersentrepreneurs", "businessowner"),
    "Sales/Marketing/HR":  ("salesmarketinghr", "smhr", "salesmarketing"),
    "Ops/Supply Chain":    ("opssupplychain", "operationssupplychain", "opssc",
                            "operations"),
    "Educators":           ("educators", "educator"),
    "Students":            ("students", "student"),
    "Healthcare":          ("healthcare",),
    "Data":                ("data",),
    "Content Creators":    ("contentcreators", "contentcreator"),
}

# Every way the sheet says "this one is for everybody".
_WHOLE = ("alldomains", "all", "commonsession", "common", "general")

_LOOKUP = {a: name for name, aliases in _ALIASES.items() for a in aliases}
_LOOKUP.update({_squash: WHOLE_BATCH for _squash in _WHOLE})

# 'Techies - AI Career Accelerator Program B35' -> 'Techies'
_PROGRAMME_TAIL = re.compile(
    r"\s*[-–]\s*AI\s*Career\s*Accelerator\s*Program\s*B?\d*\s*$", re.I)

ALL = tuple(_ALIASES)          # display order for the UI


def _squash(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def canon(text) -> str | None:
    """Any POD spelling -> its canonical name. None if unrecognised.

    Returns `WHOLE_BATCH` for 'All Domains'. Callers should warn on None rather
    than inventing a bucket - see the module docstring.
    """
    s = _PROGRAMME_TAIL.sub("", str(text or "").strip())
    k = _squash(s)
    if not k:
        return None
    return _LOOKUP.get(k)


def members(pod) -> tuple[str, ...]:
    """The canonical PODs a room's name stands for.

    'Techies'                               -> ('Techies',)
    'Sales/Marketing/HR + Content Creators' -> ('Sales/Marketing/HR', 'Content Creators')
    ''                                      -> ()

    Every "is this student invited" / "what is the denominator" decision goes
    through this rather than comparing the pod string, so a compound room is
    understood in exactly one place. A plain name comes back as a one-tuple,
    which makes the call safe on every string the roster or the marker can
    produce - UNKNOWN and COMMON are simply their own one-member rooms.
    """
    s = str(pod or "").strip()
    if not s:
        return ()
    if "+" not in s:
        return (s,)
    return tuple(p.strip() for p in s.split("+") if p.strip())


def _room(text) -> str | None:
    """A label's domain tail -> the ONE string a session column will carry.

    A single POD resolves through `canon`. A '+'-joined tail resolves only when
    EVERY item is a POD, to one compound string in `ALL` order - so 'Content
    Creators + S/M/HR' and 'S/M/HR + Content Creators' are the same room and
    the same join key. A whole-batch alias anywhere in it ('All Domains +
    Techies') is the whole batch. One item nobody has taught `canon` makes the
    whole tail None, exactly as a single unknown POD does, so the caller warns
    instead of scoring the room against a guessed denominator.
    """
    hit = canon(text)
    if hit or "+" not in str(text or ""):
        return hit
    got = []
    for item in str(text).split("+"):
        h = canon(item)
        if h is None:
            return None
        if h == WHOLE_BATCH:
            return WHOLE_BATCH
        got.append(h)
    return COMPOUND_SEP.join(p for p in ALL if p in got)


# A segment that names a batch ('AI CAP B35, B36', 'AICAPB35', 'B37-42'), in
# the same spellings `attendance_core._folder_batches` accepts.
_BATCH_TOKEN = re.compile(r"\bB\s?\d{1,3}\b|CAP\s*B?\d{1,3}\b", re.I)


def _dashless_tail(text) -> str:
    """The domain a DASHLESS label may end with, or '' when it ends in none.

    'AI CAP B35, B36, B37, B38 Finance' -> 'Finance'
    'AI CAP B17 11AM'                   -> '11AM'   (canon() then says None)
    'AI CAP B35'                        -> ''

    The last comma item with its leading batch token stripped. Shared by
    `from_l2_label` and `from_folder` so the marker's column header and the
    folder's skip key are read by ONE rule: a folder the marker labelled
    'Finance' must resolve to 'Finance' here too, or it is downloaded and
    re-marked on every incremental run instead of staying frozen.
    """
    last = str(text or "").split(",")[-1]
    return re.sub(r"^.*?\d{1,3}\b\s*", "", last, count=1).strip()


def from_roster_cell(cell) -> tuple[str | None, bool]:
    """Roster `POD pref` cell -> (canonical POD or UNKNOWN, needs_attention).

    A handful of rows hold several PODs joined by ';' - the sheet's edit history
    leaking into the cell ('Finance - ...B36; Business Owners/Enterpreneurs').
    The LAST segment is taken as the current choice, and the flag is returned so
    the caller can surface how many rows were guessed at.
    """
    raw = str(cell or "").strip()
    if not raw:
        return UNKNOWN, False
    if _squash(raw) == "unidentified":
        return UNKNOWN, False
    parts = [p for p in raw.split(";") if p.strip()]
    multi = len(parts) > 1
    got = canon(parts[-1])
    if got is None or got == WHOLE_BATCH:
        return None, multi
    return got, multi


def from_l2_label(label) -> str | None:
    """L2 `Batch Name` -> the POD it is for, or WHOLE_BATCH.

    'AI CAP B35 - Techies'      -> 'Techies'
    'AICAPB35, B36-Techies'     -> 'Techies'   (no-space form, real)
    'AI CAP B35'                -> WHOLE_BATCH (no domain named)
    'AI CAP B40 - Common , BSIAI Accelerator B1'
                                -> WHOLE_BATCH (see below)
    'AI CAP B35 - Nonsense'     -> None        (caller warns)

    Only the tail after the LAST '-' is considered: everything before it is the
    batch part, which may itself contain hyphens ('B37-42').

    ONE WEBINAR CAN HOST TWO SESSIONS from different programmes, and the tail
    then names both: `Common , BSIAI Accelerator B1` is AI CAP's Common room
    sharing a Zoom room with BSIAI Accelerator B1 (a different programme, whose
    B1 and B2 are different batches). So each comma-separated item is tried and
    the first that names a domain wins - this function answers for AI CAP, and
    `extract_batches` separately returns both programmes from the same cell.
    Reading the whole tail as one name found nothing and warned on three real,
    correctly-labelled sessions every run.

    A ROOM CAN INVITE TWO PODS: 'AI CAP B35 , B36 , B37 , B38 - S/M/HR +
    Content Creators' (27 Sep 2026) resolves to the single compound string
    'Sales/Marketing/HR + Content Creators' - see `_room` and `members`.

    THE DASH IS SOMETIMES MISSING: 'AI CAP B35, B36, B37, B38 Finance' (26 Sep
    2026, one row; every sibling that weekend reads '..., B38 - Finance').
    With no dash this read as a whole-batch session and a ~300-person Finance
    room was scored against 3,260 (2.9%, should be ~30%). So when there is no
    dash - or the tail after the last one is only batch numbering - the LAST
    comma item is tried with its leading batch token stripped ('B38 Finance'
    -> 'Finance'). Only a real POD name changes the answer: 'AI CAP B35' -> ''
    and 'AI CAP B17 11AM' -> '11AM' both stay WHOLE_BATCH. Measured over all
    578 distinct Batch Name cells in L2's history, exactly two labels resolve
    differently under these two rules - the two rows above.
    """
    s = re.sub(r"\s+", " ", str(label or "")).strip()
    if not s:
        return None
    parts = re.split(r"[-–]", s)
    tail = parts[-1].strip() if len(parts) > 1 else ""
    # No dash ('AI CAP B35'), or a tail that is just batch numbering
    # ('B37-42' -> '42'): the label names no domain the usual way. Try the
    # dashless form before settling on WHOLE_BATCH.
    if not tail or re.fullmatch(r"[Bb]?\d{1,3}", tail):
        last = _dashless_tail(s)
        hit = _room(last) if last else None
        return hit if hit else WHOLE_BATCH
    hit = _room(tail)
    if hit:
        return hit
    # Two sessions in one room: try each item on its own.
    for item in tail.split(","):
        hit = _room(item.strip())
        if hit:
            return hit
    return None


def from_folder(folder_name) -> str | None:
    """Session folder -> its POD, or None when the name does not carry one.

    '2026-08-23 - AI CAP B35 - Techies - Python with AI' -> 'Techies'
    Segment 3 is inspected only when it resolves to a known POD, so a topic that
    happens to sit there ('2026-08-02 - AI CAP B33 - Office Productivity') is
    not mistaken for a domain.

    Reads the SAME two spellings `from_l2_label` learned from the 26-27 Sep
    2026 weekend, through the same helpers, so the skip key `folder_keys`
    builds equals the column header the marker wrote from L2:

      '... - S M HR + Content Creators - ...'   -> 'Sales/Marketing/HR + Content Creators'
      '... - AI CAP B35, B36, B37, B38 Finance - ...' -> 'Finance'

    Before this, both folders resolved to the whole-batch key, never matched
    their own column, and were re-fetched and re-marked on every incremental
    run - the freeze did not apply to them at all.
    """
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", str(folder_name or ""))]
    for p in parts[1:]:
        got = _room(p)
        if got and got != WHOLE_BATCH:
            return got
        # The POD is often welded to the batch with an UNSPACED hyphen, which the
        # spaced split above leaves inside one segment: 'AI CAP B37 8PM-Techis',
        # 'AICAPB35, B36-Generalist'. Read the tail after the last hyphen, the
        # same way from_l2_label does. Measured 2026-09-10: without this, 53 of
        # 609 already-marked sessions were re-downloaded and re-marked on every
        # incremental run, so their columns were not frozen at all.
        #
        # A false positive here (a topic ending in a domain word) costs one
        # redundant download; a false negative costs the freeze. The asymmetry
        # is why the looser rule is the right one.
        if "-" in p or "–" in p:
            got = _room(re.split(r"[-–]", p)[-1])
            if got and got != WHOLE_BATCH:
                return got
        elif _BATCH_TOKEN.search(p):
            # The dashless form, only in a segment that names a batch so a
            # topic ('Session 2 Finance') cannot be read as a domain.
            got = _room(_dashless_tail(p))
            if got and got != WHOLE_BATCH:
                return got
    return None
