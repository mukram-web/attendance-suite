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


_SUBMIT_FMTS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %I:%M %p", "%Y-%m-%d %H:%M:%S")


def submission_times(text) -> list:
    """Every per-response 'Submitted Date and Time', sorted, as datetimes.

    This is what lets the retention curve carry a poll marker without anyone
    keeping a manual log. The reference app marks the moment a moderator SAYS
    they circulated the poll; this marks the moment the room actually started
    answering it, which is the better measurement and needs no new data entry.

    Measured 2026-09-07: 1,105 of 1,113 cached poll files carry the column.
    Returns [] for the other 8 rather than guessing.
    """
    from datetime import datetime
    rows = list(csv.reader((text or "").splitlines()))
    hdr = hidx = None
    for i, r in enumerate(rows[:14]):
        low = [str(c or "").strip().lower() for c in r]
        if any("submitted" in c for c in low):
            hdr, hidx = low, i
            break
    if hdr is None:
        return []
    j = next(i for i, c in enumerate(hdr) if "submitted" in c)
    out = []
    for r in rows[hidx + 1:]:
        if len(r) <= j:
            continue
        s = str(r[j] or "").strip().strip('"')
        if not s:
            continue
        for f in _SUBMIT_FMTS:
            try:
                out.append(datetime.strptime(s, f))
                break
            except ValueError:
                continue
    return sorted(out)


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


def _email(v) -> str:
    """A respondent's email as the roster side spells it, or ''.

    The same rule as `attendance_core._cell_email` — whitespace stripped,
    lower-cased, must contain '@' — so a poll respondent and a roster row that
    are the same person compare equal without either side being special-cased.
    """
    s = str(v or "")
    return re.sub(r"\s", "", s).lower() if "@" in s else ""


def _respondent_rows(rows) -> list | None:
    """The per-PERSON readers: [(email, {kind: [scores]})], one per respondent.

    Returns None when neither shape's header is present, so `parse` can fall
    through to the long `Question, Answer` form (which names nobody and cannot
    be read per person). Returns [] when a header was found but no valid rating
    followed it — the caller treats that the same way.

    Both readers keep every answer, not one per person: a respondent who
    answered the same question twice in the indexed export contributes both
    values to the histogram, exactly as the aggregate always has.
    """
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
        ui = next((i for i, c in enumerate(hdr) if "email" in c), None)
        people: dict = {}
        for r in rows[hdr_i + 1:]:
            if len(r) <= max(qi, ai) or not r[0].strip().isdigit():
                continue
            kind = _classify(r[qi])
            if kind and (v := _score(r[ai])) is not None:
                # identity: the email column when the export has one, else the
                # row's own '#', which the indexed form repeats per person
                who = (r[ui].strip().lower() if ui is not None and ui < len(r)
                       else r[0])
                p = people.setdefault(who, {k: [] for k in _KINDS})
                p[kind].append(v)
        if people:
            return [(_email(who), sc) for who, sc in people.items()]

    # ── wide form ────────────────────────────────────────────────────────────
    hdr_i = next((i for i, r in enumerate(rows)
                  if r and r[0].strip() == "#" and any("user name" in c.strip().lower()
                                                       for c in r)), None)
    if hdr_i is None:
        return None
    hdr = rows[hdr_i]
    cols = {i: k for i, c in enumerate(hdr)
            if (k := _classify(c)) and i >= 3}
    ei = next((i for i, c in enumerate(hdr) if "email" in str(c).lower()), None)
    out = []
    for r in rows[hdr_i + 1:]:
        if not r or not r[0].strip().isdigit():
            continue
        sc: dict = {k: [] for k in _KINDS}
        got = False
        for i, kind in cols.items():
            if i < len(r) and (v := _score(r[i])) is not None:
                sc[kind].append(v)
                got = True
        if got:
            out.append((_email(r[ei]) if ei is not None and ei < len(r) else "", sc))
    return out


def _aggregate(buckets: dict, respondents: int) -> dict:
    """{kind: [scores]} + a headcount -> the ratings dict every caller reads.

    One function for the whole poll AND for any subset of its respondents, so a
    batch's own slice of a shared poll is rounded, binned and NPS-scored by
    exactly the rule the joint figure was.
    """
    out = {k: (round(sum(v) / len(v), 2) if v else None) for k, v in buckets.items()}
    out["responses"] = respondents
    # The raw scores are in hand at this point whichever export shape was read,
    # so the histogram and the NPS cost nothing extra. Both ride the existing
    # {(batch, mm, pod): ratings} payload through lookup_by_session and into the
    # store, so no new plumbing is needed downstream.
    out["dist"] = {k: _distribution(v) for k, v in buckets.items()}
    out["nps"] = nps(buckets["recommend"])
    return out


def _buckets_of(responses) -> dict:
    buckets: dict[str, list] = {k: [] for k in _KINDS}
    for _who, sc in responses or ():
        for k in _KINDS:
            buckets[k].extend(sc.get(k) or ())
    return buckets


def parse(text: str) -> dict:
    """Poll CSV -> {'session': avg|None, 'trainer': …, 'recommend': …,
    'responses': int, 'dist': {kind: {'1'…'5': n}}, 'nps': int|None}.

    Averages are rounded to 1dp; `responses` is the number of people who gave at
    least one rating. `dist` is the full 1-5 histogram per kind — the mean alone
    cannot tell a room that was uniformly lukewarm from one that was half
    delighted and half furious, and those need different responses.
    """
    rows = list(csv.reader(io.StringIO(text)))
    resp = _respondent_rows(rows)
    if resp:
        return _aggregate(_buckets_of(resp), len(resp))

    # ── long form: Question, Answer ──────────────────────────────────────────
    buckets: dict[str, list] = {k: [] for k in _KINDS}
    respondents = 0
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
    return _aggregate(buckets, respondents)


def parse_responses(text: str) -> list:
    """Poll CSV -> [{'email': str, 'scores': {kind: [1-5, …]}}], one per person.

    Only the two export shapes that name their respondents can be read this
    way; the long `Question, Answer` form yields []. `email` is '' when the
    export has no email column or the cell is blank. This is what lets a poll
    for a webinar SEVERAL batches sat in be split back into each batch's own
    students (see `split_by_roster`) — the aggregate alone cannot be.

    Never memoised (derived_cache refuses it, rightly): it is PII, and the
    split depends on the roster, which is not a fact about this file.
    """
    resp = _respondent_rows(list(csv.reader(io.StringIO(text or ""))))
    return [{"email": e, "scores": sc} for e, sc in (resp or ())]


def aggregate_responses(responses) -> dict:
    """The output of `parse_responses` (or any subset of it) -> a ratings dict.

    Identity: `aggregate_responses(parse_responses(t))` equals `parse(t)` for
    the wide and indexed shapes, key for key.
    """
    pairs = [(r.get("email", ""), r.get("scores") or {}) for r in (responses or ())]
    return _aggregate(_buckets_of(pairs), len(pairs))


def split_by_roster(responses, rosters: dict) -> dict:
    """Divide one poll's respondents between the batches whose rosters hold them.

    `rosters` is {batch_label: set-of-emails}. Returns one ratings dict per
    batch (aggregated over ITS students only — an empty slice is a real result:
    responses 0, every average None) plus two counts that account for everyone
    who did not land in exactly one batch:

        _unmatched  respondents with no email, or an email on none of the rosters
        _multi      respondents on MORE than one roster — counted in each, because
                    a student enrolled twice did attend for both

    So `sum(part responses) + _unmatched - _multi == len(responses)`, which is
    the check the tests pin: nobody is dropped silently and nobody is invented.
    """
    parts = {b: [] for b in (rosters or {})}
    unmatched = multi = 0
    for r in responses or ():
        e = (r.get("email") or "").strip().lower()
        hits = [b for b, s in (rosters or {}).items() if e and e in s]
        if not hits:
            unmatched += 1
        elif len(hits) > 1:
            multi += 1
        for b in hits:
            parts[b].append(r)
    out = {b: aggregate_responses(v) for b, v in parts.items()}
    out["_unmatched"] = unmatched
    out["_multi"] = multi
    return out


def apply_pod_split(ratings: dict, texts_by_wid: dict, pod_emails: dict) -> tuple:
    """Attach a per-DOMAIN rating breakdown to every multi-domain room.

    A room with no pod of its own - an All Domains session, or the complement
    room everyone outside the day's PODs sits in - holds several domains at
    once, and its poll is one file. This divides that file by the roster's POD
    column, so Finance's rating is Finance's students' answers.

    `ratings` is `apply_roster_split`'s output; `texts_by_wid` maps a webinar id
    to its poll text; `pod_emails` is `data.roster_pod_emails`. Returns
    (ratings, stats). Only entries whose pod key is empty are touched - a named
    POD's own room is one domain by construction and needs no split.

    Each touched entry gains `pod_ratings`:

        {pod: {session, trainer, recommend, responses, dist, nps}, ...}
        _unmatched  respondents on no roster row of this batch
        _multi      respondents the roster lists under more than one pod

    Never raises for one bad entry: a poll it cannot divide simply gains
    nothing, and the room keeps the single figure it already had.
    """
    stats = {"rooms": 0, "split": 0, "kept": {}}
    per_wid: dict = {}
    for key, rt in (ratings or {}).items():
        if key[2]:
            continue                      # a named POD's room: one domain already
        stats["rooms"] += 1
        label = key[0]
        reason = "error"
        try:
            wid = rt.get("_wid")
            text = (texts_by_wid or {}).get(wid)
            pods_for_batch = (pod_emails or {}).get(label)
            if text is None:
                reason = "no-bytes"
            elif not pods_for_batch:
                reason = "no-pods"         # B17-B34: the pod era starts at B35
            else:
                ck = (wid, label)
                if ck not in per_wid:
                    resp = parse_responses(text)
                    per_wid[ck] = (split_by_pod(resp, pods_for_batch)
                                   if any(r.get("email") for r in resp) else None)
                parts = per_wid[ck]
                if parts is None:
                    reason = "no-emails"   # an anonymous poll names nobody
                else:
                    rt["pod_ratings"] = parts
                    stats["split"] += 1
                    continue
        except Exception:
            reason = "error"
        stats["kept"][reason] = stats["kept"].get(reason, 0) + 1
    return ratings, stats


def split_by_pod(responses, pod_emails: dict) -> dict:
    """Divide one poll's respondents between the PODs its roster puts them in.

    `pod_emails` is {pod name: set-of-emails} for ONE batch. Returns the same
    shape `split_by_roster` does - one ratings dict per pod plus `_unmatched`
    and `_multi` - because it IS `split_by_roster`: that function groups by
    whatever keys it is handed, and a POD is just a narrower grouping than a
    batch.

    Why membership and not the label: the room's rating used to be found by
    the pod name L2 wrote, which meant a room spelled "Common", "Common ,
    BSIAI Accelerator B1" or left blank each landed somewhere different, and a
    complement room's rating was lost entirely. A student's POD comes from the
    roster, so nothing here depends on how the schedule spells a room.

    A Techie who sat in the Common session therefore has their answer counted
    under Techies, which is the same rule the marker applies to attendance.
    """
    return split_by_roster(responses, pod_emails)


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


def parse_one(blob) -> dict | None:
    """ONE poll export's bytes -> its ratings dict, or None if it has none.

    Pure in this file's bytes: no roster, no L2, no other file. That is what
    makes it safe to memoise (see derived_cache.py) — and note it deliberately
    returns nothing name-derived, because the webinar id and date come from the
    FILENAME and must be re-read from the live listing every run.
    """
    text = (blob.decode("utf-8-sig", errors="replace")
            if isinstance(blob, bytes) else blob)
    try:
        got = parse(text)
    except Exception:
        return None
    if not any(got[k] is not None for k in _KINDS):
        return None
    # When the room started answering. Rides the same payload as the
    # ratings, so it reaches a session without any new plumbing.
    try:
        ts = submission_times(text)
        got["submitted_first"] = ts[0].isoformat() if ts else None
    except Exception:
        got["submitted_first"] = None
    return got


def dedupe_rows(rows) -> dict:
    """[(name, ratings), …] -> {webinar_id: ratings}.

    Duplicated exports of one webinar are collapsed by keeping the copy with the
    most responses - the same rule the attendee reports use, and for the same
    reason: the drives hold overlapping copies that differ by a row or two.

    The tie-break is `>=` on the INCUMBENT, so an exact tie keeps the copy seen
    first and the result depends on the order `rows` arrives in. Callers must
    therefore pass rows in listing order whether each one was freshly parsed or
    served from a memo — mixing the two orders would silently pick a different
    copy from one week to the next.
    """
    best: dict[str, dict] = {}
    for name, got in rows or ():
        key = name_key(name)
        if not key or not got:
            continue
        wid = key[0]
        prev = best.get(wid)
        if prev and prev["responses"] >= got["responses"]:
            continue
        best[wid] = got
    return best


def parse_files(poll_files) -> dict:
    """[(name, bytes), …] -> {webinar_id: ratings}."""
    return dedupe_rows([(name, parse_one(blob))
                        for name, blob in (poll_files or ())])


def lookup_by_session(poll_files, l2_bytes) -> tuple[dict, dict]:
    """[(name, bytes)] -> ({(batch, mm_dd, pod): ratings}, {webinar_id: ratings})."""
    return lookup_by_session_rows(
        [(name, parse_one(blob)) for name, blob in (poll_files or ())], l2_bytes)


def lookup_by_session_rows(rows, l2_bytes) -> tuple[dict, dict]:
    """-> ({(batch, mm_dd, pod): ratings}, {webinar_id: ratings}).

    Takes rows that are ALREADY parsed — [(name, ratings)] — so a caller can mix
    freshly parsed files with memoised facts (derived_cache.py) and get exactly
    the same answer, as long as it keeps them in listing order.

    Keyed exactly like the topic lookup, so a dashboard session finds its own
    poll. The by-webinar-id map is returned alongside it so the caller can
    recover WHICH parsed copy won a duplicate tie-break by identity - see
    pipeline's shared-poll split. (sessionmeta builds its own, separately.)
    The join is Webinar ID - the same key the marker and the topic lookup use -
    so a rating can never drift onto the wrong session.

    Note the (wid, mm_dd) pair is read from the NAME on every call, never from
    the ratings dict. That is deliberate: a mis-dated export renamed on Drive
    must move to its corrected date immediately, even though its bytes — and so
    any memoised fact about them — are unchanged.
    """
    import attendance_core as ac
    import pods as _pods

    by_wid = dedupe_rows(rows)
    if not by_wid or not l2_bytes:
        return {}, by_wid

    # webinar -> mm_dd, in one pass over the filenames
    mm_of: dict[str, str] = {}
    for name, _ in rows or ():
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
        # Every batch L2 says sat in this webinar, in one fixed order. Each
        # batch's entry is its OWN dict carrying `_wid` and `_batches`, so
        # `apply_roster_split` can later find the poll's bytes and know which
        # rosters to divide it between. The shared object used to be stored
        # under every key, which is how one poll became three identical ratings.
        labels = [batch_label(t, n) for t, n in sorted(keys)]
        for label in labels:
            prev = out.get((label, mm, pod))
            if prev and prev["responses"] >= rating["responses"]:
                continue
            out[(label, mm, pod)] = dict(rating, _wid=wid, _batches=labels)
    return out, by_wid


def batch_label(track, num) -> str:
    """An L2 (track, number) key as the dashboard names the batch: 'B35',
    'BSIAI B3'. One spelling, shared with data.py's `shared_batches`."""
    return f"B{num}" if track == "CAP" else f"{track} B{num}"


# The fields that describe the poll as a whole and survive onto a batch row's
# `rating_shared.joint`. Aggregates only — never a respondent.
_JOINT_KEYS = ("session", "trainer", "recommend", "responses", "dist", "nps")


def apply_roster_split(ratings: dict, texts_by_wid: dict, rosters: dict) -> tuple:
    """Give each batch in a SHARED webinar its own students' rating.

    `ratings` is `lookup_by_session_rows`' first result; `texts_by_wid` maps a
    webinar id to its poll export's text; `rosters` is {batch_label: emails}.
    Returns (new ratings dict, stats). Pure and never raises for one bad entry:
    a poll this cannot split keeps the joint figure, labelled as such.

    An entry whose `_batches` names one batch is untouched. For every other
    entry the batch's values are REPLACED by the aggregate over the respondents
    found on its roster, and a `shared` block is attached:

        batches    every batch L2 puts in the webinar
        joint      the whole poll's figures (what the trainer is judged on)
        split      True when the division happened
        unmatched  respondents on none of the sharing rosters (excluded from
                   every batch's own figure; still inside `joint`)
        multi      respondents on more than one roster (counted in each)
        reason     when split is False: 'no-bytes' (poll not fetched),
                   'no-emails' (a long-form export, or an ANONYMOUS poll -
                   nobody in it carries an email), 'no-roster' (this batch has
                   no roster tab), 'error'

    The split is computed once per webinar and reused for each of its batches,
    so three batches cost one parse.
    """
    stats = {"shared": 0, "split": 0, "kept": {}}
    out: dict = {}
    per_wid: dict = {}
    for key, rt in (ratings or {}).items():
        batches = list(rt.get("_batches") or ())
        if len(batches) < 2:
            out[key] = rt
            continue
        stats["shared"] += 1
        label = key[0]
        shared = {"batches": batches,
                  "joint": {k: rt.get(k) for k in _JOINT_KEYS},
                  "split": False}
        reason = "error"
        try:
            wid = rt.get("_wid")
            text = (texts_by_wid or {}).get(wid)
            if text is None:
                reason = "no-bytes"
            elif label not in (rosters or {}):
                reason = "no-roster"
            else:
                if wid not in per_wid:
                    resp = parse_responses(text)
                    # A poll run ANONYMOUSLY exports 'anonymous' (or nothing) in
                    # every email cell - B35/B36/B37's Finance poll of 6 Sep, 220
                    # answers, not one identity. Splitting it would hand every
                    # batch a blank and call all 220 "unmatched"; the truthful
                    # result is the joint figure, labelled as such.
                    per_wid[wid] = (split_by_roster(
                        resp, {b: rosters[b] for b in batches if b in rosters})
                        if any(r.get("email") for r in resp) else None)
                parts = per_wid[wid]
                if parts is None:
                    reason = "no-emails"
                else:
                    new = dict(rt)
                    new.update(parts[label])
                    new["shared"] = dict(shared, split=True,
                                         unmatched=parts["_unmatched"],
                                         multi=parts["_multi"])
                    out[key] = new
                    stats["split"] += 1
                    continue
        except Exception:
            reason = "error"
        out[key] = dict(rt, shared=dict(shared, reason=reason))
        stats["kept"][reason] = stats["kept"].get(reason, 0) + 1
    return out, stats
