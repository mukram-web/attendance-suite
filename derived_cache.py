"""
derived_cache.py — memoise the PER-FILE facts, so Monday only parses what is new.

WHAT THIS IS FOR
----------------
Every Monday the pipeline re-downloaded and re-parsed the entire Zoom corpus —
1,290 attendee reports and 1,235 poll exports, ~246 MB — to recompute facts that
had not changed since the week they were first computed. Measured on the real
corpus with a warm disk, the PARSING alone (never mind the download) is:

    sessionmeta.measure ......... 215.4s   (~1.76M join/leave rows swept)
    sessionmeta.parse_header ..... 16.1s
    polls.submission_times ....... 22.1s
    polls.parse .................. 12.9s
    ---------------------------------------
    disk read for all of it ....... 1.4s

So caching the BYTES would miss the point; the cost is the parse. This module
memoises the derived FACT instead, keyed on the file's content identity.

WHAT IS MEMOISED, AND WHERE THE LINE IS
---------------------------------------
Only the output of functions that are pure in ONE file's bytes:

    sessionmeta.parse_header + measure   ->  duration, peak, retention curve,
                                             stickiness, t0, unique viewers …
    polls.parse + submission_times       ->  the 1-5 histograms, means, NPS,
                                             responses, first submission time

Nothing that touches the roster, L2, or any other file is memoised — not marks,
not columns, not denominators, not DATA, not the store. Every L2 join, every
denominator and every whole-history fit is recomputed every single run. That is
not a compromise: measured end to end on the live 23-batch DATA, the whole model
layer (forecast.fit_curve + its backtest + recap + trainers) costs **0.18
seconds**. There is nothing to save there and a great deal to lose — see
CLAUDE.md §4f. The seven minutes were all I/O and parsing.

THE CHANGE DETECTOR IS THE LISTING, NOT THIS CACHE
--------------------------------------------------
The whole-drive files.list in scan_all_attendees and fetch_polls still runs
every week and still enumerates everything. A file added, deleted, moved or
replaced is therefore still SEEN. What a cache hit skips is only the download
and parse of a file whose exact bytes have already been parsed. This is why the
"refuse to publish partial data" gate is not weakened: it never asks "which
files did I choose to look at", and neither does this.

FOUR WAYS THIS COULD GO SILENTLY WRONG, AND WHAT STOPS EACH
-----------------------------------------------------------
1. **The key must cover every input.** `id` alone is not enough. Drive's
   "Manage versions → Upload new version" gives the same id new bytes, and that
   is the documented human remedy for a bad Zoom export (CLAUDE.md §7b.1).
   live_data's byte cache keys on the bare id on the strength of a *comment*
   ("attendee files never change once uploaded") — a claim, not a check. So the
   key here carries content identity AND the name: `id:md5-or-mtime:name`.
2. **Never memoise a fact derived from the file NAME.** `sessionmeta` reaches a
   session through `_mm`, parsed out of the filename, and polls through
   `(wid, mm)`. Those are recomputed from the live listing on every run and are
   refused entry here (`_FORBIDDEN`). Storing `_mm` would let a renamed
   mis-dated export keep its old date forever; dropping it silently would make
   `lookup_by_session` discard every hit and blank the whole column set.
3. **Never memoise a fact derived from bytes this run did not verify.** The
   local byte cache is read before any network call, so a stale local copy plus
   a fresh md5 from the listing would stamp old facts as current — and no future
   run on any machine could ever invalidate them. Callers therefore pass
   `verified=` and only verified rows are stored. `live_data` now also
   signature-addresses its byte cache, so a replaced file misses the disk cache
   exactly as it misses this one.
4. **The rule that produced the fact is part of the fact.** Two changes in the
   last month would each have poisoned a cache silently: the last-10-digit phone
   fix (+8.4% on every re-marked session) and the retention-curve one-minute trim
   (−5.4 points on stick10 across the whole corpus). So each namespace stores a
   hash of its producing module's SOURCE and is discarded whole when that
   changes. The whole module, never one function: `measure` depends on `_ts`,
   `_TIME_FMTS` and `MAX_MINUTES`. A comment-only edit costs one slow run, which
   is the right price. It fails CLOSED — an unreadable source yields a value
   that cannot match anything.

Belt and braces on top of that: every entry carries `first_seen` (never
rewritten) and is dropped after CACHE_MAX_AGE_DAYS, so ~8% of the corpus is
re-derived from scratch every week on a rolling basis and no cached number can
survive a quarter unchecked. Nobody has to remember to run anything — this repo
already has `--allow-partial` as proof that a manual escape hatch is an
untested one.

WHERE IT LIVES, AND WHY NOT ANYWHERE ELSE
-----------------------------------------
One file, `derived_facts.json.gz` (~0.6 MB gzipped), in the store folder root on
the private Shared Drive, beside attendance.duckdb, written with the existing
`live_data.upload_to_folder`. No new secret, no new folder id.

  - NOT actions/cache. The payload is aggregates-only — validated on the way in,
    see `_validate` — so §6's PII rule is not even engaged. It stays off Actions
    anyway because refresh.yml's rule is BLANKET, and a blanket rule is what
    survives the next contributor who adds one more path to a cache step that
    already exists. Never create the precedent.
  - NOT the repo. It is public.
  - NOT archive/. `archive._put` existence-checks the name and SKIPS it — that
    is what makes snapshots immutable. A mutable file written through that path
    would be uploaded once and then never updated again, while the log printed a
    healthy-looking "N already archived". The most seductive wrong answer here.
  - NOT inside attendance.duckdb. The app json.loads every meta key on every
    load, and the store is archived verbatim forever with nothing pruned.
  - NOT My Drive. Service accounts have zero Drive storage (§5.8).

The app cannot corrupt it: the app's Drive credentials are read-only.

ALWAYS DISCARDABLE
------------------
Deleting the file, `--no-cache`, a schema bump, a rule-hash mismatch and any
read error whatsoever all reach a full cold run, which costs ~6 minutes against
the workflow's 30-minute timeout. Nothing here is a source of truth; it is a
memo of arithmetic anyone can redo.
"""
from __future__ import annotations

import gzip
import hashlib
import inspect
import json

CACHE_NAME = "derived_facts.json.gz"
GZIP_MIME = "application/gzip"

# Bump to discard every namespace at once. Only needed for a change to the
# envelope itself; a change to what a namespace COMPUTES is caught by its
# rule hash without anyone touching this.
SCHEMA = 2

# A rolling re-derivation, so nothing can sit unchecked for a quarter. At ~2,500
# files this recomputes roughly 200 a week (~12s) for free.
CACHE_MAX_AGE_DAYS = 90

NAMESPACES = ("sessionmeta", "polls")

# Fields that are a function of the FILENAME, not the bytes. Refused entry so a
# rename cannot be served from cache and so a future re-add fails loudly instead
# of silently blanking every session. See failure mode 2 in the docstring.
_FORBIDDEN = frozenset({"_mm", "_name", "name", "mm", "wid"})


def rule_version(module) -> str:
    """A digest of the module's own SOURCE — the rule that produced the facts.

    Fails CLOSED: an unreadable source returns a value that cannot equal any
    stored hash, so the namespace is discarded and everything is recomputed.
    Silently matching would be the one unrecoverable outcome.
    """
    try:
        src = inspect.getsource(module)
    except Exception:
        return "unreadable-source-never-matches"
    return hashlib.sha256(src.encode("utf-8", "replace")).hexdigest()[:16]


def key_for(f: dict) -> str | None:
    """A Drive listing row -> the cache key, or None to force a miss.

    `id:signature:name`. The signature is md5Checksum when Drive reports one and
    modifiedTime otherwise (Google-native files carry no md5). Neither present
    means we cannot tell whether the bytes changed, so there is no key and the
    file is always re-parsed: missing metadata costs time, never correctness.

    The NAME is in the key on purpose. It is rename-sensitive because a rename
    IS a semantic change here — the session date is parsed out of the filename,
    and re-dating a mis-named export is a normal human fix.
    """
    fid = (f or {}).get("id")
    if not fid:
        return None
    sig = (f.get("md5Checksum") or f.get("modifiedTime") or "").strip()
    if not sig:
        return None
    return f"{fid}:{sig}:{f.get('name') or ''}"


def _scalar(v) -> bool:
    return v is None or isinstance(v, (int, float, str, bool))


def _validate(row) -> bool:
    """True when `row` is a plausible aggregate fact and nothing else.

    This is the PII control and the poisoned-value control in one, and it checks
    VALUES, not just key names — a name allow-list would happily pass
    {"topic": "<every attendee's email>"}. Anything unexpected is treated as a
    miss rather than trusted, because this file is a mutable input to published
    numbers.
    """
    if not isinstance(row, dict) or not row:
        return False
    if _FORBIDDEN & set(row):
        return False
    for k, v in row.items():
        if not isinstance(k, str):
            return False
        if k == "curve":
            # the per-minute retention series
            if v is None:
                continue
            if (not isinstance(v, list)
                    or len(v) > 400          # sessionmeta.MAX_MINUTES + slack
                    or not all(isinstance(x, int) and 0 <= x <= 100_000
                               for x in v)):
                return False
        elif k == "dist":
            # {kind: {"1".."5": count}}
            if not isinstance(v, dict):
                return False
            for _kind, h in v.items():
                if not isinstance(h, dict) or len(h) > 8:
                    return False
                for bucket, n in h.items():
                    if (str(bucket) not in "12345"
                            or not isinstance(n, int) or n < 0):
                        return False
        elif isinstance(v, str):
            # A session title or a trainer name is fine; a dumped roster is not.
            if len(v) > 300:
                return False
        elif not _scalar(v):
            return False
    return True


def stage(cache: dict, ns: str, key: str | None, row: dict, *,
          verified: bool, today: str) -> bool:
    """Put one freshly computed fact into `cache`. True if it was stored.

    `verified` must be True only when the bytes came from a source this run
    actually confirmed — see failure mode 3. An unverified row is used for THIS
    run's output and simply not remembered.
    """
    if not key or not verified or ns not in NAMESPACES:
        return False
    # REFUSED, not quietly stripped. Stripping is the trap: the row would store
    # cleanly and every later cache hit would come back without `_mm`, which
    # lookup_by_session drops on the floor -- blanking duration, peak, retention
    # and stickiness on every session while the run stayed green. Refusing makes
    # the caller keep name-derived fields out of the memoised dict and attach
    # them from the live listing instead, which is the only correct shape.
    if not _validate(row):
        return False
    prev = (cache.get(ns) or {}).get(key) or {}
    cache.setdefault(ns, {})[key] = {
        # first_seen is never rewritten, so the rolling expiry measures the age
        # of the FACT and not the age of the last file rewrite.
        "first_seen": prev.get("first_seen") or today,
        "f": dict(row),
    }
    return True


def get(cache: dict, ns: str, key: str | None) -> dict | None:
    """A memoised fact, or None. Re-validated on the way out."""
    if not key:
        return None
    e = (cache.get(ns) or {}).get(key)
    if not isinstance(e, dict):
        return None
    row = e.get("f")
    return dict(row) if _validate(row) else None


def _fresh(entry, cutoff: str) -> bool:
    fs = (entry or {}).get("first_seen") or ""
    return bool(fs) and fs >= cutoff


def loads(blob: bytes, rules: dict, cutoff: str) -> tuple[dict, dict]:
    """Bytes -> (cache, stats). Never raises; any problem yields an empty cache.

    `rules` is {namespace: current rule_version}. A namespace whose stored hash
    differs is discarded WHOLE — never merged, because two rules inside one
    store with nothing saying which row used which is the failure this exists to
    prevent.
    """
    stats = {"loaded": 0, "discarded": 0, "expired": 0, "invalid": 0,
             "namespaces": {}}
    try:
        raw = json.loads(gzip.decompress(blob).decode("utf-8"))
    except Exception:
        return {}, stats
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        stats["discarded"] = -1          # -1: the whole envelope, not N rows
        return {}, stats

    out: dict = {}
    for ns in NAMESPACES:
        blk = raw.get(ns)
        if not isinstance(blk, dict):
            continue
        if blk.get("rule") != rules.get(ns):
            stats["namespaces"][ns] = "rule changed - discarded"
            stats["discarded"] += len(blk.get("rows") or {})
            continue
        keep = {}
        for k, e in (blk.get("rows") or {}).items():
            if not _fresh(e, cutoff):
                stats["expired"] += 1
            elif get({ns: {k: e}}, ns, k) is None:
                stats["invalid"] += 1
            else:
                keep[k] = e
        out[ns] = keep
        stats["loaded"] += len(keep)
        stats["namespaces"][ns] = f"{len(keep)} row(s)"
    return out, stats


def dumps(cache: dict, rules: dict) -> bytes:
    """(cache, rules) -> the gzipped bytes to upload.

    Validates every row on the way out and RAISES on anything unexpected. This
    runs at stage time too, but a second check here is cheap and this is the
    only point where the data leaves the process.
    """
    payload = {"schema": SCHEMA}
    for ns in NAMESPACES:
        rows = cache.get(ns) or {}
        for k, e in rows.items():
            if not _validate((e or {}).get("f")):
                raise ValueError(f"{ns}: refusing to write an unexpected row shape")
        payload[ns] = {"rule": rules.get(ns), "rows": rows}
    return gzip.compress(json.dumps(payload, separators=(",", ":"),
                                    sort_keys=True).encode("utf-8"), 6)


def prune_to(cache: dict, ns: str, live_keys) -> int:
    """Drop memo entries for files no longer on the drive. Returns how many.

    Memo hygiene, not archive pruning (C3): this file is a scratch memo of
    arithmetic, and keeping it the size of the live corpus is what stops it
    growing without bound. Nothing recoverable is lost — every dropped entry can
    be recomputed from the file it came from, if that file still exists.
    """
    rows = cache.get(ns)
    if not rows:
        return 0
    live = set(live_keys)
    dead = [k for k in rows if k not in live]
    for k in dead:
        del rows[k]
    return len(dead)


def load_file(path: str, rules: dict, cutoff: str) -> tuple[dict, dict]:
    """Same as load(), from a local file. See save_file for why this exists."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return {}, {"loaded": 0, "note": "no local cache yet"}
    cache, stats = loads(blob, rules, cutoff)
    stats["bytes"] = len(blob)
    return cache, stats


def save_file(path: str, cache: dict, rules: dict) -> int:
    """Write the memo to a local file.

    This is what makes the WARM path testable. Without it the only writer is the
    weekly publish, so the first time a cached value is ever consumed would also
    be the first time it reaches the dashboard — and a cold-vs-warm store diff
    would silently compare two cold runs and pass, proving only that the
    pipeline is deterministic.
    """
    blob = dumps(cache, rules)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    import os as _os
    _os.replace(tmp, path)
    return len(blob)


def load(svc, store_folder_id: str, rules: dict, cutoff: str) -> tuple[dict, dict]:
    """Download and parse the memo. ({}, stats) on any problem at all."""
    import live_data
    if not store_folder_id:
        return {}, {"loaded": 0, "note": "no store folder - cache disabled"}
    try:
        meta = live_data.find_in_folder(svc, store_folder_id, CACHE_NAME)
        if not meta:
            return {}, {"loaded": 0, "note": "no cache yet"}
        blob = live_data.fetch_store_snapshot(meta["id"])
        cache, stats = loads(blob, rules, cutoff)
        stats["bytes"] = len(blob)
        return cache, stats
    except Exception as e:
        return {}, {"loaded": 0, "note": f"unreadable ({type(e).__name__})"}


def save(svc, store_folder_id: str, cache: dict, rules: dict) -> int:
    """Upload the memo. Returns bytes written; raises on a validation failure.

    The CALLER decides whether an upload failure is fatal — it should not be,
    a memo that failed to save costs next week a slow run and nothing else —
    but a row that fails validation must raise, because that means the shape of
    what we are about to publish is not what we think it is.
    """
    import live_data
    blob = dumps(cache, rules)
    live_data.upload_to_folder(svc, store_folder_id, CACHE_NAME, blob, GZIP_MIME)
    return len(blob)
