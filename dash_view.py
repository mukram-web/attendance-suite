"""
dash_view.py — Streamlit + Plotly rendering of the AI CAP attendance dashboard.

Mirrors the static HTML prototype: 4 KPIs → batch selector + cross-batch
comparison bar → selected-batch drill-down (metric cards, attendance-by-date
line, closing-types panel, sessions table). Colour bands and look match the
prototype. All numbers come from data.build(); this file is presentation only.
"""
from __future__ import annotations
import streamlit as st
import plotly.graph_objects as go

import data as D

# prototype colour bands
_BAND = {
    "high": {"bg": "#e3f4ec", "fg": "#0f6e56", "hex": "#1a9e75"},
    "mid":  {"bg": "#fbeedd", "fg": "#8a5108", "hex": "#c98500"},
    "low":  {"bg": "#fae4dd", "fg": "#993c1d", "hex": "#d8543a"},
}
_ACCENT = "#2a5bd7"
_INK2 = "#5a6573"

_CSS = """
<style>
  .aicap .panel-title{font-size:14px;font-weight:600;margin:18px 0 10px;color:#151a22;}
  .aicap .pill{padding:2px 9px;border-radius:999px;font-weight:600;font-size:12px;
    font-variant-numeric:tabular-nums;display:inline-block;}
  .aicap .cl-row{display:grid;grid-template-columns:140px 1fr 120px 64px;align-items:center;
    gap:12px;padding:8px 0;border-bottom:1px solid #eef1f6;}
  .aicap .cl-name{font-size:13px;color:#151a22;}
  .aicap .cl-track{height:8px;background:#eef1f6;border-radius:6px;overflow:hidden;}
  .aicap .cl-fill{height:100%;background:#9fb4e8;border-radius:6px;}
  .aicap .cl-count{font-size:12px;color:#5a6573;text-align:right;font-variant-numeric:tabular-nums;}
  .aicap table.sess{width:100%;border-collapse:collapse;font-size:13px;}
  .aicap table.sess th,.aicap table.sess td{padding:8px 10px;border-bottom:1px solid #eef1f6;text-align:left;}
  .aicap table.sess th.num,.aicap table.sess td.num{text-align:right;font-variant-numeric:tabular-nums;}
  .aicap table.sess thead th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8c95a3;}
  .aicap .flag{font-size:10px;color:#8a5108;background:#fbeedd;padding:1px 6px;border-radius:6px;margin-left:6px;}
  .aicap .dh-sub{color:#5a6573;font-weight:400;font-size:14px;}
</style>
"""


def _pill(pct: float) -> str:
    b = _BAND[D.band(pct)]
    return (f'<span class="pill" style="background:{b["bg"]};color:{b["fg"]}">'
            f'{pct:.0f}%</span>')


def _comparison_bar(DATA: dict):
    codes = list(DATA)
    pcts = [DATA[c]["avg_pct"] for c in codes]
    fig = go.Figure(go.Bar(
        x=codes, y=pcts,
        marker_color=[_BAND[D.band(p)]["hex"] for p in pcts],
        text=[f"{p:.0f}%" for p in pcts], textposition="outside",
        hovertemplate="<b>%{x}</b><br>%{y:.1f}% avg attendance<extra></extra>",
    ))
    fig.update_layout(
        height=300, margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(range=[0, max(60, max(pcts) + 8)], ticksuffix="%",
                   gridcolor="#eef1f6", title=None),
        xaxis=dict(title=None), plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="Inter, system-ui, sans-serif", color=_INK2, size=12),
        showlegend=False,
    )
    return fig


def _date_line(d: dict):
    sess = d["sessions"]
    ymax = max(60, int((d["peak"] + 6) // 5 * 5 + 5))
    cust = [[s["topic"], s["present"], s["total"],
             "(no absent logged)" if s["present_only"] else ""] for s in sess]
    fig = go.Figure(go.Scatter(
        x=[s["date_lbl"] for s in sess], y=[s["pct"] for s in sess],
        mode="lines+markers", line=dict(color=_ACCENT, width=2, shape="spline"),
        fill="tozeroy", fillcolor="rgba(42,91,215,0.08)",
        marker=dict(size=7, color=_ACCENT, line=dict(color="white", width=1.5)),
        customdata=cust,
        hovertemplate=("<b>%{x}</b><br>%{customdata[0]}<br>"
                       "Present: %{customdata[1]:,} / %{customdata[2]:,}<br>"
                       "%{y:.1f}% of strength<br>%{customdata[3]}<extra></extra>"),
    ))
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=10),
        yaxis=dict(range=[0, ymax], ticksuffix="%", gridcolor="#eef1f6", title=None),
        xaxis=dict(title=None, showgrid=False),
        plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="Inter, system-ui, sans-serif", color=_INK2, size=12),
    )
    return fig


def render(DATA: dict, summary: dict, source_note: str = "") -> None:
    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown('<div class="aicap">', unsafe_allow_html=True)

    # ── KPIs ──
    k = st.columns(4)
    k[0].metric("Enrolled", f"{summary['enrolled']:,}")
    k[1].metric("Active", f"{summary['active']:,}")
    k[2].metric("Batches", summary["batches"])
    k[3].metric("Sessions logged", summary["sessions"])
    if source_note:
        st.caption(source_note)

    if not DATA:
        st.warning("No batch tabs found in the sheet.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    codes = list(DATA)

    # ── cross-batch comparison ──
    st.markdown('<div class="panel-title">Average attendance by batch '
                '<span class="dh-sub">· present % of strength</span></div>',
                unsafe_allow_html=True)
    st.plotly_chart(_comparison_bar(DATA), width="stretch",
                    config={"displayModeBar": False})

    # ── batch selector (drives drill-down) ──
    sel = st.segmented_control("Batch — pick to drill in", codes,
                               default=codes[0], key="aicap_batch")
    if sel is None:
        sel = codes[0]
    d = DATA[sel]

    st.markdown(
        f'<div class="panel-title" style="font-size:18px">Batch {sel} '
        f'<span class="dh-sub">· {d["n_sessions"]} sessions logged · '
        f'strength {d["strength"]:,}</span></div>', unsafe_allow_html=True)

    # ── metric cards ──
    peak_s = max(d["sessions"], key=lambda s: s["pct"])
    low_s = min(d["sessions"], key=lambda s: s["pct"])
    c = st.columns(4)
    c[0].metric("Total strength", f"{d['strength']:,}", "all enrolled", delta_color="off")
    c[1].metric("Active", f"{d['active']:,}", "excl. refund / unidentified", delta_color="off")
    c[2].metric("Avg attendance", f"{d['avg_pct']:.1f}%", "of total strength", delta_color="off")
    c[3].metric("Peak → lowest", f"{d['peak']:.0f}% → {d['low']:.0f}%",
                f"{peak_s['date_lbl']} → {low_s['date_lbl']}", delta_color="off")

    # ── attendance by date ──
    st.markdown('<div class="panel-title">Attendance by date</div>', unsafe_allow_html=True)
    st.plotly_chart(_date_line(d), width="stretch", config={"displayModeBar": False})

    # ── closing types ──
    st.markdown('<div class="panel-title">Closing types '
                '<span class="dh-sub">· bar = share of batch · pill = that channel\'s '
                'avg attendance</span></div>', unsafe_allow_html=True)
    rows = []
    for ch in d["closing"]:
        rows.append(
            f'<div class="cl-row"><div class="cl-name">{ch["type"]}</div>'
            f'<div class="cl-track"><div class="cl-fill" style="width:{ch["pct"]:.1f}%"></div></div>'
            f'<div class="cl-count">{ch["count"]:,} · {ch["pct"]:.0f}%</div>'
            f'<div>{_pill(ch["att"])}</div></div>')
    st.markdown("".join(rows), unsafe_allow_html=True)

    # ── sessions table ──
    n_missing = sum(1 for s in d["sessions"] if s["no_l2"])
    sub = f' <span class="dh-sub">· {d["n_sessions"]} rows'
    if n_missing:
        sub += f' · {n_missing} without an L2 topic match</span>'
    else:
        sub += "</span>"
    st.markdown(f'<div class="panel-title">All sessions{sub}</div>', unsafe_allow_html=True)
    trs = []
    for s in d["sessions"]:
        absent = "—" if s["present_only"] else f'{s["absent"]:,}'
        flag = '<span class="flag">no absent logged</span>' if s["present_only"] else ""
        miss = '<span class="flag">no L2 match</span>' if s["no_l2"] else ""
        trs.append(
            f'<tr><td>{s["date_lbl"]}</td><td>{s["topic"]}{flag}{miss}</td>'
            f'<td class="num">{s["present"]:,}</td><td class="num">{absent}</td>'
            f'<td class="num">{_pill(s["pct"])}</td></tr>')
    st.markdown(
        '<table class="sess"><thead><tr><th>Date</th><th>Session</th>'
        '<th class="num">Present</th><th class="num">Absent</th>'
        '<th class="num">% of strength</th></tr></thead><tbody>'
        + "".join(trs) + "</tbody></table>", unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
