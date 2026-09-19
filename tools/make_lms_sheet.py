"""Create (or refresh) the Google Sheet that REPLACES the Master Batch Rosters.

The whole migration comes down to one swap: `roster_id` stops pointing at the
hand-maintained Sheet and points at this one instead. Nothing else changes —
`pipeline.py` reads it exactly as before, `attendance_core` marks into its copy,
`carryforward` freezes what is already marked, and the dashboard is built from
the result. `roster_source` stays "sheet", because this IS a sheet.

TWO DECISIONS BAKED IN, BOTH DELIBERATE
---------------------------------------
**1. No session columns.** The roster carries people, not marks. Present/Absent
lives in `Master_Batch_Rosters_marked.xlsx` and is re-attached every run by
`carryforward` — which is already how every batch from B29 on works. Writing ~570
session columns in here instead would push the file from ~3.6 MB to ~9.4 MB,
straight back into Google's 10 MB xlsx-export ceiling that this migration exists
to escape (CLAUDE.md §7b.6).

**2. Everyone already marked is kept.** By default the population is the LMS API
PLUS anyone already present in the marked workbook. Measured 2026-09-15: 9,017
people across B17-B39 are in the roster but not in their own API batch. Build
this Sheet from the API alone and `carryforward` counts them `unmatched_prev` and
bins their attendance history. `--pure` does exactly that, for a side-by-side
dataset only — never for the Sheet that becomes the system of record.

THIS SHEET IS STUDENT PII. It is created on the private Shared Drive that already
holds the store, the marked workbook and the archive, so it inherits that access
list. Never move it somewhere broader.

Run:
    python tools/make_lms_sheet.py --dry-run          # build + report, create nothing
    python tools/make_lms_sheet.py                    # create/refresh the Sheet
    python tools/make_lms_sheet.py --name "AI CAP Roster (LMS)"
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import lms_client                                       # noqa: E402
import lms_roster as lr                                 # noqa: E402
import live_data                                        # noqa: E402
import pipeline                                         # noqa: E402

SHEET_MIME = "application/vnd.google-apps.spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DEFAULT_NAME = "AI CAP Roster (LMS)"


def build_rows(cfg, svc, pure: bool, cache_dir: str, log=print):
    """The roster workbook bytes, plus a per-batch report."""
    key = lms_client.api_key(cfg.get("lms_api_key", ""))
    log("[1] LMS batches …")
    grouped = lr.group_batches(lms_client.fetch_batches(key))
    log(f"    {len(grouped)} CAP batches")

    retained = {}
    if not pure:
        log("[2] last week's marked workbook (so nobody loses their marks) …")
        prev = pipeline.fetch_prev_marked(svc, cfg, "building from the API alone")
        retained = lr.read_previous(prev) if prev else {}
        log(f"    retained {sum(len(v) for v in retained.values()):,} people "
            f"across {len(retained)} batches")
    else:
        log("[2] --pure: the API alone decides who is enrolled.")

    # EVERY batch is refreshed here, not just the live cohorts. The weekly
    # freeze (§4h) is about MARKS — a session is counted once and never
    # recomputed. This Sheet is the enrolment source, and the whole point of the
    # swap is that its contents come from the LMS, so payment, closing type and
    # POD are pulled fresh for every batch the API knows.
    want = [c for c in sorted(grouped, key=lr._num) if lr._num(c) >= lr.FIRST_BATCH]
    log(f"[3] fetching {len(want)} batch(es) from the API …")
    api, failed = {}, []
    for i, code in enumerate(want, 1):
        try:
            recs = [(b, lms_client.cached_customers(key, b["id"], cache_dir or None))
                    for b in grouped.get(code, [])]
        except Exception as e:
            # The API 504s under load. Losing one batch must not throw away the
            # other 25 after ten minutes of fetching — collect and report.
            failed.append(code)
            log(f"    [{i}/{len(want)}] {code}: FAILED ({type(e).__name__}) — "
                f"it will keep last week's rows")
            continue
        api[code] = recs
        log(f"    [{i}/{len(want)}] {code}: {sum(len(c) for _b, c in recs):,}")
    if failed:
        log(f"    !! {len(failed)} batch(es) could not be read: "
            f"{', '.join(failed)}. Re-run to pick them up (the 504s are "
            f"load-dependent), or the Sheet will carry stale rows for them.")

    log("[4] building the workbook …")
    data, report = lr.build_from(retained, api, want)   # want == live -> all refresh
    return data, report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--parent", default="", help="Shared Drive / folder id "
                                                 "(default: store_folder_id)")
    ap.add_argument("--pure", action="store_true",
                    help="API only — DROPS people the API does not know, and "
                         "their attendance history with them. Side-by-side use.")
    ap.add_argument("--cache", default="", help="reuse cached /customers payloads")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and report, create nothing on Drive")
    ap.add_argument("--out", default="", help="also save the xlsx locally")
    a = ap.parse_args()

    cfg = pipeline.load_config()
    live_data.set_service_account(cfg["sa_info"], scopes=pipeline.RW_SCOPES)
    svc = live_data._drive_service()

    data, report = build_rows(cfg, svc, a.pure, a.cache)
    print(f"\n    {lr.summary(report)}")
    for code in sorted(report["batches"], key=lr._num):
        st = report["batches"][code]
        print(f"      {code:<5} {st['total']:>6} people "
              f"(retained {st['retained']:,}, +{st['added']:,} new from API, "
              f"{st['updated']:,} refreshed)")
    for w in report.get("warnings") or ():
        print(f"    WARNING: {w}")

    if a.out:
        open(a.out, "wb").write(data)
        print(f"\n    saved {a.out} ({len(data)/1e6:.1f} MB)")

    if a.dry_run:
        print("\n--dry-run: nothing was created on Drive.")
        return

    parent = a.parent or cfg["store_folder_id"]
    if not parent:
        sys.exit("No --parent and no store_folder_id: nowhere safe to put it. "
                 "A service account has no Drive storage of its own, so this "
                 "must live on a Shared Drive.")

    from googleapiclient.http import MediaIoBaseUpload
    import io
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=XLSX_MIME, resumable=True)
    existing = live_data.find_in_folder(svc, parent, a.name)
    if existing:
        # Replace IN PLACE so the file id survives: roster_id is configured to
        # that id, and a new file would silently orphan every consumer.
        print(f"\n[5] refreshing the existing Sheet in place …")
        fid = svc.files().update(fileId=existing["id"], media_body=media,
                                 supportsAllDrives=True).execute().get("id",
                                                                       existing["id"])
        verb = "refreshed"
    else:
        print(f"\n[5] creating the Sheet on the private Shared Drive …")
        fid = svc.files().create(
            body={"name": a.name, "parents": [parent], "mimeType": SHEET_MIME},
            media_body=media, fields="id", supportsAllDrives=True).execute()["id"]
        verb = "created"

    print(f"    {verb}: {a.name}")
    print(f"    id : {fid}")
    print(f"    url: https://docs.google.com/spreadsheets/d/{fid}/edit")
    print(f"\nPoint the roster at it:")
    print(f"    .streamlit/secrets.toml   [drive] roster_id = \"{fid}\"")
    print(f"    GitHub Actions secret     ROSTER_ID = {fid}")
    print("Leave roster_source as \"sheet\" — this IS the roster sheet now.")


if __name__ == "__main__":
    main()
