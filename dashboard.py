"""
Be10X — AI CAP Attendance Dashboard (Streamlit)

Upload the Master Roster and explore batch-wise attendance:
  • KPIs (active learners, average attendance, best / weakest batch)
  • Attendance % by batch  (overall, or for a chosen session)
  • Heatmap: batch x session
  • Trend: attendance across sessions
  • Filterable detail table + CSV download

Run locally:  streamlit run dashboard.py
"""
import io
import pandas as pd
import altair as alt
import streamlit as st

import dashboard_core as dc

st.set_page_config(page_title="Be10X Attendance Dashboard", page_icon="📊", layout="wide")

# ----------------------------------------------------------------- header
st.title("📊 Be10X — AI CAP Attendance Dashboard")
st.caption("Batch-wise attendance, read straight from the Master Roster.")

# ----------------------------------------------------------------- sidebar: data + controls
with st.sidebar:
    st.header("① Data")
    up = st.file_uploader("Master Roster (.xlsx)", type=["xlsx"])
    st.caption("Later this can read live from Google — for now, upload the roster file.")

    st.header("② Count basis")
    basis = st.radio(
        "Who counts as the batch strength?",
        ["Active only (exclude refunds)", "All enrolled"],
        index=0,
        help="‘Active’ excludes rows whose Payment is a refund / unidentified / blank.",
    )

if up is None:
    st.info("👈 Upload your **Master Roster (.xlsx)** in the sidebar to build the dashboard.")
    st.stop()

# ----------------------------------------------------------------- compute
try:
    df = dc.compute(up.getvalue())
except Exception as e:
    st.error(f"Couldn't read that file: {e}")
    st.stop()

if df.empty:
    st.warning("No batch sheets with session columns were found in this file.")
    st.stop()

active_mode = basis.startswith("Active")
PCT = "PctActive" if active_mode else "PctAll"
PRES = "PresentActive" if active_mode else "PresentAll"
DEN = "Active" if active_mode else "Total"
den_label = "active learners" if active_mode else "enrolled"

# Only sessions that actually have attendance marked
marked = df[df["HasData"]].copy()
if marked.empty:
    st.warning("No sessions have attendance marked yet in this roster.")
    st.stop()

# ----------------------------------------------------------------- sidebar: filters (need data first)
all_batches = sorted(marked["Batch"].unique(), key=dc.batch_key)
with st.sidebar:
    st.header("③ Filters")
    sel_batches = st.multiselect("Batches", all_batches, default=all_batches)

    # Session options labelled by the most common topic at each index
    idx_topic = (
        marked.groupby("SessionIdx")["Topic"]
        .agg(lambda s: next((t for t in s if t), ""))
        .to_dict()
    )
    max_idx = int(marked["SessionIdx"].max())
    sess_options = ["Overall (average)"] + [
        f"Session {i}" + (f" — {idx_topic.get(i,'')}" if idx_topic.get(i) else "")
        for i in range(1, max_idx + 1)
    ]
    sess_choice = st.selectbox("Session focus", sess_options, index=0)

if not sel_batches:
    st.warning("Pick at least one batch.")
    st.stop()

view = marked[marked["Batch"].isin(sel_batches)].copy()

# ----------------------------------------------------------------- build the by-batch series for the chosen focus
if sess_choice.startswith("Overall"):
    focus_label = "Overall (avg across sessions)"
    by_batch = (
        view.groupby("Batch")
        .agg(Pct=(PCT, "mean"), Present=(PRES, "sum"), Den=(DEN, "first"), Sessions=("SessionIdx", "nunique"))
        .reset_index()
    )
    by_batch["Pct"] = by_batch["Pct"].round(1)
else:
    sidx = int(sess_choice.split(" ")[1].split("—")[0].strip())
    focus_label = sess_choice
    sub = view[view["SessionIdx"] == sidx]
    by_batch = (
        sub.groupby("Batch")
        .agg(Pct=(PCT, "first"), Present=(PRES, "first"), Den=(DEN, "first"))
        .reset_index()
    )

by_batch = by_batch.sort_values("Pct", ascending=False)

# ----------------------------------------------------------------- KPIs
total_active = int(view.groupby("Batch")[DEN].first().sum())
avg_pct = round(by_batch["Pct"].mean(), 1) if not by_batch.empty else 0
best = by_batch.iloc[0] if not by_batch.empty else None
worst = by_batch.iloc[-1] if not by_batch.empty else None

k1, k2, k3, k4 = st.columns(4)
k1.metric(f"Total {den_label}", f"{total_active:,}")
k2.metric("Avg attendance", f"{avg_pct:.0f}%")
if best is not None:
    k3.metric("Top batch", best["Batch"], f"{best['Pct']:.0f}%")
if worst is not None:
    k4.metric("Weakest batch", worst["Batch"], f"{worst['Pct']:.0f}%")

st.markdown(f"**Showing:** {focus_label} · basis = _{basis.lower()}_")

# ----------------------------------------------------------------- chart 1: bar by batch
st.subheader("Attendance % by batch")
bar = (
    alt.Chart(by_batch)
    .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
    .encode(
        x=alt.X("Batch:N", sort=list(by_batch["Batch"]), title=None),
        y=alt.Y("Pct:Q", title="% present", scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("Pct:Q", scale=alt.Scale(scheme="blues"), legend=None),
        tooltip=["Batch", alt.Tooltip("Pct", title="% present"),
                 alt.Tooltip("Present", title="present"), alt.Tooltip("Den", title=den_label)],
    )
    .properties(height=340)
)
labels = bar.mark_text(dy=-6, fontSize=11).encode(text=alt.Text("Pct:Q", format=".0f"))
st.altair_chart(bar + labels, use_container_width=True)

# ----------------------------------------------------------------- chart 2: heatmap batch x session
st.subheader("Heatmap — batch × session")
st.caption("Each cell = % present. Sessions follow the same topic order across batches.")
heat = (
    alt.Chart(view)
    .mark_rect()
    .encode(
        x=alt.X("SessionIdx:O", title="Session #"),
        y=alt.Y("Batch:N", sort=all_batches, title=None),
        color=alt.Color(f"{PCT}:Q", scale=alt.Scale(scheme="blues"), title="% present"),
        tooltip=["Batch", "SessionIdx", alt.Tooltip("Topic"), alt.Tooltip(PCT, title="% present"),
                 alt.Tooltip(PRES, title="present"), alt.Tooltip(DEN, title=den_label)],
    )
    .properties(height=28 * len(sel_batches) + 40)
)
st.altair_chart(heat, use_container_width=True)

# ----------------------------------------------------------------- chart 3: trend
st.subheader("Attendance trend across sessions")
trend = (
    alt.Chart(view)
    .mark_line(point=True)
    .encode(
        x=alt.X("SessionIdx:Q", title="Session #"),
        y=alt.Y(f"{PCT}:Q", title="% present", scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("Batch:N", sort=all_batches),
        tooltip=["Batch", "SessionIdx", alt.Tooltip("Topic"), alt.Tooltip(PCT, title="% present")],
    )
    .properties(height=360)
)
st.altair_chart(trend, use_container_width=True)

# ----------------------------------------------------------------- detail table + download
st.subheader("Detail")
show = view[["Batch", "SessionIdx", "SessionLabel", "Topic", PRES, DEN, PCT]].rename(
    columns={PRES: "Present", DEN: den_label.title(), PCT: "% Present", "SessionLabel": "Session"}
)
st.dataframe(show, use_container_width=True, hide_index=True)
st.download_button(
    "⬇️ Download this view (CSV)",
    show.to_csv(index=False).encode("utf-8"),
    file_name="attendance_dashboard.csv",
    mime="text/csv",
)
