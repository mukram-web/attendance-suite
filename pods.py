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
    'AI CAP B35 - Nonsense'     -> None        (caller warns)

    Only the tail after the LAST '-' is considered: everything before it is the
    batch part, which may itself contain hyphens ('B37-42').
    """
    s = re.sub(r"\s+", " ", str(label or "")).strip()
    if not s:
        return None
    parts = re.split(r"[-–]", s)
    if len(parts) == 1:
        return WHOLE_BATCH          # 'AI CAP B35' - names a batch, no domain
    tail = parts[-1].strip()
    if not tail:
        return WHOLE_BATCH
    # A tail that is just batch numbering ('B37-42' -> '42') names no domain.
    if re.fullmatch(r"[Bb]?\d{1,3}", tail):
        return WHOLE_BATCH
    return canon(tail)


def from_folder(folder_name) -> str | None:
    """Session folder -> its POD, or None when the name does not carry one.

    '2026-08-23 - AI CAP B35 - Techies - Python with AI' -> 'Techies'
    Segment 3 is inspected only when it resolves to a known POD, so a topic that
    happens to sit there ('2026-08-02 - AI CAP B33 - Office Productivity') is
    not mistaken for a domain.
    """
    parts = [p.strip() for p in re.split(r"\s+[-–]\s+", str(folder_name or ""))]
    for p in parts[1:]:
        got = canon(p)
        if got and got != WHOLE_BATCH:
            return got
    return None
