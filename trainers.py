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
_NOISE = {"live", "recording", "recorded", "play", "sub", "backup"}

# Delivery notes matched on a PREFIX, because they are typed by hand and get
# misspelled. A single doubled letter — 'Simuliive' — split Aman Saurav into
# three separate trainers ranked 20, 30 and 41, and fabricated the app's only
# "ambiguous name" warning. An exact-match set cannot survive a typo; the prefix
# can, and no real given name in this data starts with 'simul'.
_NOISE_PREFIX = ("simul", "simmul")


def _is_noise(tok: str) -> bool:
    return tok in _NOISE or tok.startswith(_NOISE_PREFIX)


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
    # Single letters are initials, not names. Keeping them made 'Disha K' a
    # two-token name that neither matched nor was matched by 'Disha Kharbanda',
    # leaving one person as three rows. Dropping them lets the initial form
    # collapse into the full name, and where it is genuinely ambiguous ('Disha K'
    # beside 'Disha Kharbanda' AND 'Disha Malhotra') the prefix rule still
    # refuses to guess.
    toks = [t for t in s.split()
            if len(t) > 1 and t not in _HONORIFICS and not _is_noise(t)]
    return " ".join(toks)


def _display(members) -> str:
    """The variant to show for a resolved person.

    Most name tokens wins (so a full name beats a bare first name), then the
    SHORTEST raw string — otherwise 'Varun Sahdev live' beats 'Varun Sahdev'
    purely for carrying a note about how one session was delivered.
    """
    def real_tokens(m):
        # A lone initial is not a name token. Counting it made 'Disha K' rank
        # above 'Disha Kharbanda' as the canonical spelling.
        return sum(1 for t in norm_name(m).split() if len(t) > 1)
    return min(members, key=lambda m: (-real_tokens(m), len(m), m))


def _is_prefix(short: list, long_: list) -> bool:
    return len(short) < len(long_) and long_[:len(short)] == short


def _edit_le1(a: str, b: str) -> bool:
    """True when `a` and `b` differ by at most one insertion/deletion/substitution.

    Only used on tokens of 4+ characters, where a single-character difference is
    far more likely to be a typo than a different name. It is what lets
    'Isshita' meet 'Ishita' and 'Varrun' meet 'Varun'.
    """
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1 or min(la, lb) < 4:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if la < lb else (b, a)
    for i in range(len(long_)):
        if short == long_[:i] + long_[i + 1:]:
            return True
    return False


def _same_person(a: str, b: str) -> bool:
    """Could these two spellings plausibly be one human?

    True when either name's tokens are a prefix of the other's, or any two
    tokens match within one character. Deliberately generous — it exists to
    survive typing, not to adjudicate identity.
    """
    ta, tb = norm_name(a).split(), norm_name(b).split()
    if not ta or not tb:
        return False
    if ta == tb or _is_prefix(ta, tb) or _is_prefix(tb, ta):
        return True
    return any(_edit_le1(x, y) for x in ta for y in tb)


def _components(members: list) -> list:
    """Split names into connected groups under `_same_person`, largest first."""
    groups: list = []
    for m in members:
        hit = [g for g in groups if any(_same_person(m, x) for x in g)]
        if not hit:
            groups.append([m])
            continue
        merged = [m]
        for g in hit:
            merged += g
            groups.remove(g)
        groups.append(merged)
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


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
    shared_email = {}
    for _e, members in by_email.items():
        # An address is strong evidence but not proof: measured on the live L2,
        # 56 addresses carry more than one spelling and almost all are typos of
        # one person ('Ishita'/'Isshita', 'Varun'/'Varrun') -- exactly the merges
        # worth having. One is not: kaladipti0@gmail.com is typed against
        # 'Dipti', 'Dipti Kala', 'Vansh Agrawal' AND 'Abhishek Raj Pramani'.
        # So the names under one address are split into plausibly-same-person
        # groups; the biggest keeps the address and the rest fall through to
        # name clustering. Trusting the address outright credited two people's
        # work to a third.
        comps = _components(members)
        for i, comp in enumerate(comps):
            if i:
                shared_email.setdefault(_e, []).append(comp)
                continue
            display = _display(comp)
            for m in comp:
                canon[m] = display
                groups[display].append(m)
                claimed.add(m)
            # An email-backed identity is also an anchor for the names that have
            # no email of their own: 'Ravi' should attach to the known 'Ravi
            # Kumar' rather than starting a second person.
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
            "groups": {k: sorted(v) for k, v in groups.items()},
            # addresses that turned out to name more than one person
            "shared_email": {e: [sorted(c) for c in cs]
                             for e, cs in shared_email.items()}}


_SIMULIVE = re.compile(r"\bsimulive\b", re.I)


def session_type(mentor_cell) -> str:
    """'Simulive' when the Mentor cell says so, else '' — never 'Live'.

    Deliberately not a Live-vs-Simulive verdict. Measured on the live L2, only
    44 of 1,610 mentor rows carry a simulive marker and 5 say 'live'; the other
    1,559 are a plain name. Blank means NOT RECORDED, and rendering it as 'Live'
    would assert 1,559 facts nobody entered. The reference app shows 'Live' for
    everything for exactly this reason, and it is wrong to.

    A real Live/Simulive flag needs the attendee report's own presenter marker,
    which the weekly run does not fetch.
    """
    return "Simulive" if _SIMULIVE.search(str(mentor_cell or "")) else ""


def _types_by_display(groups: dict, types: dict) -> dict:
    """{display name -> In-house/Freelance}, resolved across EVERY spelling.

    The classification is typed against whichever spelling the person happened
    to be entered as that week, which is often the short one ('Swapnil') while
    the display name is the full one ('Swapnil Narayan'). Looking it up by the
    display name alone therefore missed it. Every raw variant in the group is
    checked, and if two variants disagree the majority of the group wins.
    """
    out = {}
    for display, members in (groups or {}).items():
        tally: dict = {}
        for m in members:
            t = (types or {}).get(norm_name(m))
            if t:
                tally[t] = tally.get(t, 0) + 1
        if tally:
            out[display] = max(tally, key=lambda k: tally[k])
    return out


def annotate(session_rows, emails=None, types=None) -> list:
    """Session rows + resolved trainer identity, trainer type and session type.

    Returns NEW dicts; the input is not mutated. `types` is keyed on the
    normalised name (as `attendance_core.l2_mentor_types` returns it), so a
    classification typed against 'Swapnil' also reaches 'Swapnil Narayan'.
    """
    types = types or {}
    raw_names = []
    for r in (session_rows or ()):
        raw_names.extend(split_mentors(r.get("mentor")))
    res = resolve(raw_names, emails)
    canon = res["canon"]
    by_display = _types_by_display(res["groups"], types)
    out = []
    for r in (session_rows or ()):
        people = split_mentors(r.get("mentor"))
        shown = [canon.get(p, p) for p in people]
        # A person's type, not a session's. First one found wins for the row;
        # co-taught rows list both people in `trainers` anyway.
        tt = ""
        for p in shown:
            tt = by_display.get(p) or ""
            if tt:
                break
        out.append({**r,
                    "trainers": shown,
                    "trainer": ", ".join(shown),
                    "trainer_type": tt,
                    "session_type": session_type(r.get("mentor"))})
    return out


def build(session_rows, emails=None, types=None) -> dict:
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
    by_display = _types_by_display(res["groups"], types)

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
            "type": by_display.get(name, ""),
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
