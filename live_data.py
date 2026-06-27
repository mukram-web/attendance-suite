"""
live_data.py — read the roster / L2 / attendee data straight from Google Drive.

The unified app uses this ONLY when live mode is configured (a service-account
key + Drive file IDs are present in Streamlit secrets). If anything here is
missing, the app silently falls back to manual file uploads — so the app always
runs, with or without Google set up.

Expected secrets (`.streamlit/secrets.toml`)::

    [gcp_service_account]
    type = "service_account"
    project_id = "..."
    private_key_id = "..."
    private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
    client_email = "robot@your-project.iam.gserviceaccount.com"
    client_id = "..."
    token_uri = "https://oauth2.googleapis.com/token"
    # ...the rest of the JSON key fields...

    [drive]
    roster_id       = "<google-sheet-file-id>"   # required
    l2_id           = "<google-sheet-file-id>"   # recommended (maps webinar -> batch)
    attendee_zip_id = "<zip-file-id>"            # the Zoom .zip you replace each week
    # OR, instead of a zip, a folder of attendee CSVs:
    attendee_folder_id = "<drive-folder-id>"

Only the `google-*` packages in requirements.txt are needed for this; they are
imported lazily so upload-only use never has to install them locally.
"""
from __future__ import annotations
import datetime
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor

# Drive/Sheets MIME types we care about
_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def config_present() -> bool:
    """True only if a service account AND at least a roster id are configured."""
    try:
        import streamlit as st
        if "gcp_service_account" not in st.secrets:
            return False
        drive = st.secrets.get("drive", {})
        return bool(drive.get("roster_id"))
    except Exception:
        return False


def _drive_service():
    """Build a read-only Drive v3 client from the service-account secret."""
    import streamlit as st
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _download(request) -> bytes:
    """Run a Drive media request to completion and return the bytes."""
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _file_meta(svc, file_id: str) -> dict:
    # supportsAllDrives=True so files living in a Shared Drive resolve too.
    return svc.files().get(fileId=file_id, fields="id, name, mimeType",
                           supportsAllDrives=True).execute()


def fetch_spreadsheet_xlsx(svc, file_id: str) -> bytes:
    """Return .xlsx bytes for a file id.

    If the id points at a native Google Sheet we EXPORT it to .xlsx; if it is an
    already-uploaded .xlsx we just download it. Either way the engines get bytes
    they can open with openpyxl.
    """
    meta = _file_meta(svc, file_id)
    if meta.get("mimeType") == _SHEET_MIME:
        req = svc.files().export_media(fileId=file_id, mimeType=_XLSX_MIME)
    else:
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    return _download(req)


def fetch_file_bytes(svc, file_id: str) -> bytes:
    """Download any binary file (e.g. the attendee .zip) as raw bytes."""
    return _download(svc.files().get_media(fileId=file_id, supportsAllDrives=True))


# Per-thread Drive client — googleapiclient's http transport is not safe to share
# across threads, so each worker builds its own (cheap; discovery is bundled).
_thread_local = threading.local()


def _thread_drive():
    svc = getattr(_thread_local, "svc", None)
    if svc is None:
        svc = _drive_service()
        _thread_local.svc = svc
    return svc


def _list_children(svc, folder_id: str) -> list[dict]:
    """One folder's immediate children (id, name, mimeType), Shared-Drive aware."""
    items, page_token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000, pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        items += resp.get("files", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def _existing_sessions(roster_bytes: bytes) -> dict:
    """{batch_key -> set(mm_dd)} of session columns already present in the roster,
    so we can skip re-fetching sessions that are already marked."""
    import attendance_core as ac
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(roster_bytes), data_only=True)
    out: dict = {}
    for name in wb.sheetnames:
        k = ac._sheet_key(name)
        if not k:
            continue
        ws = wb[name]
        hr = ac._header_row(ws)
        dates = set()
        for c in range(11, (ws.max_column or 1) + 1):
            d = ac._mmdd(ws.cell(1, c).value) or (ac._mmdd(ws.cell(2, c).value) if hr == 2 else None)
            if d:
                dates.add(d)
        out[k] = dates
    wb.close()
    return out


def fetch_new_attendees(svc, folder_id: str, roster_bytes: bytes,
                        mark_all: bool = False, max_workers: int = 12):
    """Download attendee files only for sessions NOT already marked in the roster.

    The Shared Drive holds the full history (hundreds of dated session folders),
    but the roster already carries every past weekend's marks. So we list the
    top-level dated folders, keep only those whose (batch, date) is missing from
    the roster, and download just those — in parallel. `mark_all=True` ignores the
    skip logic and pulls everything (a full rebuild). Returns (attendee_files, info).
    """
    import io as _io
    import zipfile
    import attendance_core as ac

    existing = _existing_sessions(roster_bytes)
    sheet_keys = set(existing)

    def keep(name: str) -> bool:
        n = name.lower()
        return n.startswith("attendee") or n.endswith(".zip")

    # 1) pick the top-level folders that need marking
    to_fetch, skipped_nosheet, skipped_done = [], 0, 0
    for f in _list_children(svc, folder_id):
        if f["mimeType"] != _FOLDER_MIME:
            continue
        name = f["name"]
        mm = ac._mmdd(name)
        covered = [k for k in ac._folder_batches(name) if k in sheet_keys]
        if not covered:
            skipped_nosheet += 1
            continue
        if mark_all or mm is None or any(mm not in existing[k] for k in covered):
            to_fetch.append((name, f["id"]))
        else:
            skipped_done += 1

    # 2) collect the attendee files inside those folders
    entries = []  # (path, file_id, is_zip)
    for name, fid in to_fetch:
        for f in _list_children(svc, fid):
            if f["mimeType"] != _FOLDER_MIME and keep(f["name"]):
                entries.append((f"{name}/{f['name']}", f["id"],
                                f["name"].lower().endswith(".zip")))

    # 3) download in parallel
    out: list[tuple[str, bytes]] = []
    if entries:
        def _dl(e):
            path, fid, is_zip = e
            data = _download(_thread_drive().files().get_media(
                fileId=fid, supportsAllDrives=True))
            return path, is_zip, data
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for path, is_zip, data in ex.map(_dl, entries):
                if is_zip:
                    prefix = (path.rsplit("/", 1)[0] + "/") if "/" in path else ""
                    with zipfile.ZipFile(_io.BytesIO(data)) as z:
                        for inner in z.namelist():
                            if not inner.endswith("/"):
                                out.append((f"{prefix}{inner}", z.read(inner)))
                else:
                    out.append((path, data))

    info = dict(new_folders=len(to_fetch), files=len(out),
                skipped_already_marked=skipped_done, skipped_no_sheet=skipped_nosheet)
    return out, info


def load_live():
    """Pull everything the marker needs from Drive.

    Returns a dict::
        {
          "roster_bytes": bytes,
          "l2_bytes": bytes | None,
          "attendee_files": [(name, bytes), ...] | None,   # None if no zip/folder set
          "source": "<human description>",
        }
    Raises on any Drive/auth error so the caller can show a clear message and
    offer the upload fallback.
    """
    import streamlit as st
    drive = st.secrets["drive"]
    svc = _drive_service()

    roster_bytes = fetch_spreadsheet_xlsx(svc, drive["roster_id"])

    l2_bytes = None
    if drive.get("l2_id"):
        l2_bytes = fetch_spreadsheet_xlsx(svc, drive["l2_id"])

    attendee_files = None
    info = {}
    source_bits = ["roster"]
    if drive.get("attendee_zip_id"):
        import zipfile
        zb = fetch_file_bytes(svc, drive["attendee_zip_id"])
        with zipfile.ZipFile(io.BytesIO(zb)) as z:
            attendee_files = [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
        source_bits.append(f"{len(attendee_files)} files from attendee .zip")
    elif drive.get("attendee_folder_id"):
        mark_all = bool(drive.get("mark_all", False))
        attendee_files, info = fetch_new_attendees(
            svc, drive["attendee_folder_id"], roster_bytes, mark_all=mark_all)
        if mark_all:
            source_bits.append(f"{info['files']} files (full rebuild)")
        else:
            source_bits.append(f"{info['files']} file(s) from {info['new_folders']} new session(s)")

    return {
        "roster_bytes": roster_bytes,
        "l2_bytes": l2_bytes,
        "attendee_files": attendee_files,
        "info": info,
        "source": "Google Drive — " + ", ".join(source_bits),
    }
