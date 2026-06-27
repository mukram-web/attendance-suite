import io
import pandas as pd
import streamlit as st
import attendance_core as ac

st.set_page_config(page_title="AttendaSync — Attendance Marker", page_icon="✅", layout="wide")

st.title("📋 AttendaSync — Bulk Attendance Marker")
st.caption("Upload three files. Get a fully marked Master Batch Roster back.")

with st.expander("How it works", expanded=False):
    st.markdown(
        """
1. **Master Batch Roster** – your `.xlsx` with one sheet per batch (`AI CAP B17` … `B28`).
   Columns A–J hold the real student data; the app reads **Registered mail** and
   **Registered Number** by their header names (so a shifted layout is fine).
2. **L2 Weekly Sessions** – the schedule `.xlsx`. The app reads **Batch Name**,
   **Topic Name** and **Webinar ID** from every month tab to learn which session ran
   for which batch on which date.
3. **Zoom Attendee Reports** – a `.zip` of the raw Zoom `attendee_<id>_<date>.csv`
   exports (sub-folders are fine).

For every attendee file the app reads the **Webinar ID from the filename**, looks it up
in L2 to find which **batch(es)**, date and topic it belongs to (and falls back to the
attendee's folder name if a webinar isn't in L2 yet). In each covered batch's sheet it
**re-marks the matching date column** or **adds a new column** if that session isn't there
yet — *Present* when the student matches the attendee list, otherwise *Absent*. Blank cells
never match. Sessions with no attendee file are left untouched.

**Shared sessions are marked in every batch.** A webinar listed for several batches
(`AI CAP B2 + B3 + B4`, or a range like `AI CAP B1 - AI CAP B22`) is marked in *each*
batch that has a sheet in your roster — nothing is skipped. Anything that maps to a batch
with **no sheet in the uploaded roster** is listed under warnings, so you can add that
sheet if you want it marked.
        """
    )

c1, c2, c3 = st.columns(3)
roster_f = c1.file_uploader("1 · Master Batch Roster (.xlsx)", type=["xlsx"])
l2_f     = c2.file_uploader("2 · L2 Weekly Sessions (.xlsx)", type=["xlsx"])
zip_f    = c3.file_uploader("3 · Zoom Attendee Reports (.zip)", type=["zip"])

mode_label = st.radio(
    "Matching rule",
    ["Exact — Registered mail + Registered Number only (recommended)",
     "Inclusive — also WhatsApp / broadcast, last-10-digit phone match"],
    index=0,
)
mode = "exact" if mode_label.startswith("Exact") else "inclusive"

ready = bool(roster_f and l2_f and zip_f)
if st.button("Mark attendance", type="primary", disabled=not ready):
    with st.spinner("Reading files, mapping sessions and marking attendance…"):
        try:
            out, report, warnings = ac.process(
                roster_f.getvalue(), l2_f.getvalue(), zip_f.getvalue(), mode=mode
            )
            st.session_state.update(out=out, report=report, warnings=warnings,
                                    fname=getattr(roster_f, "name", "roster.xlsx"))
        except Exception as e:
            st.error(f"Something went wrong: {e}")
            st.stop()

if "out" in st.session_state:
    report   = st.session_state["report"]
    warnings = st.session_state["warnings"]
    out      = st.session_state["out"]
    df = pd.DataFrame(report)

    if df.empty:
        st.warning("No sessions matched a batch sheet. Check that the webinar IDs in L2 "
                   "match the attendee filenames and that your roster has the batch sheets.")
    else:
        new_n   = int((df["kind"] == "NEW").sum())
        remk_n  = int((df["kind"] == "re-mark").sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Columns marked", len(df))
        m2.metric("New / re-marked", f"{new_n} / {remk_n}")
        m3.metric("Batches updated", df["batch"].nunique())
        m4.metric("Total present marks", int(df["present"].sum()))

        view = df.assign(rate=(df["present"] / df["total"].clip(lower=1) * 100).round(0)
                         .astype(int).astype(str) + "%")
        view = view.rename(columns={"batch": "Batch", "date": "Date", "col": "Col",
                                    "kind": "Action", "topic": "Topic",
                                    "present": "Present", "total": "Students",
                                    "rate": "Rate"})
        st.dataframe(
            view[["Batch", "Date", "Col", "Action", "Present", "Students", "Rate", "Topic"]],
            use_container_width=True, hide_index=True,
        )

    if warnings:
        with st.expander(f"Skipped / warnings ({len(warnings)})"):
            for w in warnings:
                st.text("• " + w)

    base = st.session_state.get("fname", "roster.xlsx").rsplit(".", 1)[0]
    st.download_button(
        "⬇️ Download marked roster",
        data=out,
        file_name=f"{base}_marked.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
