"""
pipeline.py — the weekly heavy job, run OFF the Streamlit server.

Fetches the roster / L2 / new Zoom attendee reports from Google Drive, marks
attendance, builds every artefact the dashboard renders, packs them into ONE
small DuckDB file, and uploads it (plus the marked .xlsx) to a private Drive
folder. The deployed app then just downloads that file and renders — no
fetching, no marking, no crashes on Streamlit Cloud's small instance.

Runs on GitHub Actions every Monday 06:00 IST (see .github/workflows/refresh.yml)
or manually:

    python pipeline.py                # full run: fetch → mark → build → upload
    python pipeline.py --no-upload    # build .cache/attendance.duckdb only

Credentials & config resolve in this order:
  1. env vars  GDRIVE_SERVICE_ACCOUNT_JSON (the key JSON, one line)
               ROSTER_ID / L2_ID / ATTENDEE_FOLDER_ID / STORE_FOLDER_ID
  2. local fallback: .streamlit/secrets.toml + the service-account .json key
     file sitting next to this script (developer machine).

The store schema (attendance.duckdb):
  meta(key, value)       — JSON blobs: DATA, summary, report, warnings, source,
                           generated_at, batches, sheet_map, marked_xlsx_file_id,
                           forecast (predicted attendance for sessions that
                           have not run yet — aggregates only)
  compute                — dashboard_core.compute() table (per batch × session)
  grid_<batch>           — roster_grid() per batch (the Roster tab, incl. PII —
                           the store must stay in a PRIVATE Drive folder)
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import duckdb
from openpyxl import load_workbook

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import attendance_core as ac          # noqa: E402
import carryforward                   # noqa: E402
import dashboard_core as dc           # noqa: E402
import data as ddata                  # noqa: E402
import derived_cache                  # noqa: E402
import ecap                           # noqa: E402
import polls                          # noqa: E402
import forecast                       # noqa: E402
import archive                        # noqa: E402
import recap                          # noqa: E402
import trainers                       # noqa: E402
import sessionmeta                    # noqa: E402
import sheets as dsheets              # noqa: E402
import live_data                      # noqa: E402
import lms_client                     # noqa: E402
import lms_roster                     # noqa: E402

# How far ahead the forecast runs. Eight weeks is where the backtest still holds
# under ~13% MAPE and the curriculum sheet is actually filled in; past that the
# schedule thins out and the error climbs, so forecasting further would be
# confident-looking noise.
FORECAST_WEEKS = 8

IST = timezone(timedelta(hours=5, minutes=30))
STORE_NAME = "attendance.duckdb"
PREV_STORE = "_prev_store.duckdb"
MARKED_NAME = "Master_Batch_Rosters_marked.xlsx"


def parallel_store_name(store_out: str, no_upload: bool,
                        publish_parallel: bool) -> str:
    """The Drive name a PARALLEL run publishes under, or "" for a normal run.

    STORE_OUT builds a second dataset beside the real one. Whether that is safe
    depends entirely on where it gets UPLOADED, so the decision is made here,
    once, rather than inferred at three separate call sites.

    Raises SystemExit on the two combinations that would corrupt the weekly
    record:

    * a parallel store with a plain upload — [8/8] and [8b] both name the file
      by STORE_NAME, so it would publish itself as the real dashboard and, since
      archive snapshots are immutable and never pruned (§4d), freeze that week's
      history as the parallel dataset;
    * --publish-parallel with no STORE_OUT — that sends the REAL store down the
      parallel path, which deliberately skips [8a] and [8b], so the dashboard
      would move while next week's base and the archive silently did not.
    """
    parallel = bool(store_out) and store_out != STORE_NAME
    if publish_parallel and not parallel:
        raise SystemExit(
            f"--publish-parallel needs STORE_OUT set to a name other than "
            f"{STORE_NAME}; there is no parallel store to publish otherwise.")
    if parallel and not no_upload and not publish_parallel:
        raise SystemExit(
            f"STORE_OUT={store_out} builds a PARALLEL store, but the upload "
            f"publishes under {STORE_NAME} and would replace the real "
            f"dashboard. Pass --no-upload to keep it local, "
            f"--publish-parallel to publish it under its own name as a second "
            f"data set, or unset STORE_OUT to publish normally.")
    return store_out if parallel else ""
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RW_SCOPES = ["https://www.googleapis.com/auth/drive"]


# ─────────────────────────── config & credentials ────────────────────────────
def _secrets_toml() -> dict:
    """Minimal read of .streamlit/secrets.toml for local runs (no streamlit)."""
    path = os.path.join(HERE, ".streamlit", "secrets.toml")
    if not os.path.exists(path):
        return {}
    try:
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def load_config() -> dict:
    toml = _secrets_toml()
    drive = toml.get("drive", {})

    def pick(env_key, toml_key):
        return os.environ.get(env_key) or drive.get(toml_key) or ""

    cfg = {
        "roster_id": pick("ROSTER_ID", "roster_id"),
        "l2_id": pick("L2_ID", "l2_id"),
        "attendee_folder_id": pick("ATTENDEE_FOLDER_ID", "attendee_folder_id"),
        "store_folder_id": pick("STORE_FOLDER_ID", "store_folder_id"),
        # Optional: the Master Curriculum Schedule Sheet — what is PLANNED, one
        # tab per pod. Absent -> no forecast section and the tab says so. Note
        # this Sheet is owned outside the team that owns the roster, so sharing
        # it with the service account is a separate step that is easy to forget.
        "curriculum_id": pick("CURRICULUM_ID", "curriculum_id"),
        # Optional: a workbook of "AI ECAP B<n>" roster tabs, grafted onto
        # the CAP roster before marking (see ecap.py). Absent -> no ECAP
        # batches, and everything else behaves exactly as before.
        "ecap_roster_id": pick("ECAP_ROSTER_ID", "ecap_roster_id"),
        # Where the roster comes from: "sheet" (Google Sheet, the original) or
        # "lms" (the 10xstats API — see lms_roster.py). Defaults to "sheet" so
        # the swap is opt-in and reverting is one environment variable, not a
        # deploy. `roster_id` stays configured either way: it costs nothing, it
        # keeps the archive's snapshot of the owner-maintained Sheet running,
        # and it is the rollback.
        "roster_source": (pick("ROSTER_SOURCE", "roster_source") or "sheet").lower(),
        "lms_api_key": pick("LMS_API_KEY", "lms_api_key"),
        # Optional override for which cohorts are re-fetched. Default: the two
        # highest batch numbers, i.e. the current cohort and the previous one.
        "lms_live_batches": pick("LMS_LIVE_BATCHES", "lms_live_batches"),
        # Optional local cache of /customers payloads. Empty in the weekly job —
        # it must see live enrolment. Set it when BUILDING A PARALLEL DATASET or
        # iterating locally, where re-fetching ~84 batch records at a minute each
        # is the difference between a two-hour loop and a two-minute one.
        "lms_cache_dir": pick("LMS_CACHE_DIR", "lms_cache_dir"),
        # "pure": build the roster from the API ALONE, with no retention layer.
        # The normal path keeps every student already in the marked workbook —
        # that is what stops the 9,017 people the API has never heard of losing
        # their history (§4h). Pure mode deliberately drops them, so it is ONLY
        # for a side-by-side dataset written to its own store, never for the
        # workbook that becomes next week's base.
        "lms_pure": (pick("LMS_PURE", "lms_pure") or "").strip().lower()
                    in ("1", "true", "yes", "on"),
    }

    env_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if env_json:
        cfg["sa_info"] = json.loads(env_json)
    elif toml.get("gcp_service_account"):
        cfg["sa_info"] = dict(toml["gcp_service_account"])
    else:
        # Match on the FILE NAME, never the full path — a checkout under e.g.
        # .../customer-service/ would otherwise match every json in the folder
        # and hand a data file to the credential parser.
        keys = [p for p in glob.glob(os.path.join(HERE, "*.json"))
                if _looks_like_sa_key(p)
                or "service" in os.path.basename(p).lower()
                or "fresh-delight" in os.path.basename(p).lower()]
        if not keys:
            raise SystemExit("No service-account credentials: set "
                             "GDRIVE_SERVICE_ACCOUNT_JSON or add the key file.")
        with open(keys[0], encoding="utf-8") as fh:
            cfg["sa_info"] = json.load(fh)

    if cfg["roster_source"] not in ("sheet", "lms"):
        raise SystemExit(f"roster_source must be 'sheet' or 'lms', "
                         f"not {cfg['roster_source']!r}")
    # With the LMS source the Sheet id is no longer needed to BUILD the roster,
    # but it is still worth having: archive.py keeps snapshotting the
    # owner-maintained Sheet, and it is the one-variable rollback.
    need = ["attendee_folder_id"] + (["roster_id"] if cfg["roster_source"] == "sheet"
                                     else [])
    missing = [k for k in need if not cfg[k]]
    if missing:
        raise SystemExit(f"Missing config: {', '.join(missing)} "
                         "(env vars or .streamlit/secrets.toml [drive])")
    if cfg["roster_source"] == "lms" and not cfg["roster_id"]:
        print("NOTE: roster_source=lms and no roster_id — the Sheet will not be "
              "archived and there is no one-variable rollback.", flush=True)
    return cfg


def _looks_like_sa_key(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(2048)
        return '"type": "service_account"' in head
    except OSError:
        return False


# How far session coverage may fall week on week before the run refuses to
# publish. Sessions do not vanish from the past: the corpus only grows, so any
# real drop is a bug upstream. 0.8 leaves room for a handful of Zoom exports
# going missing or an L2 row being corrected, which do happen.
COVERAGE_FLOOR = 0.8


def _cache_meta(cache_stats: dict, cache_rules: dict) -> dict:
    """The memo's counters, shaped for the store's meta blob.

    The live key SETS are dropped here — they are working state for [8c]'s
    hygiene pass, they are Drive file ids, and the store is archived forever.
    What stays is counts plus the rule hash each namespace ran under, so a week
    built on a rule later found to be wrong can be identified without keeping
    anything identifying.
    """
    return {ns: {**{k: v for k, v in (cache_stats.get(ns) or {}).items()
                    if k != "keys"},
                 "rule": cache_rules.get(ns)}
            for ns in derived_cache.NAMESPACES if cache_stats.get(ns)}


def _write_cache(svc, cfg, cache, cache_rules, cache_stats, args) -> None:
    """Upload the derived-facts memo. Loud on failure, but never fatal.

    A memo that failed to save costs next week a slow run and nothing else, so
    this must not take the publish down with it. A row of an unexpected SHAPE is
    different — derived_cache.dumps raises on that, because it means what we are
    about to write is not what we think it is — but even that is caught here and
    reported rather than thrown, since by this point the store is already built
    and (on a full run) about to be published from data the memo did not affect.

    Skipped after --allow-partial: that flag exists to publish a knowingly
    incomplete week, and a knowingly incomplete week must not leave a residue
    that a later, complete run would treat as authoritative.
    """
    if not cache or not (cfg["store_folder_id"] or args.cache_file):
        return
    if args.allow_partial:
        print("[8c] Derived-facts cache: not written (--allow-partial)", flush=True)
        return
    # Memo hygiene: drop entries for files no longer on the drive, so the file
    # stays the size of the live corpus. This is NOT archive pruning (§4d) —
    # every dropped row is recomputable from the file it came from.
    dropped = sum(derived_cache.prune_to(cache, ns, cache_stats.get(ns, {}).get("keys", ()))
                  for ns in derived_cache.NAMESPACES
                  if cache_stats.get(ns, {}).get("keys"))
    try:
        n = (derived_cache.save_file(args.cache_file, cache, cache_rules)
             if args.cache_file else
             derived_cache.save(svc, cfg["store_folder_id"], cache, cache_rules))
        print(f"[8c] Derived-facts cache: {sum(len(cache.get(ns) or {}) for ns in derived_cache.NAMESPACES):,}"
              f" row(s), {n / 1e6:.2f} MB"
              + (f", {dropped:,} stale dropped" if dropped else ""), flush=True)
    except Exception as e:
        # Counts and the exception TYPE only — a public repo's Actions logs are
        # world-readable and this pipeline has never printed a Drive id into one.
        print(f"   WARNING: derived-facts cache not written "
              f"({type(e).__name__}) — next week rebuilds it from the exports.",
              flush=True)


def _store_coverage(path: str) -> dict | None:
    """How many session rows in a store carry a duration and a rating."""
    try:
        con = duckdb.connect(path, read_only=True)
        raw = con.execute("SELECT value FROM meta WHERE key = 'sessions'").fetchone()
        con.close()
        rows = json.loads(raw[0]) if raw else []
        # A batch's own slice of a shared poll can be empty (none of ITS
        # students answered) while the poll itself is intact - that is a
        # session WITH a rating, held in rating_shared.joint, not a lost one.
        return {"duration": sum(1 for r in rows if r.get("duration_hrs")),
                "ratings": sum(1 for r in rows
                               if r.get("rating") is not None
                               or ((r.get("rating_shared") or {}).get("joint") or {})
                               .get("session") is not None)}
    except Exception:
        return None


def _grid_tables(con) -> list:
    return [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name LIKE 'grid_%'").fetchall()]


def _store_marks(path: str) -> dict:
    """{(batch, column): frozenset(emails marked Present)} from a store's grids.

    The yardstick for GATE 6, and it measures the MARKS rather than the
    published `present` count on purpose. Those are different things: `present`
    is recomputed every run over the students CURRENTLY in that batch and POD,
    so one student switching POD — or one row deleted, or one email cell blanked
    — lowers the published figure for every past session that student attended.
    Nobody touched the data, a full re-mark produces the identical lower number,
    and §4g lists that erosion as known and accepted. Gating on it would refuse
    to publish on an ordinary roster edit, blame the carry-forward, and train
    whoever is on duty to reach for --allow-partial, which switches off every
    other gate too.

    A cell in the workbook is exactly what the freeze owns. The comparison is
    therefore over the students in BOTH stores' rosters: an arrival or a
    departure cannot move it, and a carry-forward that mismatched identities
    can.
    """
    out = {}
    try:
        con = duckdb.connect(path, read_only=True)
        try:
            for t in _grid_tables(con):
                cols = [r[0] for r in con.execute(f'DESCRIBE "{t}"').fetchall()]
                if "Email" not in cols:
                    continue
                for c in [x for x in cols if x[:4].isdigit()]:
                    rows = con.execute(
                        f'SELECT "Email" FROM "{t}" '
                        f'WHERE lower(CAST("{c}" AS VARCHAR)) = ?', ["present"]
                    ).fetchall()
                    out[(t[5:], c)] = frozenset(
                        str(e[0]).strip().lower() for e in rows if e[0])
        finally:
            con.close()
    except Exception:
        return {}
    return out


def lost_marks(prev_marks: dict, now_marks: dict,
               prev_roster: dict, now_roster: dict) -> tuple:
    """GATE 6's arithmetic, pulled out so it can be tested without Drive.

    -> (lost, gone) where `lost` is [((batch, column), was, now, dropped)] for
    columns that lost a Present belonging to a student BOTH weeks' rosters
    contain, and `gone` is [(batch, column)] for columns that vanished.

    Restricting to the shared roster is the whole point: a student who left, or
    joined, or moved POD must not register as a loss, because none of those is
    the carry-forward failing — and a gate that cries wolf on a roster edit is a
    gate somebody switches off.
    """
    lost, gone = [], []
    for key, was in (prev_marks or {}).items():
        if key not in (now_marks or {}):
            gone.append(key)
            continue
        both = ((prev_roster or {}).get(key[0]) or frozenset()) & \
               ((now_roster or {}).get(key[0]) or frozenset())
        if not both:
            continue
        dropped = len((was & both) - now_marks[key])
        if dropped:
            lost.append((key, len(was & both), len(now_marks[key] & both), dropped))
    return lost, gone


def _store_roster(path: str) -> dict:
    """{batch: frozenset(emails)} — who each store's grids knew about."""
    out = {}
    try:
        con = duckdb.connect(path, read_only=True)
        try:
            for t in _grid_tables(con):
                cols = [r[0] for r in con.execute(f'DESCRIBE "{t}"').fetchall()]
                if "Email" not in cols:
                    continue
                rows = con.execute(f'SELECT "Email" FROM "{t}"').fetchall()
                out[t[5:]] = frozenset(str(e[0]).strip().lower()
                                       for e in rows if e[0])
        finally:
            con.close()
    except Exception:
        return {}
    return out


def _previous_coverage(svc, store_folder_id: str) -> dict | None:
    """How many sessions last week's store had duration and ratings for.

    None when there is nothing to compare against — no store folder, no previous
    store, or a store we cannot read. A missing yardstick must never fail a run;
    it just means this week has no claim to check.
    """
    if not store_folder_id:
        return None
    tmp = None
    try:
        meta = live_data.find_in_folder(svc, store_folder_id, STORE_NAME)
        if not meta:
            return None
        # NOT fetch_store_snapshot: that one is download_cached, keyed on the
        # bare file id, and attendance.duckdb keeps its id forever because
        # upload_to_folder replaces it in place. On a machine with a warm
        # .cache/ the first copy would then be served to every later run, so
        # both gates would silently compare against the store as it stood the
        # first time — on exactly the machine somebody uses to diagnose a gate
        # failure. That cache is right for immutable archived snapshots and
        # wrong for the live store.
        blob = live_data.fetch_file_bytes(svc, meta["id"])
        tmp = os.path.join(HERE, ".cache", PREV_STORE)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(blob)
        # Left on disk on purpose: GATE 6 below reads the same copy rather than
        # downloading last week's store twice.
        return _store_coverage(tmp)
    except Exception:
        return None


# ───────────────────────────── build the store ───────────────────────────────
def _prepend_intro_sessions(DATA: dict) -> None:
    """Same rule as the app: first session of every batch = the intro call,
    counts from intro_attendance.json (aggregates precomputed from L2 customer)."""
    path = os.path.join(HERE, "intro_attendance.json")
    if not os.path.exists(path):
        # Silence here would mean a store whose batches quietly lack their intro
        # row — most likely the file wasn't committed (it's under a *.json ignore).
        print("   WARNING: intro_attendance.json is missing — the store will have "
              "no 'Intro call' session. Commit it alongside pipeline.py.",
              flush=True)
        return
    with open(path, encoding="utf-8") as fh:
        intro = json.load(fh)
    for code, d in DATA.items():
        att = intro.get(code)
        if not att:
            continue
        stg = d["strength"]
        d["sessions"].insert(0, {
            "col": None, "mm": None,
            "date_lbl": "Intro call",
            "topic": "Intro call (pre-batch)",
            "present": att, "absent": max(0, stg - att), "total": stg,
            "pct": round(min(100.0, 100 * att / stg), 1),
            "present_only": False, "no_l2": False, "is_intro": True,
            # the intro call predates the batch and has no feedback poll
            "rating": None, "rating_n": 0,
        })


def build_store(path: str, marked_bytes: bytes, report, warnings, source: str,
                l2_bytes, attendee_names, marked_xlsx_file_id: str,
                stamps: dict,
                ratings: dict | None = None,
                curric_tabs: dict | None = None,
                session_meta: dict | None = None,
                cache_stats: dict | None = None) -> dict:
    """Write attendance.duckdb next to nothing else — one self-contained file."""
    # dashboard DATA/summary (same code path the app used to run at startup)
    wb = load_workbook(io.BytesIO(marked_bytes), read_only=True, data_only=True)
    tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets}
    wb.close()
    topics, l2_labels, mentors = dsheets.webinar_topic_lookup(
        [(n, None) for n in attendee_names], l2_bytes,
        with_labels=True, with_mentors=True)
    DATA, summary = ddata.build(tabs, topics, l2_labels, ratings, mentors)
    _prepend_intro_sessions(DATA)

    # Forecast for sessions that have not run yet. Built HERE because it needs the
    # finished DATA and nothing else — no extra fetch, and the numbers can never
    # disagree with the dashboard they sit beside. None means "not configured";
    # a section carrying only warnings means "configured, could not build".
    forecast_section = None
    if curric_tabs is not None:
        try:
            forecast_section = forecast.build(DATA, curric_tabs,
                                              datetime.now(IST).date(),
                                              horizon_weeks=FORECAST_WEEKS)
        except Exception as e:
            forecast_section = forecast.empty_result(f"forecast build failed: {e}")

    # Recap and per-trainer rollups. Built here for the same reason the forecast
    # is: they need the finished DATA and nothing else, so they cannot disagree
    # with the dashboard, and the app stays a pure reader of the store.
    # Neither may take the run down - a recap is a nice-to-have beside the
    # attendance numbers people actually depend on.
    smeta = session_meta or {}
    recap_section = trainer_section = None
    sessions_section = []
    try:
        recap_section = None      # built below, from the enriched rows
    except Exception as e:
        recap_section = {"weeks": [], "latest": None, "awards": [],
                         "leaderboard": [], "sessions": [],
                         "warnings": [f"recap build failed: {e}"]}
    try:
        rows = recap.collect_sessions(DATA, datetime.now(IST).date())
        emails = ac.l2_mentor_emails(l2_bytes) if l2_bytes else {}
        mtypes, tconf = (ac.l2_mentor_types(l2_bytes) if l2_bytes else ({}, {}))
        trainer_section = trainers.build(rows, emails, mtypes)
        trainer_section["type_conflicts"] = tconf
        # Every session, flat and filterable — this is what the Sessions tab
        # reads. ~500 rows of aggregates, so it costs nothing in the store.
        sessions_section = trainers.annotate(rows, emails, mtypes)
        for r in sessions_section:
            m = smeta.get((r["batch"], r.get("mm"), r.get("pod") or "")) or {}
            r["duration_hrs"] = m.get("span_hrs") or m.get("duration_hrs")
            r["peak"] = m.get("peak_computed") or m.get("peak")
            r["unique_viewers"] = m.get("unique_viewers")
            # The retention curve and stickiness ride along: same sweep, and
            # ~0.5 MB across the whole corpus.
            r["retention"] = m.get("curve")
            r["stick10"] = m.get("stick10")
            r["stick30"] = m.get("stick30")
            # Poll marker, in minutes along the curve. Both sides come from the
            # session's own files — the poll's first submission and the first
            # join — so no manual log is needed. Dropped when either is missing
            # or the result lands outside the curve, which would be a marker
            # pointing at nothing.
            r["poll_at_min"] = None
            if r.get("poll_at") and m.get("t0") and r.get("retention"):
                try:
                    _pt = datetime.fromisoformat(r["poll_at"])
                    _t0 = datetime.fromisoformat(m["t0"])
                    _mins = int((_pt - _t0).total_seconds() // 60)
                    if 0 <= _mins < len(r["retention"]):
                        r["poll_at_min"] = _mins
                except Exception:
                    pass
        # Now that the rows carry retention, the weekly stickiness KPI and the
        # Best-retention award see the same numbers the session rows do.
        recap_section = recap.build(DATA, datetime.now(IST).date(),
                                    rows=sessions_section)
    except Exception as e:
        trainer_section = {"trainers": [], "ambiguous": [], "merged": {},
                           "n_raw": 0, "n_people": 0, "type_conflicts": {},
                           "warnings": [f"trainer build failed: {e}"]}
        sessions_section = []

    # `tabs` is the whole marked workbook, already materialised above to build
    # DATA. Handing it to these two readers instead of the raw bytes stops them
    # re-streaming the same 80k rows out of the zip: measured on the real 7.9 MB
    # workbook, 43.7s + 46.8s of a 446s run, for rows already in memory. Same
    # numbers either way — pinned by tests/test_dashboard_core_tabs.py.
    df = dc.compute(marked_bytes, tabs=tabs)
    smap = dc.batch_sheet_map(marked_bytes)   # sheetnames only, 0.1s — left alone
    for _sh, _rows in tabs.items():
        _w = dc._wide_tab_warning(_sh, max((len(r) for r in _rows), default=0))
        if _w:
            warnings = list(warnings) + [_w]
    # only real batch tabs — helper tabs like 'l2 cx data' or 'Auto pay' pass
    # dashboard_core's loose sheet filter but are not batches
    batches = sorted((b for b in df["Batch"].unique()
                      if b in smap and re.fullmatch(r"(ECAP )?B\d+", str(b))),
                     key=dc.batch_key)
    df = df[df["Batch"].isin(batches)].reset_index(drop=True)

    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    con.register("df_compute", df)
    con.execute("CREATE TABLE compute AS SELECT * FROM df_compute")

    grid_rows = {}
    for b in batches:
        grid = dc.roster_grid(marked_bytes, smap[b])
        grid_rows[b] = len(grid)
        con.register("df_grid", grid)
        con.execute(f'CREATE TABLE "grid_{b}" AS SELECT * FROM df_grid')
        con.unregister("df_grid")

    meta = {
        "generated_at": datetime.now(IST).strftime("%d %b %Y, %H:%M IST"),
        "generated_at_iso": datetime.now(IST).isoformat(),
        "DATA": DATA,
        "summary": summary,
        "report": report,
        "warnings": warnings,
        "source": source,
        "batches": batches,
        "sheet_map": smap,
        "marked_xlsx_file_id": marked_xlsx_file_id,
        "stamps": stamps,
        # What the derived-facts memo did this run, and the rule hash each
        # namespace ran under. Provenance, so a week built on a suspect rule
        # can be identified later at zero storage cost. Counts only.
        "cache": cache_stats or {},
        # None = no CURRICULUM_ID configured. A section present but with empty
        # `sessions` means it was configured and produced nothing — the app says
        # which, because "not set up" and "set up but broken" need different fixes.
        "forecast": forecast_section,
        # Aggregates only — counts and percentages, never a respondent or a
        # student — so both are safe in the store and on the public site.
        "recap": recap_section,
        "trainers": trainer_section,
        "sessions": sessions_section,
    }
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)",
                    [(k, json.dumps(v)) for k, v in meta.items()])
    con.close()
    return {"batches": len(batches), "students": sum(grid_rows.values()),
            "sessions": summary["sessions"]}


# ───────────────────────────── the roster source ─────────────────────────────
def _lms_stamp(report: dict | None) -> dict | None:
    """The provenance the store records for an LMS-sourced run.

    Counts and batch codes only — the full report holds per-batch student totals
    and nothing else, but keeping the store's meta small matters because the app
    downloads it on every cold start.
    """
    if not report:
        return None
    return {k: report.get(k) for k in
            ("live", "fetched", "failed", "degraded", "refreshed", "seeded",
             "frozen", "tabs", "rows", "retained_students")}


def fetch_prev_marked(svc, cfg: dict, why: str, log=print) -> bytes | None:
    """Last week's marked workbook, or None.

    Fetched ONCE per run and handed to both the LMS roster builder (which retains
    the people the API has never heard of) and `carryforward` (which puts their
    marks back). It used to be fetched inside step [1c]; the LMS source needs it
    earlier, and downloading 8 MB twice would be silly.

    Never fatal. Without it an LMS run degrades to a pure API seed and an
    incremental run degrades to a full re-mark — both slower and louder, neither
    wrong.
    """
    if not cfg.get("store_folder_id"):
        return None
    try:
        hit = live_data.find_in_folder(svc, cfg["store_folder_id"], MARKED_NAME)
    except Exception as e:
        log(f"   WARNING: could not look for {MARKED_NAME} ({e}) — {why}")
        return None
    if not hit:
        log(f"   WARNING: no {MARKED_NAME} in the store folder — {why}")
        return None
    try:
        return live_data.fetch_file_bytes(svc, hit["id"])
    except Exception as e:
        log(f"   WARNING: could not fetch {MARKED_NAME} ({e}) — {why}")
        return None


def build_lms_roster(cfg: dict, prev_marked: bytes | None, allow_partial=False,
                     log=print) -> tuple[bytes, str, dict]:
    """The roster workbook, built from the LMS API. See lms_roster.py.

    Only the live cohorts and never-seen batches are actually fetched — every
    other batch is frozen and reuses last week's rows verbatim. That is the
    owner's rule, and it is also what keeps the run affordable: a full sweep is
    ~95 batch records against an endpoint that 504s under load.
    """
    key = lms_client.api_key(cfg.get("lms_api_key", ""))
    cache_dir = cfg.get("lms_cache_dir") or None
    warns: list[str] = []
    # Pure mode ignores last week's workbook entirely — see the config comment.
    retained = ({} if cfg.get("lms_pure")
                else lms_roster.read_previous(prev_marked, warns))
    if cfg.get("lms_pure"):
        log("   PURE mode: the API alone decides who is enrolled. Students it "
            "does not know are NOT carried over.")

    # An unreachable API is survivable — but only knowingly. Every frozen batch
    # is built from `retained` alone and needs no API at all, so the roster is
    # still complete as of last week; what is missing is this week's NEW
    # enrolments in the live cohorts, who would then not be counted at all.
    # That understates attendance, so it refuses by default like every other
    # gate here. Under --allow-partial the run proceeds AND step [8a] declines
    # to publish the marked workbook, so a degraded roster never becomes next
    # week's frozen base.
    try:
        raw = lms_client.fetch_batches(key, log=log)
    except Exception as e:
        if not (allow_partial and retained):
            raise SystemExit(
                f"REFUSING TO PUBLISH: the LMS API is unreachable ({e}).\n"
                f"   Re-run later, set ROSTER_SOURCE=sheet to use the Google "
                f"Sheet, or pass --allow-partial to publish last week's "
                f"enrolment without this week's new students.")
        log(f"   WARNING: LMS unreachable ({e}) — building from last week's "
            f"roster alone. --allow-partial, so the marked workbook will NOT "
            f"be republished.")
        data, report = lms_roster.build_from(retained, {}, [], log=log)
        report["warnings"] = warns + [f"LMS unreachable: {e}"]
        report["live"], report["fetched"] = [], []
        report["degraded"] = True
        return data, datetime.now(IST).isoformat(timespec="seconds"), report

    grouped = lms_roster.group_batches(raw)
    log(f"   LMS: {len(raw)} batch record(s), {len(grouped)} CAP batch(es)")
    if retained:
        log(f"   carried forward: {len(retained)} batch tab(s), "
            f"{sum(len(v) for v in retained.values()):,} student(s)")

    override = [b.strip().upper() for b in
                (cfg.get("lms_live_batches") or "").replace(";", ",").split(",")
                if b.strip()]
    live = override or lms_roster.live_codes(sorted(set(grouped) | set(retained)))
    _missed: list[str] = []
    want = lms_roster.plan_fetch(list(grouped), list(retained), live,
                                 missing=_missed)
    log(f"   live: {', '.join(live) or 'none'} · fetching {len(want)} batch(es)"
        + (f" ({', '.join(want)})" if want else ""))
    for code in _missed:
        # The current cohort with no API record is the case that would otherwise
        # pass unnoticed: it stays on last week's enrolment while the log claims
        # a refresh. Usually a renamed batch or one not created in the LMS yet.
        warns.append(f"{code}: named live but the LMS has no batch record for "
                     f"it — left on last week's roster")
        log(f"   WARNING: {warns[-1]}")

    api_by_code: dict[str, list] = {}
    failed: list[str] = []
    for code in want:
        recs = []
        try:
            for b in grouped.get(code, []):
                recs.append((b, lms_client.cached_customers(
                    key, b["id"], cache_dir, log=log)))
        except Exception as e:
            # One batch failing must not cost the other 24 their refresh. A
            # frozen-equivalent result (retained rows only) is the safe outcome
            # for that batch; the gate below decides whether it is acceptable.
            failed.append(code)
            log(f"      {code}: FAILED ({e}) — leaving it on last week's roster")
            continue
        api_by_code[code] = recs
        log(f"      {code}: {sum(len(c) for _b, c in recs):,} customer(s) "
            f"from {len(recs)} record(s)")

    if failed:
        lost = [c for c in failed if c not in retained]
        if lost and not allow_partial:
            raise SystemExit(
                f"REFUSING TO PUBLISH: batch(es) {', '.join(lost)} could not be "
                f"read from the LMS and have no rows to fall back on, so they "
                f"would be missing from the dashboard entirely.\n"
                f"   Re-run (the 504s are load-dependent and usually clear), or "
                f"pass --allow-partial.")
        if not allow_partial:
            raise SystemExit(
                f"REFUSING TO PUBLISH: batch(es) {', '.join(failed)} could not "
                f"be refreshed, so this week's new enrolments in them would be "
                f"missing and their attendance understated.\n"
                f"   Re-run, or pass --allow-partial to publish last week's "
                f"enrolment for them.")

    data, report = lms_roster.build_from(retained, api_by_code, live, log=log)
    report["warnings"] = (warns + list(report.get("warnings") or ())
                          + [f"{c}: not refreshed (LMS read failed)" for c in failed])
    report["live"] = live
    report["fetched"] = sorted(api_by_code)
    report["failed"] = failed
    report["retained_students"] = sum(len(v) for v in retained.values())
    stamp = datetime.now(IST).isoformat(timespec="seconds")
    return data, stamp, report


# ────────────────────────────────── main ─────────────────────────────────────
def main() -> None:
    # Windows consoles default to cp1252, which can't print every character in
    # Drive file names — never let logging kill the pipeline.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-upload", action="store_true",
                    help="build the store locally, skip the Drive upload")
    ap.add_argument("--marked-out", default="",
                    help="also save the marked roster workbook to this local "
                         "path. The marks otherwise exist only in memory and "
                         "in the [8a] upload, so a --no-upload run that exists "
                         "to produce them had no way to hand them over.")
    ap.add_argument("--publish-parallel", action="store_true",
                    help="upload the STORE_OUT store to the Drive store folder "
                         "UNDER ITS OWN NAME, so the deployed app can offer it "
                         "as a second data set. Requires STORE_OUT. Never "
                         "touches the real store, next week's base or the "
                         "archive (see the early return at [8/8]).")
    ap.add_argument("--mode", default="exact", choices=["exact", "inclusive"],
                    help="marker matching rule (default: exact, same as the app)")
    ap.add_argument("--no-site", action="store_true",
                    help="skip rendering site/ (the static website)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="publish even if some attendee files/folders failed "
                         "(default: refuse, so a complete store is never "
                         "overwritten by a partial one)")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the derived-facts memo and re-parse every Zoom "
                         "export (a fresh memo is still written afterwards)")
    ap.add_argument("--cache-file", default="",
                    help="read/write the derived-facts memo at this local path "
                         "instead of the Drive store folder — this is how the "
                         "cold-vs-warm store diff is run offline")
    ap.add_argument("--incremental", action="store_true",
                    help="mark ONLY sessions that are not marked yet: last "
                         "week's marked workbook is carried onto this week's "
                         "roster, so past columns are frozen and never "
                         "recomputed. Minutes instead of the full run — the "
                         "price is that a later rule fix never reaches them "
                         "(see CLAUDE.md §4g). Omit it for a full rebuild.")
    args = ap.parse_args()

    # Validate the STORE_OUT / upload combination NOW, not at [6/8] where the
    # name is actually used. The check is pure, so it costs nothing here — and
    # a run that fetches every Zoom export before refusing an argument mistake
    # spends a quarter of an hour to say something it knew at startup.
    parallel_store_name(os.path.basename(os.environ.get("STORE_OUT") or "").strip(),
                        args.no_upload, args.publish_parallel)

    cfg = load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=RW_SCOPES)
    svc = live_data._drive_service()

    today_iso = datetime.now(IST).date().isoformat()
    cache, cache_stats = {}, {}
    # A hash of each producing module's own SOURCE. Discards the memo whenever
    # the rule that made it changes, without anyone remembering to bump a
    # constant — forgetting is the documented failure mode here, see
    # derived_cache.py and CLAUDE.md §4f.
    cache_rules = {"sessionmeta": derived_cache.rule_version(sessionmeta),
                   "polls": derived_cache.rule_version(polls)}

    print("[1/8] Fetching roster + L2 …", flush=True)
    l2_bytes, l2_stamp = (live_data.fetch_sheet_cached(svc, cfg["l2_id"])
                          if cfg["l2_id"] else (None, ""))

    # Last week's marked workbook, fetched once and used twice: the LMS builder
    # retains the students the API has never heard of, and carryforward then puts
    # their marks back. Only downloaded when something actually needs it.
    _use_lms = cfg.get("roster_source") == "lms"
    prev_marked = None
    if _use_lms or args.incremental:
        prev_marked = fetch_prev_marked(
            svc, cfg,
            "this run will re-mark everything" if args.incremental
            else "this run will seed the roster from the API alone")

    lms_report = None
    if _use_lms:
        roster_bytes, roster_stamp, lms_report = build_lms_roster(
            cfg, prev_marked, allow_partial=args.allow_partial)
        print(f"   roster {len(roster_bytes):,} bytes from the LMS API — "
              + lms_roster.summary(lms_report))
        for w in lms_report.get("warnings") or ():
            print(f"   WARNING: {w}", flush=True)
    else:
        roster_bytes, roster_stamp = live_data.fetch_sheet_cached(svc, cfg["roster_id"])
        print(f"   roster {len(roster_bytes):,} bytes (modified {roster_stamp})")

    # [1b] The derived-facts memo: last week's per-file parse results, so this
    # run only parses what is genuinely new. Any problem at all yields an empty
    # memo and a full, correct, slower run — it is never a source of truth.
    # Counts only in the log: a public repo's Actions logs are world-readable
    # and this pipeline has never printed a Drive file id into them.
    _cut = (datetime.now(IST).date()
            - timedelta(days=derived_cache.CACHE_MAX_AGE_DAYS)).isoformat()
    if args.no_cache:
        print("[1b] Derived-facts cache: ignored (--no-cache)", flush=True)
    else:
        cache, _cstat = (
            derived_cache.load_file(args.cache_file, cache_rules, _cut)
            if args.cache_file else
            derived_cache.load(svc, cfg["store_folder_id"], cache_rules, _cut))
        print(f"   [1b] cache: {_cstat.get('loaded', 0):,} row(s) loaded"
              + (f", {_cstat['discarded']:,} discarded (rule changed)"
                 if _cstat.get("discarded") else "")
              + (f", {_cstat['expired']:,} expired" if _cstat.get("expired") else "")
              + (f", {_cstat['invalid']:,} invalid" if _cstat.get("invalid") else "")
              + (f" — {_cstat['note']}" if _cstat.get("note") else ""), flush=True)

    # [1c] INCREMENTAL: this week's roster for structure, last week's workbook
    # for the session columns. `process_files` only ever writes the columns it
    # was handed files for, so every carried column is frozen exactly as it was
    # — and `_existing_sessions` reading the merged workbook is what stops those
    # sessions being downloaded again. Failing to fetch last week's workbook is
    # NOT fatal: the merge falls back to the pristine export, which is simply a
    # full run — slower, more correct, and it says so.
    base_bytes, carry = roster_bytes, {"carried": 0, "warnings": []}
    if args.incremental:
        print("[1c] Carrying last week's marks forward …", flush=True)
        # `prev_marked` was fetched at [1]; the LMS roster builder needs it too.
        base_bytes, carry = carryforward.merge_marks(roster_bytes, prev_marked)
        print("   " + carryforward.summary(carry), flush=True)
        # `summary` already quotes the warnings when it carried nothing, so only
        # list them separately when there is a result they qualify.
        if carry.get("carried"):
            for w in carry.get("warnings") or ():
                print(f"   WARNING: {w}", flush=True)

        # GATE 0 — nobody may fall out of an LMS-built roster.
        #
        # GATE 6 cannot catch this. `lost_marks` restricts its comparison to
        # students present in BOTH weeks' rosters, so a student who vanishes from
        # the roster entirely is invisible to it — and that is exactly the failure
        # mode of building the roster from an API that has never heard of 9,017 of
        # our people (§4h). `unmatched_prev` is the signal that does see it.
        #
        # By construction it must be zero: lms_roster retains every row of last
        # week's workbook before it adds anything. Non-zero means the retention
        # layer missed a tab, and republishing would bin those students' history
        # for good, since the marked workbook is the system of record (§4g).
        _dropped = int(carry.get("unmatched_prev") or 0)
        if _use_lms and _dropped and not cfg.get("lms_pure"):
            msg = (f"{_dropped} student(s) in last week's marked workbook are "
                   f"absent from the LMS-built roster, so their marks were NOT "
                   f"carried. Publishing would lose that history permanently.")
            if args.allow_partial:
                print(f"   WARNING: {msg} Continuing: --allow-partial.", flush=True)
            else:
                raise SystemExit(
                    f"REFUSING TO PUBLISH: {msg}\n"
                    f"   Run tools/lms_cutover_diff.py to see which batches, or "
                    f"set ROSTER_SOURCE=sheet to fall back to the Google Sheet.")

    # [1d] ECAP — a second programme, rostered from a snapshot (see ecap.py).
    #
    # It has to happen HERE, before [2/8], not later. `fetch_new_attendees`
    # decides which session folders to download by checking each folder's
    # batches against the TABS IN THIS WORKBOOK: with no ECAP tab, all 69 ECAP
    # folders are counted "without a roster tab" and never fetched, so the
    # marking below would have nothing to mark even though the rest of the
    # stack understands ECAP perfectly well.
    #
    # Never fatal. ECAP is an addition; a Drive hiccup fetching its snapshot
    # must not cost the CAP dashboard its weekly refresh.
    # `warnings` does not exist yet - [3/8] creates it from the marker - so
    # these are stashed and merged in there.
    _ecap_warn: list = []
    if cfg.get("ecap_roster_id"):
        print("[1d] Grafting the ECAP roster tabs …", flush=True)
        try:
            _eb = live_data.fetch_file_bytes(svc, cfg["ecap_roster_id"])
            base_bytes, _er = ecap.graft(base_bytes, _eb)
            print(f"   {len(_er['added'])} tab(s) added"
                  + (f" ({', '.join(_er['added'])}, {_er['rows']:,} students)"
                     if _er["added"] else "")
                  + (f" · {len(_er['skipped'])} already in the workbook"
                     if _er["skipped"] else ""), flush=True)
            for _w in _er.get("warnings") or ():
                _ecap_warn.append(f"ECAP: {_w}")
                print(f"   WARNING: {_w}", flush=True)
        except Exception as e:
            _ecap_warn.append(
                f"ECAP roster could not be grafted ({e}) — its batches are "
                f"missing from this build.")
            print(f"   WARNING: {e} — continuing without ECAP.", flush=True)

    print("[2/8] Fetching attendee reports (new sessions only) …", flush=True)
    attendee_files, info = live_data.fetch_new_attendees(
        svc, cfg["attendee_folder_id"], base_bytes)
    print(f"   {info['files']} file(s) from {info['new_folders']} folder(s), "
          f"{info['failed']} failed, {info['skipped_no_sheet']} without a roster tab")

    # Publishing a store built from partial data would overwrite a COMPLETE one
    # and the job would still go green — so refuse. Re-running picks up whatever
    # failed (successful downloads stay cached, so a retry is cheap).
    problems = []
    if info.get("listing_errors"):
        problems.append(f"{len(info['listing_errors'])} session folder(s) could not "
                        f"be listed: {'; '.join(info['listing_errors'][:5])}")
    if info.get("bad_zips"):
        problems.append(f"{len(info['bad_zips'])} unreadable zip(s): "
                        f"{'; '.join(info['bad_zips'][:5])}")
    if info.get("failed"):
        problems.append(f"{info['failed']} attendee file(s) failed to download: "
                        f"{'; '.join(info.get('download_errors', [])[:5])}")
    if problems and not args.allow_partial:
        raise SystemExit("Refusing to publish partial data — the previous store is "
                         "left in place:\n  - " + "\n  - ".join(problems)
                         + "\nRe-run the job; pass --allow-partial to override.")

    print("[3/8] Marking attendance …", flush=True)
    # Carried columns are frozen at the point of WRITE, not just in the fetch.
    # The fetch decides per folder and one session can sit in two folders whose
    # names disagree, so a second copy of an already-marked export used to slip
    # through and rewrite its column - measured as B35 23 Aug Educators going
    # 63 -> 60 present, which GATE 6 then refused to publish.
    _frozen = carry.pop("_carried_keys", None) if args.incremental else None
    if attendee_files:
        marked_bytes, report, warnings = ac.process_files(
            base_bytes, l2_bytes, attendee_files, mode=args.mode,
            values_only=True, frozen=_frozen)
    else:
        # Nothing new to mark is a normal outcome for an incremental run — a
        # mid-week upload of one session, or a re-run of a week already loaded.
        # It publishes the carried history unchanged rather than failing.
        marked_bytes, report, warnings = base_bytes, [], []
    warnings = list(warnings) + _ecap_warn
    if args.marked_out:
        with open(args.marked_out, "wb") as _fh:
            _fh.write(marked_bytes)
        print(f"   marked workbook saved to {args.marked_out} "
              f"({len(marked_bytes) / 1e6:.1f} MB)", flush=True)

    new_n = sum(1 for r in report if r["kind"] == "NEW")
    froze_n = sum(1 for r in report if r["kind"] == "frozen")
    print(f"   {len(report) - froze_n} session column(s) marked "
          f"({new_n} new) · {len(warnings)} warning(s)"
          + (f" · {froze_n} already frozen, left alone" if froze_n else "")
          + (" — nothing new this run" if not report else ""))

    # Same rule as the attendee gate: a session dropped because Zoom exported
    # the wrong shape is a session MISSING from the dashboard. The file
    # downloaded fine, so the gate above cannot see it. Left ungated this
    # publishes quietly incomplete numbers on a green run. Fix the export, or
    # pass --allow-partial to publish the week without those sessions.
    unreadable = [w for w in warnings if ac.ZERO_ATTENDEE_TAG in w]
    if unreadable and not args.allow_partial:
        raise SystemExit(
            f"Refusing to publish: {len(unreadable)} attendee file(s) yielded no "
            "attendees, so their sessions were skipped rather than marking those "
            "batches absent — the previous store is left in place:\n  - "
            + "\n  - ".join(unreadable[:5])
            + (f"\n  ... and {len(unreadable) - 5} more" if len(unreadable) > 5 else "")
            + "\nRe-export those sessions as a Zoom Attendee Report, or pass "
              "--allow-partial to publish this week without them.")

    if not args.no_upload and not cfg["store_folder_id"]:
        raise SystemExit("STORE_FOLDER_ID missing — where should the store "
                         "be uploaded? (or run with --no-upload)")

    # The per-batch roster tabs, read once. [5a] below divides shared polls by
    # these rosters (ddata.roster_emails).
    print("[4/8] Reading the roster tabs …", flush=True)
    roster_tabs = {}
    tab_err = ""
    try:
        wb_r = load_workbook(io.BytesIO(marked_bytes), read_only=True, data_only=True)
        # ECAP tabs belong here too: a room shared by CAP and ECAP
        # ("AI CAP B30 , ECAP B1 & B2") can only have its poll divided per
        # batch if every sharing batch's roster is in this map.
        roster_tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
                       for ws in wb_r.worksheets
                       if re.fullmatch(r"\s*AI\s*(E\s*-?\s*)?CAP\s*B\d+\s*",
                                       ws.title, re.I)}
        wb_r.close()
    except Exception as e:
        tab_err = str(e)
    print(f"   {len(roster_tabs)} batch tab(s)"
          + (f" — read FAILED: {tab_err}" if tab_err else ""), flush=True)

    # Gated, because no later gate can see the damage. With no roster tabs,
    # `polls.apply_roster_split` cannot tell a shared room's batches apart, so
    # every batch in it keeps the whole room's JOINT rating - the exact
    # double-count [5a.1] exists to prevent. GATE 5 counts that joint figure as
    # a valid rating, so its coverage does not move; GATE 6 only compares marks;
    # and the app then labels the result "anonymous poll", which is false. The
    # dashboard builds normally and only the ratings are wrong, which is
    # precisely why this must not pass quietly.
    #
    # Zero tabs does NOT imply a broken workbook: this fullmatch is stricter
    # than `ac._sheet_key`, so a rename the rest of the stack tolerates
    # ("AI CAP B37 8PM") reads as zero here while the marking stays correct.
    if not roster_tabs:
        _why = (f"could not read the roster tabs: {tab_err}" if tab_err
                else 'no "AI CAP B<n>" tab matched in the marked workbook')
        warnings.append(
            "Shared-session poll ratings were NOT split per batch — " + _why
            + ". Every batch in a shared room carries the joint rating.")
        if not args.allow_partial:
            raise SystemExit(
                "Refusing to publish: " + _why + ", so a shared room's poll "
                "rating cannot be divided between its batches and each of them "
                "would publish the joint figure — the previous store is left "
                "in place.\nCheck the roster tab names (the pipeline wants a "
                'bare "AI CAP B<n>"), or pass --allow-partial to publish with '
                "unsplit ratings.")

    # The marked workbook stops being only an OUTPUT: under --incremental it is
    # next week's base. Uploading it HERE - before build_store, before GATE 5
    # and GATE 6 - would freeze sessions the store never published, and the next
    # incremental run would skip them because their columns are already in the
    # base. So it is held back to [8a], after every gate AND after the store.
    #
    # ALWAYS held back, in full runs too, and not conditioned on a previous copy
    # existing. Making the deferral depend on `find_in_folder` returning
    # something meant a single transient Drive error - swallowed by the bare
    # `except` below, and a separate API call from the one [1c] already made -
    # silently reverted to publishing before the gates, including under
    # --allow-partial. A rule that quietly turns itself off is not a rule. The
    # only cost of deferring unconditionally is that the very first run leaves
    # `marked_xlsx_file_id` empty, so the store's download button waits a week.
    marked_xlsx_file_id = ""
    if cfg["store_folder_id"]:
        try:
            # Just the id, for the store's download button: upload_to_folder
            # replaces in place, so the id is stable across runs.
            _hit = live_data.find_in_folder(svc, cfg["store_folder_id"], MARKED_NAME)
            marked_xlsx_file_id = _hit["id"] if _hit else ""
        except Exception:
            marked_xlsx_file_id = ""
    if not args.no_upload:
        print("[5/8] Marked roster xlsx held back until the gates pass "
              "(it is next week's base)", flush=True)

    # Session feedback polls: one small CSV per session, disk-cached like the
    # attendee reports. Never fatal - a missing poll costs that session its
    # rating, not the run.
    print("[5a] Session feedback polls …", flush=True)
    ratings, ratings_by_wid = {}, {}
    # The LISTING runs outside the try: it is the change detector, and a failure
    # to enumerate must not degrade quietly to "no polls this week".
    _pl = live_data.list_polls(svc, cfg["attendee_folder_id"])
    try:
        _prows, _pstat = [], {"hit": 0, "miss": 0}
        _pneed = []
        for _f in _pl:
            _k = derived_cache.key_for(_f)
            _got = derived_cache.get(cache, "polls", _k)
            if _got is None:
                _pneed.append(_f)
            else:
                _pstat["hit"] += 1
                _prows.append((_f["id"], _f["name"], _got))

        def _poll_take(f, blob, verified):
            got = polls.parse_one(blob)
            if got is None:
                return
            derived_cache.stage(cache, "polls", derived_cache.key_for(f), got,
                                verified=verified, today=today_iso)
            _pstat["miss"] += 1
            _prows.append((f["id"], f["name"], got))

        _pi = live_data.fetch_stream(svc, _pneed, _poll_take)
        # Back into LISTING order before the dedupe: its tie-break keeps the
        # copy seen first, so hits and misses must not be segregated or a tie
        # would resolve differently from one week to the next.
        _order = {f["id"]: i for i, f in enumerate(_pl)}
        _prows.sort(key=lambda r: _order.get(r[0], 1 << 30))
        ratings, ratings_by_wid = polls.lookup_by_session_rows(
            [(n, g) for _i, n, g in _prows], l2_bytes)

        # [5a.1] A webinar SEVERAL batches sat in has one poll. Copying its
        # aggregate onto each batch made B35, B36 and B37 show the same Finance
        # rating and counted it three times in every rollup. So each such poll
        # is re-read PER RESPONDENT and divided between the batches whose
        # rosters hold them. Per-respondent rows are PII and roster-dependent,
        # so they are never memoised: the file's bytes are fetched again here
        # (`fetch_stream` serves them from its disk cache when it has them),
        # and only the split's aggregates leave this block.
        _win = {}                       # wid -> the copy dedupe_rows kept
        for _fid, _n, _g in _prows:
            _k = polls.name_key(_n)
            # identity, not equality: dedupe_rows stored this very dict, so
            # this recovers the winning copy without a second tie-break
            if _k and ratings_by_wid.get(_k[0]) is _g:
                _win.setdefault(_k[0], _fid)
        _need = {rt.get("_wid") for rt in ratings.values()
                 if len(rt.get("_batches") or ()) > 1}
        # [5a.2] and every MULTI-DOMAIN room - an All Domains session or the
        # complement room - so its poll can be divided by the roster's POD
        # column. Those are exactly the entries with an empty pod key.
        _need |= {rt.get("_wid") for k, rt in ratings.items() if not k[2]}
        _want = {_win[w] for w in _need if w in _win}
        _texts = {}

        def _poll_text(f, blob, _verified):
            _k = polls.name_key(f["name"])
            if _k:
                _texts[_k[0]] = blob.decode("utf-8-sig", errors="replace")

        _si = live_data.fetch_stream(svc, [f for f in _pl if f["id"] in _want],
                                     _poll_text)
        ratings, _sstat = polls.apply_roster_split(
            ratings, _texts, ddata.roster_emails(roster_tabs))
        ratings, _pdstat = polls.apply_pod_split(
            ratings, _texts, ddata.roster_pod_emails(roster_tabs))
        print(f"   per-domain poll split: {_pdstat['split']} of "
              f"{_pdstat['rooms']} multi-domain room(s)"
              + (f" · kept whole: {_pdstat['kept']}" if _pdstat["kept"] else ""),
              flush=True)
        del _texts
        cache_stats["polls"] = dict(
            _pstat, listed=len(_pl), failed=_pi["failed"],
            shared=_sstat["shared"], shared_split=_sstat["split"],
            shared_kept=_sstat["kept"],
            # every key seen on the drive this run, so [8c] can drop memo
            # entries for files that no longer exist
            keys={k for k in (derived_cache.key_for(f) for f in _pl) if k})
        print(f"   {len(_prows)}/{len(_pl)} poll file(s) "
              f"({_pstat['hit']} cached, {_pstat['miss']} parsed) · "
              f"{len(ratings_by_wid)} webinar(s) rated · "
              f"{len(ratings)} session key(s)")
        print(f"   {_sstat['shared']} shared session key(s) over {len(_need)} "
              f"webinar(s): {_sstat['split']} split by roster, "
              f"{sum(_sstat['kept'].values())} kept joint"
              + (f" {_sstat['kept']}" if _sstat["kept"] else "")
              + (f" · {_si['failed']} poll fetch(es) failed" if _si["failed"] else ""))
    except Exception as e:
        print(f"   WARNING: poll ratings unavailable ({e}) - sessions show no rating.")

    # [5a2] Duration and peak, from every attendee report's own summary header
    # plus a sweep of its join/leave rows. Needs the FULL history, not just the
    # unmarked sessions, so it is its own pass: ~1,284 files / 246 MB / ~2.7 min
    # measured 2026-09-06. STREAMED - one file's bytes at a time, only the small
    # per-session dict kept, because holding 246 MB is how the marking used to
    # blow up on a small instance.
    print("[5a2] Session duration & peak ...", flush=True)
    session_meta = {}
    _al = live_data.list_attendees(svc, cfg["attendee_folder_id"])
    try:
        _mrows, _mstat = [], {"hit": 0, "miss": 0}
        _mneed = []

        def _row(f, h):
            """Attach the name-derived key FRESH, every run, on hit and miss alike.

            `_mm` is parsed out of the filename, so it is never memoised (the
            memo refuses it outright). Recomputing it here is what lets a
            mis-dated export renamed on Drive move to its corrected date even
            though its bytes are unchanged — and it is what stops a cache hit
            arriving without an `_mm` that lookup_by_session would drop, which
            would blank duration, peak, retention and stickiness on every
            session while the run stayed green.
            """
            k = sessionmeta.name_key(f["name"])
            out = dict(h)
            out["_mm"] = k[1] if k else None
            _mrows.append((f["id"], f["name"], out))

        for _f in _al:
            _k = derived_cache.key_for(_f)
            _got = derived_cache.get(cache, "sessionmeta", _k)
            if _got is None:
                _mneed.append(_f)
            else:
                _mstat["hit"] += 1
                _row(_f, _got)

        def _take(f, blob, verified):
            txt = blob.decode("utf-8-sig", errors="replace")
            h = sessionmeta.parse_header(txt)
            if not h or not h.get("duration_min"):
                return                      # a poll/overview export, not a report
            h.update(sessionmeta.measure(txt))
            derived_cache.stage(cache, "sessionmeta", derived_cache.key_for(f), h,
                                verified=verified, today=today_iso)
            _mstat["miss"] += 1
            _row(f, h)

        _mi = live_data.fetch_stream(svc, _mneed, _take)
        # Listing order, for the same reason as the polls above: collect()'s
        # tie-break keeps the incumbent, and 119 of 320 duplicated webinars in
        # the corpus TIE on unique_viewers.
        _order = {f["id"]: i for i, f in enumerate(_al)}
        _mrows.sort(key=lambda r: _order.get(r[0], 1 << 30))
        session_meta = sessionmeta.lookup_by_session(
            sessionmeta.collect([(n, h) for _i, n, h in _mrows]), l2_bytes)
        cache_stats["sessionmeta"] = dict(
            _mstat, listed=len(_al), failed=_mi["failed"],
            keys={k for k in (derived_cache.key_for(f) for f in _al) if k})
        print(f"   {len(_al)} file(s) listed, {_mi['failed']} failed "
              f"({_mstat['hit']} cached, {_mstat['miss']} parsed) - "
              f"{len(_mrows)} report(s) -> {len(session_meta)} session(s) "
              "with duration/peak", flush=True)
    except Exception as e:
        # Never fatal: a missing duration should cost that column, not the run.
        print(f"   WARNING: duration/peak scan failed ({e}) - those columns "
              "will be blank.", flush=True)

    # The coverage gate for these two steps lives after the store is built, so
    # it can compare like with like against last week's store — see GATE 5.

    # The Master Curriculum Schedule — what is PLANNED, one tab per pod. Read as
    # xlsx like every other Sheet so the same modifiedTime cache applies.
    # A failure here must never sink the run: the forecast is an extra, and last
    # week's dashboard is worth far more than a red build over a missing schedule.
    curric_tabs = None
    if cfg["curriculum_id"]:
        print("[5c] Curriculum schedule …", flush=True)
        try:
            c_bytes, _c_stamp = live_data.fetch_sheet_cached(svc, cfg["curriculum_id"])
            cwb = load_workbook(io.BytesIO(c_bytes), read_only=True, data_only=True)
            curric_tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
                           for ws in cwb.worksheets}
            cwb.close()
            print(f"   {len(curric_tabs)} tab(s)")
        except Exception as e:
            curric_tabs = {}          # {} = configured but unreadable; None = unset
            print(f"   WARNING: curriculum unavailable ({e}) - the Forecast tab "
                  "will say so.")
    else:
        print("[5c] Forecast skipped (no CURRICULUM_ID configured)", flush=True)

    print("[6/8] Building the DuckDB store …", flush=True)
    names = live_data.list_attendee_names(cfg["attendee_folder_id"])
    source = (f"Google Drive — roster, {info['files']} file(s) from "
              f"{info['new_folders']} new session(s)")
    os.makedirs(os.path.join(HERE, ".cache"), exist_ok=True)
    # STORE_OUT lets a PARALLEL dataset be built without touching the real
    # store: the app reads .cache/attendance.duckdb, so a side-by-side run must
    # write somewhere else or it silently replaces the dashboard everyone is
    # looking at. Only the file NAME is configurable — it always lands in
    # .cache/, because that is the directory the app and .gitignore both know.
    _store_out = os.path.basename(os.environ.get("STORE_OUT") or "").strip()
    _parallel_name = parallel_store_name(_store_out, args.no_upload,
                                         args.publish_parallel)
    store_path = os.path.join(HERE, ".cache", _store_out or STORE_NAME)
    stats = build_store(store_path, marked_bytes, report, warnings, source,
                        l2_bytes, names, marked_xlsx_file_id,
                        {"roster": roster_stamp, "l2": l2_stamp, "mode": args.mode,
                         "incremental": bool(args.incremental), "carry": carry,
                         "roster_source": cfg["roster_source"],
                         "lms": _lms_stamp(lms_report)},
                        ratings=ratings,
                        session_meta=session_meta,
                        curric_tabs=curric_tabs,
                        cache_stats=_cache_meta(cache_stats, cache_rules))
    size = os.path.getsize(store_path)
    print(f"   {stats['batches']} batches · {stats['students']:,} students · "
          f"{stats['sessions']} sessions · {size / 1e6:.1f} MB")

    # GATE 5 — coverage must not collapse. [5a] and [5a2] are the only steps in
    # this pipeline with no gate above them and a blanket `except` inside them,
    # so a failure there used to cost one column and still go green. That was
    # tolerable while those columns were recomputed from the exports every week.
    # It is not tolerable now a memoised fact can reach a published number: a
    # memo defect must be RED on the Monday it happens, not found a quarter
    # later in an immutable archive nobody can prune.
    #
    # The yardstick is last week's own store. Sessions do not disappear from the
    # past — the corpus only grows — so a real drop is a bug upstream, never a
    # fact about the week. No previous store (a first run) means no claim to
    # check and therefore no gate.
    _prev_cov = _previous_coverage(svc, cfg["store_folder_id"])
    _now_cov = _store_coverage(store_path)
    if _prev_cov and _now_cov and not args.allow_partial:
        _drop = [f"{k}: {_prev_cov[k]:,} last week → {_now_cov[k]:,} now"
                 for k in _now_cov
                 if _prev_cov.get(k, 0) >= 20
                 and _now_cov[k] < _prev_cov[k] * COVERAGE_FLOOR]
        if _drop:
            raise SystemExit(
                "Refusing to publish: session coverage collapsed — the previous "
                "store is left in place:\n  - " + "\n  - ".join(_drop)
                + "\nSessions do not vanish from the past, so this is a bug in the "
                  "fetch or in the derived-facts memo, not a fact about the week. "
                  "Re-run with --no-cache to rebuild straight from the Zoom "
                  "exports; pass --allow-partial to publish anyway.")
    if _prev_cov and _now_cov:
        print(f"   coverage: duration {_prev_cov['duration']}→{_now_cov['duration']}, "
              f"ratings {_prev_cov['ratings']}→{_now_cov['ratings']} (vs last week)",
              flush=True)

    # GATE 6 - the freeze's own safety net, and the only detector it has.
    #
    # With no weekly rebuild, a carry-forward that mismatches students writes
    # 'Absent' over real Presents and NOTHING would ever correct it. The one
    # observable symptom is a past session's present count going DOWN, which
    # cannot happen legitimately: the marks for a frozen column are copied, not
    # recomputed, and a re-marked column can only gain people. So any drop on a
    # session last week's store already published refuses the publish.
    #
    # A session column that DISAPPEARS is not gated here on purpose - an L2 row
    # being corrected legitimately hides one (REQUIRE_L2, §5.6), and gating that
    # would go red every time somebody fixed the schedule. It is reported.
    # _prev_cov is not None only when this run actually wrote _prev_store.duckdb,
    # so this can never compare against a leftover from an earlier local run.
    _prev_path = os.path.join(HERE, ".cache", PREV_STORE)
    if _prev_cov and os.path.exists(_prev_path):
        _pm, _nm = _store_marks(_prev_path), _store_marks(store_path)
        _pr, _nr = _store_roster(_prev_path), _store_roster(store_path)
        if not _pm or not _nm:
            # "the gate ran and found nothing" and "the gate did not run" must
            # never look identical in the log - not on a system where a bad
            # mark is permanent.
            print("   GATE 6: SKIPPED - no roster grids readable in "
                  + ("last week's" if not _pm else "this week's")
                  + " store, so the freeze's only detector did NOT run.",
                  flush=True)
        else:
            _lost, _gone = lost_marks(_pm, _nm, _pr, _nr)
            if _gone:
                print(f"   GATE 6: {len(_gone)} session column(s) last week's "
                      "store had are not in this one (an L2 row corrected, or a "
                      "Zoom export withdrawn): "
                      + ", ".join(f"{b} {c}" for b, c in _gone[:5]), flush=True)
            if _lost and not args.allow_partial:
                raise SystemExit(
                    "Refusing to publish: "
                    f"{len(_lost)} already-published session(s) LOST marks for "
                    "students who are in BOTH weeks' rosters — the previous "
                    "store is left in place:\n  - "
                    + "\n  - ".join(
                        f"{b} {c}: {was:,} → {now:,} present ({d:,} lost)"
                        for (b, c), was, now, d in _lost[:8])
                    + (f"\n  ... and {len(_lost) - 8} more"
                       if len(_lost) > 8 else "")
                    + "\nThe same students are in both weeks, so no roster edit "
                      "explains this: the carry-forward failed to match them. "
                      "Check the [1c] line for how many it matched, re-run "
                      "WITHOUT --incremental to re-mark from the Zoom exports, "
                      "or pass --allow-partial to publish anyway.")
            print(f"   GATE 6: {len(_pm):,} published session column(s) checked "
                  f"over the students both weeks share, {len(_lost)} lost marks",
                  flush=True)

    # The static website the team actually opens. Built from the store that was
    # just written, so the site can never disagree with the app.
    if not args.no_site:
        print("[7/8] Rendering the static site …", flush=True)
        import site_build
        site_build.build_site(store_path, os.path.join(HERE, "site"))

    # [8c] The derived-facts memo, written BEFORE the --no-upload return on
    # purpose. It is content-keyed and idempotent, so writing it on a dry run is
    # harmless — and it is the only way the warm path is ever exercised outside
    # a production publish. A memo that only ever gets written by the Monday job
    # is a code path whose first execution is also its first publish.
    _write_cache(svc, cfg, cache, cache_rules, cache_stats, args)

    if args.no_upload:
        print(f"[8/8] Skipped upload (--no-upload). Store at: {store_path}")
        return

    with open(store_path, "rb") as fh:
        store_bytes = fh.read()

    # A PARALLEL dataset publishes under its own name and STOPS. The early
    # return is the safety property, not a shortcut: [8a] writes next week's
    # frozen base and [8b] writes an immutable archive snapshot, and both of
    # them name the file by the REAL constants. A parallel run reaching either
    # one would hand the weekly record a workbook built from a different roster
    # — the exact corruption the STORE_OUT guard above exists to prevent, just
    # arriving one step later.
    if _parallel_name:
        print(f"[8/8] Uploading the PARALLEL store as {_parallel_name} …",
              flush=True)
        live_data.upload_to_folder(svc, cfg["store_folder_id"], _parallel_name,
                                   store_bytes)
        print(f"[8a] NOT updating the marked roster xlsx: a parallel dataset "
              f"never becomes next week's base.", flush=True)
        print(f"[8b] NOT archiving: the week's immutable snapshot belongs to "
              f"{STORE_NAME}.", flush=True)
        print(f"Done — parallel dataset published as {_parallel_name}. The "
              f"real dashboard is untouched.")
        return

    print("[8/8] Uploading the store …", flush=True)
    live_data.upload_to_folder(svc, cfg["store_folder_id"], STORE_NAME,
                               store_bytes)

    # [8a] The store is published, so the workbook behind it may now become next
    # week's base. This order matters: the base is the irreversible write of the
    # two, so it goes SECOND. If the store upload fails, the base simply lacks
    # this week's columns, their folders are fetched again next run and GATE 6
    # sees equal-or-greater counts — the failure heals itself. The other order
    # left the base ahead of the published data, which is the exact asymmetry
    # holding it back past the gates was meant to remove.
    #
    # Under --allow-partial the base is not written at all: that flag means this
    # week's marks are knowingly incomplete, and freezing them would turn a
    # deliberate one-off into permanent history.
    _base_published = False
    if args.allow_partial:
        print("[8a] NOT updating the marked roster xlsx: --allow-partial means "
              "this week's marks are knowingly incomplete, so they must not "
              "become next week's frozen base.", flush=True)
    else:
        print("[8a] Uploading the marked roster xlsx (next week's base) …",
              flush=True)
        marked_xlsx_file_id = live_data.upload_to_folder(
            svc, cfg["store_folder_id"], MARKED_NAME, marked_bytes, XLSX_MIME)
        _base_published = True

    # Dated snapshots of the four source Sheets (+ this store) into archive/ on
    # the same private Shared Drive. The store itself is REPLACED every week, so
    # without this there is no copy of anything: a sheet deleted on Tuesday is
    # gone from the dashboard by the next Monday with no way back. Every source
    # is owned by a different person outside this repo - see archive.py.
    #
    # Deliberately AFTER the upload and never fatal: the dashboard refreshing is
    # what people actually depend on, and a Drive hiccup here must not cost them
    # that. Failures are reported, not raised.
    print("[8b] Archiving the source sheets …", flush=True)
    try:
        # The dated marked snapshot is what §4g's restore procedure installs AS
        # the live base, and archive snapshots are immutable by design — so it
        # must never hold marks [8a] refused to publish. Skipping it under
        # --allow-partial keeps that restore trustworthy.
        archive.run(svc, cfg, cfg["store_folder_id"],
                    when=datetime.now(IST).date(), store_bytes=store_bytes,
                    marked_bytes=marked_bytes if _base_published else None,
                    # The unmarked API answer for this week. Archived even on a
                    # partial run: it is what the API said, and that is true
                    # regardless of whether the marking was complete.
                    lms_bytes=roster_bytes if lms_report else None)
    except Exception as e:
        print(f"   WARNING: archive step failed entirely ({e})")

    print(f"Done — dashboard data refreshed "
          f"({datetime.now(IST).strftime('%d %b %Y, %H:%M IST')}).")


if __name__ == "__main__":
    main()
