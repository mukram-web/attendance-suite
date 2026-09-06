"""
dash_view.py — Streamlit + Plotly rendering of the AI CAP attendance dashboard.

Mirrors the static HTML prototype: 4 KPIs → batch selector + cross-batch
comparison bar → selected-batch drill-down (metric cards, attendance-by-date
line, closing-types panel, sessions table). Colour bands and look match the
prototype. All numbers come from data.build(); this file is presentation only.

The figure builders and the HTML fragments (_comparison_bar, _date_line,
closing_rows_html, sessions_table_html, CSS) are PURE — no Streamlit — because
site_build.py reuses them to render the identical front end as a static
website. Change the look here and both the app and the site follow.
"""
from __future__ import annotations
import html
import plotly.graph_objects as go

import data as D
import polls as _polls          # pure; the NPS rule must live in one place only
import pods as _pods            # for the WHOLE_BATCH sentinel below

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
    # intro-call rows carry no percentage — the % line shows class sessions only
    sess = [s for s in d["sessions"] if not s.get("is_intro")]
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


def pod_names(d: dict) -> list:
    """PODs worth offering as a filter, biggest first.

    'Unassigned' is included when real - those students are enrolled and their
    absence from a POD is itself worth seeing - but a batch whose only bucket is
    Unassigned has no PODs at all and gets no filter.
    """
    pods = d.get("pods") or {}
    real = [p for p in pods if p != "Unassigned"]
    if not real:
        return []
    return sorted(pods, key=lambda p: -pods[p]["strength"])


def pod_view(d: dict, pod: str | None) -> dict:
    """One batch as seen through a single POD, in the same shape `d` has.

    `pod` None/"" means every POD: the date rollup drives the chart and the
    headline, so a week with eleven sessions is one point weighted by POD size
    rather than eleven competing lines.
    """
    if not pod:
        rows = d.get("by_date")
        if not rows:
            # No date rollup (BSIAI builds none - one session per date, no PODs).
            # Return the batch untouched rather than an empty list, which blanked
            # the whole BSIAI tab with "No sessions logged for None yet".
            return d
        rows = list(rows)
        # These rows are rendered by the same table as real sessions, so they must
        # carry the SAME keys - a rollup row missing "absent" took the whole tab
        # down with a KeyError.
        by_mm = {}
        for sx in d["sessions"]:
            by_mm.setdefault(sx["mm"], []).append(sx)
        sess = []
        for r in rows:
            same = by_mm.get(r["mm"], [])
            n = r.get("n_pods", 1)
            # Carry the poll rating onto the rollup row, or it vanishes from the
            # default view - which is where almost everyone looks. Several PODs
            # on one date average by RESPONSES, so a 316-response session is not
            # outweighed by a 13-response one.
            rated = [x for x in same if x.get("rating") is not None]
            if rated:
                wn = sum((x.get("rating_n") or 1) for x in rated)
                rating = round(sum(x["rating"] * (x.get("rating_n") or 1)
                                   for x in rated) / wn, 2)
            else:
                rating, wn = None, 0
            sess.append({
                "rating": rating, "rating_n": wn,
                # same reason as the rating: without this the rolled-up row
                # loses the trainer and the column reads blank for pod days
                "mentor": next((x.get("mentor") for x in same
                                if (x.get("mentor") or "").strip()), ""),
                # Weighted by responses like the overall rating above. It used
                # to be carried only when exactly one POD had a poll, which was
                # fine for a tooltip but leaves a dedicated column blank on every
                # multi-POD day - which is most of them since 23 Aug.
                "rating_trainer": (
                    round(sum(x["rating_trainer"] * (x.get("rating_n") or 1)
                              for x in rated if x.get("rating_trainer") is not None)
                          / sum((x.get("rating_n") or 1) for x in rated
                                if x.get("rating_trainer") is not None), 2)
                    if any(x.get("rating_trainer") is not None for x in rated)
                    else None),
                "rating_recommend": (rated[0].get("rating_recommend")
                                     if len(rated) == 1 else None),
                # NPS is a percentage, so pod rows are combined by SUMMING their
                # 1-5 histograms and recomputing - averaging the percentages
                # would weight a 13-response pod like a 316-response one.
                "rating_dist": (merged := _polls.merge_dists(
                    x.get("rating_dist") for x in same)),
                "rating_nps": _polls.nps_from_dist(merged.get("recommend")),
                "col": None, "mm": r["mm"], "date_lbl": r["date_lbl"],
                "topic": (f"{n} POD sessions" if n > 1
                          else (same[0]["topic"] if same else "Session")),
                "pod": "", "l2_batch": "" if n > 1 else (same[0].get("l2_batch", "")
                                                         if same else ""),
                "present": r["present"], "total": r["total"],
                "absent": max(0, r["total"] - r["present"]),
                "pct": r["pct"], "present_only": False, "no_l2": False,
                "is_intro": False, "n_pods": n,
            })
        # intro rows live on d["sessions"] and have no date bucket - keep them
        sess = [s for s in d["sessions"] if s.get("is_intro")] + sess
        return dict(d, sessions=sess)

    if pod == _pods.WHOLE_BATCH:
        # The sessions L2 runs for EVERYONE. Its Batch Name cell says so in six
        # different ways across the live sheet -- 'All Domains', 'AllDomains',
        # 'Common', 'General', or just the bare batch -- and pods.from_l2_label
        # folds all of them to WHOLE_BATCH, so they carry no pod and were only
        # reachable under 'All PODs', mixed in with the domain sessions.
        # Strength stays the whole batch, because the whole batch was invited.
        sess = [s for s in d["sessions"] if not s.get("pod")]
        if not sess:
            return dict(d, sessions=[], n_sessions=0, avg_pct=0.0, peak=0.0, low=0.0)
        pcts = [s["pct"] for s in sess]
        return dict(d, sessions=sess, n_sessions=len(sess),
                    avg_pct=round(sum(pcts) / len(pcts), 1),
                    peak=max(pcts), low=min(pcts))

    sess = [s for s in d["sessions"] if s.get("pod") == pod]
    if not sess:
        return dict(d, sessions=[], n_sessions=0, avg_pct=0.0, peak=0.0, low=0.0)
    pcts = [s["pct"] for s in sess]
    info = (d.get("pods") or {}).get(pod, {})
    return dict(d, sessions=sess, n_sessions=len(sess),
                avg_pct=round(sum(pcts) / len(pcts), 1),
                peak=max(pcts), low=min(pcts),
                strength=info.get("strength", d["strength"]),
                active=info.get("active", d["active"]))


def closing_rows_html(d: dict) -> str:
    """The closing-types panel rows — shared verbatim by the app and the site."""
    rows = []
    for ch in d["closing"]:
        rows.append(
            f'<div class="cl-row"><div class="cl-name">{ch["type"]}</div>'
            f'<div class="cl-track"><div class="cl-fill" style="width:{ch["pct"]:.1f}%"></div></div>'
            f'<div class="cl-count">{ch["count"]:,} · {ch["pct"]:.0f}%</div>'
            f'<div>{_pill(ch["att"])}</div></div>')
    return "".join(rows)


def _num2(v) -> str:
    """A 1-5 score to 2dp, or a muted dash when no poll ran."""
    if not isinstance(v, (int, float)):
        return '<span style="color:#9aa3b2">&mdash;</span>'
    return f"{v:.2f}"


def _signed(v) -> str:
    """NPS, always signed: -20 and +20 are opposite findings and a bare '20'
    hides which one you are looking at."""
    if not isinstance(v, (int, float)):
        return '<span style="color:#9aa3b2">&mdash;</span>'
    colour = "#1f8b4c" if v >= 50 else ("#b07d18" if v >= 0 else "#c0392b")
    return f'<span style="color:{colour};font-weight:600">{v:+.0f}</span>'


def _mentor(s: dict) -> str:
    """Who taught it, from L2's Mentor column.

    Blank when L2 did not record one - which is most older sessions, since the
    column only appears on the recent monthly tabs. An em dash would read as
    "nobody taught this"; empty reads as "not recorded", which is the truth.
    """
    who = (s.get("mentor") or "").strip()
    if not who:
        return '<span style="color:#c8cdd6">-</span>'
    return f'<span style="white-space:nowrap">{html.escape(who)}</span>'


def _rating(s: dict) -> str:
    """The session's poll score, or "no poll conducted" when none was run.

    Shown out of 5 with the response count, because 4.8 from 9 people and 4.8
    from 500 are not the same claim. Trainer / recommend ride in the tooltip so
    the column stays readable.
    """
    v = s.get("rating")
    if v is None:
        # Say it plainly. A dash reads as "missing data" and invites someone to
        # go looking for a number that was never collected.
        return ('<span style="color:#9aa3b2;font-size:11px;font-style:italic">'
                'no poll conducted</span>')
    n = s.get("rating_n") or 0
    tips = [f"session {v:.2f}"]
    if s.get("rating_trainer") is not None:
        tips.append(f"trainer {s['rating_trainer']:.2f}")
    if s.get("rating_recommend") is not None:
        tips.append(f"recommend {s['rating_recommend']:.2f}")
    if s.get("rating_nps") is not None:
        # Signed, because an NPS of -20 and one of 20 are opposite findings and
        # a bare "20" hides which one you are looking at.
        tips.append(f"NPS {s['rating_nps']:+d}")
    colour = "#1f8b4c" if v >= 4.5 else ("#b07d18" if v >= 4.0 else "#c0392b")
    return (f'<span title="{" · ".join(tips)} ({n} responses)" '
            f'style="color:{colour};font-weight:600">{v:.1f}</span>'
            f'<span style="color:#9aa3b2;font-size:11px"> /5 ({n})</span>')


def sessions_table_html(d: dict) -> str:
    """The all-sessions table — shared verbatim by the app and the site."""
    trs = []
    for s in d["sessions"]:
        if s.get("is_intro"):   # intro call: attendees + how many then joined the batch
            joined = (' <span style="font-size:11px;color:#8a5108;background:#fbeedd;'
                      'padding:1px 7px;border-radius:6px;margin-left:6px;white-space:nowrap">'
                      f'{s["total"]:,} joined this batch</span>')
            trs.append(
                f'<tr><td>{s["date_lbl"]}</td><td>{s["topic"]}{joined}</td>'
                f'<td class="num">{s["present"]:,}</td><td class="num">—</td>'
                f'<td class="num">—</td><td>—</td><td class="num">—</td></tr>')
            continue
        absent = "—" if s["present_only"] else f'{s["absent"]:,}'
        flag = '<span class="flag">no absent logged</span>' if s["present_only"] else ""
        miss = '<span class="flag">no L2 match</span>' if s["no_l2"] else ""
        # The same topic runs across many batches, so show L2's own batch wording
        # ("AI CAP B35 - Techies") to tell two identically-named sessions apart.
        lbl = (s.get("pod") or s.get("l2_batch") or "").strip()
        who = (f'<span style="font-size:11px;color:#3d5a80;background:#eaf1f8;'
               f'padding:1px 7px;border-radius:6px;margin-right:6px;'
               f'white-space:nowrap">{lbl}</span>') if lbl else ""
        trs.append(
            f'<tr><td>{s["date_lbl"]}</td><td>{who}{s["topic"]}{flag}{miss}</td>'
            f'<td class="num">{s["present"]:,}</td><td class="num">{absent}</td>'
            f'<td class="num">{_pill(s["pct"])}</td>'
            f'<td>{_mentor(s)}</td>'
            f'<td class="num">{_rating(s)}</td>'
            f'<td class="num">{_num2(s.get("rating_trainer"))}</td>'
            f'<td class="num">{_signed(s.get("rating_nps"))}</td></tr>')
    return ('<table class="sess"><thead><tr><th>Date</th><th>Session</th>'
            '<th class="num">Present</th><th class="num">Absent</th>'
            '<th class="num">% of strength</th>'
            '<th>Trainer</th>'
            '<th class="num">Rating</th>'
            '<th class="num">Trainer &#9733;</th>'
            '<th class="num">NPS</th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table>")


def sessions_subtitle(d: dict) -> str:
    """Row count, plus how many session columns L2 does not register.

    Under `data.REQUIRE_L2` those are filtered out before they get here, so the
    honest thing to report is that they are HIDDEN - otherwise the page silently
    shows a shorter list than the workbook contains. `no_l2` rows are still
    counted for any caller that keeps them (BSIAI never has one)."""
    n_hidden = int(d.get("hidden_no_l2") or 0)
    n_missing = sum(1 for s in d["sessions"] if s.get("no_l2"))
    sub = f' <span class="dh-sub">· {len(d["sessions"])} rows'
    if n_hidden:
        sub += (f' · {n_hidden} session column(s) hidden - not in the L2 schedule')
    elif n_missing:
        sub += f' · {n_missing} without an L2 topic match'
    return sub + "</span>"


def render(DATA: dict, summary: dict, source_note: str = "", *,
           key: str = "aicap_batch", show_closing: bool = True,
           closing_title: str = "Closing types",
           closing_sub: str = ("bar = share of batch · pill = that "
                               "channel's avg attendance")) -> None:
    """Draw one programme's dashboard.

    `key` must differ per programme - two segmented_controls sharing a key is a
    Streamlit duplicate-key error. `show_closing=False` drops the closing-type
    panel for programmes whose roster has no Close Type column (BSIAI)."""
    import streamlit as st
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
                               default=codes[0], key=key)
    if sel is None:
        sel = codes[0]
    d = DATA[sel]

    # ── POD filter (B35+ run domain PODs; earlier batches have none) ──
    plist = pod_names(d)
    pod_sel = None
    if plist:
        pinfo = d.get("pods") or {}
        n_whole = sum(1 for s in d["sessions"] if not s.get("pod") and s.get("mm"))
        whole_lbl = f"All Domains ({n_whole})" if n_whole else None
        opts = (["All PODs"] + ([whole_lbl] if whole_lbl else [])
                + [f"{p} ({pinfo[p]['strength']:,})" for p in plist])
        picked = st.segmented_control("POD", opts, default=opts[0], key=f"{key}_pod")
        if picked and picked != "All PODs":
            if whole_lbl and picked == whole_lbl:
                # not a POD - the sessions the whole batch was invited to
                pod_sel = _pods.WHOLE_BATCH
            else:
                pod_sel = plist[opts.index(picked) - (2 if whole_lbl else 1)]
        if d.get("pod_guessed"):
            st.caption(f"{d['pod_guessed']} student(s) list more than one POD; the "
                       "last one in the cell was used.")
    d = pod_view(d, pod_sel)

    if not d["sessions"]:
        st.info(f"No sessions logged for {pod_sel} yet."
                if pod_sel else "No sessions logged for this batch yet.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    scope = f" · {pod_sel}" if pod_sel else ""
    st.markdown(
        f'<div class="panel-title" style="font-size:18px">Batch {sel}{scope} '
        f'<span class="dh-sub">· {d["n_sessions"]} sessions logged · '
        f'strength {d["strength"]:,}</span></div>', unsafe_allow_html=True)

    # ── metric cards ── (intro-call rows excluded: they measure a different thing)
    real_sess = [s for s in d["sessions"] if not s.get("is_intro")] or d["sessions"]
    peak_s = max(real_sess, key=lambda s: s["pct"])
    low_s = min(real_sess, key=lambda s: s["pct"])
    c = st.columns(4)
    c[0].metric("Total strength", f"{d['strength']:,}",
                "in this POD" if pod_sel else "all enrolled", delta_color="off")
    c[1].metric("Active", f"{d['active']:,}", "excl. refund / unidentified", delta_color="off")
    c[2].metric("Avg attendance", f"{d['avg_pct']:.1f}%",
                "of POD strength" if pod_sel else "of batch strength, per week",
                delta_color="off")
    c[3].metric("Peak → lowest", f"{d['peak']:.0f}% → {d['low']:.0f}%",
                f"{peak_s['date_lbl']} → {low_s['date_lbl']}", delta_color="off")

    # ── attendance by date ──
    st.markdown('<div class="panel-title">Attendance by date</div>', unsafe_allow_html=True)
    st.plotly_chart(_date_line(d), width="stretch", config={"displayModeBar": False})

    if show_closing:
        # ── closing types ──
        st.markdown(f'<div class="panel-title">{closing_title} '
                    f'<span class="dh-sub">· {closing_sub}</span></div>',
                    unsafe_allow_html=True)
        st.markdown(closing_rows_html(d), unsafe_allow_html=True)
    # ── sessions table ──
    st.markdown(f'<div class="panel-title">All sessions{sessions_subtitle(d)}</div>',
                unsafe_allow_html=True)
    st.markdown(sessions_table_html(d), unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
