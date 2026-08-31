"""
Be10X — AI CAP Attendance (unified app)
========================================

The app renders a PREBUILT store when one is available: pipeline.py (run by
GitHub Actions every Monday 06:00 IST) fetches the roster + Zoom reports, marks
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
import io
import json
import pickle
from datetime import datetime
from pathlib import Path

import streamlit as st

import attendance_core as ac
import dashboard_core as dc
import live_data
import data as ddata          # new dashboard data layer (aliased; 'data' is used as a local below)
import sheets as dsheets      # gspread / xlsx source adapter
import dash_view              # Plotly drill-down dashboard UI

st.set_page_config(page_title="Be10X Attendance", page_icon="📊", layout="wide")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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
    topics, l2_labels = dsheets.webinar_topic_lookup(
        [(n, None) for n in attendee_names], l2_bytes, with_labels=True)
    DATA, summary = ddata.build(tabs, topics, l2_labels)
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
_STORE_TTL_SECONDS = 30 * 60


@st.cache_data(show_spinner="Loading the latest dashboard…", ttl=_STORE_TTL_SECONDS)
def _load_store(nonce):
    """Download the prebuilt attendance.duckdb the pipeline uploaded to Drive
    (a few MB — seconds, not minutes), keep a disk copy, and read out everything
    the UI needs. If Drive is unreachable, an existing local copy still serves.

    Returns the meta dict (+ df/path/generated_at), {"error": …} when there is
    nothing readable, or None when no store exists at all. Never raises: a
    corrupt / locked / version-mismatched file must degrade to the legacy live
    and upload modes, not paint a traceback over the whole app."""
    err = None
    fid = _store_folder_id()
    if fid:
        try:
            raw, _stamp = live_data.fetch_store(fid)
            if raw:
                _DISK_CACHE.mkdir(exist_ok=True)
                tmp = _STORE_PATH.with_suffix(".tmp")
                tmp.write_bytes(raw)
                try:
                    tmp.replace(_STORE_PATH)
                except OSError:
                    pass                     # another session holds it open — keep old copy
        except Exception as e:
            err = str(e)
    if not _STORE_PATH.exists():
        return {"error": err} if err else None
    try:
        import duckdb
        con = duckdb.connect(str(_STORE_PATH), read_only=True)
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
    out["path"] = str(_STORE_PATH)
    out["drive_error"] = err
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
st.title("📊 Be10X — AI CAP Attendance")

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

with st.sidebar:
    st.header("① Data source")

    if st.button("🔄 Refresh from Google", width='stretch',
                 disabled=not (live_ready or _store_available),
                 help="Pull the latest prebuilt dashboard from Drive "
                      "(the pipeline rebuilds it every Monday 06:00 IST)"):
        st.session_state.nonce += 1
        st.cache_data.clear()
        st.rerun()

    st.header("② Count basis")
    basis = st.radio(
        "Who counts as batch strength?",
        ["Active only (exclude refunds)", "All enrolled"],
        index=0,
        help="Sets the default for the Roster tab’s ‘Active learners only’ filter. "
             "Dashboard percentages are always measured against total strength.",
    )

    # The matching rule only means something when THIS server does the marking.
    # With prebuilt data the marking already happened in the pipeline, so showing
    # a live-looking radio here would be a lie.
    mode = "exact"
    if not _store_available:
        with st.expander("⚙️ Matching rule (advanced)"):
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

if _store_available:
    loaded = _load_store(st.session_state.nonce)
    if loaded and "df" in loaded:
        store = loaded
        store_mode = True
        report, warnings = store["report"], store["warnings"]
        source_label = store["source"]
        if store.get("drive_error"):
            st.sidebar.warning("Drive unreachable — showing the last downloaded "
                               f"data.\n\n{store['drive_error']}")
        if _store_local_only:
            st.sidebar.warning(
                f"Using a **locally built** store from {store['generated_at']}. "
                "🔄 Refresh cannot update it — no `store_folder_id` is configured. "
                "Re-run `pipeline.py`, or delete `.cache/attendance.duckdb` to read "
                "Google Drive directly."
            )
        else:
            st.sidebar.caption(f"📅 Data as of **{store['generated_at']}** · "
                               "rebuilt every Monday 06:00 IST")
        pipeline_mode = (store.get("stamps") or {}).get("mode") or "exact"
        st.sidebar.caption(f"Matching rule: **{pipeline_mode}** (set by the pipeline)")
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
            up_l2 = st.file_uploader("L2 schedule (.xlsx)", type=["xlsx"])
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

# basis-driven column names
active_mode = basis.startswith("Active")
PCT = "PctActive" if active_mode else "PctAll"
PRES = "PresentActive" if active_mode else "PresentAll"
DEN = "Active" if active_mode else "Total"
den_label = "active learners" if active_mode else "enrolled"

# status line
if store_mode:
    auto = "🟢 Prebuilt data"
    st.caption(f"{auto} · {source_label} · data as of {store['generated_at']}")
else:
    auto = "🟢 Live from Google Drive" if (live_ready and not live_failed) else "📤 Manual upload"
    st.caption(f"{auto} · {source_label} · refreshed {datetime.now():%d %b %Y, %H:%M}")
if report:
    new_n = sum(1 for r in report if r["kind"] == "NEW")
    st.success(f"✅ Auto-marked attendance: {len(report)} session column(s) across "
               f"{len({r['batch'] for r in report})} batch(es) "
               f"({new_n} new, {len(report)-new_n} re-marked).")


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
    fid = store.get("marked_xlsx_file_id") or ""
    if not fid:
        st.caption("The marked workbook isn’t on Drive yet — it is uploaded by a "
                   "full pipeline run (not by `--no-upload`).")
        return
    # The pipeline REPLACES the xlsx in place, so the Drive file id is the same
    # every week — cache on the store's build time too, or a long-lived session
    # keeps serving last week's workbook after a refresh.
    token = f"{fid}@{store['generated_at_iso']}"
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
            file_name="Master_Batch_Rosters_marked.xlsx",
            mime=XLSX_MIME, type="primary", key=key,
        )


# Prominent: the full marked roster (all sessions) — same deliverable as the original marker
_marked_roster_download("dl_top")
st.caption("The complete marked workbook — every batch, all sessions, Present/Absent.")


# Batch list for the Roster tab (the new Dashboard has its own selector).
all_batches = sorted(marked["Batch"].unique(), key=dc.batch_key)


# ───────────────────────────── tabs ──────────────────────────────────────────
tab_dash, tab_roster, tab_day1, tab_bsiai = st.tabs(
    ["📊 Dashboard", "📋 Roster (marked attendance)", "🎯 Day-1 analysis",
     "🧩 BSIAI"]
)

# ============================ TAB 1 — DASHBOARD ==============================
with tab_dash:
    if store_mode:
        # Everything was prebuilt by pipeline.py — render straight from the store.
        DATA, summary = store["DATA"], store["summary"]
        note = f"🟢 Data as of {store['generated_at']} · rebuilt every Monday 06:00 IST"
    else:
        # Legacy: build from the roster this server just marked.
        if live_ready and not live_failed:
            names = _attendee_names(st.session_state.nonce)
            note = f"🟢 Live — showing the updated roster · refreshed {datetime.now():%d %b %Y, %H:%M}"
        else:
            names = upload_attendee_names
            note = "📤 Showing the uploaded roster"
        DATA, summary = _build_dashboard(marked_bytes, names, l2_bytes)
    dash_view.render(DATA, summary, note)

# ============================ TAB 2 — ROSTER =================================
with tab_roster:
    st.caption("The marked attendance, student-by-student, exactly like the roster sheet.")
    if store_mode:
        grid_batches = [b for b in store["batches"] if b in set(all_batches)] or store["batches"]
    else:
        smap = _sheet_map(marked_bytes)
        grid_batches = [b for b in all_batches if b in smap]
    ctop1, ctop2 = st.columns([3, 2])
    pick = ctop1.selectbox("Batch", grid_batches,
                           index=0 if grid_batches else None, key="roster_pick")
    show_pii = ctop2.checkbox("Show full contact details (real PII)", value=False,
                              help="Off by default — emails/phones are masked.")
    g = None
    if pick:
        if store_mode:
            g = _store_grid(store["path"], pick, store["generated_at_iso"])
            g = g.copy() if g is not None else None
        else:
            g = _grid(marked_bytes, smap[pick]).copy()
    if g is None:
        st.info("No roster rows to show for this batch — try 🔄 Refresh from Google."
                if pick else "No batch sheets to show.")
    else:
        active_only = st.checkbox("Active learners only", value=active_mode, key="roster_active")
        if active_only:
            g = g[g["Active"].astype(bool)]
        sess_cols = [c for c in g.columns if c not in ("Email", "Phone", "Active", "Present")]
        n_students = len(g)
        n_present_any = int((g["Present"] > 0).sum())
        m1, m2, m3 = st.columns(3)
        m1.metric("Students shown", f"{n_students:,}")
        m2.metric("Attended ≥1 session", f"{n_present_any:,}")
        m3.metric("Sessions", len(sess_cols))

        disp = g.copy()
        if not show_pii:
            disp["Email"] = disp["Email"].map(mask_email)
            disp["Phone"] = disp["Phone"].map(mask_phone)
        disp = disp[["Email", "Phone", "Active", "Present"] + sess_cols]

        def _hl(v):
            s = str(v).lower()
            if s == "present":
                return "background-color: #d8f3dc; color:#1b4332"
            if s == "absent":
                return "background-color: #ffe5e5; color:#7d1128"
            return ""
        styled = disp.style.map(_hl, subset=sess_cols)  # .map (pandas >=2.1; applymap removed in 3.0)
        st.dataframe(styled, width='stretch', hide_index=True, height=520)

    st.divider()
    _marked_roster_download("dl_roster")

# ========================= TAB 3 — DAY-1 ANALYSIS ============================
# Day-one (and latest-session) attendance cut against payment (col I) and close
# type (col J), for the newest batches. Built by pipeline.py — day one is the
# earliest session the L2 schedule registers as a real class, so marketing
# walkthroughs never get mistaken for class 1 — and carried in the store as
# aggregates only (no PII). New batches appear on their own each Monday.
with tab_day1:
    import streamlit.components.v1 as _components
    _day1 = (store or {}).get("day1") if store_mode else None
    if not store_mode:
        st.info("This tab is built by the weekly pipeline. It appears once the app "
                "is reading the prebuilt store — see **SETUP_PIPELINE.md**.")
    elif not _day1 or not _day1.get("batches"):
        st.warning("No day-1 analysis in this build of the data.")
        for _s in (_day1 or {}).get("skipped", []):
            st.caption("• " + _s)
    else:
        _all = _day1["batches"]
        _codes = [b["batch"] for b in _all]
        # Batch choice lives HERE, not inside the page: st.components.html ignores
        # a frame-height message when a height is given, so the only way to avoid
        # either a nested scrollbar or a screen of dead space is for Python to
        # know how many batches it is about to draw.
        _pick = st.multiselect("Batches", _codes, default=_codes, key="day1_batches",
                               help="The newest batches; the window rolls on its own "
                                    "as new ones start.")
        _sel = [b for b in _all if b["batch"] in _pick] or _all
        _payload = dict(_day1, batches=_sel)
        _payload["generated"] = store["generated_at"]
        _payload["sheet_url"] = (
            "https://docs.google.com/spreadsheets/d/"
            f"{st.secrets.get('drive', {}).get('roster_id', '')}/edit"
            if _store_configured or live_ready else "")
        _tpl = (Path(__file__).parent / "day1_template.html").read_text(encoding="utf-8")
        # `</` must be escaped or a session title containing "</script>" would
        # terminate the inline script and blank the tab with no error shown.
        _blob = json.dumps(_payload).replace("</", "<\\/")
        # Measured: ~3.3k px of fixed chrome + ~1.15k per batch of chart rows.
        _components.html(_tpl.replace("__DATA__", _blob),
                         height=3350 + 1150 * len(_sel), scrolling=True)
# ============================ TAB 4 - BSIAI ==================================
with tab_bsiai:
    st.caption("The BSIAI programme. Separate roster, separate batch numbering - "
               "a session counts only once the L2 schedule lists its webinar.")

    _b = store.get("bsiai") if store_mode else None

    if not store_mode:
        st.info("The BSIAI tab reads the prebuilt store. It is empty in upload / "
                "legacy-live mode because BSIAI's roster is a different Sheet that "
                "the app does not fetch at runtime.")
    elif _b is None:
        st.warning(
            "**BSIAI is not configured yet.** Add `BSIAI_ROSTER_ID` (repo secret, "
            "or `bsiai_roster_id` under `[drive]` in secrets.toml) pointing at the "
            "BSIAI roster Sheet, and share that Sheet with the service account. "
            "The next refresh will fill this tab in."
        )
    elif not _b.get("DATA"):
        st.warning("BSIAI is configured but produced no batches in the last refresh.")
        for _w in _b.get("warnings", [])[:20]:
            st.caption("- " + str(_w))
    else:
        _note = (f"\U0001F7E2 Data as of {store['generated_at']} - {_b.get('source','')}"
                 .strip(" -"))
        dash_view.render(
            _b["DATA"], _b["summary"], _note, key="bsiai_batch",
            # BSIAI has no Close Type to slice by, so the same panel carries the
            # question its data CAN answer: how much of the course each learner
            # actually attends. A 55% average hides very different cohorts.
            show_closing=True,
            closing_title="Engagement",
            closing_sub=("bar = share of active learners · pill = that group's "
                         "own attendance rate"))
        _w = _b.get("warnings") or []
        if _w:
            with st.expander(f"Skipped / warnings ({len(_w)})"):
                for _line in _w:
                    st.text("- " + str(_line))
