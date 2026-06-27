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


# ───────────────────────────── sidebar filters (need data) ───────────────────
all_batches = sorted(marked["Batch"].unique(), key=dc.batch_key)
with st.sidebar:
    st.header("③ Filters")
    sel_batches = st.multiselect("Batches", all_batches, default=all_batches)
    idx_topic = (marked.groupby("SessionIdx")["Topic"]
                 .agg(lambda s: next((t for t in s if t), "")).to_dict())
    max_idx = int(marked["SessionIdx"].max())
    sess_options = ["Overall (average)"] + [
        f"Session {i}" + (f" — {idx_topic.get(i,'')}" if idx_topic.get(i) else "")
        for i in range(1, max_idx + 1)
    ]
    sess_choice = st.selectbox("Session focus", sess_options, index=0)

if not sel_batches:
    st.warning("Pick at least one batch in the sidebar.")
    st.stop()

view = marked[marked["Batch"].isin(sel_batches)].copy()


# ───────────────────────────── tabs ──────────────────────────────────────────
tab_dash, tab_roster, tab_log = st.tabs(
    ["📊 Dashboard", "📋 Roster (marked attendance)", "🪵 Marking log"]
)

# ============================ TAB 1 — DASHBOARD ==============================
with tab_dash:
    if sess_choice.startswith("Overall"):
        focus_label = "Overall (avg across sessions)"
        by_batch = (view.groupby("Batch")
                    .agg(Pct=(PCT, "mean"), Present=(PRES, "sum"),
                         Den=(DEN, "first"), Sessions=("SessionIdx", "nunique"))
                    .reset_index())
        by_batch["Pct"] = by_batch["Pct"].round(1)
    else:
        sidx = int(sess_choice.split(" ")[1].split("—")[0].strip())
        focus_label = sess_choice
        sub = view[view["SessionIdx"] == sidx]
        by_batch = (sub.groupby("Batch")
                    .agg(Pct=(PCT, "first"), Present=(PRES, "first"), Den=(DEN, "first"))
                    .reset_index())
    by_batch = by_batch.sort_values("Pct", ascending=False)

    total_strength = int(view.groupby("Batch")[DEN].first().sum())
    avg_pct = round(by_batch["Pct"].mean(), 1) if not by_batch.empty else 0
    best = by_batch.iloc[0] if not by_batch.empty else None
    worst = by_batch.iloc[-1] if not by_batch.empty else None

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"Total {den_label}", f"{total_strength:,}")
    k2.metric("Avg attendance", f"{avg_pct:.0f}%")
    if best is not None:
        k3.metric("Top batch", best["Batch"], f"{best['Pct']:.0f}%")
    if worst is not None:
        k4.metric("Weakest batch", worst["Batch"], f"{worst['Pct']:.0f}%")
    st.markdown(f"**Showing:** {focus_label} · basis = _{basis.lower()}_")

    st.subheader("Attendance % by batch")
    bar = (alt.Chart(by_batch)
           .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
           .encode(
               x=alt.X("Batch:N", sort=list(by_batch["Batch"]), title=None),
               y=alt.Y("Pct:Q", title="% present", scale=alt.Scale(domain=[0, 100])),
               color=alt.Color("Pct:Q", scale=alt.Scale(scheme="blues"), legend=None),
               tooltip=["Batch", alt.Tooltip("Pct", title="% present"),
                        alt.Tooltip("Present", title="present"),
                        alt.Tooltip("Den", title=den_label)],
           ).properties(height=340))
    labels = bar.mark_text(dy=-6, fontSize=11).encode(text=alt.Text("Pct:Q", format=".0f"))
    st.altair_chart(bar + labels, width='stretch')

    st.subheader("Heatmap — batch × session")
    st.caption("Each cell = % present. Sessions follow the same topic order across batches.")
    heat = (alt.Chart(view).mark_rect().encode(
        x=alt.X("SessionIdx:O", title="Session #"),
        y=alt.Y("Batch:N", sort=all_batches, title=None),
        color=alt.Color(f"{PCT}:Q", scale=alt.Scale(scheme="blues"), title="% present"),
        tooltip=["Batch", "SessionIdx", alt.Tooltip("Topic"),
                 alt.Tooltip(PCT, title="% present"), alt.Tooltip(PRES, title="present"),
                 alt.Tooltip(DEN, title=den_label)],
    ).properties(height=28 * len(sel_batches) + 40))
    st.altair_chart(heat, width='stretch')

    st.subheader("Attendance trend across sessions")
    trend = (alt.Chart(view).mark_line(point=True).encode(
        x=alt.X("SessionIdx:Q", title="Session #"),
        y=alt.Y(f"{PCT}:Q", title="% present", scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("Batch:N", sort=all_batches),
        tooltip=["Batch", "SessionIdx", alt.Tooltip("Topic"),
                 alt.Tooltip(PCT, title="% present")],
    ).properties(height=360))
    st.altair_chart(trend, width='stretch')

    st.subheader("Per-session detail")
    show = view[["Batch", "SessionIdx", "SessionLabel", "Topic", PRES, DEN, PCT]].rename(
        columns={PRES: "Present", DEN: den_label.title(), PCT: "% Present",
                 "SessionLabel": "Session"})
    st.dataframe(show, width='stretch', hide_index=True)
    st.download_button("⬇️ Download this view (CSV)",
                       show.to_csv(index=False).encode("utf-8"),
                       file_name="attendance_dashboard.csv", mime="text/csv")

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
