"""
Be10X — AI CAP Attendance (unified app)
========================================

The app renders a PREBUILT store when one is available: pipeline.py (run by
GitHub Actions, dispatched from the Add data tab) fetches the roster + Zoom reports, marks
attendance, and uploads one small attendance.duckdb to a private Drive folder.
The app just downloads that file and renders — opens in seconds, and the
memory-heavy marking never runs on the Streamlit server.

Fallbacks, in order, when no store is configured:
  • legacy live mode — fetch + mark right here (slow first load), or
  • manual upload — roster .xlsx (+ optional L2 + attendee .zip).

Run locally:   streamlit run attendance_app.py
"""
from __future__ import annotations
import hashlib
import hmac
import io
import json
import pickle
import datetime as _dt
import html as _html
from datetime import datetime

import polls as _polls_mod          # the NPS rule, shared with the pipeline
import recap as _recap_mod          # award rules, shared with the pipeline
import trainers as _trainers_mod    # identity resolution, shared likewise
from pathlib import Path

import streamlit as st

import attendance_core as ac
import dashboard_core as dc
import live_data
import data as ddata          # new dashboard data layer (aliased; 'data' is used as a local below)
import sheets as dsheets      # gspread / xlsx source adapter
import dash_view              # Plotly drill-down dashboard UI
import bsiai_view             # the BSIAI programme's pages, over its own store
import ui_theme as T          # brand tokens, the one stylesheet, KPI tiles, plain-word labels

# The tab icon is the wordmark's disc (ui_theme.BRAND["favicon"]). The emoji
# stays as the fallback because a missing file makes set_page_config raise,
# and a cosmetic asset must never be able to take the app down.
import os as _os                                            # noqa: E402
import ui_theme as _brand                                   # noqa: E402
_ICON = _brand.BRAND.get("favicon")
st.set_page_config(
    page_title="Be10X Attendance",
    page_icon=(_ICON if _ICON and _os.path.exists(_ICON) else "📊"),
    layout="wide")


def _brand_title() -> None:
    """The wordmark beside the page name, falling back to a plain title.

    `st.logo()` is deliberately not used: it pins the image to the top of the
    SIDEBAR, and this app's sidebar is an Admin drawer that starts collapsed, so
    the brand would be invisible on first paint. An inline <img> in the header is
    the only place it is always seen. The file is inlined base64 rather than
    served, because Streamlit has no static route for a repo path.

    Which file: the black wordmark on a light surface, the white one on a dark
    surface. They are NOT interchangeable — the white file is white ink on
    transparent, so picking it wrongly gives a blank header rather than a
    faint one. `theme.base` is Streamlit's resolved theme, so it follows
    .streamlit/config.toml (or a deployment that overrides it).

    The `.mark` wrapper crops the file's 30% vertical padding; the geometry and
    the 140px width both live in ui_theme.BRAND.
    """
    import base64
    dark = str(st.get_option("theme.base") or "light").lower() == "dark"
    path = T.BRAND.get("logo_dark" if dark else "logo_light")
    if not (path and _os.path.exists(path)):
        st.title("Be10X — AI CAP Attendance")
        return
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    mime = "image/svg+xml" if path.lower().endswith(".svg") else "image/png"
    st.markdown(
        f'<div class="brandbar">'
        f'<span class="mark"><img src="data:{mime};base64,{b64}" alt="be10X"></span>'
        f'<span class="t">AI CAP Attendance</span></div>',
        unsafe_allow_html=True)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# ───────────────────────── password gate ─────────────────────────────────────
# This app is reachable by anyone with the link, and its Roster tab shows student
# emails and phone numbers. Everything below — including downloading the store —
# happens only after this returns, so an unauthenticated visitor never causes the
# PII to be fetched, let alone rendered.
#
# One SHARED password, read from secrets. It is a deterrent against a stray link,
# not real authentication: it cannot tell users apart, cannot be revoked for one
# person, and is only as private as the least careful person it is given to. For
# genuine access control use Streamlit Cloud's own viewer allow-list
# (Settings -> Sharing), which authenticates against real accounts. See §6.
_PASSWORD_KEY = "app_password"


def _password_ok() -> None:
    """Stop the script unless this session has entered the right password.

    Fails CLOSED: with no password configured nobody gets in. An unset secret is
    far more likely to mean "not set up yet" than "deliberately public", and the
    failure mode of guessing wrong is publishing the roster."""
    if st.session_state.get("_authed"):
        return
    try:
        expected = str(st.secrets.get(_PASSWORD_KEY, "") or "")
    except Exception:            # no secrets file at all
        expected = ""

    _brand_title()
    if not expected:
        st.error(
            f"**No `{_PASSWORD_KEY}` is configured, so the app is locked.** "
            "Add it to the app's secrets (Streamlit Cloud: Manage app → Settings → "
            f"Secrets) as `{_PASSWORD_KEY} = \"your-password\"`, or to "
            "`.streamlit/secrets.toml` when running locally."
        )
        st.stop()

    def _submit():
        # compare_digest keeps the check constant-time, and the plaintext is
        # dropped from session state the moment it has been used.
        if hmac.compare_digest(st.session_state.get("_pw", ""), expected):
            st.session_state["_authed"] = True
        else:
            st.session_state["_authed"] = False
        st.session_state.pop("_pw", None)

    st.text_input("Password", type="password", key="_pw", on_change=_submit,
                  placeholder="Enter the password and press Enter")
    if st.session_state.get("_authed") is False:
        st.error("Incorrect password.")
    st.caption("Ask the AI CAP team for access.")
    st.stop()


_password_ok()




# ───────────────────────────── cached heavy work ─────────────────────────────
_DISK_CACHE = Path(__file__).parent / ".cache"


def _disk_memo(name: str, key_material: bytes, builder):
    """Persistent twin of st.cache_data: the in-memory cache dies with the server
    process, so a restart used to redo every expensive step (marking 170+ session
    columns takes minutes). Results are pickled to .cache/ keyed by an input hash;
    only one file per artefact is kept (a new key evicts the old one). All disk
    errors fall through to just rebuilding — the cache can never break the app."""
    digest = hashlib.sha256(key_material).hexdigest()[:24]
    path = _DISK_CACHE / f"{name}_{digest}.pkl"
    if path.exists():
        try:
            return pickle.loads(path.read_bytes())
        except Exception:
            pass
    value = builder()
    try:
        _DISK_CACHE.mkdir(exist_ok=True)
        for old in _DISK_CACHE.glob(f"{name}_*.pkl"):    # evict superseded keys
            old.unlink(missing_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(value))
        tmp.replace(path)
    except Exception:
        pass
    return value


def _inputs_key(roster_bytes, l2_bytes, attendee_files, mode) -> bytes:
    h = hashlib.sha256()
    h.update(roster_bytes)
    h.update(l2_bytes or b"")
    for name, data in sorted(attendee_files or ()):
        h.update(name.encode("utf-8", "replace"))
        h.update(data)
    h.update(mode.encode())
    return h.digest()


@st.cache_data(show_spinner="Marking new sessions…")
def _mark(roster_bytes, l2_bytes, attendee_files, mode):
    """Run the marker once per unique input set. values_only=True so formula-based
    attendance columns keep their values through the save (the dashboard reads
    values, not formulas)."""
    return _disk_memo(
        "marked", _inputs_key(roster_bytes, l2_bytes, attendee_files, mode),
        lambda: ac.process_files(roster_bytes, l2_bytes, list(attendee_files),
                                 mode=mode, values_only=True))


@st.cache_data(show_spinner="Building dashboard…")
def _compute(roster_bytes):
    return _disk_memo("compute", hashlib.sha256(roster_bytes).digest(),
                      lambda: dc.compute(roster_bytes))


@st.cache_data(show_spinner=False)
def _attendee_names(nonce):
    """All attendee filenames (one fast Drive query) — only used to label sessions
    with their real topic name via the Webinar-ID join."""
    try:
        return tuple(live_data.list_attendee_names())
    except Exception:
        return ()


@st.cache_data(show_spinner="Building dashboard from the updated roster…")
def _build_dashboard(roster_bytes, attendee_names, l2_bytes):
    """The dashboard reads the SAME updated roster the app produced (the one you
    can download) — no second fetch. Session names come from the Webinar-ID join
    (attendee filenames ⋈ L2)."""
    key = hashlib.sha256()
    key.update(roster_bytes)
    key.update("\n".join(sorted(attendee_names)).encode("utf-8", "replace"))
    key.update(l2_bytes or b"")
    return _disk_memo("dashdata", key.digest(),
                      lambda: _build_dashboard_impl(roster_bytes, attendee_names, l2_bytes))


def _build_dashboard_impl(roster_bytes, attendee_names, l2_bytes):
    import io as _io
    from openpyxl import load_workbook
    wb = load_workbook(_io.BytesIO(roster_bytes), read_only=True, data_only=True)
    tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets}
    wb.close()
    topics, l2_labels, mentors = dsheets.webinar_topic_lookup(
        [(n, None) for n in attendee_names], l2_bytes,
        with_labels=True, with_mentors=True)
    DATA, summary = ddata.build(tabs, topics, l2_labels, None, mentors)
    _prepend_intro_sessions(DATA)
    return DATA, summary


def _prepend_intro_sessions(DATA: dict) -> None:
    """First session of every batch = the intro call. Counts come from
    intro_attendance.json (unique attendees per batch, precomputed from the
    'L2 customer' sheet — aggregates only). Batches missing from the file are
    left untouched. Intro rows are flagged is_intro so avg/peak/low keep
    measuring real class sessions."""
    import json
    from pathlib import Path
    f = Path(__file__).parent / "intro_attendance.json"
    if not f.exists():
        return
    intro = json.loads(f.read_text(encoding="utf-8"))
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


# ─────────────────────── prebuilt store (pipeline.py) ────────────────────────
_STORE_PATH = _DISK_CACHE / "attendance.duckdb"


def _store_folder_id() -> str:
    try:
        return st.secrets.get("drive", {}).get("store_folder_id", "")
    except Exception:
        return ""


# The store is re-checked this often, so the Monday 06:00 IST rebuild reaches
# viewers on its own. Without a TTL, one cached load would pin the whole server
# to that week's data until somebody happened to press Refresh.
# Short on purpose. The owner now adds a week's data themselves and the
# dashboard is expected to show it straight away; 5 minutes meant a
# colleague could be looking at pre-upload numbers with no way to tell.
_STORE_TTL_SECONDS = 5 * 60


@st.cache_data(show_spinner="Loading the latest dashboard…", ttl=_STORE_TTL_SECONDS)
def _load_store(nonce, store_name="attendance.duckdb"):
    """Download the prebuilt attendance.duckdb the pipeline uploaded to Drive
    (a few MB — seconds, not minutes), keep a disk copy, and read out everything
    the UI needs. If Drive is unreachable, an existing local copy still serves.

    Returns the meta dict (+ df/path/generated_at), {"error": …} when there is
    nothing readable, or None when no store exists at all. Never raises: a
    corrupt / locked / version-mismatched file must degrade to the legacy live
    and upload modes, not paint a traceback over the whole app."""
    err = None
    # Each dataset keeps its OWN disk path. _store_grid caches on
    # (path, batch, generated_at_iso), so two datasets sharing one path could
    # serve the wrong batch grid whenever their timestamps happened to match.
    path = _DISK_CACHE / store_name
    fid = _store_folder_id()
    if fid:
        try:
            raw, _stamp = live_data.fetch_store(fid, name=store_name)
            if raw:
                _DISK_CACHE.mkdir(exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(raw)
                try:
                    tmp.replace(path)
                except OSError:
                    pass                     # another session holds it open — keep old copy
        except Exception as e:
            err = str(e)
    if not path.exists():
        return {"error": err} if err else None
    try:
        import duckdb
        con = duckdb.connect(str(path), read_only=True)
        try:
            out = {k: json.loads(v)
                   for k, v in con.execute("SELECT key, value FROM meta").fetchall()}
            out["df"] = con.execute("SELECT * FROM compute").df()
        finally:
            con.close()
    except Exception as e:
        # Unreadable store (mid-build write lock, interrupted build with no meta
        # table, truncated upload, newer duckdb storage format, …).
        return {"error": f"the prebuilt data file could not be read ({e})"}
    if "df" not in out or "generated_at" not in out:
        return {"error": "the prebuilt data file is incomplete (rebuild it)"}
    out["path"] = str(path)
    out["drive_error"] = err
    return out


@st.cache_data(show_spinner=False, ttl=_STORE_TTL_SECONDS)
def _snapshot_list(nonce):
    """Past weeks available in archive/, newest first. Never raises: history is
    a bonus, and a Drive hiccup here must not take the live dashboard with it."""
    fid = _store_folder_id()
    if not fid:
        return []
    try:
        return live_data.list_store_snapshots(fid)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def _archived_marked(day):
    """That week's dated marked workbook in archive/, or None.

    None is a real answer, not an error: weeks refreshed before the archive
    existed have no marked copy, and saying so beats serving today's file under
    an old week's name."""
    fid = _store_folder_id()
    if not fid:
        return None
    try:
        import archive
        return live_data.find_archived(
            fid, archive.snapshot_name(archive.MARKED_LABEL,
                                       datetime.strptime(day, "%Y-%m-%d").date()))
    except Exception:
        return None


@st.cache_data(show_spinner="Opening that week's dashboard…")
def _load_snapshot(file_id, name):
    """Render an ARCHIVED week instead of the current one.

    Kept separate from _load_store rather than parameterised, for two reasons:
    a snapshot is immutable so it wants permanent caching rather than the live
    store's 5-minute TTL, and it must never be written to _STORE_PATH — doing
    so would leave last month's data sitting where the live loader expects
    today's, and the app would serve it as current long after the user moved on.
    """
    path = _DISK_CACHE / f"snap_{name}"
    try:
        if not path.exists():
            _DISK_CACHE.mkdir(exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(live_data.fetch_store_snapshot(file_id))
            tmp.replace(path)
        import duckdb
        con = duckdb.connect(str(path), read_only=True)
        try:
            out = {k: json.loads(v)
                   for k, v in con.execute("SELECT key, value FROM meta").fetchall()}
            out["df"] = con.execute("SELECT * FROM compute").df()
        finally:
            con.close()
    except Exception as e:
        return {"error": f"that week's snapshot could not be opened ({e})"}
    if "df" not in out or "generated_at" not in out:
        return {"error": "that week's snapshot is incomplete"}
    out["path"] = str(path)
    out["drive_error"] = None
    return out


@st.cache_data(show_spinner=False)
def _store_grid(path, batch, stamp):
    """One batch's roster grid from the store. `stamp` (generated_at_iso) keys
    the cache so a fresh store invalidates old grids. Returns None rather than
    raising if the store became unreadable since it was loaded."""
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
        try:
            return con.execute(f'SELECT * FROM "grid_{batch}"').df()
        finally:
            con.close()
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def _grid(roster_bytes, sheet_name):
    return dc.roster_grid(roster_bytes, sheet_name)


# What a session cell shows. The grid holds the words 'Present' / 'Absent' /
# '' (dashboard_core.roster_grid); the words are what they mean, but forty of
# them across a row is a wall of text nobody reads a pattern out of, and they
# force every session column wide enough to spell "Present". The glyph carries
# the same three states and the colour behind it is the same status pair the
# dashboard's pills use — never colour alone, which is why they are glyphs and
# not blank coloured squares.
_MARK_PRESENT = "✓"       # ✓
_MARK_ABSENT = "✗"        # ✗
_MARK_BLANK = "—"         # —


@st.cache_data(show_spinner=False)
def _roster_display(_g, batch, active_only, show_pii, stamp):
    """The Roster grid's DISPLAY frame: filtered, masked and turned into glyphs.

    Returns (frame, session columns, students shown, students present ≥ once).

    `_g` has a leading underscore so Streamlit does NOT hash it — hashing a
    3,000-row frame on every rerun costs more than the work below. The cache
    key is therefore the four things that actually change the output plus
    `stamp` (the store's `generated_at_iso`), and that last one is not
    optional: without it a refresh to a new store would keep serving the
    previous week's grid under the same batch and toggles.
    """
    g = _g[_g["Active"].astype(bool)] if active_only else _g
    sess_cols = [c for c in g.columns
                 if c not in ("Email", "Phone", "Active", "Present")]
    n_students = len(g)
    n_present_any = int((g["Present"] > 0).sum())
    disp = g.copy()
    if not show_pii:
        disp["Email"] = disp["Email"].map(mask_email)
        disp["Phone"] = disp["Phone"].map(mask_phone)
    glyph = {"present": _MARK_PRESENT, "absent": _MARK_ABSENT}
    for c in sess_cols:
        disp[c] = disp[c].map(
            lambda v: glyph.get(str(v).strip().lower(), _MARK_BLANK))
    disp = disp[["Email", "Phone", "Active", "Present"] + sess_cols]
    return disp, sess_cols, n_students, n_present_any


@st.cache_data(show_spinner=False)
def _sheet_map(roster_bytes):
    return dc.batch_sheet_map(roster_bytes)


@st.cache_data(show_spinner="Syncing from Google Drive & marking… (first run is the slow one)")
def _load_live_marked(nonce, mode):
    """Fetch from Drive AND mark, in one cached step that returns only the small
    results (marked roster bytes, report, warnings). The raw attendee CSVs are
    released as soon as marking is done instead of living in the cache forever —
    that retained 100+ MB was the main reason the app got slow / crashed.

    New sessions are only added on weekends, so there's deliberately NO time-based
    expiry — the result stays cached (shared across all viewers) until someone
    clicks '🔄 Refresh from Google', which bumps `nonce` and clears the cache.
    Marking is additionally disk-memoized, so even a server restart doesn't redo
    it unless the roster or the attendee file set actually changed."""
    data = live_data.load_live()
    roster_bytes, l2_bytes = data["roster_bytes"], data["l2_bytes"]
    attendee_files = data["attendee_files"] or []
    marked, report, warnings, mark_error = roster_bytes, [], [], None
    if attendee_files:
        # Key on the sheets' modifiedTime stamps + the attendee file set, NOT on
        # the exported bytes: Google's xlsx export differs run-to-run for an
        # unchanged sheet, which would make a byte-keyed cache never hit.
        key = (f"{data.get('roster_stamp', '')}|{data.get('l2_stamp', '')}|{mode}|"
               + "\n".join(sorted(n for n, _ in attendee_files))).encode("utf-8", "replace")
        try:
            marked, report, warnings = _disk_memo(
                "marked", key,
                lambda: ac.process_files(roster_bytes, l2_bytes, list(attendee_files),
                                         mode=mode, values_only=True))
        except Exception as e:
            mark_error = str(e)
    return {
        "marked_bytes": marked,
        "l2_bytes": l2_bytes,
        "report": report,
        "warnings": warnings,
        "source": data["source"],
        "n_attendee_files": len(attendee_files),
        "mark_error": mark_error,
    }


# ───────────────────────────── small helpers ─────────────────────────────────
def mask_email(e: str) -> str:
    e = (e or "").strip()
    if "@" not in e:
        return "•••" if e else ""
    local, dom = e.split("@", 1)
    keep = local[:3] if len(local) > 3 else local[:1]
    return f"{keep}…@{dom}"


def mask_phone(p: str) -> str:
    digits = "".join(ch for ch in str(p or "") if ch.isdigit())
    return ("•" * max(0, len(digits) - 4)) + digits[-4:] if digits else ""


# ───────────────────────────── header ────────────────────────────────────────
_brand_title()
T.inject_css()          # the one stylesheet: tiles, chips, expander spacing, dashboard fragments

if "nonce" not in st.session_state:
    st.session_state.nonce = 0


# ───────────────────────────── sidebar: source + controls ────────────────────
live_ready = live_data.config_present()
# Prefer the prebuilt store: with store_folder_id set it is the real source and
# refreshes from Drive. A local-only store (someone ran `pipeline.py --no-upload`)
# is still used — that's the point of building it — but it can never be refreshed
# from Drive, so say so out loud rather than serving stale data silently.
_store_configured = bool(_store_folder_id())
_store_local_only = not _store_configured and _STORE_PATH.exists()
_store_available = _store_configured or _store_local_only

# Everything technical about WHERE the data comes from lives in one collapsed
# Admin box: the refresh button, the matching rule, and the data-source
# warnings that are appended to it further down. The Week selector (archived
# weeks) stays outside it because it is for readers, not operators.
_admin = st.sidebar.expander("Admin", expanded=False)
with _admin:
    st.caption("Data source")

    if st.button("🔄 Refresh from Google", width='stretch',
                 disabled=not (live_ready or _store_available),
                 help="Pull the latest prebuilt dashboard from Drive "
                      "(rebuilt when a week's Zoom exports are added)"):
        st.session_state.nonce += 1
        st.cache_data.clear()
        st.rerun()

    # The matching rule only means something when THIS server does the marking.
    # With prebuilt data the marking already happened in the pipeline, so showing
    # a live-looking radio here would be a lie.
    mode = "exact"
    if not _store_available:
        st.caption("Matching rule (advanced)")
        mode_label = st.radio(
            "How to match a student to an attendee",
            ["Exact — Registered mail + number (recommended)",
             "Inclusive — also WhatsApp / broadcast, last-10-digit phone"],
            index=0, label_visibility="collapsed",
        )
        mode = "exact" if mode_label.startswith("Exact") else "inclusive"


# ───────────────────────────── acquire the data ──────────────────────────────
# Preferred: the prebuilt store (instant). Fallbacks: legacy fetch+mark here,
# then manual upload. Only the store path is exercised once the pipeline runs.
marked_bytes = l2_bytes = None
report, warnings = [], []
n_attendee_files = 0
upload_attendee_names = ()
source_label = None
live_failed = False
mark_error = None
store = None
store_mode = False

# ── which week are we looking at? ────────────────────────────────────────────
# Every Monday the pipeline archives the store it just built, so each past week's
# dashboard survives verbatim rather than being overwritten. Offer them here, or
# the archive is data nobody can actually reach.
_snapshots = _snapshot_list(st.session_state.nonce) if _store_configured else []
_viewing = None
if _snapshots:
    with st.sidebar:
        _labels = ["Latest (live)"] + [
            datetime.strptime(s["date"], "%Y-%m-%d").strftime("%d %b %Y")
            for s in _snapshots]
        _pick_week = st.selectbox(
            "Week", _labels, index=0, key="week_pick",
            help="Past weeks are archived every Monday. Choosing one shows the "
                 "dashboard exactly as it stood then — attendance, "
                 "forecast and roster all as they were.")
        if _pick_week != "Latest (live)":
            _viewing = _snapshots[_labels.index(_pick_week) - 1]

# ── which DATA SET? ──────────────────────────────────────────────────────────
# One published data set. The sidebar used to offer a second, "LMS API matched
# data", so Sheet-sourced enrolment could be compared against the LMS API - but
# since 2026-09-22 the roster Sheet IS the API-built one ("LMS Attendance
# Roaster"), so both sides are LMS-derived and the toggle only offered a staler
# rebuild of the same thing. pipeline.py keeps --publish-parallel and STORE_OUT,
# so a side-by-side can be rebuilt whenever a roster question needs one.
_store_name = "attendance.duckdb"

if _viewing:
    loaded = _load_snapshot(_viewing["id"], _viewing["name"])
    if loaded and "df" in loaded:
        store = loaded
        store_mode = True
        report, warnings = store["report"], store["warnings"]
        source_label = store["source"]
        # Say it on the PAGE, not just the sidebar. Someone screenshotting a
        # number from a historical week must not be able to mistake it for today.
        st.warning(
            f"📅 **Viewing the archived week of "
            f"{datetime.strptime(_viewing['date'], '%Y-%m-%d'):%d %b %Y}** — this "
            f"is the dashboard as it stood then (built {store['generated_at']}), "
            "not current data. Switch **Week** back to “Latest (live)” in the "
            "sidebar for today's numbers.")
    else:
        st.sidebar.error(
            "Couldn’t open that week: "
            + str((loaded or {}).get("error", "unknown")))
        _viewing = None

if not _viewing and _store_available:
    loaded = _load_store(st.session_state.nonce, _store_name)
    if loaded and "df" in loaded:
        store = loaded
        store_mode = True
        report, warnings = store["report"], store["warnings"]
        source_label = store["source"]
        if store.get("drive_error"):
            _admin.warning("Drive unreachable — showing the last downloaded "
                           f"data.\n\n{store['drive_error']}")
        if _store_local_only:
            _admin.warning(
                f"Using a **locally built** store from {store['generated_at']}. "
                "🔄 Refresh cannot update it — no `store_folder_id` is configured. "
                "Re-run `pipeline.py`, or delete `.cache/attendance.duckdb` to read "
                "Google Drive directly."
            )
        else:
            # The build stamp is the ONE caption under the title (see
            # `_data_stamp`). Repeating it here is how the page came to print
            # it three times on first paint.
            _admin.caption("Rebuilt when a week's data is added")
        pipeline_mode = (store.get("stamps") or {}).get("mode") or "exact"
        _admin.caption(f"Source: {source_label}")
        _admin.caption(f"Matching rule: **{pipeline_mode}** (set by the pipeline)")
    elif loaded and loaded.get("error"):
        st.sidebar.error(f"Couldn’t load the prebuilt dashboard:\n\n{loaded['error']}"
                         + ("\n\nFalling back to reading Google Drive directly."
                            if live_ready else ""))

if not store_mode and live_ready:
    try:
        live = _load_live_marked(st.session_state.nonce, mode)
        marked_bytes = live["marked_bytes"]
        l2_bytes = live["l2_bytes"]
        report, warnings = live["report"], live["warnings"]
        source_label = live["source"]
        n_attendee_files = live["n_attendee_files"]
        mark_error = live["mark_error"]
    except Exception as e:  # fall back to uploads, but tell the user why
        live_failed = True
        st.sidebar.error(f"Couldn’t read from Google Drive:\n\n{e}")

if not store_mode and marked_bytes is None:
    with st.sidebar:
        if not live_ready:
            st.caption("Google not connected yet — upload files below. "
                       "See **SETUP_LIVE.md** to make it automatic.")
        up_roster = st.file_uploader("Master Roster (.xlsx)", type=["xlsx"])
        with st.expander("Optional: mark fresh attendance"):
            up_l2 = st.file_uploader("Weekly schedule (.xlsx)", type=["xlsx"])
            up_zip = st.file_uploader("Zoom attendee reports (.zip)", type=["zip"])
    if up_roster is None:
        st.info("👈 **Upload your Master Roster** in the sidebar to begin "
                "(or set up Google for automatic updates — see SETUP_LIVE.md).")
        st.stop()
    roster_bytes = up_roster.getvalue()
    l2_bytes = up_l2.getvalue() if up_l2 else None
    marked_bytes = roster_bytes
    if up_zip is not None:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(up_zip.getvalue())) as z:
            attendee_files = [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
        n_attendee_files = len(attendee_files)
        upload_attendee_names = tuple(n for n, _ in attendee_files)
        source_label = f"Manual upload — roster + {n_attendee_files} attendee files"
        try:
            marked_bytes, report, warnings = _mark(roster_bytes, l2_bytes, tuple(attendee_files), mode)
        except Exception as e:
            mark_error = str(e)
    else:
        source_label = "Manual upload — roster only"

# ── the BSIAI programme's own store ──────────────────────────────────────────
# A SEPARATE file (`bsiai.duckdb`, written by bsiai_build.py and uploaded with
# `--upload`), read through the same loader as the AI CAP store so it refreshes
# from Drive on the same TTL. It never joins the AI CAP numbers — §4b holds:
# the tab below is the only place it is drawn. Absent on Drive or on disk
# simply means the tab says so; nothing else on the page depends on it.
_BSIAI_STORE_NAME = "bsiai.duckdb"
_bsiai_available = _store_configured or (_DISK_CACHE / _BSIAI_STORE_NAME).exists()
bsiai_store = None
if _bsiai_available:
    _b = _load_store(st.session_state.nonce, _BSIAI_STORE_NAME)
    if _b and "df" in _b:
        bsiai_store = _b

if mark_error:
    st.error(f"Marking failed, showing the roster as-is: {mark_error}")

if store_mode:
    df = store["df"]
else:
    try:
        df = _compute(marked_bytes)
    except Exception as e:
        st.error(f"Couldn’t read the roster: {e}")
        st.stop()

if df.empty:
    st.warning("No batch sheets with session columns were found.")
    st.stop()

marked = df[df["HasData"]].copy()
if marked.empty:
    st.warning("No sessions have attendance marked yet in this roster.")
    st.stop()


# status line — ONE quiet caption, and the only "Data as of" on the page.
# "🟢 Prebuilt data · Data as of" is the five-second health check
# (CLAUDE.md §8); the source detail is in Admin.
def _refresh_note(report: list) -> str:
    """What the last run did to the session columns, in the pipeline's own
    three buckets.

    `attendance_core.process_files` tags every report row `NEW`, `re-mark` or
    `frozen`, and `pipeline.py` counts the third separately for exactly the
    reason this function used not to: a carried-forward column was NOT
    re-marked — nothing was written to it — so counting it as "re-marked"
    claimed 36 columns had been recomputed on a run that recomputed none.

    A report whose rows carry no `kind` at all is an older store, and the
    honest answer there is to print no counts rather than guess a bucket.
    """
    if not report:
        return ""
    kinds = [r.get("kind") for r in report]
    if not any(k for k in kinds):
        return ""
    new_n = sum(1 for k in kinds if k == "NEW")
    re_n = sum(1 for k in kinds if k == "re-mark")
    fz_n = sum(1 for k in kinds if k == "frozen")
    if new_n + re_n + fz_n != len(kinds):          # a bucket we do not know
        return ""
    return (f"last refresh: {new_n} new, {re_n} re-marked, "
            f"{fz_n} frozen session column(s)")


def _sessions_through(store: dict) -> str:
    """The newest session date the store carries, as '27 Sep 2026'.

    The build stamp says when the pipeline RAN; this says what it got to,
    which is the question people actually ask of a dashboard ("is last
    weekend in?"). recap's session rows carry ISO dates (recap.py), so this
    is a max over strings and needs no parsing to compare.
    """
    days = [str(s.get("date") or "") for s in (store.get("sessions") or ())]
    days = [d for d in days if len(d) >= 10]
    if not days:
        return ""
    try:
        return f"{_dt.date.fromisoformat(max(days)[:10]):%d %b %Y}"
    except ValueError:
        return ""


if store_mode:
    _through = _sessions_through(store)
    _stamp = (f"🟢 Prebuilt data · Data as of {store['generated_at']}"
              + (f" · sessions through {_through}" if _through else ""))
else:
    auto = "🟢 Live from Google Drive" if (live_ready and not live_failed) else "📤 Manual upload"
    _stamp = f"{auto} · {source_label} · refreshed {datetime.now():%d %b %Y, %H:%M}"
st.caption(" · ".join(x for x in (_stamp, _refresh_note(report)) if x))

# ── the run's own warnings, in Admin ────────────────────────────────────────
# `store["warnings"]` is what the pipeline wants a human to know about the
# week it published — an unregistered webinar, a skipped sheet. It was read
# into a variable and then never rendered anywhere, so nobody has seen one.
# It belongs in the Admin drawer with the rest of the operational detail, not
# as a yellow box over the numbers: these are notes about the data, not a
# reason to distrust what is on screen.
if warnings:
    _admin.caption(f"Notes from the last refresh ({len(warnings)})")
    for _w0 in warnings:
        _admin.caption("• " + str(_w0))


def _marked_roster_download(key: str) -> None:
    """The full marked workbook. In store mode the ~8 MB xlsx isn't in memory —
    it's fetched from Drive only when someone actually asks for it."""
    if not store_mode:
        st.download_button(
            "⬇️ Download marked roster — ALL sessions (.xlsx)",
            data=marked_bytes,
            file_name="Master_Batch_Rosters_marked.xlsx",
            mime=XLSX_MIME, type="primary", key=key,
        )
        return
    # While viewing an archived week, `marked_xlsx_file_id` in that old store
    # points at Master_Batch_Rosters_marked.xlsx, which the pipeline REPLACES
    # every Monday — following it would hand over TODAY's workbook labelled as
    # that week's. Use the dated copy in archive/ instead, and say plainly when
    # a week predates archiving rather than serving the wrong file.
    if _viewing:
        _arch = _archived_marked(_viewing["date"])
        if not _arch:
            st.caption(
                f"No marked workbook was archived for {_viewing['date']} — that "
                "week ran before the archive existed. Only weeks refreshed since "
                "then have one.")
            return
        fid, token = _arch["id"], f"{_arch['id']}@{_viewing['date']}"
        fname = _arch["name"]
    else:
        fid = store.get("marked_xlsx_file_id") or ""
        token = f"{fid}@{store['generated_at_iso']}" if fid else ""
        fname = "Master_Batch_Rosters_marked.xlsx"
    if not fid:
        st.caption("The marked workbook isn’t on Drive yet — it is uploaded by a "
                   "full pipeline run (not by `--no-upload`).")
        return
    # For the LIVE file the Drive id never changes (the pipeline replaces it in
    # place), so the token folds in the build time — otherwise a long-lived
    # session keeps serving last week's workbook after a refresh.
    if st.session_state.get("marked_xlsx_token") != token:
        if st.button("⬇️ Prepare marked roster download (.xlsx)", key=f"prep_{key}",
                     help="Fetches the full marked workbook (~8 MB) from Drive"):
            try:
                with st.spinner("Fetching the marked workbook from Drive…"):
                    st.session_state["marked_xlsx"] = live_data.fetch_file_bytes(
                        live_data._drive_service(), fid)
                    st.session_state["marked_xlsx_token"] = token
            except Exception as e:
                st.error(f"Couldn’t fetch the marked workbook from Drive: {e}")
            else:
                st.rerun()
    if st.session_state.get("marked_xlsx_token") == token:
        st.download_button(
            "⬇️ Download marked roster — ALL sessions (.xlsx)",
            data=st.session_state["marked_xlsx"],
            file_name=fname,
            mime=XLSX_MIME, type="primary", key=key,
        )


# The marked-roster download lives at the top of the Roster tab (it used to sit
# above the tab strip, on every tab).

# Batch list for the Roster tab (the new Dashboard has its own selector).
all_batches = sorted(marked["Batch"].unique(), key=dc.batch_key)


# ─────────────── proof that the fragments below are really fragments ─────────
# Each tab body is an @st.fragment, so a widget inside one repaints that tab
# alone instead of re-running all six. That is invisible from the outside —
# the page looks identical either way — so each body stamps a counter here and
# tests/test_ui_layout.py asserts that changing the Dashboard's batch does not
# move the Roster's. Cheap, and the only observable evidence the wrapping is
# in force; without it a later refactor can quietly drop @st.fragment and no
# test notices.
def _painted(name: str) -> None:
    n = dict(st.session_state.get("_painted") or {})
    n[name] = n.get(name, 0) + 1
    st.session_state["_painted"] = n


def _card(kicker: str, title: str, who: str, mentor: str, value: str,
          why: str) -> str:
    """One Weekend Recap card — the award cards and the Below-the-curve cards
    are the same object and must stay the same object, or the page reads as
    two unrelated blocks that happen to sit under each other.

    Everything here is hand-typed in the schedule (topic, mentor) and renders
    with unsafe_allow_html, so every field is escaped: a stray '<' in a
    session title would otherwise eat the rest of the card, and anything
    worse than that would run.
    """
    e = _html.escape
    return (
        "<div style='border:1px solid rgba(128,128,128,.28);border-radius:10px;"
        "padding:12px 14px'>"
        "<div style='font-size:10px;letter-spacing:.08em;opacity:.7;"
        f"font-weight:700'>{e(str(kicker)).upper()}</div>"
        "<div style='font-weight:650;margin:6px 0 2px;font-size:13px'>"
        f"{e(str(title)[:46])}</div>"
        f"<div style='opacity:.7;font-size:12px'>{e(str(who))}"
        + (f" &middot; {e(str(mentor))}" if mentor else "")
        + "</div><div style='font-size:26px;font-weight:700;margin-top:8px'>"
        f"{e(str(value))}</div>"
        "<div style='opacity:.6;font-size:11px;margin-top:4px;"
        f"line-height:1.35'>{e(str(why))}</div></div>")


# ───────────────────────────── tabs ──────────────────────────────────────────
(tab_dash, tab_sessions, tab_weekend, tab_roster, tab_fcst,
 tab_bsiai, tab_add) = st.tabs(
    ["📊 Dashboard", "📚 Sessions", "🎬 Weekend Recap",
     "📋 Roster (marked attendance)", "🔮 Forecast",
     "💼 BSIAI", "➕ Add data"]
)

# Browse / This week / Trainers are three views of the SAME thing — the session
# list — so they are sub-tabs rather than three more entries on a strip that was
# already too wide to read. The containers are created here and filled further
# down, exactly as the outer tabs are.
with tab_sessions:
    sub_browse, sub_recap, sub_trainer = st.tabs(
        ["🔎 Browse", "🏆 This week", "🎓 Trainers"])

# ============================ TAB 1 — DASHBOARD ==============================
@st.fragment
def _tab_dashboard():
    _painted("dashboard")
    if store_mode:
        # Everything was prebuilt by pipeline.py — render straight from the store.
        # The build stamp is already the caption under the title; not repeated.
        DATA, summary = store["DATA"], store["summary"]
        note = ""
    else:
        # Legacy: build from the roster this server just marked.
        if live_ready and not live_failed:
            names = _attendee_names(st.session_state.nonce)
            note = f"🟢 Live — showing the updated roster · refreshed {datetime.now():%d %b %Y, %H:%M}"
        else:
            names = upload_attendee_names
            note = "📤 Showing the uploaded roster"
        DATA, summary = _build_dashboard(marked_bytes, names, l2_bytes)
    # The batch selector, the domain control and the click-to-select bar all
    # live inside this fragment, so a pick repaints the Dashboard alone.
    dash_view.render(DATA, summary, note, store=store if store_mode else None,
                     rerun_scope="fragment")


with tab_dash:
    _tab_dashboard()

# ============================ TAB 2 — ROSTER =================================
@st.fragment
def _tab_roster():
    _painted("roster")
    # The full marked roster (all sessions) — same deliverable as the original marker
    _marked_roster_download("dl_roster")
    st.caption("The complete marked workbook — every batch, all sessions, Present/Absent.")
    st.divider()
    st.caption("The marked attendance, student-by-student, exactly like the roster sheet.")
    if store_mode:
        grid_batches = [b for b in store["batches"] if b in set(all_batches)] or store["batches"]
    else:
        smap = _sheet_map(marked_bytes)
        grid_batches = [b for b in all_batches if b in smap]
    # NEWEST FIRST, the same ordering the Dashboard's selector uses. This list
    # came straight off `batch_key`, so the tab opened on B17 — a cohort that
    # finished months ago — and every visit began by scrolling to today's.
    grid_batches = dash_view.batch_order(grid_batches)
    ctop1, ctop2 = st.columns([3, 2])
    pick = ctop1.selectbox("Batch", grid_batches,
                           index=0 if grid_batches else None, key="roster_pick")
    show_pii = ctop2.checkbox("Show full contact details (real PII)", value=False,
                              help="Off by default — emails/phones are masked.")
    g = None
    if pick:
        if store_mode:
            g = _store_grid(store["path"], pick, store["generated_at_iso"])
        else:
            g = _grid(marked_bytes, smap[pick])
    if g is None:
        st.info("No roster rows to show for this batch — try 🔄 Refresh from Google."
                if pick else "No batch sheets to show.")
    else:
        # Defaults ON, which is what the removed "Count basis" radio did (it
        # defaulted to "Active only"). Behaviour here is deliberately unchanged.
        # Note it drops refunds AND blank-payment rows, and only ~1/3 of those
        # are actual refunds - 1,033 of 3,030 on the 2026-09-22 roster, the rest
        # are simply missing a Payment value. Dashboard percentages are not
        # affected either way: they always use total strength (data.py).
        active_only = st.checkbox("Active learners only", value=True, key="roster_active")
        disp, sess_cols, n_students, n_present_any = _roster_display(
            g, pick, active_only, show_pii,
            store["generated_at_iso"] if store_mode else "")
        T.tiles([
            {"label": "Students shown", "value": f"{n_students:,}"},
            {"label": "Attended ≥1 session", "value": f"{n_present_any:,}"},
            {"label": "Sessions", "value": len(sess_cols)},
        ])

        def _hl(v):
            """Attendance glyphs in the theme's status colours — the same
            good / low pair the dashboard pills use, from ui_theme, so a
            rebrand changes them in one place."""
            if v == _MARK_PRESENT:
                b = T.STATUS["high"]
            elif v == _MARK_ABSENT:
                b = T.STATUS["low"]
            else:
                return ""
            return f"background-color: {b['bg']}; color:{b['fg']}"
        styled = disp.style.map(_hl, subset=sess_cols)  # .map (pandas >=2.1; applymap removed in 3.0)
        st.dataframe(
            styled, width='stretch', hide_index=True, height=520,
            column_config={
                # Pinned, so the person a row belongs to stays on screen while
                # you scroll thirty session columns sideways. Without it the
                # glyphs are anonymous past about the eighth column.
                "Email": st.column_config.TextColumn("Email", pinned=True),
                **{c: st.column_config.TextColumn(c, width="small")
                   for c in sess_cols},
            })
        st.caption(f"{_MARK_PRESENT} present · {_MARK_ABSENT} absent · "
                   f"{_MARK_BLANK} not marked for that session (the student "
                   "was not in the room the column covers, or joined the "
                   "batch after it ran).")


with tab_roster:
    _tab_roster()

# ========================== TAB 4 — FORECAST =================================
# Predicted attendance for sessions that have not run yet, built by pipeline.py
# from the dashboard's own DATA plus the Master Curriculum Schedule. Aggregates
# only. Everything shown here is a PREDICTION — the copy says so in every place
# a number could otherwise be mistaken for a measurement.
@st.fragment
def _tab_forecast():
    _painted("forecast")
    _f = (store or {}).get("forecast") if store_mode else None

    if not store_mode:
        st.caption("No forecast in this data set.")
        with st.expander("Notes on this forecast", expanded=False):
            st.info("The Forecast tab reads the prebuilt store. It is empty in "
                    "upload / legacy-live mode because the curriculum schedule is a "
                    "separate Sheet the app does not fetch at runtime.")
    elif _f is None:
        st.caption("No forecast in this data set yet.")
        with st.expander("Notes on this forecast", expanded=False):
            st.warning(
                "**The forecast is not configured yet.** Add `CURRICULUM_ID` (repo "
                "secret, or `curriculum_id` under `[drive]` in secrets.toml) pointing "
                "at the Master Curriculum Schedule, and share that Sheet with the "
                "service account. The next refresh will fill this tab in."
            )
    elif not _f.get("sessions"):
        st.caption("The forecast produced no sessions in the last refresh.")
        with st.expander("Notes on this forecast", expanded=False):
            for _w in _f.get("warnings", []):
                st.caption("• " + str(_w))
    else:
        import pandas as _pd

        _acc = _f.get("accuracy") or {}
        _rows = _f["sessions"]

        st.caption(
            f"Predicted attendance for the next {_f['horizon_weeks']} weeks, from "
            f"the batch's own attendance so far and the decay curve measured "
            f"across every past batch. Built {store['generated_at']}."
        )

        # Count SESSIONS, not per-batch rows: one session that three batches sit
        # in is one thing to run and staff, not three. The rows are still there
        # in the detail table and the full CSV.
        _ft = [
            {"label": "Sessions ahead", "value": f"{len(_f.get('by_session') or []):,}",
             "help": f"{len(_rows):,} batch-slots across them — a session that "
                     "several batches attend counts once here."},
            {"label": "Predicted attendees", "value": f"{sum(r['pred'] for r in _rows):,}"},
        ]
        if _acc.get("mape_1_4wk") is not None:
            _ft.append({"label": "Typical error, 1–4 wks", "value": f"±{_acc['mape_1_4wk']}%",
                        "help": "Mean absolute % error on headcount, measured by "
                                "replaying every past batch: fit on what was known at "
                                "the time, score against what actually happened."})
        if _acc.get("baseline_mape_1_4wk") is not None:
            _ft.append({"label": "Naive baseline", "value": f"±{_acc['baseline_mape_1_4wk']}%",
                        "help": "What you would get by assuming the next session "
                                "repeats the last one's attendance rate. The model is "
                                "only worth having while it beats this."})
        T.tiles(_ft)

        if _f.get("warnings"):
            with st.expander(f"Notes on this forecast ({len(_f['warnings'])})",
                             expanded=False):
                for _w in _f["warnings"]:
                    st.caption("• " + str(_w))

        # ── every session, batch-wise and total ──────────────────────────────
        # One row per SESSION with a column per batch, because that is the unit
        # the programme is staffed and briefed in: "Build Your Marketing OS on
        # 6 Sep draws ~230 across B35/B36/B37", not three unrelated numbers.
        st.subheader("Every session — batch-wise and total")
        _sess = _f.get("by_session") or []
        _all_b = sorted({b for s in _sess for b in s["batches"]}, key=dc.batch_key)

        # All three filters are multiselects: they are type-to-search and apply on
        # selection. A free-text box would only commit on Enter, which reads as a
        # broken filter to anyone who types and then looks straight at the table.
        c1, c2, c3 = st.columns([2, 2, 3])
        _fdate = c1.multiselect("Date", list(dict.fromkeys(
            _pd.to_datetime([s["date"] for s in _sess]).strftime("%a %d %b"))),
            key="fcst_date")
        _fpod = c2.multiselect("Domain", sorted({s["pod"] for s in _sess}),
                               key="fcst_pod")
        _ftopic = c3.multiselect("Topic — type to search",
                                 sorted({s["topic"] for s in _sess}),
                                 key="fcst_topic")

        _v = _sess
        if _fpod:
            _v = [s for s in _v if s["pod"] in _fpod]
        if _ftopic:
            _v = [s for s in _v if s["topic"] in _ftopic]
        _tbl = []
        for s in _v:
            _lbl = _pd.to_datetime(s["date"]).strftime("%a %d %b")
            if _fdate and _lbl not in _fdate:
                continue
            _r = {"Date": _lbl, "Domain": s["pod"], "Topic": s["topic"],
                  "Trainer": s["trainer"] or "—"}
            for b in _all_b:
                _pb = s["per_batch"].get(b)
                _r[b] = _pb["pred"] if _pb else None
            _r["TOTAL"] = s["pred"]
            _r["Range"] = f"{s['lo']:,} – {s['hi']:,}"
            _r["Of"] = s["denom"]
            _r["%"] = s["pred_pct"]
            _r["≈"] = "≈" if s["any_assumed"] else ""
            _tbl.append(_r)

        if not _tbl:
            st.info("No sessions match those filters.")
        else:
            _tdf = _pd.DataFrame(_tbl)
            st.dataframe(
                _tdf, width='stretch', hide_index=True, height=520,
                column_config={
                    **{b: st.column_config.NumberColumn(b, format="%d", width="small")
                       for b in _all_b},
                    "TOTAL": st.column_config.NumberColumn(
                        "TOTAL", format="%d",
                        help="Sum across the batches running this session"),
                    "Of": st.column_config.NumberColumn(
                        "Of", format="%d",
                        help="Combined size of the groups invited"),
                    "%": st.column_config.NumberColumn("%", format="%.1f%%"),
                    "≈": st.column_config.TextColumn(
                        "≈", width="small",
                        help="At least one batch here has not started, so its "
                             "size is assumed rather than measured"),
                })
            st.caption(
                f"Showing **{len(_tbl)}** of {len(_sess)} sessions · "
                f"**{sum(r['TOTAL'] for r in _tbl):,}** predicted attendees. "
                "Blank cell = that batch is not scheduled for this session. "
                "The range widens for dates further out, and totals add each "
                "batch's range rather than assuming their errors cancel — "
                "batches sharing a date also share their bad days.")

        _c1, _c2 = st.columns(2)
        _c1.download_button(
            "⬇️ Sessions, batch-wise (.csv)",
            data=(_pd.DataFrame(_tbl).to_csv(index=False).encode("utf-8")
                  if _tbl else b""),
            file_name=f"forecast_by_session_{_f.get('generated_for','')}.csv",
            mime="text/csv", key="dl_fcst_sess", disabled=not _tbl)
        _c2.download_button(
            "⬇️ Full detail, one row per batch (.csv)",
            data=_pd.DataFrame(_rows).to_csv(index=False).encode("utf-8"),
            file_name=f"attendance_forecast_{_f.get('generated_for','')}.csv",
            mime="text/csv", key="dl_fcst")

        with st.expander("How this is calculated, and how much to trust it"):
            st.markdown(
                "**predicted = group size × decay curve(week) × batch offset × "
                "domain multiplier**\n\n"
                "- **Decay curve** — attendance against weeks-since-batch-start, "
                "pooled over every past batch. The shape is very stable: batches "
                "open near 50% and fall about 10% a week.\n"
                "- **Batch offset** — how this batch is actually tracking against "
                "that curve. A batch running cold shows up here first.\n"
                "- **Domain multiplier** — how a domain draws relative to its own "
                "batch on the same day. **This is the weak layer**: the domain split "
                "only began in late Aug 2026, so it rests on few sessions and is "
                "shrunk toward the batch average.\n\n"
                "Accuracy is re-measured on live data every refresh by replaying "
                "history, so if the model stops working this page will say so.")
            _cA, _cB = st.columns(2)
            with _cA:
                st.caption("**Error by weeks ahead** (measured)")
                _mh = _acc.get("mape_by_horizon") or {}
                if _mh:
                    st.dataframe(_pd.DataFrame(
                        {"Weeks ahead": list(_mh), "Error ±%": list(_mh.values())}),
                        width='stretch', hide_index=True, height=240)
            with _cB:
                st.caption("**Domain multipliers** (>1 attends more than its batch)")
                _pmd = _f.get("pod_multiplier") or {}
                if _pmd:
                    st.dataframe(_pd.DataFrame(
                        [{"Domain": k, "×": v["mult"], "Sessions seen": v["n"]}
                         for k, v in sorted(_pmd.items(),
                                            key=lambda kv: -kv[1]["mult"])]),
                        width='stretch', hide_index=True, height=240)
            st.caption("**Batch offset** — ×1.00 is exactly on the curve; "
                       "below 1 means that batch is running cold")
            _bo = _f.get("batch_offset") or {}
            if _bo:
                st.dataframe(_pd.DataFrame(
                    [{"Batch": k, "×": v} for k, v in sorted(
                        _bo.items(), key=lambda kv: dc.batch_key(kv[0]))]),
                    width='stretch', hide_index=True, height=240)


with tab_fcst:
    _tab_forecast()

# ===================== SESSIONS - BROWSE (sub-tab) ===========================
@st.fragment
def _sub_browse():
    _painted("browse")
    _S = (store or {}).get("sessions") if store_mode else None
    if _S:
        # Backfill from DATA for any field the stored sessions section predates.
        # The store is only as new as the last pipeline run, so a code change can
        # land before the data does -- that is exactly how `rating_trainer` came
        # to render as a column of dashes while the value sat in DATA all along.
        # DATA is in the same file, keyed the same way, so healing it is free.
        _byk = {}
        for _b, _d in (store.get("DATA") or {}).items():
            for _x in _d.get("sessions") or ():
                if _x.get("mm"):
                    _byk[(_b, _x["mm"], _x.get("pod") or "")] = _x
        for _r in _S:
            _src = _byk.get((_r["batch"], _r.get("mm"), _r.get("pod") or ""))
            if not _src:
                continue
            for _f2 in ("rating", "rating_trainer", "rating_n", "rating_nps",
                        "rating_dist", "l2_batch", "mentor", "topic",
                        "shared_batches", "rating_shared"):
                if _r.get(_f2) in (None, "") and _src.get(_f2) not in (None, ""):
                    _r[_f2] = _src[_f2]
            if _r.get("nps") is None and _src.get("rating_nps") is not None:
                _r["nps"] = _src["rating_nps"]
    if not store_mode:
        st.caption("No session list in this data set.")
        with st.expander("Why", expanded=False):
            st.info("The session browser reads the prebuilt data file.")
    elif not _S:
        st.warning("No sessions in the last refresh.")
    else:
        import pandas as _pd
        st.caption("Every logged session across every batch. Filter, then tick "
                   "a row for its full breakdown.")
        with st.expander("How this is calculated", expanded=False):
            st.markdown(
                "**Attendance % = present ÷ strength of whoever was invited** — "
                "the batch, a domain, a compound room, or the day's common room "
                "— taken from the roster automatically, so it is filled in for all "
                f"{len({s['batch'] for s in _S})} batches, not only the ones somebody "
                "remembered to configure.\n\n"
                "Title, trainer, duration and peak are one session's facts, so "
                "they span its batches. **Att %**, **Present**, **Absent** and the "
                "first rating block are per batch — each batch's own roster, and "
                "its own students' poll answers. Present + Absent is that row's "
                "strength: for a domain session that is the domain's strength, "
                "not the whole batch's — the same denominator Att % uses. "
                "The **Whole room** block is a shared room's poll over everyone "
                "found on a sharing roster, once — an answer from an email on "
                "none of those rosters (a BSIAI student in a shared AI CAP "
                "room) is counted nowhere, the Whole room figure included; for a "
                "single-batch session Whole room equals the batch's own. Hover a "
                "rating cell in a shared session for the split and the count of "
                "everyone who answered. *anonymous poll* means the hosts ran that "
                "feedback poll anonymously — no emails in the export, so it "
                "cannot be divided; only the Whole room figure exists.\n\n"
                "**Duration (hrs)** is first-join to last-leave, not Zoom's "
                "'Actual Duration' — that one runs from the host starting to the "
                "host leaving, so a trainer who forgets to end the webinar "
                "inflates it. **Peak** is the highest number of people in the "
                "room at once, swept from the join/leave times: it matches Zoom's "
                "own *Max Concurrent Views* exactly on 93% of the reports that "
                "carry that field, and covers the 64% that do not. **Simulive** "
                "is blank unless the schedule says so — only 49 of 1,610 mentor "
                "rows record it, so a blank means *not recorded*, never *Live*."
            )

        # ── filters ──────────────────────────────────────────────────────────
        _dates = sorted({s["date"] for s in _S})
        f1, f2, f3 = st.columns([2, 1, 1])
        _dr = f1.date_input(
            "Date range",
            value=(_dt.date.fromisoformat(_dates[0]), _dt.date.fromisoformat(_dates[-1])),
            min_value=_dt.date.fromisoformat(_dates[0]),
            max_value=_dt.date.fromisoformat(_dates[-1]), key="ss_dates")
        _fb = f2.multiselect("Batch", sorted({s["batch"] for s in _S},
                                             key=dc.batch_key), key="ss_batch")
        _fp = f3.multiselect("Domain", sorted({s["pod"] for s in _S if s["pod"]}),
                             key="ss_pod")
        g1, g2, g3 = st.columns([2, 1, 2])
        _ft = g1.multiselect("Trainer", sorted({t for s in _S
                                                for t in (s.get("trainers") or [])}),
                             key="ss_trainer")
        _fy = g2.multiselect("Trainer type",
                             sorted({s["trainer_type"] for s in _S
                                     if s.get("trainer_type")}), key="ss_ttype")
        _fq = g3.text_input("Search title", key="ss_q").strip().lower()

        _v = _S
        if isinstance(_dr, (tuple, list)) and len(_dr) == 2:
            _a, _b = _dr[0].isoformat(), _dr[1].isoformat()
            _v = [s for s in _v if _a <= s["date"] <= _b]
        if _fb:
            _v = [s for s in _v if s["batch"] in _fb]
        if _fp:
            _v = [s for s in _v if s["pod"] in _fp]
        if _ft:
            _v = [s for s in _v if set(s.get("trainers") or []) & set(_ft)]
        if _fy:
            _v = [s for s in _v if s.get("trainer_type") in _fy]
        if _fq:
            _v = [s for s in _v if _fq in (s["topic"] or "").lower()]

        # ── headline numbers, over the FILTERED set ──────────────────────────
        # Sessions and polls are counted over ROOMS (recap.group_sessions): a
        # webinar three batches sat in is one session with one poll, not three.
        # Attendance stays over the batch rows - each against its own roster.
        _G = _recap_mod.group_sessions(_v)
        _rated = [g for g in _G if g.get("rating") is not None]
        _rn = sum(g["rating_n"] for g in _rated)
        _merged = _polls_mod.merge_dists(g.get("dist") for g in _G)
        _pres = sum(s["present"] for s in _v)
        _inv = sum(s["total"] for s in _v)
        _trd = [g for g in _G if g.get("rating_trainer") is not None]
        _trn = sum(g["rating_n"] for g in _trd)
        _nps = _polls_mod.nps_from_dist(_merged.get("recommend"))
        T.tiles([
            {"label": "Sessions", "value": f"{len(_G):,}",
             "help": f"{len(_v):,} batch rows. A room several batches sat in "
                     "is one session here."},
            {"label": "Attendance", "value": f"{_pres / _inv * 100:.1f}%" if _inv else "—",
             "help": f"{_pres:,} present of {_inv:,} invited, pooled — not a "
                     "mean of per-session percentages."},
            {"label": "Avg overall",
             "value": (f"{sum(g['rating'] * g['rating_n'] for g in _rated) / _rn:.2f}"
                       if _rn else "—"),
             "help": "Weighted by responses, so a 12-response session does not "
                     "outweigh a 900-response one. A shared room's poll counts "
                     "once, whole."},
            {"label": "Avg trainer",
             "value": (f"{sum(g['rating_trainer'] * g['rating_n'] for g in _trd) / _trn:.2f}"
                       if _trn else "—")},
            {"label": "NPS", "value": f"{_nps:+d}" if _nps is not None else "—",
             "help": "Promoter 5, passive 4, detractor 1-3, computed from the "
                     "SUMMED 1-5 histograms of every session shown — not an "
                     "average of their NPS percentages."},
        ])

        # ── the table ────────────────────────────────────────────────────────
        # ONE SESSION, MANY BATCHES. A session several batches sit in produces
        # one row per batch, because attendance is measured against each batch's
        # own roster. Showing those as separate lines repeats the title, trainer,
        # duration and peak 2-3 times over -- 140 of 522 rows were repeats.
        # So the rows are kept and the room-level columns are MERGED with
        # rowspan, exactly as they would be in a spreadsheet. What counts as one
        # session is recap.session_key - the same rule the trainer and weekly
        # rollups use - so this table and those numbers cannot disagree.
        #
        # RATINGS ARE PER BATCH since the poll split (pipeline [5a.1]): each
        # batch's cells hold ITS students' answers, and the poll over everyone
        # found on a sharing roster sits once in the merged "Joint" columns
        # (rating_shared.joint). A respondent on NO sharing roster - a BSIAI
        # student in a shared AI CAP room - is counted nowhere, the Joint
        # figure included (owner's decision, 2026-09-28); the whole room's
        # count survives in rating_shared.room, and that is what "Whole room"
        # means wherever it is printed below. An older store has no `room`:
        # its joint WAS the room, so every reader falls back to joint. When
        # the poll could not be split (an export naming nobody) every batch
        # shows the joint figure and the cell says so.
        _groups: dict = {}
        for s in _v:
            _groups.setdefault(_recap_mod.session_key(s), []).append(s)
        _gof = {g["key"]: g for g in _G}          # key -> the room's rollup

        def _f(v, fmt="{:.1f}", dash="—"):
            return fmt.format(v) if isinstance(v, (int, float)) else dash

        def _bar(v):
            if not isinstance(v, (int, float)):
                return "—"
            return (f'<div class="bw"><div class="bf" style="width:'
                    f'{max(0, min(100, v / 5 * 100)):.0f}%"></div>'
                    f'<span>{v:.2f}</span></div>')

        def _own_title(s, n):
            """Why a batch's own cells read as they do, as a hover title."""
            sh = s.get("rating_shared") or {}
            if n == 1 or not sh:
                return ""
            if sh.get("split"):
                # The room is `room` (everyone who answered), never `joint`
                # (only those on a sharing roster) - or the unmatched count
                # beside it would name people outside the figure it is
                # printed against.
                w = sh.get("room") or sh.get("joint") or {}
                return (f' title="{s["batch"]}\'s own students only. Everyone who '
                        f'answered: {w.get("responses") or 0}'
                        + (f', {sh["unmatched"]} on no sharing roster'
                           + (' (not in the Whole room columns)' if sh.get("room") else '')
                           if sh.get("unmatched") else "") + '"')
            return (' title="This poll was run anonymously (no emails in the '
                    'export), so it cannot be divided between the batches. '
                    'The room\'s figure is in the Whole room columns."')

        def _unsplit(s, n):
            """True when a shared room's poll could NOT be divided per batch.

            Those cells read '—', never a repeat of the room's number: the
            whole point of the per-batch block is that it is per batch, and a
            figure copied three times says nothing a reader can act on.
            """
            sh = s.get("rating_shared") or {}
            return n > 1 and bool(sh) and not sh.get("split")

        _rows_html = []
        _gsort = lambda k: (_gof[k].get("date") or "", _gof[k].get("batch") or "")
        for k in sorted(_groups, key=_gsort, reverse=True):
            grp = sorted(_groups[k], key=lambda s: dc.batch_key(s["batch"]))
            n = len(grp)
            g0, G = grp[0], _gof[k]
            # merged cells: written once, spanning every batch row in the session
            merged = (
                f'<td rowspan="{n}">{_html.escape(g0["date"])}</td>'
                f'<td rowspan="{n}" class="t">{_html.escape(g0.get("topic") or "—")}</td>'
                f'<td rowspan="{n}">{_html.escape(g0.get("trainer") or "—")}</td>'
                f'<td rowspan="{n}">{_html.escape(g0.get("trainer_type") or "—")}</td>'
                f'<td rowspan="{n}">{_html.escape(g0.get("pod") or "—")}</td>')
            # the room: duration, peak and the WHOLE poll, once
            tail = (
                f'<td rowspan="{n}" class="n">{_f(G.get("duration_hrs"))}</td>'
                f'<td rowspan="{n}" class="n">{_f(G.get("peak"), "{:,.0f}")}</td>'
                f'<td rowspan="{n}" class="n">{_f(G.get("rating_trainer"), "{:.2f}")}</td>'
                f'<td rowspan="{n}">{_bar(G.get("rating"))}</td>'
                f'<td rowspan="{n}" class="n">{_f(G.get("nps"), "{:+.0f}")}</td>'
                f'<td rowspan="{n}" class="n">{G.get("rating_n") or 0:,}</td>')
            for i, s in enumerate(grp):
                # per-batch: attendance against its own roster, and its own
                # students' answers to the poll
                t = _own_title(s, n)
                if _unsplit(s, n):
                    own = (f'<td class="n"{t}>—</td><td{t}><span class="anon">'
                           f'anonymous poll</span></td>'
                           f'<td class="n"{t}>—</td><td class="n"{t}>—</td>')
                else:
                    own = (f'<td class="n"{t}>{_f(s.get("rating_trainer"), "{:.2f}")}</td>'
                           f'<td{t}>{_bar(s.get("rating"))}</td>'
                           f'<td class="n"{t}>{_f(s.get("nps"), "{:+.0f}")}</td>'
                           f'<td class="n"{t}>{s.get("rating_n") or 0:,}</td>')
                # Present/Absent are the COUNTS behind Att %, on the same
                # denominator: a POD session is scored against that POD's
                # strength, not the whole batch, so these add up per row.
                _pres, _tot = s.get("present"), s.get("total")
                _abs = (_tot - _pres if isinstance(_pres, (int, float))
                        and isinstance(_tot, (int, float)) else None)
                per = (f'<td>{_html.escape(s["batch"])}</td>'
                       f'<td class="n">{_f(s.get("pct"))}%</td>'
                       f'<td class="n">{_f(_pres, "{:,.0f}")}</td>'
                       f'<td class="n">{_f(_abs, "{:,.0f}")}</td>' + own)
                _rows_html.append("<tr>" + (merged if i == 0 else "") + per
                                  + (tail if i == 0 else "") + "</tr>")

        # Scoped under .sess-wrap: these rules used to be global and restyled
        # the Dashboard tab's own sessions table (12px, no wrapping).
        st.markdown(
            f"""<style>
            .sess-wrap{{max-height:560px;overflow:auto;border:1px solid rgba(128,128,128,.28);border-radius:8px}}
            .sess-wrap table.sess{{border-collapse:collapse;width:100%;font-size:12px}}
            .sess-wrap table.sess th{{position:sticky;top:0;background:{T.INK['surface']};text-align:left;
              padding:7px 9px;font-weight:600;color:{T.INK['secondary']};border-bottom:1px solid rgba(128,128,128,.28);
              white-space:nowrap;z-index:1}}
            .sess-wrap table.sess td{{padding:6px 9px;border-bottom:1px solid rgba(128,128,128,.14);
              border-right:1px solid rgba(128,128,128,.08);white-space:nowrap;vertical-align:middle}}
            .sess-wrap table.sess td.n{{text-align:right;font-variant-numeric:tabular-nums}}
            .sess-wrap table.sess td.t{{max-width:300px;overflow:hidden;text-overflow:ellipsis}}
            .sess-wrap table.sess td[rowspan]{{background:rgba(128,128,128,.04)}}
            .sess-wrap .bw{{display:flex;align-items:center;gap:6px;min-width:96px}}
            .sess-wrap .bf{{height:7px;background:{T.BRAND['accent']};border-radius:4px}}
            .sess-wrap .bw span{{font-variant-numeric:tabular-nums}}
            .sess-wrap .anon{{opacity:.6;font-size:11px;font-style:italic;white-space:nowrap}}
            </style>""", unsafe_allow_html=True)
        # ── MARK A ROW, GET ITS BREAKDOWN ────────────────────────────────
        # Streamlit's row selection only exists on a real dataframe; the merged
        # table below is raw HTML and carries no click events, which is why the
        # drill-down used to need a separate dropdown nobody scrolled to. This
        # is the surface people click; the merged view is kept as the compact
        # read, one row per session instead of one per batch.
        _order = sorted(_groups, key=_gsort, reverse=True)
        _flat = [(k, x) for k in _order
                 for x in sorted(_groups[k], key=lambda y: dc.batch_key(y["batch"]))]
        _dfrows = []
        for _k, _s in _flat:
            _G2 = _gof[_k]
            _u2 = _unsplit(_s, len(_groups[_k]))
            _p2, _t2 = _s.get("present"), _s.get("total")
            _dfrows.append({
                "Date": _s["date"], "Title": _s.get("topic") or "",
                "Trainer": _G2.get("trainer") or "", "Type": _G2.get("trainer_type") or "",
                "Domain": _s.get("pod") or "", "Batch": _s["batch"],
                "Att %": _s.get("pct"),
                # The ranking key the rest of the app uses, on the surface
                # people actually sort. Att % alone always favours the
                # youngest cohort in view.
                "vs expected": _s.get("index"), "Present": _p2,
                "Absent": (_t2 - _p2 if isinstance(_p2, (int, float))
                           and isinstance(_t2, (int, float)) else None),
                # None, never the room's figure, when a shared poll could not be
                # divided - the same rule the merged table uses.
                "Trainer ★": None if _u2 else _s.get("rating_trainer"),
                "Overall": None if _u2 else _s.get("rating"),
                "NPS": None if _u2 else _s.get("nps"),
                "Resp": None if _u2 else (_s.get("rating_n") or 0),
                "Dur (h)": _G2.get("duration_hrs"), "Peak": _G2.get("peak"),
                "Whole room ★": _G2.get("rating_trainer"),
                "Whole room overall": _G2.get("rating"), "Whole room NPS": _G2.get("nps"),
                "Whole room resp": _G2.get("rating_n") or 0,
            })
        _num = st.column_config.NumberColumn
        _room_help = ("The shared room's poll over everyone found on a sharing "
                      "roster, counted once. Equals the batch's own for a "
                      "single-batch session.")
        _sel = st.dataframe(
            _pd.DataFrame(_dfrows), hide_index=True, height=430, width="stretch",
            on_select="rerun", selection_mode="single-row", key="ss_rows",
            column_config={
                "Att %": _num(format="%.1f%%"),
                "vs expected": _num(
                    format="%.2f×",
                    help="What this batch drew over what the decay curve says a "
                         "cohort of that age, level and domain should draw. "
                         "1.00 is exactly on curve."),
                "Present": _num(format="%d"), "Absent": _num(format="%d"),
                "Trainer ★": _num(format="%.2f"),
                "Overall": st.column_config.ProgressColumn(
                    format="%.2f", min_value=0, max_value=5),
                "NPS": _num(format="%+d"), "Resp": _num(format="%d"),
                "Dur (h)": _num(format="%.1f"), "Peak": _num(format="%d"),
                "Whole room ★": _num(format="%.2f", help=_room_help),
                "Whole room overall": st.column_config.ProgressColumn(
                    format="%.2f", min_value=0, max_value=5, help=_room_help),
                "Whole room NPS": _num(format="%+d", help=_room_help),
                "Whole room resp": _num(format="%d", help=_room_help),
            })
        try:
            _selrows = list((_sel.selection or {}).get("rows") or [])
        except Exception:
            _selrows = []          # an older Streamlit without row selection
        st.caption("⬜ Tick a row to open its full breakdown below — "
                   "retention curve, stickiness, ratings and the per-batch split.")

        with st.expander(f"Compact view — {len(_groups):,} sessions, "
                         "one row each (merged cells)", expanded=False):
            st.markdown(
                '<div class="sess-wrap"><table class="sess"><tr>'
                '<th>Date</th><th>Title</th><th>Trainer</th><th>Type</th><th>Domain</th>'
                '<th>Batch</th><th>Att %</th><th>Present</th><th>Absent</th>'
                '<th>Trainer ★</th><th>Overall</th><th>NPS</th><th>Resp</th>'
                '<th>Dur (h)</th><th>Peak</th>'
                '<th>Whole room ★</th><th>Whole room overall</th>'
                '<th>Whole room NPS</th><th>Whole room resp</th>'
                '</tr>'
                + "".join(_rows_html) + '</table></div>',
                unsafe_allow_html=True)
        st.caption(f"{len(_groups):,} sessions · {len(_v):,} batch rows. "
                   "What each column means is under *How this is calculated* "
                   "at the top of this tab.")

        def _sh(s, k, default=None):
            return ((s.get("rating_shared") or {}).get("joint") or {}).get(k, default)

        def _own(s, k):
            """A batch's own poll value, or blank when there is no such thing.

            Same rule as the table: a shared room whose poll could not be
            divided has ONE figure, and repeating it in a per-batch column
            would be the copied number this change exists to remove.
            """
            sh = s.get("rating_shared") or {}
            return None if (sh and not sh.get("split")) else s.get(k)

        _tbl = _pd.DataFrame([{
            "Date": s["date"], "Title": s["topic"], "Trainer": s.get("trainer"),
            "Type": s.get("trainer_type"), "Batch": s["batch"],
            "L2 batch": s.get("l2_batch"), "POD": s["pod"],
            "Shared with": ", ".join(b for b in (s.get("shared_batches") or [])
                                     if b != s["batch"]),
            "Attendance %": s["pct"], "Present": s["present"],
            "Absent": (s["total"] - s["present"]
                       if isinstance(s.get("present"), (int, float))
                       and isinstance(s.get("total"), (int, float)) else None),
            "Invited": s["total"], "vs curve": s.get("index"),
            "Duration (hrs)": s.get("duration_hrs"), "Peak": s.get("peak"),
            "Trainer rating": _own(s, "rating_trainer"), "Overall": _own(s, "rating"),
            "NPS": _own(s, "nps"), "Responses": _own(s, "rating_n"),
            "Joint trainer rating": _sh(s, "trainer"),
            "Joint overall": _sh(s, "session"), "Joint NPS": _sh(s, "nps"),
            "Joint responses": _sh(s, "responses"),
            "Whole-room responses": (((s.get("rating_shared") or {}).get("room")
                                      or (s.get("rating_shared") or {}).get("joint")
                                      or {}).get("responses")),
            "Unmatched respondents": (s.get("rating_shared") or {}).get("unmatched"),
            "Own rating unavailable": (
                "anonymous poll" if ((s.get("rating_shared") or {}).get("reason")
                                     == "no-emails") else ""),
            "Simulive": s.get("session_type") or "",
        } for s in _v])
        st.download_button("Download these sessions (CSV)",
                           _tbl.to_csv(index=False).encode(),
                           file_name="sessions.csv", mime="text/csv",
                           key="dl_sessions")

        st.divider()
        if not _selrows:
            st.caption("Tick a row in the table above to see that session's full "
                       "breakdown here — attendance, retention curve, "
                       "stickiness and how the room rated it.")
        else:
            # The TICKED ROW is one batch of one session. Room facts (duration,
            # peak, the curve) belong to the whole room and are shown as such;
            # attendance and ratings are that batch's own, because those are the
            # numbers measured against its roster and answered by its students.
            _k0, s = _flat[_selrows[0]]
            _grp = sorted(_groups[_k0], key=lambda x: dc.batch_key(x["batch"]))
            _Gp = _gof[_k0]
            st.subheader(s["topic"] or "Session")
            st.caption(f"{s.get('l2_batch') or s['batch']} · {s['date_lbl']} "
                       f"({s['date']})"
                       + (f" · {s['pod']}" if s["pod"] else "")
                       + (f" · {s['trainer']}" if s.get("trainer") else "")
                       + (f" ({s['trainer_type']})" if s.get("trainer_type") else ""))

            def _m(label, val, fmt="{:,}", help=None):
                """One tile: the value formatted exactly as before, '—' when
                there is no number."""
                return {"label": label,
                        "value": fmt.format(val) if isinstance(val, (int, float)) else "—",
                        "help": help}

            _curve = _Gp.get("retention") or s.get("retention")
            # END COUNT is the MEAN over the closing ten minutes, never the
            # final minute. Two reasons, and they agree: it is what the
            # weekly-sessions-analysis app means by the words ("Average
            # attendee count over the last 10 minutes"), so the two dashboards
            # can be read against each other - and the final minute of a Zoom
            # export is frequently one person who never clicked Leave. Scored
            # on that, 74 of this store's 536 rated sessions read 0.00,
            # including a 1,698-strong room rated 4.45.
            _end = (round(sum(_curve[-10:]) / len(_curve[-10:]))
                    if _curve else None)
            _sh0 = s.get("rating_shared") or {}
            _mine = {} if (_sh0 and not _sh0.get("split")) else s

            # RETENTION SCORE - did they stay AND did they like it, in one
            # number out of 5. Deliberately the same formula the
            # weekly-sessions-analysis app uses on its own session card
            # (end / peak x overall rating), so the two dashboards can be read
            # against each other.
            #
            # ONE CHANGE: no poll means NO SCORE. Theirs multiplies by a rating
            # of 0 and prints 0.00, so a full room nobody happened to survey
            # reads as the worst session on record. 79 of 640 sessions here have
            # no rating, so that is not a hypothetical.
            #
            # The rating is THIS BATCH'S own (the same figure the Overall tile
            # below shows), while end and peak are the whole room's - so the
            # score is checkable by eye against the tiles beside it. For a
            # single-batch session, which is most of them, the two are the same
            # thing anyway.
            _peak0 = _Gp.get("peak") or s.get("peak")
            _ov0 = _mine.get("rating")
            _ret = ((_end / _peak0) * _ov0
                    if _end and _peak0 and _ov0 is not None else None)
            T.tiles([
                _m("Retention score", _ret, "{:.2f}",
                   help=("End count ÷ peak × overall rating, out of 5"
                         + (f" — {_end:,} ÷ {_peak0:,} × {_ov0:.2f}"
                            if _ret is not None else "")
                         + ". Rewards a session that both holds the room and rates "
                           "well. Blank when no poll was run: a session nobody was "
                           "asked about has no score, which is not the same as a "
                           "bad one.")),
                _m("vs expected", s.get("index"), "{:.2f}× expected",
                   help="What this batch actually drew over what the decay curve "
                        "says a cohort of its age, level and domain should draw. "
                        "1.00 is exactly on curve. Ranking on raw attendance just "
                        "crowns the youngest cohort every week."),
                _m("Stickiness (10 min)", _Gp.get("stick10") or s.get("stick10"),
                   "{:.0f}%", help="Mean concurrency over the closing 10 minutes as "
                                   "a share of the session's peak — the end "
                                   "count above, over the peak beside it."),
                _m("Stickiness (30 min)", _Gp.get("stick30") or s.get("stick30"),
                   "{:.0f}%"),
            ])

            T.tiles([
                _m("Overall", _mine.get("rating"), "{:.2f}",
                   help=("This batch's own students. The whole room's poll is in "
                         "the table below.") if len(_grp) > 1 else None),
                _m("Trainer", _mine.get("rating_trainer"), "{:.2f}"),
                _m("NPS", _mine.get("nps"), "{:+d}",
                   help="Promoter 5, passive 4, detractor 1-3 on the recommend "
                        "question."),
                _m("Peak", _Gp.get("peak") or s.get("peak"),
                   help="Most people in the room at once, swept from the join and "
                        "leave times — the whole room, every batch in it."),
                _m("Duration", _Gp.get("duration_hrs") or s.get("duration_hrs"),
                   "{:.1f} h", help="First join to last leave, not Zoom's Actual "
                                    "Duration — that runs from the host starting "
                                    "to the host leaving."),
                _m("End count", _end,
                   help="Mean people in the room over the closing ten minutes — "
                        "not the last minute, which is often a single person who "
                        "never clicked Leave."),
            ])

            # Attendance last, because it is the one figure that is ALWAYS this
            # batch's own and never the room's - a room three batches sat in has
            # three different attendance rates and no single correct one.
            _p, _t = s.get("present"), s.get("total")
            T.tiles([
                _m(f"Present · {s['batch']}", _p),
                _m("Invited", _t),
                _m("Attendance", s.get("pct"), "{:.1f}%"),
            ])
            if len(_grp) > 1:
                _pp = sum(x["present"] for x in _grp)
                _tt = sum(x["total"] for x in _grp)
                st.caption(f"Pooled over the {len(_grp)} batches in this room: "
                           f"{_pp:,} of {_tt:,}"
                           + (f" ({_pp / _tt * 100:.1f}%)" if _tt else ""))

            if _curve:
                st.markdown("**Retention curve**")
                st.caption("People in the room, minute by minute, from the first "
                           "join to the last leave — swept from the Zoom "
                           "report's own join/leave times, the same pass that "
                           "gives the peak. The whole room, not one batch.")
                import plotly.graph_objects as _go2
                _f2 = _go2.Figure(_go2.Scatter(
                    x=list(range(len(_curve))), y=_curve, mode="lines",
                    line=dict(color=T.BRAND["accent"], width=2), fill="tozeroy",
                    fillcolor=T.BRAND["accent_soft"],
                    hovertemplate="minute %{x}<br>%{y:,} in the room<extra></extra>"))
                _pm2 = _Gp.get("poll_at_min", s.get("poll_at_min"))
                if _pm2 is not None:
                    _f2.add_vline(x=_pm2, line_width=1, line_dash="dot",
                                  line_color=T.INK["muted"])
                    _f2.add_annotation(x=_pm2, y=max(_curve), yshift=12,
                                       text="poll", showarrow=False,
                                       font=dict(size=11, color=T.INK["secondary"]))
                _f2.update_layout(T.plotly_layout(
                    280, margin=dict(t=24),
                    xaxis=dict(title="Time (min)"),
                    yaxis=dict(title="Attendees", rangemode="tozero")))
                st.plotly_chart(_f2, width="stretch",
                                config={"displayModeBar": False})
                st.download_button(
                    "Download retention data (CSV)",
                    _pd.DataFrame({"minute": range(len(_curve)),
                                   "attendees": _curve}).to_csv(index=False).encode(),
                    file_name=f"retention_{s['date']}_{s['batch']}.csv",
                    mime="text/csv", key="ss_dl_curve")
            else:
                st.caption("No Zoom report for this session, so no retention "
                           "curve, peak or stickiness. Nothing is inferred from "
                           "attendance — that would be a different metric "
                           "wearing the same name.")
            if len(_grp) > 1:
                # Per batch: attendance against its own roster, and its own
                # students' answers. The chart below is the JOINT figure - the
                # poll over everyone on a sharing roster, once - not the whole
                # room: a respondent on none of these rosters (a BSIAI student
                # in a shared AI CAP room) is counted nowhere. The whole room's
                # count is rating_shared.room; an older store has no `room`
                # and its joint WAS the room, hence the fallback below.
                _shp = s.get("rating_shared") or {}
                _ok = _shp.get("split", not _shp)     # per-batch figures exist?
                st.dataframe(_pd.DataFrame([{
                    "Batch": x["batch"], "Attendance %": x["pct"],
                    "Own overall": x.get("rating") if _ok else None,
                    "Own trainer ★": x.get("rating_trainer") if _ok else None,
                    "Own NPS": x.get("nps") if _ok else None,
                    "Own responses": (x.get("rating_n") or 0) if _ok else None,
                } for x in _grp]), width='stretch', hide_index=True)
                if _shp.get("split"):
                    _room = _shp.get("room") or _shp.get("joint") or {}
                    st.caption(
                        f"Everyone who answered: {_room.get('responses') or 0:,}"
                        + (f" · Whole room figure (the chart below): "
                           f"{_Gp.get('rating_n') or 0:,}" if _shp.get("room") else "")
                        + (f" · {_shp['unmatched']} answered from an email on none "
                           "of these batches' rosters (in the count of everyone who "
                           "answered, in no batch's own"
                           + (" and not in the Whole room figure)" if _shp.get("room") else ")")
                           if _shp.get("unmatched") else "")
                        + (f" · {_shp['multi']} enrolled in more than one of them "
                           "(counted in each batch's own, once in Whole room)"
                           if _shp.get("multi") else ""))
                elif _shp:
                    st.caption("This feedback poll was run anonymously — the "
                               "export carries no emails — so it cannot be "
                               "divided between the batches. Only the room's "
                               "figure below exists.")
            _dist = (_Gp.get("dist") or s.get("dist") or {})
            if any(_dist.values()):
                st.markdown("**How the room rated it**")
                _cols = st.columns(3)
                for _c, _kind in zip(_cols, ("session", "trainer", "recommend")):
                    _h = _dist.get(_kind) or {}
                    if not sum(_h.values()):
                        continue
                    with _c:
                        st.caption(_kind.title())
                        st.bar_chart(_pd.DataFrame(
                            {"count": [_h.get(str(i), 0) for i in range(1, 6)]},
                            index=[str(i) for i in range(1, 6)]))
            else:
                st.caption("No poll was run for this session.")


with sub_browse:
    _sub_browse()

# ===================== SESSIONS - THIS WEEK (sub-tab) ========================
@st.fragment
def _sub_this_week():
    _painted("this_week")
    _r = (store or {}).get("recap") if store_mode else None
    if not store_mode:
        st.caption("No weekly recap in this data set.")
        with st.expander("Why", expanded=False):
            st.info("The recap is built by the weekly pipeline, so it needs the "
                    "prebuilt data file. This server is running in live/upload mode.")
    elif not _r or not _r.get("weeks"):
        st.warning("No recap in the last refresh.")
        if _r and _r.get("warnings"):
            with st.expander("Details", expanded=False):
                for _w in _r["warnings"]:
                    st.caption("• " + str(_w))
    else:
        import pandas as _pd
        _lat = _r["latest"]
        with st.expander("How this is calculated", expanded=False):
            st.markdown(
                "Every headline here is **attendance vs expected** — what a session "
                "actually drew over what the decay curve says a batch of that age, "
                "level and domain should draw. Ranking on raw attendance just crowns "
                "the youngest cohort every week, because attendance falls about 10% "
                "a week over a batch's life. 1.00× expected is exactly on curve. "
                f"Built {store['generated_at']}."
            )

        # One delta helper for the whole app (ui_theme.delta_text): the unit
        # comes from WHICH FIGURE changed, so "+3" under NPS and "+3" under
        # Learners can no longer look like the same claim.
        _dl0 = _lat["delta"]
        T.tiles([
            {"label": "Sessions", "value": f"{_lat['sessions']:,}"},
            {"label": "Learners present", "value": f"{_lat['present']:,}",
             "delta": T.delta_text("present", _dl0["present"])},
            {"label": "Attendance", "value": f"{_lat['pct']:.1f}%" if _lat["pct"] else "—",
             "delta": T.delta_text("pct", _dl0["pct"])},
            {"label": "vs expected",
             "value": T.fmt_index(_lat["index"]) if _lat["index"] else "—",
             "delta": T.delta_text("index", _dl0["index"]),
             "help": "Above 1.00 means these sessions beat what cohorts of "
                     "their age normally draw. This is the one to watch — the "
                     "raw percentage falls every week by design."},
            {"label": "NPS", "value": f"{_lat['nps']:+d}" if _lat["nps"] is not None else "—",
             "delta": T.delta_text("nps", _dl0["nps"]),
             "help": "Promoter 5, passive 4, detractor 1-3, on the poll's "
                     "recommend question."},
        ])

        if _r.get("awards"):
            st.subheader("This week")
            for _a, _col in zip(_r["awards"], st.columns(len(_r["awards"]))):
                with _col:
                    # The topic and mentor are hand-typed in L2 and this block
                    # renders with unsafe_allow_html, so they are escaped. A
                    # stray '<' in a session title would otherwise eat the rest
                    # of the card, and anything worse than that would run.
                    _esc = _html.escape
                    st.markdown(
                        f"**{_esc(_a['award'])}**  \n"
                        f"### {_esc(T.award_value(_a))}  \n"
                        f"{_esc(_a['batch'])} · {_esc(_a['topic'][:44])}"
                        + (f" · {_esc(_a['pod'])}" if _a.get("pod") else "")
                        + (f"  \n_{_esc(_a['mentor'])}_" if _a.get("mentor") else "")
                        + "  \n<span style='opacity:.65;font-size:12px'>"
                        + _esc(_a["why"]) + "</span>",
                        unsafe_allow_html=True)

        _idx_col = st.column_config.NumberColumn(
            "vs expected", format="%.2f×",
            help="1.00 = exactly what the decay curve predicts for a batch of "
                 "that age, level and domain.")
        st.subheader("Week by week")
        st.dataframe(_pd.DataFrame([{
            "Week of": w["week"], "Sessions": w["sessions"],
            "Batches": len(w["batches"]), "Present": w["present"],
            "Invited": w["invited"], "Attendance %": w["pct"],
            "vs expected": w["index"], "Rating": w["rating"], "NPS": w["nps"],
        } for w in reversed(_r["weeks"])]), width='stretch', hide_index=True,
            column_config={"vs expected": _idx_col})

        if _r.get("leaderboard"):
            st.subheader("Trainers, this week")
            st.caption("Ranked on attendance vs expected, so a trainer who taught "
                       "an old cohort is not punished for its age.")
            st.dataframe(_pd.DataFrame([{
                "Trainer": b["mentor"], "Sessions": b["sessions"],
                "Present": b["present"], "Attendance %": b["pct"],
                "vs expected": b["index"], "Rating": b["rating"], "NPS": b["nps"],
            } for b in _r["leaderboard"]]), width='stretch', hide_index=True,
                column_config={"vs expected": _idx_col})


with sub_recap:
    _sub_this_week()

# ===================== SESSIONS - TRAINERS (sub-tab) =========================
@st.fragment
def _sub_trainers():
    _painted("trainers")
    _t = (store or {}).get("trainers") if store_mode else None
    if not store_mode:
        st.caption("No trainer rollup in this data set.")
        with st.expander("Why", expanded=False):
            st.info("Trainer rollups are built by the weekly pipeline.")
    elif not _t or not _t.get("trainers"):
        st.warning("No trainer data in the last refresh.")
        if _t and _t.get("warnings"):
            with st.expander("Details", expanded=False):
                for _w in _t["warnings"]:
                    st.caption("• " + str(_w))
    else:
        import pandas as _pd
        with st.expander("How this is calculated", expanded=False):
            st.markdown(
                f"**{_t['n_raw']} spellings in the schedule resolve to "
                f"{_t['n_people']} people.** "
                "The Mentor cell is hand-typed, so one person arrives as 'Swapnil', "
                "'Swapnil Narayan' and 'Swapnil (Play Simulive)'. Where the schedule "
                "carries an email it is trusted; otherwise a short name joins a "
                "longer one only when it is an unambiguous prefix of it. A room "
                "several batches sat in (one domain webinar for B35, B36 and B37) "
                "is **one session**, rated once by its whole poll — attendance "
                "still pools every batch. **Co-taught** counts sessions credited "
                "to more than one trainer — both get the session, so totals across "
                "trainers exceed the session count."
            )
        _min = st.slider("Minimum sessions", 1, 25, 5, key="tr_min",
                         help="A trainer's figure over one session is noise.")
        _rows = [x for x in _t["trainers"] if x["sessions"] >= _min]
        st.dataframe(_pd.DataFrame([{
            "Trainer": x["trainer"], "Sessions": x["sessions"],
            "Co-taught": x["co_taught"], "Batches": len(x["batches"]),
            "Present": x["present"], "Invited": x["invited"],
            "Attendance %": x["pct"], "vs expected": x["index"],
            "Rating": x["rating"], "Responses": x["rating_n"], "NPS": x["nps"],
            "First": x["first"], "Last": x["last"],
        } for x in _rows]), width='stretch', hide_index=True, height=520,
            column_config={"vs expected": st.column_config.NumberColumn(
                "vs expected", format="%.2f×",
                help="1.00 = exactly what the decay curve predicts for the "
                     "batches this trainer taught, at their age.")})
        st.caption(f"{len(_rows)} of {_t['n_people']} shown.")

        if _t.get("merged"):
            with st.expander(f"Spellings merged ({len(_t['merged'])} people)"):
                st.dataframe(_pd.DataFrame(
                    [{"Shown as": k, "Merged from": ", ".join(v)}
                     for k, v in sorted(_t["merged"].items())]),
                    width='stretch', hide_index=True)
        if _t.get("ambiguous"):
            with st.expander(f"Names left unmerged ({len(_t['ambiguous'])})",
                             expanded=False):
                st.warning(
                    "Left unmerged because the name could be more than one person: "
                    + ", ".join(f"**{n}**" for n in _t["ambiguous"])
                    + ". Add their email to the schedule's *Mentor's email* "
                      "column to resolve them.")


with sub_trainer:
    _sub_trainers()

# ========================= TAB 3 - WEEKEND RECAP =============================
@st.fragment
def _tab_weekend():
    _painted("weekend")
    _W = (store or {}).get("recap") if store_mode else None
    _WS = (store or {}).get("sessions") if store_mode else None
    if not store_mode:
        st.caption("No weekend recap in this data set.")
        with st.expander("Why", expanded=False):
            st.info("The weekend recap is built by the weekly pipeline.")
    elif not _W or not _W.get("weeks"):
        st.warning("No recap in the last refresh.")
    else:
        import pandas as _pd
        _lbl = {}
        for _w0 in reversed(_W["weeks"]):            # newest first
            _d0 = _dt.date.fromisoformat(_w0["week"])
            _lbl[f"{_d0:%d %b} \u2013 {_d0 + _dt.timedelta(days=6):%d %b %Y}"] = _w0
        _pickw = st.selectbox("Select week (Monday\u2013Sunday)", list(_lbl),
                              key="wr_week")
        _w = _lbl[_pickw]
        _d0 = _dt.date.fromisoformat(_w["week"])
        _dl = _w.get("delta") or {}

        st.markdown(
            f"<div style='background:{T.BRAND['accent_soft']};"
            "border:1px solid rgba(128,128,128,.22);"
            "border-radius:12px;padding:20px 22px;margin:6px 0 14px'>"
            # Secondary ink, NOT the accent: accent ink on the accent's own
            # 10% wash measures 3.89:1, under AA's 4.5 for 11px bold (large-text
            # relief starts at 18.66px bold). The same 3.89 was measured and
            # fixed on `.chip` one file over. 7.00:1 here. The wash stays as the
            # banner's SURFACE — it is the text that may not be the series colour.
            f"<div style='font-size:11px;letter-spacing:.09em;color:{T.INK['secondary']};"
            "font-weight:700'>WEEKEND RECAP</div>"
            f"<div style='font-size:30px;font-weight:700;margin:4px 0 2px'>"
            f"Week of {_d0:%d %B %Y}</div>"
            f"<div style='opacity:.7;font-size:13px'>{_w['sessions']} sessions"
            f" &middot; {_d0:%d %b} \u2013 {_d0 + _dt.timedelta(days=6):%d %b %Y}"
            f" &middot; {len(_w['batches'])} batches</div>"
            # The build stamp belongs ON this page, not only in the sidebar.
            # Four times now a stale cached store has been read as missing
            # data ("no reports", "no curve") because the only clue that the
            # store predated the code was a caption on another tab.
            f"<div style='opacity:.55;font-size:11px;margin-top:6px'>"
            f"Built from the store of {store['generated_at']}</div></div>",
            unsafe_allow_html=True)

        # Same helper as This week (ui_theme.delta_text). It replaces a local
        # `{:+g}` that printed a bare "+3" under both NPS and Learners, where
        # one meant three points of NPS and the other three people.
        T.tiles([
            {"label": "Sessions", "value": f"{_w['sessions']:,}"},
            {"label": "Avg overall",
             "value": f"{_w['rating']:.2f}" if _w.get("rating") else "\u2014",
             "delta": T.delta_text("rating", _dl.get("rating"))},
            {"label": "Avg trainer",
             "value": f"{_w['rating_trainer']:.2f}" if _w.get("rating_trainer") else "\u2014",
             "delta": T.delta_text("rating_trainer", _dl.get("rating_trainer"))},
            {"label": "Avg NPS",
             "value": f"{_w['nps']:+d}" if _w.get("nps") is not None else "\u2014",
             "delta": T.delta_text("nps", _dl.get("nps"))},
            {"label": "Learners", "value": f"{_w['present']:,}",
             "delta": T.delta_text("present", _dl.get("present")),
             "help": "Attendances across the week. Someone who came to three "
                     "sessions counts three times \u2014 attendances, not people."},
            {"label": "Avg stickiness",
             "value": f"{_w['stickiness']:.0f}%" if _w.get("stickiness") else "\u2014",
             "delta": T.delta_text("stickiness", _dl.get("stickiness")),
             "help": "Of the fullest each room got, how much was still there "
                     "over the closing half hour. Averaged over the "
                     f"{_w.get('n_sticky', 0)} sessions with a Zoom report."},
        ])

        # Awards are recomputed for the SELECTED week, using the same rules the
        # pipeline uses, so picking an older week does not show this week's.
        _rows_w = [r for r in (_WS or []) if r.get("week") == _w["week"]]
        _aw = _recap_mod._awards(_rows_w) if _rows_w else (_W.get("awards") or [])
        if _aw:
            st.subheader("Highlights of the week")
            for _a, _c in zip(_aw, st.columns(len(_aw))):
                with _c:
                    st.markdown(_card(_a["award"], _a["topic"],
                                      _a.get("batch") or "", _a.get("mentor") or "",
                                      T.award_value(_a), _a.get("why") or ""),
                                unsafe_allow_html=True)

        # \u2500\u2500 the other end of the same ranking \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # "Beat the curve" names the week's best session against expectation;
        # nothing named the worst, so a session that drew half what its cohort
        # normally does left no trace on the page anybody reads on a Monday.
        # Same key, same cards, bottom three. Sessions with no curve point are
        # left out entirely rather than sorted to the bottom \u2014 no expectation
        # is not the same as falling short of one.
        _below = sorted((r for r in _rows_w if r.get("index") is not None),
                        key=lambda r: r["index"])[:3]
        if _below:
            st.subheader("Below the curve")
            st.caption("The three sessions furthest under what the decay curve "
                       "expected of a cohort that age, level and domain. Same "
                       "measure as *Beat the curve* above, read from the other end.")
            for _b0, _c in zip(_below, st.columns(len(_below))):
                _why = (f"{_b0['pct']:.1f}% attended against "
                        f"{_b0['expected_pct']:.1f}% expected"
                        if _b0.get("expected_pct") else
                        f"{_b0.get('present', 0):,} of {_b0.get('total', 0):,} invited")
                with _c:
                    st.markdown(_card("Below the curve",
                                      _b0.get("topic") or "Session",
                                      _b0.get("l2_batch") or _b0.get("batch") or "",
                                      _b0.get("mentor") or "",
                                      T.fmt_index(_b0["index"]), _why),
                                unsafe_allow_html=True)

        if _rows_w:
            # RANKED ON vs EXPECTED, not on the raw poll score \u2014 the same key
            # This week's leaderboard and every other ranking in this app use
            # (recap.build's `board`). Sorting on `rating` crowned whoever
            # happened to teach the best-rated room, and a trainer with no
            # rating at all vanished from a board they had earned a place on.
            # Unrated / unindexed trainers sort last rather than being dropped.
            _lb = list(_trainers_mod.build(_rows_w).get("trainers") or [])
            _lb.sort(key=lambda t: (t.get("index") is None, -(t.get("index") or 0)))
            if _lb:
                st.subheader("Trainer leaderboard")
                st.caption("Ranked on attendance vs expected, so a trainer who "
                           "taught an old cohort is not punished for its age \u2014 "
                           "the same measure the Sessions tab ranks on. A "
                           "session taught by two people credits both.")
                st.dataframe(_pd.DataFrame([{
                    "#": i + 1, "Trainer": t["trainer"],
                    "Type": t.get("type") or "\u2014",
                    "Sessions": t["sessions"], "Learners": t["present"],
                    "Attendance %": t.get("pct"), "vs expected": t.get("index"),
                    "Avg rating": t["rating"], "NPS": t["nps"],
                } for i, t in enumerate(_lb[:12])]),
                    width="stretch", hide_index=True,
                    column_config={"vs expected": st.column_config.NumberColumn(
                        "vs expected", format="%.2f\u00d7",
                        help="1.00 = exactly what the decay curve predicts for "
                             "the batches this trainer taught, at their age."),
                        "Attendance %": st.column_config.NumberColumn(
                            "Attendance %", format="%.1f%%")})

        if _rows_w:
            st.subheader("Session breakdown")
            # Sessions WITH a retention curve first, so the tab does not open on
            # "no Zoom report for this session" and read as broken.
            # When NO row has a curve the store simply predates the sweep, and
            # tagging all 75 of them "(no report)" says the Zoom reports are
            # missing when they are not. The tag only earns its place when it
            # distinguishes one session from another.
            # One entry per ROOM, not per batch row: a webinar B35, B36 and B37
            # sat in is one session with one poll, so the room's whole figures
            # are shown and the per-batch split follows underneath.
            _anyc = any(x.get("retention") for x in _rows_w)
            _byk = {}
            for _r0 in sorted(_recap_mod.group_sessions(_rows_w),
                              key=lambda x: (not x.get("retention"), x["date"])):
                _byk[f"{_r0['date']} \u00b7 {(_r0.get('topic') or 'Session')[:50]}"
                     + (f" \u00b7 {_r0['pod']}" if _r0.get("pod") else "")
                     + f" \u00b7 {_r0.get('l2_batch') or _r0['batch']}"
                     + ("" if _r0.get("retention") or not _anyc
                        else "  (no report)")] = _r0
            _pick = st.selectbox("Session", list(_byk), key="wr_sess")
            r = _byk[_pick]
            T.tiles([
                {"label": "Overall", "value": f"{r['rating']:.2f}" if r.get("rating") else "\u2014"},
                {"label": "Trainer", "value": (f"{r['rating_trainer']:.2f}"
                                               if r.get("rating_trainer") else "\u2014")},
                {"label": "NPS", "value": f"{r['nps']:+d}" if r.get("nps") is not None else "\u2014"},
                {"label": "Responses", "value": f"{r.get('rating_n', 0):,}"},
                {"label": "Duration", "value": (f"{r['duration_hrs']:.1f} h"
                                                if r.get("duration_hrs") else "\u2014")},
                {"label": "Peak", "value": f"{r['peak']:,}" if r.get("peak") else "\u2014"},
            ])
            if len(r.get("rows") or ()) > 1:
                _shw = next((x.get("rating_shared") for x in r["rows"]
                             if x.get("rating_shared")), None) or {}
                if _shw and not _shw.get("split"):
                    st.caption(f"Shared by {r['batch']}. This feedback poll was run "
                               "anonymously \u2014 no emails in the export \u2014 so it "
                               "cannot be divided between the batches; the "
                               "figures above are the whole room's.")
                else:
                    _own = " \u00b7 ".join(
                        f"**{x['batch']}** "
                        + (f"{x['rating']:.2f} ({x.get('rating_n') or 0})"
                           if x.get("rating") is not None else "\u2014")
                        for x in sorted(r["rows"], key=lambda x: dc.batch_key(x["batch"])))
                    st.caption(
                        f"Shared by {r['batch']}. Own students' overall rating: {_own}"
                        + (f" \u00b7 {_shw['unmatched']} respondents matched no roster"
                           if _shw.get("unmatched") else ""))

            T.tiles([
                {"label": "Stickiness (10 min)",
                 "value": f"{r['stick10']:.0f}%" if r.get("stick10") else "\u2014",
                 "help": "Mean concurrency over the closing 10 minutes, "
                         "as a share of the session's peak."},
                {"label": "Stickiness (30 min)",
                 "value": f"{r['stick30']:.0f}%" if r.get("stick30") else "\u2014"},
                {"label": "Attendance", "value": f"{r['pct']:.1f}%" if r.get("pct") else "\u2014"},
            ])

            _curve = r.get("retention")
            if _curve:
                st.markdown("**Retention curve**")
                st.caption("People in the room, minute by minute, from the first "
                           "join to the last leave \u2014 swept from the Zoom "
                           "report's join/leave times, the same pass that gives "
                           "the peak.")
                import plotly.graph_objects as _go
                _fig = _go.Figure(_go.Scatter(
                    x=list(range(len(_curve))), y=_curve, mode="lines",
                    line=dict(color=T.BRAND["accent"], width=2),
                    fill="tozeroy", fillcolor=T.BRAND["accent_soft"],
                    hovertemplate="minute %{x}<br>%{y:,} in the room<extra></extra>"))
                _pm = r.get("poll_at_min")
                if _pm is not None:
                    # Where the room STARTED ANSWERING the poll -- the first
                    # submission in the poll export, not a moderator's note.
                    _fig.add_vline(x=_pm, line_width=1, line_dash="dot",
                                   line_color=T.INK["muted"])
                    _fig.add_annotation(x=_pm, y=max(_curve), yshift=12,
                                        text="poll", showarrow=False,
                                        font=dict(size=11, color=T.INK["secondary"]))
                _fig.update_layout(T.plotly_layout(
                    280, margin=dict(t=24),
                    xaxis=dict(title="Time (min)"),
                    yaxis=dict(title="Attendees", rangemode="tozero")))
                st.plotly_chart(_fig, width="stretch",
                                config={"displayModeBar": False})
                if _pm is not None:
                    st.caption(f"The dotted line at minute {_pm} is when the room "
                               "began answering the poll — the first "
                               "submission in the poll export, not a note of "
                               "when it was circulated.")
                else:
                    # Say WHY there is no line. Silence here reads as a broken
                    # chart, which is how "no curve" was reported in the first
                    # place.
                    st.caption("No poll marker: this session's poll export "
                               "carries no submission times, or the first "
                               "answer lands outside the measured window.")
                st.download_button(
                    "Download retention data (CSV)",
                    _pd.DataFrame({"minute": range(len(_curve)),
                                   "attendees": _curve}).to_csv(index=False).encode(),
                    file_name=f"retention_{r['date']}_{r['batch'].replace(', ', '_')}.csv",
                    mime="text/csv", key="wr_dl_curve")
            elif not any(x.get("retention") for x in _rows_w):
                # NO session in the week has a curve. That is not 75 missing
                # Zoom reports, it is a store built before the sweep existed --
                # and saying "no report" here sent people looking for missing
                # files four times. Name the real cause.
                st.caption("No retention curve for any session this week.")
                with st.expander("Why", expanded=False):
                    st.info("No session this week has a retention curve, which "
                            f"means the store of {store['generated_at']} predates "
                            "the code that measures it. The next pipeline run "
                            "fills these in \u2014 the Zoom reports are already there.")
            else:
                st.caption("No Zoom report for this session, so no retention "
                           "curve. Nothing is inferred from attendance \u2014 that "
                           "would be a different metric wearing the same name.")

            _dist = r.get("dist") or r.get("rating_dist") or {}
            if any(sum((_dist.get(kk) or {}).values())
                   for kk in ("session", "trainer", "recommend")):
                st.markdown("**Rating breakdown**")
                for _c2, _kind in zip(st.columns(3),
                                      ("session", "trainer", "recommend")):
                    _h = _dist.get(_kind) or {}
                    if not sum(_h.values()):
                        continue
                    with _c2:
                        st.caption(_kind.title())
                        st.bar_chart(_pd.DataFrame(
                            {"responses": [_h.get(str(i), 0) for i in range(1, 6)]},
                            index=[str(i) for i in range(1, 6)]), height=200)

        st.caption("**Phase-wise retention (Teaching / Q&A) is deliberately "
                   "absent.** It needs someone to record when Q&A started; the "
                   "reference app asks for it at upload time. Nothing in the "
                   "schedule, the curriculum sheet or the Zoom report carries it, "
                   "so a boundary "
                   "here would be a guess wearing a percentage.")


with tab_weekend:
    _tab_weekend()


# ============================ TAB - BSIAI ====================================
# The BSIAI programme (Build Side Income Using AI), from ITS OWN store. The
# pages are bsiai_view's, the renderers are the same ones the tabs above use,
# and every widget key carries the "bsiai" prefix so nothing collides with the
# AI CAP Dashboard's or Roster's widgets in this one script.
@st.fragment
def _tab_bsiai():
    _painted("bsiai")
    if bsiai_store is None:
        st.caption("No BSIAI data in this deployment yet.")
        with st.expander("How to add it", expanded=False):
            st.info(
                "BSIAI is built separately from the AI CAP pipeline: run "
                "`python bsiai_build.py --upload` on a machine with the LMS key "
                "and the Drive service account. It writes `bsiai.duckdb` into the "
                "same private Drive folder as the AI CAP store, and this tab picks "
                "it up on the next refresh.")
        return
    if _viewing:
        st.caption("BSIAI is not archived week by week - this is its latest build, "
                   "whichever week is selected in the sidebar.")
    bsiai_view.render(bsiai_store, key="bsiai")


with tab_bsiai:
    _tab_bsiai()


# ======================== TAB 8 - ADD THIS WEEK'S DATA =======================
# The owner supplies each week's Zoom exports by hand (2026-09-10) and needs
# them live on THIS dashboard, not on a laptop. So this page does the light work
# - read the names, check them against L2, put them on Drive - and then asks
# GitHub Actions to run the pipeline. Marking stays on the runner: in-process
# marking is what made this app take three minutes and die on a 1 GB instance
# (CLAUDE.md section 2), and no upload button is worth bringing that back.
with tab_add:
    st.caption(
        "Add a week's Zoom exports and the shared dashboard updates for "
        "everyone. Marking runs on GitHub, so this page stays responsive and "
        "the app never holds the roster in memory."
    )

    try:
        _gh = dict(st.secrets.get("github", {}) or {})
    except Exception:
        _gh = {}
    _gh_token = str(_gh.get("token") or "")
    _gh_repo = str(_gh.get("repo") or "")
    try:
        _up_pw = str(st.secrets.get("upload_password", "") or "")
    except Exception:
        _up_pw = ""

    if not st.session_state.get("_upload_authed"):
        # A second gate on purpose. The app password is shared with everyone who
        # reads the dashboard; WRITING data is a different privilege, and the
        # blast radius of a wrong upload is now permanent (nothing is rebuilt).
        if not _up_pw:
            st.caption("Adding data is switched off on this deployment.")
            with st.expander("Setup", expanded=False):
                st.info(
                    "Set `upload_password` in the app's secrets (Streamlit Cloud: "
                    "Manage app -> Settings -> Secrets) to enable this page. It is "
                    "deliberately separate from the password used to view the "
                    "dashboard."
                )
            st.stop()

        def _upload_submit():
            if hmac.compare_digest(st.session_state.get("_upw", ""), _up_pw):
                st.session_state["_upload_authed"] = True
            else:
                st.session_state["_upload_authed"] = False
            st.session_state.pop("_upw", None)

        st.text_input("Data-entry password", type="password", key="_upw",
                      on_change=_upload_submit,
                      placeholder="Enter the password and press Enter")
        if st.session_state.get("_upload_authed") is False:
            st.error("Incorrect password.")
        st.stop()

    if not (_gh_token and _gh_repo):
        st.error(
            "**Not configured yet.** This page needs a GitHub token so it can "
            "start the rebuild. Add to the app's secrets:\n\n"
            '```toml\n[github]\nrepo = "owner/repo"\ntoken = "github_pat_..."\n'
            '# optional: ref = "main", workflow = "refresh.yml"\n```\n\n'
            "Use a fine-grained token limited to this repository with only "
            "**Actions: read and write** - it can start the pipeline and nothing "
            "else. Never commit it; the repository is public."
        )
        st.stop()
    if not live_data.config_present():
        st.error("Google credentials are not configured in this app's secrets, "
                 "so the files cannot be put on Drive.")
        st.stop()

    import time
    import zipfile
    import pandas as _pd

    import ingest as _ingest

    _l2_id = _att_id = ""
    try:
        _drive_cfg = dict(st.secrets.get("drive", {}) or {})
        _l2_id = str(_drive_cfg.get("l2_id") or "")
        _att_id = str(_drive_cfg.get("attendee_folder_id") or "")
    except Exception:
        pass
    if not _att_id:
        st.error("`drive.attendee_folder_id` is not configured, so there is "
                 "nowhere to put the files.")
        st.stop()

    st.markdown("**1 - Choose this week's files**")
    _files = st.file_uploader(
        "Zoom exports", accept_multiple_files=True, key="add_files",
        type=["csv", "zip"],
        help="The attendee report for each session, plus its poll export if "
             "there is one. A .zip of them is fine. Names must be Zoom's own: "
             "attendee_<webinar>_<YYYY>_<MM>_<DD>.csv / poll_...csv")

    if not _files:
        st.info("Nothing selected yet.")
        st.stop()

    # Flatten: a zip contributes its members, everything else itself.
    _blobs = {}
    _bad_zip = []
    for _f in _files:
        if _f.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(_f.getvalue())) as _z:
                    # Read only the members that are Zoom exports. Reading
                    # everything first and filtering after would pull a session
                    # RECORDING - gigabytes - into this 1 GB process to decide
                    # it was not wanted.
                    for _m in _z.namelist():
                        if not _m.endswith("/") and _ingest.classify_name(_m):
                            _blobs[_m] = _z.read(_m)
            except Exception as _e:
                _bad_zip.append(f"{_f.name}: {_e}")
        else:
            _blobs[_f.name] = _f.getvalue()
    for _line in _bad_zip:
        st.error(f"Unreadable zip - {_line}")

    _sessions, _rejected = _ingest.classify(list(_blobs))
    _l2_bytes = None
    if not _l2_id:
        # Without L2 every session looks unscheduled and the page would blame
        # the schedule for a configuration problem.
        st.error("`drive.l2_id` is not configured, so the schedule sheet cannot be "
                 "read and every session below will look unscheduled. Add it to "
                 "the app's secrets — this is a setup problem, not a problem "
                 "with your files.")
    else:
        try:
            _l2_bytes, _ = live_data.fetch_sheet_cached(
                live_data._drive_service(), _l2_id)
        except Exception as _e:
            st.warning(f"Could not read the schedule sheet ({_e}) - every session "
                       "will look unscheduled until it is readable.")
    _l2_map, _l2_labels = (ac.parse_l2(_l2_bytes, with_labels=True)
                           if _l2_bytes else ({}, {}))
    _rows = _ingest.preflight(_sessions, _l2_map, _l2_labels)

    st.markdown("**2 - Check what will be added**")
    if _rows:
        st.dataframe(_pd.DataFrame([{
            "Date": _r0["date"], "Webinar": _r0["wid"],
            "In schedule": "yes" if _r0["in_l2"] else "NO",
            "Batches": ", ".join(_r0["batches"]) or "-",
            "Domain": _r0["pod"] or "whole batch",
            "Title": _r0["topic"] or "-",
            "Attendee files": _r0["n_attendee"], "Poll files": _r0["n_poll"],
        } for _r0 in _rows]), width="stretch", hide_index=True)

    # Sessions already marked are FROZEN: the pipeline will leave them exactly
    # as they are, whatever arrives (attendance_core.process_files(frozen=...)).
    # Saying nothing here would mean a green run, a longer file list on Drive and
    # no change at all - the same trap the L2 check above exists to prevent.
    _already = []
    for _r0 in _rows:
        _mm = f"{_r0['ymd'][5:7]}_{_r0['ymd'][8:10]}"
        for _b0 in _r0["batches"]:
            if any(_s0.get("batch") == _b0 and _s0.get("mm") == _mm
                   and (_s0.get("pod") or "") == _r0["pod"]
                   for _s0 in ((store or {}).get("sessions") or ())):
                _already.append(f"{_r0['date']} {_b0}"
                                + (f" {_r0['pod']}" if _r0["pod"] else ""))
                break
    if _already:
        st.warning(
            "**Already marked, and marks are frozen** — these will be uploaded "
            "and kept on Drive, but the run will leave their existing numbers "
            "untouched: " + ", ".join(_already[:6])
            + (f" and {len(_already) - 6} more" if len(_already) > 6 else "")
            + ". To replace them, run the workflow from GitHub's Actions tab "
              "with **incremental** unticked.")

    _blocks = _ingest.blockers(_rows, _rejected) + _bad_zip
    if _blocks:
        for _b in _blocks:
            st.error(_b)
        st.caption("Nothing has been uploaded. Fix the above and choose the "
                   "files again - it is safer to stop here than to publish a "
                   "week that quietly misses a session.")
        st.stop()

    _n_files = sum(len(_r0["files"]) for _r0 in _rows)
    st.success(f"{len(_rows)} session(s), {_n_files} file(s), all in the schedule.")

    st.markdown("**3 - Add them**")
    st.caption(
        "The files go to the Zoom extracts Shared Drive, then the pipeline runs "
        "on GitHub and marks **only** these sessions - everything already marked "
        "is left exactly as it is."
    )
    if st.button("Upload and refresh the dashboard", type="primary", key="add_go"):
        # A WRITE-capable client, built just for this. The app's default Drive
        # client is read-only (live_data._SCOPES, §6) and upload_to_folder on it
        # is a 403 — which would only have surfaced after the files were chosen.
        _svc = live_data.rw_service()
        _drive = _ingest.target_drive(_att_id)
        with st.status("Adding this week's data...", expanded=True) as _status:
            try:
                st.write(f"Uploading {_n_files} file(s) to Drive...")
                for _r0 in _rows:
                    _got = _ingest.upload_session(_svc, _drive, _r0, _blobs)
                    st.write(f"- {_r0['date']} - "
                             f"{(_r0['topic'] or _r0['wid'])[:40]} "
                             f"({len(_got['uploaded'])} file(s))")
                _since = _dt.datetime.now(_dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                st.write("Starting the rebuild on GitHub...")
                _ingest.dispatch(
                    _gh_token, _gh_repo,
                    workflow=str(_gh.get("workflow") or "refresh.yml"),
                    ref=str(_gh.get("ref") or "main"),
                    inputs={"incremental": True})
            except Exception as _e:
                _status.update(label="Could not start the rebuild", state="error")
                st.error(str(_e))
                st.stop()

            # GitHub answers a dispatch with 204 and no run id, so the run is
            # found by time. Poll rather than guess: telling the owner it is done
            # while it is still queued is how a stale dashboard gets mistaken for
            # a working one.
            _run = None
            for _ in range(20):
                try:
                    _run = _ingest.latest_run(
                        _gh_token, _gh_repo,
                        str(_gh.get("workflow") or "refresh.yml"), _since)
                except Exception:
                    _run = None
                if _run:
                    break
                time.sleep(3)
            if not _run:
                _status.update(label="Started, but the run is not visible yet",
                               state="error")
                st.warning("GitHub accepted the request but has not listed the "
                           "run yet. Check the Actions tab in a minute - the "
                           "files are already uploaded, so it is safe to start "
                           "it again from there.")
                st.stop()

            st.write(f"[Run on GitHub]({_run['url']})")
            _state = {"status": _run.get("status"), "step": "",
                      "url": _run.get("url"), "conclusion": None}
            _seen = ""
            for _ in range(120):                      # ~10 minutes
                try:
                    _state = _ingest.run_state(_gh_token, _gh_repo, _run["id"])
                except Exception:
                    pass
                if _state.get("step") and _state["step"] != _seen:
                    _seen = _state["step"]
                    st.write(f"- {_seen}")
                if _state.get("status") == "completed":
                    break
                time.sleep(5)

            if _state.get("conclusion") == "success":
                _status.update(label="Done - the dashboard is up to date",
                               state="complete")
                # Clearing the cache is not enough on its own - the tabs above
                # were rendered from the OLD store earlier in this same script
                # run, so they need one more pass to pick up the new one.
                st.cache_data.clear()
                st.session_state["_upload_done"] = True
                st.success("Added. Anyone else looking at the dashboard sees it "
                           "on their next refresh.")
            elif _state.get("status") == "completed":
                _status.update(label="The rebuild failed", state="error")
                st.error(
                    f"GitHub finished with '{_state.get('conclusion')}'. **Nothing "
                    "was published** - the pipeline refuses to overwrite a good "
                    "dashboard with a partial one, so the numbers you see are "
                    f"still the previous ones. Open [the run]({_state.get('url')}) "
                    "for the reason; the files are already on Drive, so a re-run "
                    "costs nothing.")
            else:
                _status.update(label="Still running", state="error")
                st.warning(
                    "Taking longer than expected. It is still going - follow "
                    f"[the run]({_state.get('url')}) and refresh this page when "
                    "it finishes.")

    # Any button click reruns the script, and the cache was cleared above, so
    # this is what actually repaints the other tabs from the new store.
    if st.session_state.get("_upload_done"):
        st.button("Show the new numbers", key="add_reload")
