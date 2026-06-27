"""
Be10X — AI CAP Attendance (unified app)
========================================

ONE place that:
  • pulls the roster + Zoom attendee data straight from Google Drive (when set up),
  • marks Present/Absent automatically — no button, no manual upload,
  • shows the marked roster (like the spreadsheet) AND a full dashboard with %s.

If Google isn't wired up yet, it falls back to manual file uploads, so it always
works. To go fully automatic, add the Drive secrets (see SETUP_LIVE.md / live_data.py).

Run locally:   streamlit run attendance_app.py
"""
from __future__ import annotations
import io
from datetime import datetime

import pandas as pd
import altair as alt
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
@st.cache_data(show_spinner="Marking new sessions…")
def _mark(roster_bytes, l2_bytes, attendee_files, mode):
    """Run the marker once per unique input set. values_only=True so formula-based
    attendance columns keep their values through the save (the dashboard reads
    values, not formulas)."""
    return ac.process_files(roster_bytes, l2_bytes, list(attendee_files),
                            mode=mode, values_only=True)


@st.cache_data(show_spinner="Building dashboard…")
def _compute(roster_bytes):
    return dc.compute(roster_bytes)


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
    import io as _io
    from openpyxl import load_workbook
    wb = load_workbook(_io.BytesIO(roster_bytes), read_only=True, data_only=True)
    tabs = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets}
    wb.close()
    topics = dsheets.webinar_topic_lookup([(n, None) for n in attendee_names], l2_bytes)
    return ddata.build(tabs, topics)


@st.cache_data(show_spinner=False)
def _grid(roster_bytes, sheet_name):
    return dc.roster_grid(roster_bytes, sheet_name)


@st.cache_data(show_spinner=False)
def _sheet_map(roster_bytes):
    return dc.batch_sheet_map(roster_bytes)


@st.cache_data(show_spinner="Reading live data from Google Drive… (one-time)")
def _fetch_live(nonce):
    """Pull roster/L2/attendee data from Drive ONCE and keep it cached.

    New sessions are only added on weekends, so there's deliberately NO time-based
    expiry — the data stays cached (shared across all viewers) until someone clicks
    '🔄 Refresh from Google', which bumps `nonce` and clears the cache. This is what
    stops the app from re-fetching on every little interaction."""
    return live_data.load_live()


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
with st.sidebar:
    st.header("① Data source")

    live_ready = live_data.config_present()
    if st.button("🔄 Refresh from Google", width='stretch', disabled=not live_ready,
                 help="Re-pull the latest roster + attendee data from Drive"):
        st.session_state.nonce += 1
        st.cache_data.clear()
        st.rerun()

    st.header("② Count basis")
    basis = st.radio(
        "Who counts as batch strength?",
        ["Active only (exclude refunds)", "All enrolled"],
        index=0,
        help="‘Active’ excludes rows whose Payment is a refund / unidentified / blank.",
    )

    with st.expander("⚙️ Matching rule (advanced)"):
        mode_label = st.radio(
            "How to match a student to an attendee",
            ["Exact — Registered mail + number (recommended)",
             "Inclusive — also WhatsApp / broadcast, last-10-digit phone"],
            index=0, label_visibility="collapsed",
        )
        mode = "exact" if mode_label.startswith("Exact") else "inclusive"


# ───────────────────────────── acquire raw inputs ────────────────────────────
roster_bytes = l2_bytes = None
attendee_files = None
source_label = None
live_failed = False

if live_ready:
    try:
        data = _fetch_live(st.session_state.nonce)
        roster_bytes = data["roster_bytes"]
        l2_bytes = data["l2_bytes"]
        attendee_files = data["attendee_files"]
        source_label = data["source"]
    except Exception as e:  # fall back to uploads, but tell the user why
        live_failed = True
        st.sidebar.error(f"Couldn’t read from Google Drive:\n\n{e}")

if roster_bytes is None:
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
    if up_zip is not None:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(up_zip.getvalue())) as z:
            attendee_files = [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
        source_label = f"Manual upload — roster + {len(attendee_files)} attendee files"
    else:
        source_label = "Manual upload — roster only"


# ───────────────────────────── mark (auto) then compute ──────────────────────
report, warnings = [], []
marked_bytes = roster_bytes
if attendee_files:
    try:
        marked_bytes, report, warnings = _mark(roster_bytes, l2_bytes, tuple(attendee_files), mode)
    except Exception as e:
        st.error(f"Marking failed, showing the roster as-is: {e}")

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
auto = "🟢 Live from Google Drive" if (live_ready and not live_failed) else "📤 Manual upload"
st.caption(f"{auto} · {source_label} · refreshed {datetime.now():%d %b %Y, %H:%M}")
if attendee_files and report:
    new_n = sum(1 for r in report if r["kind"] == "NEW")
    st.success(f"✅ Auto-marked attendance: {len(report)} session column(s) across "
               f"{len({r['batch'] for r in report})} batch(es) "
               f"({new_n} new, {len(report)-new_n} re-marked).")

# Prominent: the full marked roster (all sessions) — same deliverable as the original marker
st.download_button(
    "⬇️ Download marked roster — ALL sessions (.xlsx)",
    data=marked_bytes,
    file_name="Master_Batch_Rosters_marked.xlsx",
    mime=XLSX_MIME, type="primary", key="dl_top",
)
st.caption("The complete marked workbook — every batch, all sessions, Present/Absent.")


# Batch list for the Roster tab (the new Dashboard has its own selector).
all_batches = sorted(marked["Batch"].unique(), key=dc.batch_key)


# ───────────────────────────── tabs ──────────────────────────────────────────
tab_dash, tab_roster, tab_log = st.tabs(
    ["📊 Dashboard", "📋 Roster (marked attendance)", "🪵 Marking log"]
)

# ============================ TAB 1 — DASHBOARD ==============================
with tab_dash:
    # The dashboard shows the SAME updated roster the app produced (the downloadable
    # one) — no second fetch. Session names come from the Webinar-ID topic join.
    if live_ready and not live_failed:
        names = _attendee_names(st.session_state.nonce)
        note = f"🟢 Live — showing the updated roster · refreshed {datetime.now():%d %b %Y, %H:%M}"
    else:
        names = tuple(n for n, _ in (attendee_files or ()))
        note = "📤 Showing the uploaded roster"
    DATA, summary = _build_dashboard(marked_bytes, names, l2_bytes)
    dash_view.render(DATA, summary, note)

# ============================ TAB 2 — ROSTER =================================
with tab_roster:
    st.caption("The marked attendance, student-by-student, exactly like the roster sheet.")
    smap = _sheet_map(marked_bytes)
    grid_batches = [b for b in all_batches if b in smap]
    ctop1, ctop2 = st.columns([3, 2])
    pick = ctop1.selectbox("Batch", grid_batches,
                           index=0 if grid_batches else None, key="roster_pick")
    show_pii = ctop2.checkbox("Show full contact details (real PII)", value=False,
                              help="Off by default — emails/phones are masked.")
    if not pick:
        st.info("No batch sheets to show.")
    else:
        g = _grid(marked_bytes, smap[pick]).copy()
        active_only = st.checkbox("Active learners only", value=active_mode, key="roster_active")
        if active_only:
            g = g[g["Active"]]
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
    st.download_button(
        "⬇️ Download the full marked roster (.xlsx)",
        data=marked_bytes,
        file_name="Master_Batch_Rosters_marked.xlsx",
        mime=XLSX_MIME, type="primary", key="dl_roster",
    )

# ============================ TAB 3 — MARKING LOG ============================
with tab_log:
    if not attendee_files:
        st.info("No fresh attendee data was processed this run — the dashboard is "
                "reading marks already in the roster. Add an attendee .zip (or connect "
                "Google Drive) to mark new sessions automatically.")
    else:
        if report:
            rep = pd.DataFrame(report)
            rep["rate"] = (rep["present"] / rep["total"].clip(lower=1) * 100).round(0).astype(int).astype(str) + "%"
            rep = rep.rename(columns={"batch": "Batch", "date": "Date", "col": "Col",
                                      "kind": "Action", "topic": "Topic",
                                      "present": "Present", "total": "Students", "rate": "Rate"})
            st.dataframe(rep[["Batch", "Date", "Col", "Action", "Present", "Students", "Rate", "Topic"]],
                         width='stretch', hide_index=True)
        if warnings:
            with st.expander(f"⚠️ Skipped / warnings ({len(warnings)})", expanded=False):
                for w in warnings:
                    st.text("• " + w)
        elif report:
            st.success("No warnings — every attendee file mapped cleanly to a batch.")
