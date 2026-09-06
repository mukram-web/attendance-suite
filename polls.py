"""
polls.py — session ratings out of the Zoom poll exports.

Every session folder carries a `poll_<webinarid>_<date>.csv` next to its attendee
report. The poll is a feedback form, so it answers the question attendance never
can: people turned up, but was it any good?

Three ratings are extracted, each 1-5:

    session    "What was your overall session feedback?"
    trainer    "How would you rate the trainer?"
    recommend  "How likely would you recommend it to your friends?"

The wording drifts a lot across 1,100+ files - '(Description)' suffixes, a
'Please note: 1 = Very Poor, 5 = Excellent' tail, 'How satisfied are you with
today's project session?' - so columns are matched on KEYWORDS, never on exact
text. Anything unrecognised is ignored rather than guessed at: a wrong column
would put a plausible number on screen with nothing to reveal it.

Two export shapes exist and both are handled:

  wide  `#, User Name, Email Address, Submitted Date and Time, <Q1>, <Q2>, …`
        one row per respondent, one column per question
  long  `Question, Answer` pairs

Values outside 1-5 are dropped - the free-text "what can we improve" column
sometimes lands where a number is expected.
"""
from __future__ import annotations

import csv
import io
import re

# keyword -> which rating. Order matters: 'trainer' is checked before the
# generic session match, because "How would you rate the trainer?" also contains
# nothing session-specific but would fall through to a looser rule otherwise.
_MATCHERS = (
    ("trainer",   ("rate the trainer", "trainer")),
    ("recommend", ("recommend",)),
    # 'trainer' and 'recommend' are matched first, so a bare "rate the ..." here
    # cannot swallow them.
    ("session",   ("overall session", "session feedback", "overall experience",
                   "satisfied", "overall session feedback", "rate the overall",
                   "rate the today", "rate today", "today's class", "rate the class",
                   "rate the session")),
)

_KINDS = ("session", "trainer", "recommend")


def _classify(question: str) -> str | None:
    q = re.sub(r"\s+", " ", str(question or "")).strip().lower()
    if not q:
        return None
    # free text, never a rating
    if "improve" in q or "description" in q and "rate" not in q:
        return None
    for kind, needles in _MATCHERS:
        if any(n in q for n in needles):
            return kind
    return None


def _score(v) -> float | None:
    """A 1-5 rating, or None. Zoom writes them as plain integers."""
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"([1-5])(?:\s*[-–].*)?", s)
    if m:
        return float(m.group(1))
    try:
        f = float(s)
    except ValueError:
        return None
    return f if 1.0 <= f <= 5.0 else None


def nps(scores) -> int | None:
    """Net Promoter Score from Be10x's 1-5 recommend question, top-box.

        promoter = 5, passive = 4, detractor = 1-3
        NPS = (promoters - detractors) / answered x 100

    Why top-box and not a rescale. Rescaling 1-5 onto 0-10 linearly gives
    x10 = (x5 - 1) x 2.5, so 5 -> 10 (promoter), 4 -> 7.5 (passive) and 3 -> 5
    (detractor): exactly this split. The convention and the arithmetic agree, so
    nothing is being fudged to make the number look better.

    Why the scale is NOT sniffed from the data. The obvious implementation picks
    the 0-10 rule when it sees a value above 5 and the 1-5 rule otherwise. That
    is a trap: a genuine 0-10 poll where everybody answered 5 or less would then
    be scored on the 1-5 rule, turning every detractor into a promoter and a
    -100 session into +100. Measured over all 1,113 cached poll files, Be10x has
    never asked a 0-10 recommend question - answers are {1: 4188, 2: 4747,
    3: 19380, 4: 71401, 5: 216290} - so this function commits to 1-5 and
    `_score` already rejects anything outside it. If a 0-10 question is ever
    introduced, add an explicit scale argument; do not infer it from the values.

    Returns None when nobody answered, never 0 - "no data" and "as many
    detractors as promoters" must not render as the same number.
    """
    return nps_from_dist(_distribution(scores))


def nps_from_dist(dist) -> int | None:
    """The same rule as `nps`, for callers holding counts rather than answers.

    This is the one place the promoter/detractor split is written down, so the
    rollup on the dashboard and the per-session number can never drift apart.

    To combine several sessions (the eleven POD sessions on one date, or a whole
    week), SUM their histograms and call this — never average their NPS
    percentages. A 13-response pod would otherwise weigh the same as a
    316-response one.
    """
    if not dist:
        return None
    c = {str(i): int(dist.get(str(i), 0) or 0) for i in range(1, 6)}
    n = sum(c.values())
    if not n:
        return None
    return round((c["5"] - c["1"] - c["2"] - c["3"]) / n * 100)


def merge_dists(dists) -> dict:
    """Sum several {kind: {'1'…'5': n}} histograms into one."""
    out: dict = {}
    for d in dists or ():
        for kind, hist in (d or {}).items():
            m = out.setdefault(kind, {str(i): 0 for i in range(1, 6)})
            for b, n in (hist or {}).items():
                if b in m:
                    m[b] += int(n or 0)
    return out


def _distribution(scores) -> dict:
    """{'1': n, …, '5': n} over 1-5, zeros included.

    Every bucket is present even when empty: the UI draws a five-bar chart, and
    a missing key would silently shorten it rather than showing a gap.
    """
    out = {str(i): 0 for i in range(1, 6)}
    for v in scores or ():
        if v is None:
            continue
        b = str(int(round(v)))
        if b in out:
            out[b] += 1
    return out


def parse(text: str) -> dict:
    """Poll CSV -> {'session': avg|None, 'trainer': …, 'recommend': …,
    'responses': int, 'dist': {kind: {'1'…'5': n}}, 'nps': int|None}.

    Averages are rounded to 1dp; `responses` is the number of people who gave at
    least one rating. `dist` is the full 1-5 histogram per kind — the mean alone
    cannot tell a room that was uniformly lukewarm from one that was half
    delighted and half furious, and those need different responses.
    """
    rows = list(csv.reader(io.StringIO(text)))
    buckets: dict[str, list] = {k: [] for k in _KINDS}
    respondents = 0

    # ── indexed long form: '#, User Name, User Email, Submitted, Question, Answer'
    # one ROW per answer. Zoom's "Poll Report" export (as opposed to "Overview")
    # uses this, and it defeats both other readers: the header looks wide, but
    # its question columns are literally named 'Question' and 'Answer'.
    hdr_i = next((i for i, r in enumerate(rows)
                  if r and r[0].strip() == "#"
                  and any(c.strip().lower() == "question" for c in r)
                  and any(c.strip().lower() == "answer" for c in r)), None)
    if hdr_i is not None:
        hdr = [c.strip().lower() for c in rows[hdr_i]]
        qi, ai = hdr.index("question"), hdr.index("answer")
        people = set()
        ui = next((i for i, c in enumerate(hdr) if "email" in c), None)
        for r in rows[hdr_i + 1:]:
            if len(r) <= max(qi, ai) or not r[0].strip().isdigit():
                continue
            kind = _classify(r[qi])
            if kind and (v := _score(r[ai])) is not None:
                buckets[kind].append(v)
                people.add(r[ui].strip().lower() if ui is not None and ui < len(r)
                           else r[0])
        respondents = len(people)

    # ── wide form ────────────────────────────────────────────────────────────
    if not any(buckets.values()):
        hdr_i = next((i for i, r in enumerate(rows)
                      if r and r[0].strip() == "#" and any("user name" in c.strip().lower()
                                                           for c in r)), None)
    else:
        hdr_i = None
    if hdr_i is not None:
        hdr = rows[hdr_i]
        cols = {i: k for i, c in enumerate(hdr)
                if (k := _classify(c)) and i >= 3}
        for r in rows[hdr_i + 1:]:
            if not r or not r[0].strip().isdigit():
                continue
            got = False
            for i, kind in cols.items():
                if i < len(r) and (v := _score(r[i])) is not None:
                    buckets[kind].append(v)
                    got = True
            respondents += bool(got)

    # ── long form: Question, Answer ──────────────────────────────────────────
    if not any(buckets.values()):
        qi = next((i for i, r in enumerate(rows)
                   if len(r) >= 2 and r[0].strip().lower() == "question"), None)
        if qi is not None:
            for r in rows[qi + 1:]:
                if len(r) < 2:
                    continue
                kind = _classify(r[0])
                if kind and (v := _score(r[1])) is not None:
                    buckets[kind].append(v)
                    respondents += 1

    out = {k: (round(sum(v) / len(v), 2) if v else None) for k, v in buckets.items()}
    out["responses"] = respondents
    # The raw scores are in hand at this point whichever export shape was read,
    # so the histogram and the NPS cost nothing extra. Both ride the existing
    # {(batch, mm, pod): ratings} payload through lookup_by_session and into the
    # store, so no new plumbing is needed downstream.
    out["dist"] = {k: _distribution(v) for k, v in buckets.items()}
    out["nps"] = nps(buckets["recommend"])
    return out


def name_key(name) -> tuple[str, str] | None:
    """Poll filename -> (webinar_id, mm_dd). Two conventions are in use:

        poll_99299344465_2026_08_30.csv                 (Zoom's own export)
        99299344465 - 2026-08-30 - Poll Report.csv      (saved by hand)

    Reading only the first left 59 webinars' feedback stranded on Drive.
    """
    n = str(name).rsplit("/", 1)[-1]
    m = re.match(r"poll_(\d+)_(20\d\d)[_-]?(\d{2})[_-]?(\d{2})", n)
    if not m:
        m = re.match(r"(\d{9,})\s*[-_]\s*(20\d\d)[-_](\d{2})[-_](\d{2})", n)
    if not m:
        return None
    return m.group(1), f"{int(m.group(3)):02d}_{int(m.group(4)):02d}"


def parse_files(poll_files) -> dict:
    """[(name, bytes), …] -> {webinar_id: ratings}.

    Duplicated exports of one webinar are collapsed by keeping the copy with the
    most responses - the same rule the attendee reports use, and for the same
    reason: the drives hold overlapping copies that differ by a row or two.
    """
    best: dict[str, dict] = {}
    for name, blob in poll_files or ():
        key = name_key(name)
        if not key:
            continue
        wid = key[0]
        text = (blob.decode("utf-8-sig", errors="replace")
                if isinstance(blob, bytes) else blob)
        try:
            got = parse(text)
        except Exception:
            continue
        if not any(got[k] is not None for k in _KINDS):
            continue
        prev = best.get(wid)
        if prev and prev["responses"] >= got["responses"]:
            continue
        best[wid] = got
    return best


def lookup_by_session(poll_files, l2_bytes) -> tuple[dict, dict]:
    """-> ({(batch, mm_dd, pod): ratings}, {webinar_id: ratings}).

    Keyed exactly like the topic lookup, so a dashboard session finds its own
    poll, and by webinar id too for callers that already work that way (BSIAI).
    The join is Webinar ID - the same key the marker and the topic lookup use -
    so a rating can never drift onto the wrong session.
    """
    import attendance_core as ac
    import pods as _pods

    by_wid = parse_files(poll_files)
    if not by_wid or not l2_bytes:
        return {}, by_wid

    # webinar -> mm_dd, in one pass over the filenames
    mm_of: dict[str, str] = {}
    for name, _ in poll_files or ():
        if (k := name_key(name)):
            mm_of.setdefault(k[0], k[1])

    wid_map, wid_labels = ac.parse_l2(l2_bytes, with_labels=True)
    out: dict = {}
    for wid, rating in by_wid.items():
        info, mm = wid_map.get(wid), mm_of.get(wid)
        if not info or not mm:
            continue
        keys, _topic = info
        raw = (wid_labels.get(wid) or "").strip()
        pod = _pods.from_l2_label(raw) if raw else None
        pod = "" if pod in (None, _pods.WHOLE_BATCH) else pod
        for track, num in keys:
            label = f"B{num}" if track == "CAP" else f"{track} B{num}"
            prev = out.get((label, mm, pod))
            if prev and prev["responses"] >= rating["responses"]:
                continue
            out[(label, mm, pod)] = rating
    return out, by_wid
