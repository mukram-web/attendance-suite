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
                           day1 (the Day-1 analysis for the newest batches —
                           aggregates only)
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
import dashboard_core as dc           # noqa: E402
import data as ddata                  # noqa: E402
import day1_analysis                  # noqa: E402
import sheets as dsheets              # noqa: E402
import live_data                      # noqa: E402

# How many of the newest batches the day-1 dashboard covers. The window rolls by
# itself: a new batch tab joins, the oldest drops off.
DAY1_BATCHES = 4

IST = timezone(timedelta(hours=5, minutes=30))
STORE_NAME = "attendance.duckdb"
MARKED_NAME = "Master_Batch_Rosters_marked.xlsx"
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

    missing = [k for k in ("roster_id", "attendee_folder_id") if not cfg[k]]
    if missing:
        raise SystemExit(f"Missing config: {', '.join(missing)} "
                         "(env vars or .streamlit/secrets.toml [drive])")
    return cfg


def _looks_like_sa_key(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(2048)
        return '"type": "service_account"' in head
    except OSError:
        return False


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
        })


def build_store(path: str, marked_bytes: bytes, report, warnings, source: str,
                l2_bytes, attendee_names, marked_xlsx_file_id: str,
                stamps: dict, day1: dict | None = None) -> dict:
    """Write attendance.duckdb next to nothing else — one self-contained file."""
    # dashboard DATA/summary (same code path the app used to run at startup)
    wb = load_workbook(io.BytesIO(marked_bytes), read_only=True, data_only=True)
    tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets}
    wb.close()
    topics = dsheets.webinar_topic_lookup([(n, None) for n in attendee_names],
                                          l2_bytes)
    DATA, summary = ddata.build(tabs, topics)
    _prepend_intro_sessions(DATA)

    df = dc.compute(marked_bytes)
    smap = dc.batch_sheet_map(marked_bytes)
    # only real batch tabs — helper tabs like 'l2 cx data' or 'Auto pay' pass
    # dashboard_core's loose sheet filter but are not batches
    batches = sorted((b for b in df["Batch"].unique()
                      if b in smap and re.fullmatch(r"B\d+", str(b))),
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
        "day1": day1 or {"batches": [], "skipped": []},
    }
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)",
                    [(k, json.dumps(v)) for k, v in meta.items()])
    con.close()
    return {"batches": len(batches), "students": sum(grid_rows.values()),
            "sessions": summary["sessions"]}


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
    ap.add_argument("--mode", default="exact", choices=["exact", "inclusive"],
                    help="marker matching rule (default: exact, same as the app)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="publish even if some attendee files/folders failed "
                         "(default: refuse, so a complete store is never "
                         "overwritten by a partial one)")
    args = ap.parse_args()

    cfg = load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=RW_SCOPES)
    svc = live_data._drive_service()

    print("[1/7] Fetching roster + L2 …", flush=True)
    roster_bytes, roster_stamp = live_data.fetch_sheet_cached(svc, cfg["roster_id"])
    l2_bytes, l2_stamp = (live_data.fetch_sheet_cached(svc, cfg["l2_id"])
                          if cfg["l2_id"] else (None, ""))
    print(f"   roster {len(roster_bytes):,} bytes (modified {roster_stamp})")

    print("[2/7] Fetching attendee reports (new sessions only) …", flush=True)
    attendee_files, info = live_data.fetch_new_attendees(
        svc, cfg["attendee_folder_id"], roster_bytes)
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
        problems.append(f"{info['failed']} attendee file(s) failed to download")
    if problems and not args.allow_partial:
        raise SystemExit("Refusing to publish partial data — the previous store is "
                         "left in place:\n  - " + "\n  - ".join(problems)
                         + "\nRe-run the job; pass --allow-partial to override.")

    print("[3/7] Marking attendance …", flush=True)
    if attendee_files:
        marked_bytes, report, warnings = ac.process_files(
            roster_bytes, l2_bytes, attendee_files, mode=args.mode,
            values_only=True)
    else:
        marked_bytes, report, warnings = roster_bytes, [], []
    new_n = sum(1 for r in report if r["kind"] == "NEW")
    print(f"   {len(report)} session column(s) marked "
          f"({new_n} new) · {len(warnings)} warning(s)")

    if not args.no_upload and not cfg["store_folder_id"]:
        raise SystemExit("STORE_FOLDER_ID missing — where should the store "
                         "be uploaded? (or run with --no-upload)")

    # Day-1 analysis runs BEFORE anything is uploaded: if it refuses below, this
    # run must have left Drive exactly as it found it.
    print(f"[4/7] Day-1 analysis (newest {DAY1_BATCHES} batches) …", flush=True)
    day1 = {"batches": [], "skipped": [], "errors": []}
    try:
        wb_r = load_workbook(io.BytesIO(marked_bytes), read_only=True, data_only=True)
        roster_tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
                       for ws in wb_r.worksheets
                       if re.fullmatch(r"\s*AI\s*CAP\s*B\d+\s*", ws.title, re.I)}
        wb_r.close()
        day1 = day1_analysis.build(svc, roster_tabs, l2_bytes,
                                   cfg["attendee_folder_id"],
                                   n_batches=DAY1_BATCHES)
    except Exception as e:
        day1 = {"batches": [], "skipped": [], "errors": [f"analysis failed: {e}"]}

    # Same rule as the attendee gate above: a transiently broken day-1 analysis
    # must not overwrite last week's complete tab with an empty one and go green.
    if (day1.get("errors") or not day1["batches"]) and not args.allow_partial:
        raise SystemExit(
            "Refusing to publish: the day-1 analysis is incomplete — the previous "
            "store is left in place:\n  - "
            + "\n  - ".join(day1.get("errors") or ["no batches produced"])
            + "\nRe-run the job; pass --allow-partial to publish without it.")

    marked_xlsx_file_id = ""
    if not args.no_upload:
        print("[5/7] Uploading marked roster xlsx …", flush=True)
        marked_xlsx_file_id = live_data.upload_to_folder(
            svc, cfg["store_folder_id"], MARKED_NAME, marked_bytes, XLSX_MIME)
    elif cfg["store_folder_id"]:
        # --no-upload: keep pointing at the xlsx a previous full run left on
        # Drive, so a locally built store doesn't lose the download button if it
        # is copied into the store folder by hand.
        try:
            prev = live_data.find_in_folder(svc, cfg["store_folder_id"], MARKED_NAME)
            marked_xlsx_file_id = prev["id"] if prev else ""
        except Exception:
            marked_xlsx_file_id = ""

    print("[6/7] Building the DuckDB store …", flush=True)
    names = live_data.list_attendee_names(cfg["attendee_folder_id"])
    source = (f"Google Drive — roster, {info['files']} file(s) from "
              f"{info['new_folders']} new session(s)")
    os.makedirs(os.path.join(HERE, ".cache"), exist_ok=True)
    store_path = os.path.join(HERE, ".cache", STORE_NAME)
    stats = build_store(store_path, marked_bytes, report, warnings, source,
                        l2_bytes, names, marked_xlsx_file_id,
                        {"roster": roster_stamp, "l2": l2_stamp, "mode": args.mode},
                        day1=day1)
    size = os.path.getsize(store_path)
    print(f"   {stats['batches']} batches · {stats['students']:,} students · "
          f"{stats['sessions']} sessions · {size / 1e6:.1f} MB")

    if args.no_upload:
        print(f"[7/7] Skipped upload (--no-upload). Store at: {store_path}")
        return

    print("[7/7] Uploading the store …", flush=True)
    with open(store_path, "rb") as fh:
        live_data.upload_to_folder(svc, cfg["store_folder_id"], STORE_NAME,
                                   fh.read())
    print(f"Done — dashboard data refreshed "
          f"({datetime.now(IST).strftime('%d %b %Y, %H:%M IST')}).")


if __name__ == "__main__":
    main()
