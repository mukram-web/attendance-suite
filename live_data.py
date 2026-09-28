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
import hashlib
import io
import os
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

# Zoom export bytes are cached on disk so restarts skip re-downloading hundreds
# of CSVs. This used to say "attendee files never change once uploaded" and key
# purely on the file id — a claim, not a check, and false: Drive's "Manage
# versions → Upload new version" keeps the id, and that is the documented remedy
# for a bad Zoom export (CLAUDE.md §7b.1). The listing paths now key on
# (id, md5-or-modifiedTime) via _sig_path, so a replaced file misses the cache.
# download_cached() below still keys on the bare id, and that is safe for only
# ONE of fetch_store_snapshot's two callers. The app reads archive/ snapshots,
# which are immutable once written. But derived_cache.load fetches the LIVE
# derived_facts.json.gz, which derived_cache.save replaces IN PLACE through
# upload_to_folder - same id for ever - so a warm .cache/attendee/<id> serves
# the first memo that machine ever downloaded. fetch_file_bytes exists for
# exactly this reason; see pipeline._previous_coverage.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "attendee")
_SHEET_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "sheets")

# Never let a single Drive request hang the whole app forever (the #1 cause of an
# app "stuck loading"). Any socket with no progress for this long raises instead.
socket.setdefaulttimeout(60)

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


# Optional credential override so this module also runs OUTSIDE Streamlit
# (pipeline.py on GitHub Actions). When unset, credentials come from st.secrets.
_CREDS_INFO: dict | None = None
_CREDS_SCOPES: list | None = None


def set_service_account(info: dict, scopes: list | None = None) -> None:
    """Use an explicit service-account JSON dict (and optionally wider scopes,
    e.g. drive read-write for the pipeline's uploads) instead of st.secrets."""
    global _CREDS_INFO, _CREDS_SCOPES
    _CREDS_INFO = dict(info)
    _CREDS_SCOPES = list(scopes) if scopes else None


def _drive_service():
    """Build a Drive v3 client — from the override if set, else st.secrets."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if _CREDS_INFO is not None:
        info, scopes = dict(_CREDS_INFO), (_CREDS_SCOPES or _SCOPES)
    else:
        import streamlit as st
        info, scopes = dict(st.secrets["gcp_service_account"]), _SCOPES
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def rw_service():
    """A Drive client that may WRITE, built WITHOUT changing the module default.

    `_SCOPES` is read-only on purpose: the app reads a prebuilt store and must
    not be able to alter anything on Drive (§6 — this repository is public and
    the store holds student PII). The "Add data" tab is the one exception, and
    it needs an explicit, local widening rather than `set_service_account`,
    which mutates module globals and would hand write access to every other
    code path in the process for the life of the server.

    Without this the tab could not work at all: `upload_to_folder` on a
    read-only client is a 403, and the failure would surface only after the
    user had already chosen their files.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    if _CREDS_INFO is not None:
        info = dict(_CREDS_INFO)
    else:
        import streamlit as st
        info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"])
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


def fetch_sheet_cached(svc, file_id: str) -> tuple[bytes, str]:
    """Like fetch_spreadsheet_xlsx, but disk-cached by the file's modifiedTime.

    Google's Sheet→xlsx export is NON-deterministic — exporting the same
    unchanged sheet twice returns different bytes — so downstream caches must
    key on the returned `stamp` (the Drive modifiedTime), never on the bytes.
    While the stamp is unchanged we also skip the export download entirely.
    Returns (xlsx_bytes, stamp)."""
    meta = svc.files().get(fileId=file_id, fields="modifiedTime, mimeType",
                           supportsAllDrives=True).execute()
    stamp = meta.get("modifiedTime", "")
    cpath = os.path.join(_SHEET_CACHE,
                         f"{file_id}_{re.sub(r'[^0-9A-Za-z]', '', stamp)}.xlsx")
    try:
        with open(cpath, "rb") as fh:
            return fh.read(), stamp
    except OSError:
        pass
    if meta.get("mimeType") == _SHEET_MIME:
        req = svc.files().export_media(fileId=file_id, mimeType=_XLSX_MIME)
    else:
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    try:
        data = _download(req)
    except Exception as e:
        # Drive refuses to export a Google Sheet whose xlsx form exceeds 10 MB.
        # The roster is already ~8 MB and grows with every batch, so name the
        # cause instead of leaving a bare 403 in the logs.
        if "exportSizeLimitExceeded" in str(e) or "too large" in str(e).lower():
            raise RuntimeError(
                f"Google refused to export sheet {file_id} as .xlsx — it has grown "
                "past Drive's 10 MB export limit. The roster must be split (e.g. "
                "archive old batches into a separate sheet) before the pipeline "
                "can read it again."
            ) from e
        raise
    try:
        os.makedirs(_SHEET_CACHE, exist_ok=True)
        for old in os.listdir(_SHEET_CACHE):        # evict stale stamps of this file
            if old.startswith(file_id + "_"):
                try:
                    os.remove(os.path.join(_SHEET_CACHE, old))
                except OSError:
                    pass
        tmp = cpath + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, cpath)
    except OSError:
        pass
    return data, stamp


# Per-thread Drive client — googleapiclient's http transport is not safe to share
# across threads, so each worker builds its own (cheap; discovery is bundled).
_thread_local = threading.local()


def _thread_drive():
    svc = getattr(_thread_local, "svc", None)
    if svc is None:
        svc = _drive_service()
        _thread_local.svc = svc
    return svc


def _folder_ids(folder_id) -> list[str]:
    """Split a comma-separated attendee-folder config value into individual ids.
    Sessions can live on more than one Shared Drive (the original one plus the
    'Zoom extracts' drive new sessions are written to since Aug 2026)."""
    return [p.strip() for p in str(folder_id or "").split(",") if p.strip()]


def _list_children(svc, folder_id: str) -> list[dict]:
    """One folder's immediate children (id, name, mimeType), Shared-Drive aware."""
    items, page_token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            # `size` matters: duplicate session folders exist on this drive and
            # callers break the tie on it. Drive only returns requested fields,
            # so leaving it out silently makes every file look 0 bytes.
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=1000, pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        items += resp.get("files", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def list_attendee_names(folder_id: str | None = None, l2_dates=None,
                        exempt_dates=None) -> list:
    """All attendee filenames across the Shared Drive — names only, no downloads.

    One Drive query (`name contains 'attendee_'`) over the whole Shared Drive, so
    it's fast even with 500+ session folders. The filenames carry the Webinar ID
    and date, which is all the dashboard's topic join needs. Falls back to a
    recursive name-only walk for a plain (non-Shared-Drive) folder.

    `l2_dates` / `exempt_dates` apply the L2 gate to the names (l2_gate_reason);
    the default is no gate, which is what the app's legacy path passes.
    """
    fid = folder_id
    if fid is None:
        import streamlit as st
        fid = st.secrets["drive"].get("attendee_folder_id")
    if not fid:
        return []
    svc = _drive_service()
    names: list = []
    for one in _folder_ids(fid):
        if one.startswith("0A"):                        # Shared Drive root → whole-drive search
            token = None
            while True:
                resp = svc.files().list(
                    q="name contains 'attendee_' and trashed = false",
                    corpora="drive", driveId=one,
                    includeItemsFromAllDrives=True, supportsAllDrives=True,
                    fields="nextPageToken, files(name)", pageSize=1000, pageToken=token,
                ).execute()
                names += [f["name"] for f in resp.get("files", [])]
                token = resp.get("nextPageToken")
                if not token:
                    break
        else:                                           # plain folder → recurse names only
            def walk(f):
                for c in _list_children(svc, f):
                    if c["mimeType"] == _FOLDER_MIME:
                        walk(c["id"])
                    elif "attendee" in c["name"].lower():
                        names.append(c["name"])
            walk(one)
    # The topic join keys on webinar id alone, so a day-early twin here would
    # only hand the real session its own topic - but a room L2 never
    # registered would still be offered a name. Same gate, same scope.
    return apply_l2_gate(names, l2_dates, exempt_dates, stage="attendee names",
                         name_of=lambda n: n)[0]


def find_in_folder(svc, folder_id: str, name: str) -> dict | None:
    """First non-trashed file with this exact name inside the folder (Shared-Drive
    aware). Returns its metadata dict or None."""
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    resp = svc.files().list(
        q=f"'{folder_id}' in parents and name = '{safe}' and trashed = false",
        fields="files(id, name, mimeType, modifiedTime, size)", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None


def upload_to_folder(svc, folder_id: str, name: str, data: bytes,
                     mime: str = "application/octet-stream") -> str:
    """Create-or-replace `name` inside the folder with these bytes; returns the
    file id. Needs the service account to have Editor access on the folder and
    a read-write Drive scope (see set_service_account)."""
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=True)
    existing = find_in_folder(svc, folder_id, name)
    if existing:
        return svc.files().update(fileId=existing["id"], media_body=media,
                                  supportsAllDrives=True).execute().get("id", existing["id"])
    meta = {"name": name, "parents": [folder_id]}
    return svc.files().create(body=meta, media_body=media, fields="id",
                              supportsAllDrives=True).execute()["id"]


def _download_any(svc, file_id: str) -> bytes:
    """get_media, falling back to a text/csv export for Google-native files —
    attendee reports occasionally get converted to a Google Sheet on upload,
    which the binary endpoint refuses with 403 fileNotDownloadable."""
    try:
        return _download(svc.files().get_media(fileId=file_id,
                                               supportsAllDrives=True))
    except Exception as e:
        if "fileNotDownloadable" not in str(e):
            raise
        return _download(svc.files().export_media(fileId=file_id,
                                                  mimeType="text/csv"))


def download_cached(svc, file_id: str) -> bytes:
    """Download a Drive file, reusing the per-file-id byte cache.

    Correct ONLY for files that are never replaced under the same id - the
    archive/ snapshots. It is also reached, via fetch_store_snapshot, from
    derived_cache.load, whose file IS replaced in place; use fetch_file_bytes
    when the bytes behind an id can change."""
    cpath = os.path.join(_CACHE_DIR, file_id)
    try:
        with open(cpath, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    data = _download_any(svc, file_id)
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = cpath + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, cpath)
    except OSError:
        pass
    return data


def fetch_store(store_folder_id: str, name: str = "attendance.duckdb"):
    """Download the prebuilt dashboard store from the Drive folder the pipeline
    writes to. Returns (bytes, modifiedTime) or (None, None) if absent."""
    svc = _drive_service()
    meta = find_in_folder(svc, store_folder_id, name)
    if not meta:
        return None, None
    data = _download(svc.files().get_media(fileId=meta["id"], supportsAllDrives=True))
    return data, meta.get("modifiedTime", "")


def store_exists(store_folder_id: str, name: str) -> bool:
    """Is a store with this name present in the Drive folder? A metadata lookup,
    never a download: the app asks on every page load merely to decide whether to
    OFFER a data set, and answering a yes/no by pulling several MB would put that
    cost on every viewer.

    False on any Drive error, so an unreachable Drive hides the control rather
    than offering one whose file cannot be fetched.
    """
    try:
        svc = _drive_service()
        return find_in_folder(svc, store_folder_id, name) is not None
    except Exception:
        return False


def list_store_snapshots(store_folder_id: str) -> list[dict]:
    """Past weeks' stores from archive/, newest first.

    Each is {"name", "id", "date", "size"}. Returns [] when the folder or the
    archive does not exist yet, so a caller can offer history when there is some
    and stay quiet when there is not.
    """
    import archive
    svc = _drive_service()
    try:
        folder = archive.ensure_folder(svc, store_folder_id)
    except Exception:
        return []
    out = []
    for f in _list_children(svc, folder):
        name = f.get("name", "")
        m = re.match(r"attendance_store_(\d{4}-\d{2}-\d{2})\.duckdb$", name)
        if m:
            out.append({"name": name, "id": f["id"], "date": m.group(1),
                        "size": int(f.get("size") or 0)})
    return sorted(out, key=lambda d: d["date"], reverse=True)


def find_archived(store_folder_id: str, name: str) -> dict | None:
    """One named file inside archive/, or None. Used to fetch a PAST week's
    marked workbook, which lives only under its dated name."""
    import archive
    svc = _drive_service()
    try:
        return find_in_folder(svc, archive.ensure_folder(svc, store_folder_id), name)
    except Exception:
        return None


def fetch_store_snapshot(file_id: str) -> bytes:
    """One store by file id, through the byte cache. For archive/ snapshots that
    is always valid — they are immutable, so reopening last month's week costs
    nothing after the first. NOT valid for a file replaced under the same id
    (see download_cached)."""
    return download_cached(_drive_service(), file_id)


def _existing_sessions(roster_bytes: bytes) -> dict:
    """{batch_key -> set of `attendance_core.session_key`} already marked here.

    One key per column, from the same definition the marker keys its own `dmap`
    on, so "already marked" and "would be re-marked" can never disagree. **The
    POD is part of it**: from B35 one date carries up to eleven sessions, and
    keying on the date alone meant marking any one POD suppressed the download
    of the other ten — a whole day missing from a green run.

    Uses openpyxl read-only mode and reads only the top rows of each sheet — far
    lighter on memory than a full load (matters on Streamlit's small instances).
    """
    import attendance_core as ac
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(roster_bytes), read_only=True, data_only=True)

    def _has(row, needle):
        return any(isinstance(v, str) and needle in v.strip().lower() for v in row)

    out: dict = {}
    for name in wb.sheetnames:
        k = ac._sheet_key(name)
        if not k:
            continue
        ws = wb[name]
        top = list(ws.iter_rows(min_row=1, max_row=3, max_col=200, values_only=True))
        row1 = top[0] if top else ()
        row2 = top[1] if len(top) > 1 else ()
        hr = 2 if (_has(row2, "registered number") or _has(row2, "payment")) else 1
        dates = set()
        width = max(len(row1), len(row2))
        for c in range(10, width):              # 0-based: session columns start at K (index 10)
            v1 = row1[c] if c < len(row1) else None
            v2 = row2[c] if (hr == 2 and c < len(row2)) else None
            sk = ac.session_key(v1, v2, hr)
            if sk:
                dates.add(sk)
        # FIRST tab wins when two tabs map to the same batch key, matching
        # `process_files`' `key_sheet` (attendance_core.py: `if k not in
        # key_sheet`). Last-wins here meant the fetch and the marker disagreed
        # about which tab a duplicate batch key refers to.
        out.setdefault(k, dates)
    wb.close()
    return out


_ATTENDEE_NAME = re.compile(r"attendee_(\d+)_((?:20\d\d)_\d{2}_\d{2})", re.I)


def dedupe_by_webinar(files):
    """One webinar, several folders -> keep the copy with the MOST attendees.

    The two Shared Drives name the same session differently - webinar
    91641846331 on 23 Aug sits in '...B35 - Educators - Blueprint to Launch...'
    on one and '...B35 - Blueprint to Launch: Designing Courses with AI' on the
    other - so the same export arrives twice and the copies are not identical.
    `attendance_core.process_files` marks both and the LAST one wins, so which
    copy became the published column depended on Drive's listing order: B35's
    23 Aug Educators read 62 present in one run and 59 in the next, and GATE 6
    refused the drop - correctly, but the gate compares against the PUBLISHED
    store, so an unstable column wedges every later run.

    The tie-break is the PARSED ATTENDEE COUNT, which is what
    the marker already uses for this same duplication. Byte
    size was tried and is a bad proxy: B26's 22 Aug had a bigger file with 22
    FEWER people present, so ranking on size moved three sessions the wrong way
    while fixing one.

    Ties keep the incumbent, so a genuine tie resolves identically week to
    week. A name carrying no webinar id is passed through - there is nothing to
    group it with.
    """
    import attendance_core as ac

    def n_people(data):
        try:
            txt = None
            for enc in ("utf-8-sig", "utf-16", "cp1252"):
                try:
                    txt = data.decode(enc)
                    break
                except Exception:
                    continue
            if txt is None:
                return -1
            emails, _pf, ph = ac.parse_attendees(txt)
            return len(emails) + len(ph)
        except Exception:
            return -1          # unparseable: never beats a readable copy

    best, out, order, counts = {}, [], [], {}
    for path, data in files:
        m = _ATTENDEE_NAME.match(path.rsplit("/", 1)[-1])
        if not m:
            out.append((path, data))
            continue
        key = (m.group(1), m.group(2))
        n = n_people(data)
        if key not in best:
            best[key], counts[key] = (path, data), n
            order.append(key)
        elif n > counts[key]:
            best[key], counts[key] = (path, data), n
    return out + [best[k] for k in order]


# ---------------- The L2 gate ----------------
# Owner's rule, 2026-09-28: "only add sessions which are in L2 sheet" and
# "leave 25th sept". The "Zoom extracts" drive sometimes exports a session into
# a folder AND filename dated one day early. On the 26-27 Sep 2026 weekend 14
# files were named attendee_<wid>_2026_09_25.csv: 11 were day-early twins of a
# webinar whose correct _2026_09_26 export also exists, 3 were rooms L2 never
# registered (a walkthrough, a Telegram broadcast, a BSI room).
# attendance_core._parse_filename takes the session DATE from the filename, so
# an unfiltered run mints a phantom Friday column beside the real Saturday one;
# and the poll and duration passes, which tie-break on webinar id alone, let a
# twin hijack the real session's rating and duration.
#
# ONE register (attendance_core.l2_dates: L2's own dates plus ffa.py's), ONE
# predicate (l2_gate_reason), applied at every listing choke point - the fetch,
# list_attendees, list_polls, list_attendee_names - and scoped to NEW sessions
# only: a file dated on a day the base workbook already has a marked column for
# is exempt, so no historical column, rating, duration or topic can move. Every
# drop is logged by name (apply_l2_gate); silent dropping is not acceptable.
# Every entry point defaults to NO gate, so the Streamlit live-fetch path and
# every caller that never learned the new arguments behave exactly as before.


def _dates_of(existing: dict) -> set:
    """{date} over every session key in an `_existing_sessions` map: the full
    'YYYY_MM_DD' from marker-written headers and the year-blind 'MM_DD' from
    legacy hand-typed ones, exactly as `folder_keys` matches them."""
    return {sk[0] for keys in existing.values() for sk in keys if sk and sk[0]}


def marked_dates(roster_bytes: bytes) -> set:
    """Every session date already marked ANYWHERE in the workbook: the gate's
    exemption set. Anywhere, not per batch, because the point is that nothing
    already published may move, and a date one batch has a column for is a
    weekend that has already been loaded."""
    return _dates_of(_existing_sessions(roster_bytes))


def _name_wid_date(name):
    """(webinar id, date) a filename states, or None when it carries no webinar id.

    The two conventions already in the codebase, REUSED rather than re-written:
    `attendance_core._parse_filename` for attendee_<wid>_<YYYY>_<MM>_<DD>, and
    `polls.name_key` for the two poll spellings. name_key returns a year-blind
    'MM_DD', so the year is read back with `attendance_core._ymd`; a poll name
    written without one is judged on 'MM_DD' alone.
    """
    import attendance_core as ac
    import polls
    base = str(name).rsplit("/", 1)[-1]
    got = ac._parse_filename(base)
    if got:
        return got
    k = polls.name_key(base)
    if not k:
        return None
    wid, d = k[0], str(k[1])
    ymd = ac._ymd(base)
    return wid, (ymd if ymd and ymd.endswith(d[-5:]) else d)


def _date_in(d: str, dates) -> bool:
    """Is a file's date among these? Full dates compare exactly; a legacy
    'MM_DD' on either side compares year-blind, as `session_key` does."""
    if not dates:
        return False
    if d in dates:
        return True
    if len(d) == 5:                                    # 'MM_DD' from the file
        return any(x.endswith(d) for x in dates)
    return d[5:] in dates                              # 'MM_DD' in the workbook


def l2_gate_reason(name, l2_dates, exempt_dates=None):
    """Why the L2 gate drops this file, or None when it may be used.

    Passes: a name with no webinar id (nothing to judge); a date already
    marked in the base workbook (`exempt_dates` - history is never touched);
    a webinar L2 registers ON that date; a webinar L2 registers with no
    parseable date at all (in L2, nothing to judge it on).

    Drops: a webinar L2 does not list ("not in L2"); a webinar L2 lists on a
    DIFFERENT date only, the day-early twin ("L2 has it on 2026_09_26").

    `l2_dates=None` means no gate at all, never "nothing is registered".
    """
    if l2_dates is None:
        return None
    got = _name_wid_date(name)
    if got is None:
        return None
    wid, d = got
    if _date_in(d, exempt_dates):
        return None
    dates = l2_dates.get(wid)
    if dates is None:
        return "not in L2"
    if not dates or _date_in(d, dates):
        return None
    return "L2 has it on " + ", ".join(sorted(dates))


def l2_registered(name, l2_dates, exempt_dates=None) -> bool:
    """The gate as a predicate: True when the file may be used."""
    return l2_gate_reason(name, l2_dates, exempt_dates) is None


def _gate_log(msg):
    print(msg, flush=True)


def apply_l2_gate(items, l2_dates, exempt_dates=None, stage="", name_of=None,
                  log=_gate_log):
    """Filter `items` through the gate and LOG every drop by name.

    `items` may be (path, bytes) tuples, listing dicts with a 'name', or bare
    names; `name_of` overrides how the name is read. Returns
    (kept, [(filename, reason), ...]). With `l2_dates=None` nothing is
    filtered and nothing is printed."""
    if l2_dates is None:
        return list(items), []
    if name_of is None:
        def name_of(it):
            return it["name"] if isinstance(it, dict) else (
                it if isinstance(it, str) else it[0])
    kept, dropped = [], []
    for it in items:
        n = name_of(it)
        why = l2_gate_reason(n, l2_dates, exempt_dates)
        if why is None:
            kept.append(it)
        else:
            dropped.append((str(n).rsplit("/", 1)[-1], why))
    if dropped and log:
        log(f"[L2 gate{(': ' + stage) if stage else ''}] {len(dropped)} file(s) not "
            f"registered on their date, dropped: "
            + "; ".join(f"{n} ({why})" for n, why in dropped))
    return kept, dropped


def fetch_new_attendees(svc, folder_id: str, roster_bytes: bytes,
                        mark_all: bool = False, max_workers: int = 8,
                        l2_dates: dict | None = None,
                        exempt_dates: set | None = None):
    """Download attendee files only for sessions NOT already marked in the roster.

    The Shared Drive holds the full history (hundreds of dated session folders),
    but the roster already carries every past weekend's marks. So we list the
    top-level dated folders, keep only those whose (batch, date, POD) is missing
    from the roster, and download just those — in parallel. `mark_all=True`
    ignores the skip logic and pulls everything (a full rebuild). Returns
    (attendee_files, info).

    A session is identified by `attendance_core.session_key` — (date, POD) —
    because from B35 one date carries up to eleven sessions and keying on the
    date alone meant marking any one POD suppressed the other ten.

    `l2_dates` (attendance_core.l2_dates) switches on the L2 gate - see
    l2_gate_reason - over the returned files AND the failure lists, for new
    sessions only: `exempt_dates` defaults to every date already marked in
    this workbook, which `info['marked_dates']` also returns so later
    listings can share the same scope. None (the default) means no gate.
    """
    import io as _io
    import zipfile
    import attendance_core as ac

    existing = _existing_sessions(roster_bytes)
    sheet_keys = set(existing)

    def keep(name: str) -> bool:
        """Attendee reports only. A .zip qualifies solely when its name says it
        holds attendee data — otherwise a session-recording zip dropped into a
        folder would be downloaded whole (potentially gigabytes) for nothing."""
        n = name.lower()
        return n.startswith("attendee") or (n.endswith(".zip") and "attendee" in n)

    # 1) pick the top-level folders that need marking — across every configured
    #    drive (comma-separated ids: the original drive plus any newer one)
    to_fetch, skipped_nosheet, skipped_done = [], 0, 0
    top: list[dict] = []
    for fid in _folder_ids(folder_id):
        top += _list_children(svc, fid)
    for f in top:
        if f["mimeType"] != _FOLDER_MIME:
            continue
        name = f["name"]
        covered = [k for k in ac._folder_batches(name) if k in sheet_keys]
        if not covered:
            skipped_nosheet += 1
            continue
        # A session is identified by its date AND its POD - '2026-08-23 - AI CAP
        # B35 - Techies - ...' is not the same session as the Finance one beside
        # it. Either date form counts as a match, so a legacy column carrying no
        # year still suppresses its own re-download.
        cand = ac.folder_keys(name)
        if mark_all or not cand or any(not (cand & existing[k]) for k in covered):
            to_fetch.append((name, f["id"]))
        else:
            skipped_done += 1

    # 2) collect the attendee files inside those folders — in parallel: one API
    #    call per folder, and at 200+ unmarked folders doing them sequentially
    #    dominates cold-start time
    entries = []  # (path, file_id, is_zip)
    listing_errors = []
    if to_fetch:
        def _kids(item):
            name, fid = item
            try:
                return name, _list_children(_thread_drive(), fid), None
            except Exception as e:
                return name, [], str(e)
        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_fetch))) as ex:
            for name, kids, kerr in ex.map(_kids, to_fetch):
                if kerr:
                    # A folder we couldn't list is a session we'd silently drop —
                    # surface it so the caller can refuse to publish partial data.
                    listing_errors.append(f"{name}: {kerr}")
                    continue
                for f in kids:
                    if f["mimeType"] != _FOLDER_MIME and keep(f["name"]):
                        entries.append((f"{name}/{f['name']}", f["id"],
                                        f["name"].lower().endswith(".zip")))

    # 3) download (parallel, but resilient — one slow/failed file can't hang or
    #    sink the whole batch; the 60s socket timeout caps any single request).
    #    Bytes are disk-cached by file id, so only never-seen files hit the network.
    out: list[tuple[str, bytes]] = []
    _dl_failed: list[tuple[str, str]] = []    # (path, error); strings built after the gate
    _zip_failed: list[tuple[str, str]] = []
    if entries:
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
        except OSError:
            pass

        def _dl(e):
            path, fid, is_zip = e
            cpath = os.path.join(_CACHE_DIR, fid)
            try:
                with open(cpath, "rb") as fh:
                    return path, fid, is_zip, fh.read(), None
            except OSError:
                pass
            try:
                data = _download_any(_thread_drive(), fid)
                try:
                    tmp = cpath + ".tmp"
                    with open(tmp, "wb") as fh:
                        fh.write(data)
                    os.replace(tmp, cpath)
                except OSError:
                    pass
                return path, fid, is_zip, data, None
            except Exception as e:
                return path, fid, is_zip, None, str(e)
        with ThreadPoolExecutor(max_workers=min(max_workers, len(entries))) as ex:
            for path, fid, is_zip, data, err in ex.map(_dl, entries):
                if data is None:
                    _dl_failed.append((path, err))
                    continue
                if is_zip:
                    # A corrupt / password-protected / non-attendee .zip must not
                    # sink the whole run — and its bytes must not stay cached, or
                    # every later run would fail identically without a network call.
                    prefix = (path.rsplit("/", 1)[0] + "/") if "/" in path else ""
                    try:
                        with zipfile.ZipFile(_io.BytesIO(data)) as z:
                            expanded = [(f"{prefix}{inner}", z.read(inner))
                                        for inner in z.namelist()
                                        if not inner.endswith("/")]
                    except Exception as e:
                        _zip_failed.append((path, str(e)))
                        try:
                            os.remove(os.path.join(_CACHE_DIR, fid))
                        except OSError:
                            pass
                        continue
                    out.extend(expanded)
                else:
                    out.append((path, data))

    # Both copies of a duplicated webinar have been downloaded by now; keep the
    # fuller one. Done HERE rather than at the listing, because the only honest
    # tie-break is how many people each copy actually names.
    _n_raw = len(out)
    out = dedupe_by_webinar(out)
    dropped_dupes = _n_raw - len(out)

    # THE L2 GATE - see l2_gate_reason. NEW sessions only: a file whose date is
    # already a marked column anywhere in this workbook is exempt, so the gate
    # can never move a published column. A download that failed on a file the
    # gate would have dropped anyway must not trip the refuse-to-publish check
    # in pipeline.py, so the failure lists go through the same gate. A folder
    # that could not be LISTED stays fatal: nothing is known about what it held.
    marked = _dates_of(existing)
    if exempt_dates is None:
        exempt_dates = marked
    out, gated = apply_l2_gate(out, l2_dates, exempt_dates, stage="fetch")
    _dl_failed, _g2 = apply_l2_gate(_dl_failed, l2_dates, exempt_dates,
                                    stage="fetch, failed downloads")
    _zip_failed, _g3 = apply_l2_gate(_zip_failed, l2_dates, exempt_dates,
                                     stage="fetch, unreadable zips")
    gated += _g2 + _g3
    download_errors = [f"{p}: {e}" for p, e in _dl_failed]
    bad_zips = [f"{p}: {e}" for p, e in _zip_failed]

    info = dict(new_folders=len(to_fetch), files=len(out),
                failed=len(download_errors) + len(bad_zips),
                skipped_already_marked=skipped_done, skipped_no_sheet=skipped_nosheet,
                listing_errors=listing_errors, bad_zips=bad_zips,
                download_errors=download_errors, duplicate_copies=dropped_dupes,
                l2_gate=gated, l2_gate_dropped=len(gated), marked_dates=marked)
    return out, info


# Fields every whole-drive listing asks for. md5Checksum and modifiedTime are
# what let a cross-run cache tell a REPLACED file from an unchanged one — Drive's
# "Manage versions → Upload new version" keeps the id, and that is the documented
# remedy for a bad Zoom export. They are free: the listing already runs, and
# these are two more fields on a response we already page through.
_LIST_FIELDS = "nextPageToken, files(id, name, md5Checksum, modifiedTime)"

_ATTENDEE_Q = "name contains 'attendee_' and trashed = false"
# both conventions — see polls.name_key
_POLL_Q = ("(name contains 'poll_' or name contains 'Poll Report') "
           "and trashed = false")


def _list_by_query(svc, folder_id, q) -> list[dict]:
    """Every matching file across every configured drive.

    THIS IS THE CHANGE DETECTOR. It is deliberately separate from fetching and
    is never skipped or cached: a file added, deleted, moved or replaced has to
    be seen even when nothing needs downloading. Only the download and the parse
    are ever avoided downstream.
    """
    files, seen = [], set()
    for did in _folder_ids(folder_id):
        token = None
        while True:
            kw = dict(q=q, fields=_LIST_FIELDS, pageSize=1000, pageToken=token,
                      includeItemsFromAllDrives=True, supportsAllDrives=True)
            if str(did).startswith("0A"):
                kw.update(corpora="drive", driveId=did)
            resp = svc.files().list(**kw).execute()
            for f in resp.get("files", []):
                if f["id"] not in seen:
                    seen.add(f["id"])
                    files.append(f)
            token = resp.get("nextPageToken")
            if not token:
                break
    return files


def list_attendees(svc, folder_id, l2_dates=None, exempt_dates=None) -> list[dict]:
    """Every attendee report on every configured drive (listing only).

    With `l2_dates` the L2 gate is applied to the listing (l2_gate_reason):
    these rows feed the duration/peak pass, which tie-breaks on webinar id
    alone, so a day-early twin left in here hijacks the real session's
    duration. Files dated inside `exempt_dates` pass untouched."""
    files = _list_by_query(svc, folder_id, _ATTENDEE_Q)
    return apply_l2_gate(files, l2_dates, exempt_dates, stage="attendee listing")[0]


def list_polls(svc, folder_id, l2_dates=None, exempt_dates=None) -> list[dict]:
    """Every poll export on every configured drive (listing only). Gated like
    list_attendees: the poll reader also tie-breaks on webinar id alone."""
    files = _list_by_query(svc, folder_id, _POLL_Q)
    return apply_l2_gate(files, l2_dates, exempt_dates, stage="poll listing")[0]


def _sig_path(f: dict) -> tuple[str, bool]:
    """(disk cache path, verified) for one listing row.

    SIGNATURE-ADDRESSED. The byte cache used to key on the bare file id, on the
    strength of a comment saying attendee files never change once uploaded —
    a claim, not a check, and falsified by upload_to_folder in this very module,
    which does files().update(fileId=…) every Monday. A file replaced in place
    therefore kept serving its OLD bytes from disk forever, and no md5 was ever
    compared. Including the signature in the path means a replaced file misses
    the disk cache exactly as it should.

    `verified` is False when Drive reports neither md5 nor modifiedTime: the
    bytes may be current but nothing proves it, so they must not be memoised.
    """
    sig = (f.get("md5Checksum") or f.get("modifiedTime") or "").strip()
    if not sig:
        return os.path.join(_CACHE_DIR, f["id"]), False
    short = hashlib.sha1(sig.encode("utf-8", "replace")).hexdigest()[:10]
    return os.path.join(_CACHE_DIR, f"{f['id']}.{short}"), True


def fetch_stream(svc, files, consume, max_workers: int = 8) -> dict:
    """Download each listed file and hand `consume(f, bytes, verified)` its bytes.

    **Nothing is accumulated.** The bytes are dropped straight after `consume`
    returns, because holding 246 MB is exactly how the marking used to blow up
    on a small instance. Callers keep the small aggregate, never the corpus.

    Failures are counted, never raised: a missing report should cost that
    session its duration, not the whole refresh.
    """
    done = failed = 0
    if not files:
        return {"files": 0, "failed": 0}
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except OSError:
        pass

    def _dl(f):
        cpath, verified = _sig_path(f)
        try:
            with open(cpath, "rb") as fh:
                return f, fh.read(), verified, None
        except OSError:
            pass
        try:
            data = _download_any(_thread_drive(), f["id"])
            try:
                tmp = cpath + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, cpath)
            except OSError:
                pass
            return f, data, verified, None
        except Exception as e:
            return f, None, verified, str(e)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(files))) as ex:
        for f, data, verified, err in ex.map(_dl, files):
            if data is None:
                failed += 1
                continue
            try:
                consume(f, data, verified)
            except Exception:
                failed += 1
            finally:
                del data          # keep peak memory at one file, not 246 MB
            done += 1
    return {"files": done, "failed": failed}


def scan_all_attendees(svc, folder_id, consume, max_workers: int = 8):
    """Download EVERY attendee report and hand each to `consume(name, bytes)`.

    Kept for callers that want the whole corpus with no cache in the way. The
    pipeline uses list_attendees + fetch_stream directly so it can consult the
    derived-facts memo per file; this wrapper is the un-memoised equivalent.
    """
    files = list_attendees(svc, folder_id)
    return fetch_stream(svc, files,
                        lambda f, data, _v: consume(f["name"], data),
                        max_workers=max_workers)


def fetch_polls(svc, folder_id, max_workers: int = 8):
    """Every `poll_<webinarid>_<date>.csv` across the configured drives.

    Returns ([(name, bytes)], info). As with scan_all_attendees, the pipeline
    now lists and fetches separately; this stays for the un-memoised path.
    """
    files = list_polls(svc, folder_id)
    out = []
    info = fetch_stream(svc, files,
                        lambda f, data, _v: out.append((f["name"], data)),
                        max_workers=max_workers)
    return out, {"found": len(files), "files": len(out),
                 "failed": info["failed"]}


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

    roster_bytes, roster_stamp = fetch_sheet_cached(svc, drive["roster_id"])

    l2_bytes, l2_stamp = None, ""
    if drive.get("l2_id"):
        l2_bytes, l2_stamp = fetch_sheet_cached(svc, drive["l2_id"])

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
        # modifiedTime stamps: the stable identity for downstream caches
        # (export bytes vary run-to-run even when the sheet is unchanged)
        "roster_stamp": roster_stamp,
        "l2_stamp": l2_stamp,
    }
