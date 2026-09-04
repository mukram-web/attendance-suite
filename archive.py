"""
archive.py — dated snapshots of the source Sheets, so a deletion is survivable.

THE PROBLEM
-----------
Every number this project produces is derived from four Google Sheets that the
robot does not own. Measured 2026-09-04 they belong to FOUR DIFFERENT individuals
— one of them a personal gmail rather than a company account. Their addresses are
not listed here because this repository is public; to see who currently owns each,
ask Drive:

    svc.files().get(fileId=<id>, fields='name,owners',
                    supportsAllDrives=True).execute()

Any one of those owners can delete a tab, restructure a column, or bin the file, and the
next Monday run simply rebuilds without it. **The store is not a backup**: the
pipeline REPLACES attendance.duckdb in place each week, so the moment a source
disappears the derived history goes with it on the following run. There is no
undo anywhere in the current design.

L2 is the worst case. It is the register of what actually ran — 1,235 webinars
back to Feb 2025, with the topic and mentor for each — and `data.REQUIRE_L2`
means a session with no L2 row is not merely unlabelled but *hidden*. Lose L2 and
the dashboard goes blank, not stale.

WHAT THIS DOES
--------------
Once a week, before anything else can fail, copy each source Sheet as a dated
.xlsx into an `archive/` folder on the private Shared Drive, plus the store that
was built from them. Never overwrite: a snapshot is named for its date and is
skipped if it already exists, so re-running the pipeline on the same day costs
one cheap existence check and cannot clobber the morning's copy.

WHERE IT GOES, AND WHY THAT MATTERS
-----------------------------------
The PRIVATE Shared Drive that already holds the store — never the repo, which is
public, and never a local path that gets committed by accident. The roster
snapshot is every student's name, email and phone. Treat these files exactly like
the store: same drive, same access list, same care.
"""
from __future__ import annotations

from datetime import date

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
ARCHIVE_FOLDER = "archive"

# What gets snapshotted, in priority order. `key` is the config key holding the
# Sheet id; a missing or unconfigured id is skipped, not an error — BSIAI and the
# curriculum are both optional and their absence must not fail the run.
SHEETS = [
    ("l2", "l2_id", "L2_Weekly_Live_Sessions"),
    ("roster", "roster_id", "Master_Batch_Rosters"),
    ("curriculum", "curriculum_id", "Master_Curriculum_Schedule"),
    ("bsiai", "bsiai_roster_id", "BSIAI_Roster"),
]


def snapshot_name(label: str, when: date, ext: str = "xlsx") -> str:
    """`Master_Batch_Rosters_2026-09-04.xlsx` — sorts chronologically by name."""
    return f"{label}_{when.isoformat()}.{ext}"


def ensure_folder(svc, parent_id: str, name: str = ARCHIVE_FOLDER) -> str:
    """The archive subfolder's id, creating it the first time.

    Looked up by name rather than remembered in config: one less id to keep in
    sync, and a human who moves or renames it gets a new empty folder rather than
    a crash. Shared-Drive aware — a service account cannot create folders
    anywhere else, since it has no storage quota of its own.
    """
    resp = svc.files().list(
        q=(f"'{parent_id}' in parents and name = '{name}' "
           f"and mimeType = '{FOLDER_MIME}' and trashed = false"),
        fields="files(id)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    hit = resp.get("files", [])
    if hit:
        return hit[0]["id"]
    return svc.files().create(
        body={"name": name, "parents": [parent_id], "mimeType": FOLDER_MIME},
        fields="id", supportsAllDrives=True).execute()["id"]


def run(svc, cfg: dict, store_folder_id: str, when: date | None = None,
        store_bytes: bytes | None = None, log=print) -> dict:
    """Snapshot every configured source Sheet (+ optionally the store).

    Returns {"saved": [...], "skipped": [...], "errors": [...]}. NEVER raises:
    losing an archive is bad, but failing the weekly refresh over it would be
    worse — the dashboard is what people actually open. The caller decides how
    loudly to report `errors`.
    """
    import live_data

    when = when or date.today()
    out = {"saved": [], "skipped": [], "errors": [], "folder_id": None}
    try:
        folder = ensure_folder(svc, store_folder_id)
        out["folder_id"] = folder
    except Exception as e:
        out["errors"].append(f"could not open the archive folder: {e}")
        return out

    def _put(name: str, data: bytes, mime: str) -> None:
        # Existence check first: snapshots are immutable, so re-running on the
        # same day must not re-upload 7 MB, and must never replace a copy taken
        # before someone edited the sheet later that day.
        try:
            if live_data.find_in_folder(svc, folder, name):
                out["skipped"].append(name)
                return
            live_data.upload_to_folder(svc, folder, name, data, mime)
            out["saved"].append(f"{name} ({len(data) / 1e6:.1f} MB)")
        except Exception as e:
            out["errors"].append(f"{name}: {e}")

    for _key, cfg_key, label in SHEETS:
        fid = (cfg or {}).get(cfg_key) or ""
        if not fid:
            out["skipped"].append(f"{label} (not configured)")
            continue
        name = snapshot_name(label, when)
        try:
            if live_data.find_in_folder(svc, folder, name):
                out["skipped"].append(name)
                continue
            # Same cached export the rest of the pipeline uses, so archiving
            # costs no extra download on a normal run.
            data, _stamp = live_data.fetch_sheet_cached(svc, fid)
        except Exception as e:
            out["errors"].append(f"{label}: {e}")
            continue
        _put(name, data, XLSX_MIME)

    if store_bytes:
        _put(snapshot_name("attendance_store", when, "duckdb"),
             store_bytes, "application/octet-stream")

    for line in out["saved"]:
        log(f"   archived {line}")
    if out["skipped"]:
        log(f"   {len(out['skipped'])} already archived / not configured")
    for e in out["errors"]:
        log(f"   WARNING: archive - {e}")
    return out
