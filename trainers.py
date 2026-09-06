"""
trainers.py — who taught what, once the same person stops being three people.

Pure: session rows in, per-trainer rollups out. No I/O, unit-tested.

THE PROBLEM THIS EXISTS TO SOLVE
--------------------------------
L2's Mentor cell is hand-typed, so one person arrives under several spellings.
Measured on the live store: 94 distinct mentor strings across 447 sessions, but
only about 60 actual people. 'Swapnil' appears 28 times, 'Swapnil Narayan' 17,
'Swapnil (Play Simulive)' once — three rows in any per-trainer table, one human.
Rank that table and every one of them is mid-table on a third of their work.

The email column that would settle it does not: it covers 1,425 of 1,615 L2
rows, is absent entirely before June 2025, and is itself dirty — one address is
bound to four different given names. So email is used when present and trusted,
and names are clustered otherwise.

THE CLUSTERING RULE, AND WHY NOT FIRST-NAME
-------------------------------------------
The obvious rule — group on the first token — merges 'Swapnil' with 'Swapnil
Narayan' correctly and then merges 'Ravi Kumar' with 'Ravi Sharma', which is two
people's work credited to one. Instead a short name joins a longer one only when
its tokens are a genuine PREFIX of the longer one's, and only when exactly one
longer name matches. 'Ravi' beside both 'Ravi Kumar' and 'Ravi Sharma' is
ambiguous, so it stays its own entry and is flagged rather than guessed at.

That is deliberately conservative: leaving a name unmerged shows a small
duplicate row, while merging wrongly puts one person's poor week on another
person's record. Only one of those is a fair thing to do to someone.

CO-TAUGHT SESSIONS
------------------
A cell reading 'A, B' is one session taught by two people. Both get credit for
it — attributing it to neither would lose the session, and to the first name
alone would be arbitrary — but the count is reported separately as `co_taught`
so nobody reads a trainer's session total as hours they personally led.
"""
from __future__ import annotations

import re
from collections import defaultdict

import polls as _polls

# Splits a mentor cell into people. Ampersands, plus signs, slashes and the word
# 'and' all appear in real cells alongside commas.
_SPLIT = re.compile(r"\s*(?:,|&|\+|/|\band\b)\s*", re.I)

# '(Play Simulive)', '(Recording)', '[sub]' — a note about the SESSION that got
# typed into the person's name. Stripped for identity, so the same human is not
# split in two by how one week was delivered.
_PARENS = re.compile(r"[\(\[][^)\]]*[)\]]")

_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "sir", "sri", "smt", "shri"}

# Session notes that got typed into the name WITHOUT brackets, so _PARENS misses
# them: 'Varun Sahdev live', 'Swapnil simulive'. They describe how one week was
# delivered, not who delivered it, and left in they split one person in two.
_NOISE = {"live", "simulive", "recording", "recorded", "play", "sub", "backup"}


def split_mentors(cell) -> list:
    """A raw L2 Mentor cell -> the people named in it, in order, de-duplicated."""
    s = str(cell or "").strip()
    if not s:
        return []
    out, seen = [], set()
    for part in _SPLIT.split(s):
        p = part.strip(" .-")
        if not p:
            continue
        k = norm_name(p)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def norm_name(v) -> str:
    """A name reduced to comparable tokens: lowercase, no notes, no honorifics."""
    s = _PARENS.sub(" ", str(v or "")).lower()
    s = re.sub(r"[^a-z\s]", " ", s)
    toks = [t for t in s.split() if t and t not in _HONORIFICS and t not in _NOISE]
    return " ".join(toks)


def _display(members) -> str:
    """The variant to show for a resolved person.

    Most name tokens wins (so a full name beats a bare first name), then the
    SHORTEST raw string — otherwise 'Varun Sahdev live' beats 'Varun Sahdev'
    purely for carrying a note about how one session was delivered.
    """
    return min(members, key=lambda m: (-len(norm_name(m).split()), len(m), m))


def _is_prefix(short: list, long_: list) -> bool:
    return len(short) < len(long_) and long_[:len(short)] == short


def resolve(names, emails=None) -> dict:
    """{raw name -> canonical display name}, plus which ones stayed ambiguous.

    `emails` is an optional {raw name: email}. An email is authoritative when
    present: every name sharing one collapses to a single identity regardless of
    spelling. Names without an email fall back to prefix clustering.

    Returns {"canon": {raw: display}, "ambiguous": [raw, ...],
             "groups": {display: [raw, ...]}}.
    """
    emails = {k: (v or "").strip().lower() for k, v in (emails or {}).items()}
    raw = [str(n) for n in dict.fromkeys(n for n in names if str(n or "").strip())]

    # 1. email-backed identities first — they are the only hard evidence here.
    by_email: dict = defaultdict(list)
    for n in raw:
        e = emails.get(n)
        if e and "@" in e:
            by_email[e].append(n)

    canon: dict = {}
    groups: dict = defaultdict(list)
    claimed = set()
    anchors: list = []           # (tokens, display)
    for _e, members in by_email.items():
        display = _display(members)
        for m in members:
            canon[m] = display
            groups[display].append(m)
            claimed.add(m)
        # An email-backed identity is also an anchor for the names that have no
        # email of their own: 'Ravi' should attach to the known 'Ravi Kumar'
        # rather than starting a second person because it was resolved earlier.
        toks = norm_name(display).split()
        if toks:
            anchors.append((toks, display))

    # 2. prefix clustering for the rest, longest names first so short forms
    #    attach to a full name rather than becoming their own anchor.
    rest = [n for n in raw if n not in claimed]
    rest.sort(key=lambda n: (-len(norm_name(n).split()), n))
    for n in rest:
        toks = norm_name(n).split()
        if not toks:
            continue
        exact = [a for a in anchors if a[0] == toks]
        if exact:
            canon[n] = exact[0][1]
            groups[exact[0][1]].append(n)
            continue
        pref = [a for a in anchors if _is_prefix(toks, a[0])]
        if len(pref) == 1:
            canon[n] = pref[0][1]
            groups[pref[0][1]].append(n)
            continue
        # zero matches -> a new person. More than one -> genuinely ambiguous
        # ('Ravi' under both 'Ravi Kumar' and 'Ravi Sharma'), so it stands alone
        # and is reported rather than assigned to whichever sorted first.
        canon[n] = n
        groups[n].append(n)
        anchors.append((toks, n))

    ambiguous = []
    for n in rest:
        toks = norm_name(n).split()
        if toks and canon.get(n) == n:
            if len([a for a in anchors if _is_prefix(toks, a[0])]) > 1:
                ambiguous.append(n)
    return {"canon": canon, "ambiguous": sorted(ambiguous),
            "groups": {k: sorted(v) for k, v in groups.items()}}


def build(session_rows, emails=None) -> dict:
    """Per-trainer rollups from recap.collect_sessions() rows.

    Percentages are pooled by headcount, and the NPS by summed histogram — the
    same rules the weekly recap uses, so a trainer's number and the week's
    number are computed the same way and can be compared.
    """
    rows = [r for r in (session_rows or ()) if (r.get("mentor") or "").strip()]
    # every raw name that appears anywhere, so resolution sees the full field
    raw_names = []
    for r in rows:
        raw_names.extend(split_mentors(r["mentor"]))
    res = resolve(raw_names, emails)
    canon = res["canon"]

    per: dict = defaultdict(list)
    co: dict = defaultdict(int)
    for r in rows:
        people = split_mentors(r["mentor"])
        for p in people:
            key = canon.get(p, p)
            per[key].append(r)
            if len(people) > 1:
                co[key] += 1

    out = []
    for name, rs in per.items():
        present = sum(x["present"] for x in rs)
        invited = sum(x["total"] for x in rs)
        idx = [(x["index"], x["total"]) for x in rs if x["index"] is not None]
        wsum = sum(t for _i, t in idx)
        rated = [x for x in rs if x["rating"] is not None]
        rn = sum(x["rating_n"] for x in rated)
        merged = _polls.merge_dists(x.get("dist") for x in rs)
        out.append({
            "trainer": name,
            "sessions": len(rs),
            "co_taught": co.get(name, 0),
            "batches": sorted({x["batch"] for x in rs}),
            "pods": sorted({x["pod"] for x in rs if x["pod"]}),
            "present": present,
            "invited": invited,
            "pct": round(present / invited * 100, 1) if invited else None,
            "index": (round(sum(i * t for i, t in idx) / wsum, 3) if wsum else None),
            "rating": (round(sum(x["rating"] * x["rating_n"] for x in rated) / rn, 2)
                       if rn else None),
            "rating_n": rn,
            "nps": _polls.nps_from_dist(merged.get("recommend")),
            "first": min(x["date"] for x in rs),
            "last": max(x["date"] for x in rs),
        })
    out.sort(key=lambda t: (t["index"] is None, -(t["index"] or 0), -t["sessions"]))
    return {
        "trainers": out,
        "ambiguous": res["ambiguous"],
        "merged": {k: v for k, v in res["groups"].items() if len(v) > 1},
        "n_raw": len({norm_name(n) for n in raw_names if norm_name(n)}),
        "n_people": len(out),
    }
