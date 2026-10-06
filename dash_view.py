"""
dash_view.py — Streamlit + Plotly rendering of the AI CAP attendance dashboard.

Top of the tab: the LAST WEEKEND — the newest Sat/Sun in the store — with a
per-batch bar, the sessions that ran furthest above and below expectation, and
the eight-week attendance line. Below it the all-time view: 4 KPIs → cross-batch
comparison bar (clickable) + batch selector → selected-batch drill-down (metric
cards, attendance-by-date line, sessions table, closing-types panel, domain
matrix). All numbers come from data.build() / recap.py; this file is
presentation only, and every colour comes from ui_theme.

The figure builders and the HTML fragments (_comparison_bar, _date_line,
closing_rows_html, sessions_table_html, CSS) are PURE — no Streamlit — because
site_build.py reuses them to render the identical front end as a static
website. Change the look here and both the app and the site follow.
"""
from __future__ import annotations
import datetime as _dt
import html
import plotly.graph_objects as go

import data as D
import polls as _polls          # pure; the NPS rule must live in one place only
import pods as _pods            # for the WHOLE_BATCH sentinel below
import recap as _recap          # the week's rollup rules, shared with the pipeline
import dashboard_core as _dc    # batch_key — the one batch ordering
import ui_theme as T

# status bands (pills / closing panel) — defined once, in ui_theme
_BAND = T.STATUS
_ACCENT = T.BRAND["accent"]
_NEUTRAL = T.BRAND["neutral"]
_INK2 = T.INK["secondary"]
# How many tick labels a categorical x axis may carry before they are thinned.
# A 700px plot fits ~16 labels at the ~42px "16 May" measures at size 11, and
# these render at size 10; 14 leaves real slack, keeps every label of a batch
# that has run a full quarter (13 weekends), and caps B17's 36 dates at 12.
_TICK_BUDGET = 14

# The static site scopes the same rules under its own wrapper; the app injects
# them once, from ui_theme.css(), under `.dash`.
_CSS = "<style>" + T.dash_rules(".aicap ") + "</style>"

# What the drill-down heading calls the two non-POD scopes. `pod_view` still
# takes the sentinels; only the words on screen change.
_SCOPE_LABEL = {_pods.WHOLE_BATCH: "All Domains", _pods.COMMON: "Common room"}


def _pill(pct: float) -> str:
    b = _BAND[D.band(pct)]
    return (f'<span class="pill" style="background:{b["bg"]};color:{b["fg"]}">'
            f'{pct:.0f}%</span>')


def _band_lines(fig, ymax: float) -> None:
    """The 45 / 30 attendance thresholds (data.BAND_HIGH / BAND_MID) as dotted
    reference lines, drawn only where the fitted axis can show them."""
    for y in (D.BAND_HIGH, D.BAND_MID):
        if y < ymax:
            fig.add_hline(y=y, line_width=1, line_dash="dot", line_color=T.INK["axis"],
                          annotation_text=f"{y}%", annotation_position="top left",
                          annotation_font=dict(size=10, color=T.INK["muted"]))


def _comparison_bar(DATA: dict, sel: str | None = None):
    """Average attendance per batch. One series colour; the selected batch in
    the accent; a value label only on the selected, best and worst bars;
    x labels horizontal (ECAP codes wrap instead of rotating)."""
    codes = list(DATA)
    pcts = [DATA[c]["avg_pct"] for c in codes]
    best = max(range(len(pcts)), key=pcts.__getitem__) if pcts else None
    worst = min(range(len(pcts)), key=pcts.__getitem__) if pcts else None
    sel_i = codes.index(sel) if sel in codes else None
    text = [f"{p:.0f}%" if i in (best, worst, sel_i) else "" for i, p in enumerate(pcts)]
    ymax = max(pcts) * 1.15 if pcts else 10
    fig = go.Figure(go.Bar(
        x=codes, y=pcts,
        marker=dict(color=[_ACCENT if c == sel else _NEUTRAL for c in codes],
                    line_width=0, cornerradius=4),
        text=text, textposition="outside", cliponaxis=False, constraintext="none",
        textfont=dict(size=11, color=_INK2),
        selected=dict(marker=dict(opacity=1)), unselected=dict(marker=dict(opacity=1)),
        hovertemplate="<b>%{x}</b><br>%{y:.1f}% avg attendance<extra></extra>",
    ))
    fig.update_layout(T.plotly_layout(
        300, bargap=0.35,
        xaxis=dict(tickmode="array", tickvals=codes,
                   ticktext=[T.ecap_tick(c) for c in codes],
                   # pinned horizontal: Plotly would otherwise rotate the
                   # ticks to 30° / 90° as the plot narrows (ruling D).
                   tickfont=dict(size=10), tickangle=0),
        yaxis=dict(range=[0, ymax], ticksuffix="%"),
    ))
    _band_lines(fig, ymax)
    return fig


def _date_line(d: dict):
    """Attendance by date for one batch (or one domain of it)."""
    # intro-call rows carry no percentage — the % line shows class sessions only
    sess = [s for s in d["sessions"] if not s.get("is_intro")]
    ymax = (max(s["pct"] for s in sess) * 1.15) if sess else 10
    # One tick per DISTINCT date, thinned to at most _TICK_BUDGET of them. 21 of
    # the 29 batches feed this axis 17-36 date labels, and left to itself Plotly
    # walks its autotickangles [0, 30, 90] and stands every label on its end
    # once they stop fitting - the rotation ruling D bans and the two bar charts
    # already pin against. Thinning the AXIS loses nothing: every point keeps
    # its hover, and the sessions table below is this chart's table-view twin.
    uniq = list(dict.fromkeys(s["date_lbl"] for s in sess))
    stride = max(1, -(-len(uniq) // _TICK_BUDGET))
    # Walk back from the NEWEST date. `uniq[::stride]` anchors at index 0 and
    # so drops the LAST label unless (len(uniq)-1) % stride == 0 - which left
    # 13 of the 29 batches with their newest point unlabelled (B17/B19/B24/B26
    # a full week short of the 27 Sep they end on). The right-hand end is the
    # end a time series is read from. The first is kept too, so neither end
    # goes bare; tickmode="array" renders only what is listed here.
    keep = set(uniq[::-1][::stride]) | {uniq[0]}
    ticks = [u for u in uniq if u in keep]
    cust = [[s["topic"], s["present"], s["total"],
             "(no absent logged)" if s["present_only"] else ""] for s in sess]
    fig = go.Figure(go.Scatter(
        x=[s["date_lbl"] for s in sess], y=[s["pct"] for s in sess],
        mode="lines+markers", line=dict(color=_ACCENT, width=2, shape="linear"),
        fill="tozeroy", fillcolor=T.BRAND["accent_soft"],
        marker=dict(size=8, color=_ACCENT, line=dict(color=T.INK["surface"], width=2)),
        customdata=cust,
        hovertemplate=("<b>%{x}</b><br>%{customdata[0]}<br>"
                       "Present: %{customdata[1]:,} / %{customdata[2]:,}<br>"
                       "%{y:.1f}% of strength<br>%{customdata[3]}<extra></extra>"),
    ))
    fig.update_layout(T.plotly_layout(
        340, yaxis=dict(range=[0, ymax], ticksuffix="%"),
        xaxis=dict(tickmode="array", tickvals=ticks,
                   tickangle=0, tickfont=dict(size=10)),
    ))
    _band_lines(fig, ymax)
    return fig


def _weekend_bar(per_batch: list):
    """Last weekend's present % of strength per batch — one colour, the best
    and worst bars labelled, x labels horizontal. `per_batch` is
    [(code, pct), ...]."""
    codes = [c for c, _ in per_batch]
    pcts = [p for _, p in per_batch]
    best = max(range(len(pcts)), key=pcts.__getitem__) if pcts else None
    worst = min(range(len(pcts)), key=pcts.__getitem__) if pcts else None
    text = [f"{p:.0f}%" if i in (best, worst) else "" for i, p in enumerate(pcts)]
    ymax = max(pcts) * 1.15 if pcts else 10
    fig = go.Figure(go.Bar(
        x=codes, y=pcts,
        marker=dict(color=_ACCENT, line_width=0, cornerradius=4),
        text=text, textposition="outside", cliponaxis=False, constraintext="none",
        textfont=dict(size=11, color=_INK2),
        hovertemplate="<b>%{x}</b><br>%{y:.1f}% of strength<extra></extra>",
    ))
    fig.update_layout(T.plotly_layout(
        240, bargap=0.35, margin=dict(t=18),
        xaxis=dict(tickmode="array", tickvals=codes,
                   ticktext=[T.ecap_tick(c) for c in codes],
                   # pinned horizontal: Plotly would otherwise rotate the
                   # ticks to 30° / 90° as the plot narrows (ruling D).
                   tickfont=dict(size=10), tickangle=0),
        yaxis=dict(range=[0, ymax], ticksuffix="%"),
    ))
    return fig


def _weeks_line(weeks: list):
    """Weekly attendance across every batch, the last N weeks (recap.weeks)."""
    pts = [(w["week"], w["pct"]) for w in weeks if w.get("pct") is not None]
    labels = [f"{_dt.date.fromisoformat(w):%d %b}".lstrip("0") for w, _ in pts]
    ys = [p for _, p in pts]
    ymax = max(ys) * 1.15 if ys else 10
    fig = go.Figure(go.Scatter(
        x=labels, y=ys, mode="lines+markers+text",
        line=dict(color=_ACCENT, width=2, shape="linear"),
        marker=dict(size=8, color=_ACCENT, line=dict(color=T.INK["surface"], width=2)),
        text=[f"{y:.1f}%" if i == len(ys) - 1 else "" for i, y in enumerate(ys)],
        textposition="top center", textfont=dict(size=11, color=_INK2),
        cliponaxis=False,
        hovertemplate="week of %{x}<br>%{y:.1f}% attendance<extra></extra>",
    ))
    fig.update_layout(T.plotly_layout(
        240, margin=dict(t=18),
        yaxis=dict(range=[0, ymax], ticksuffix="%"),
        xaxis=dict(tickfont=dict(size=10)),
    ))
    return fig


def newest_batch(DATA: dict, sessions: list | None = None) -> str | None:
    """The most recently STARTED batch with at least one dated session.

    With the store's dated rows the start is the batch's first session date;
    without them (legacy mode) the highest CAP batch number stands in, ECAP
    last, since `batch_key` offsets ECAP past every CAP batch and ECAP's
    numbers say nothing about age.
    """
    with_dates = [c for c in DATA if any(s.get("mm") for s in DATA[c].get("sessions") or ())]
    if not with_dates:
        return None
    starts: dict = {}
    for r in sessions or ():
        b, d = r.get("batch"), r.get("date")
        if b and d and (b not in starts or d < starts[b]):
            starts[b] = d
    if starts:
        return max(with_dates, key=lambda c: (starts.get(c, ""), _dc.batch_key(c)))
    return max(with_dates, key=lambda c: (not c.upper().startswith("ECAP"), _dc.batch_key(c)))


def batch_order(codes) -> list:
    """Batches newest first: CAP by number, then ECAP by number."""
    return sorted(codes, key=lambda c: (c.upper().startswith("ECAP"), -_dc.batch_key(c)))


def weekend_rows(sessions: list, sat: _dt.date, sun: _dt.date) -> list:
    """The store's session rows that fall on that weekend."""
    a, b = sat.isoformat(), sun.isoformat()
    return [s for s in sessions or () if a <= (s.get("date") or "") <= b]


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
        # B41+ (data.paired): one row per WEEKEND, each person counted once
        # over Saturday and Sunday - the same session runs on both days.
        rows = d.get("weekends") or d.get("by_date")
        if not rows:
            # A batch with no date rollup: return it untouched rather than an
            # empty list, which rendered "No sessions logged for None yet".
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
            same = [x for mm in (r.get("days") or [r["mm"]])
                    for x in by_mm.get(mm, [])]
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
                # A weekend row names every trainer it covers (Common and
                # Techies are taught by different people).
                "mentor": (", ".join(dict.fromkeys(
                               x["mentor"].strip() for x in same
                               if (x.get("mentor") or "").strip()))
                           if r.get("days") else
                           next((x.get("mentor") for x in same
                                 if (x.get("mentor") or "").strip()), "")),
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
                "topic": r.get("topic") or (f"{n} domain sessions" if n > 1
                          else (same[0]["topic"] if same else "Session")),
                "pod": "", "l2_batch": "" if n > 1 else (same[0].get("l2_batch", "")
                                                         if same else ""),
                # A shared room is still a shared room when it is the day's only
                # session; the rollup of several rooms carries no chip.
                "shared_batches": ((same[0].get("shared_batches") or [])
                                   if (n == 1 and same) else []),
                # ... and the poll's provenance travels with it, or the
                # "room poll, shared with ..." note can never fire in the view
                # the tab OPENS on - which for B17-B34 is the ONLY view there
                # is, since they have no domains and so no domain selector.
                # Empty when n > 1: a rollup of several domain rooms is a
                # weighted mean, and calling that "the room's own poll" would
                # be a different wrong claim.
                "rating_shared": ((same[0].get("rating_shared") or {})
                                  if (n == 1 and same) else {}),
                "present": r["present"], "total": r["total"],
                "absent": max(0, r["total"] - r["present"]),
                "pct": r["pct"], "present_only": False, "no_l2": False,
                "is_intro": False, "n_pods": n,
            })
        # intro rows live on d["sessions"] and have no date bucket - keep them
        sess = [s for s in d["sessions"] if s.get("is_intro")] + sess
        return dict(d, sessions=sess)

    if pod == _pods.COMMON:
        # The complement room: everyone the day's POD rooms did not invite. It
        # is NOT a roster POD - nobody's POD cell says "Common" - so it cannot
        # come from d["pods"], and its strength is carried on the sessions
        # themselves, which already divided by the people actually invited.
        sess = [s for s in d["sessions"] if s.get("pod") == _pods.COMMON]
        if not sess:
            return dict(d, sessions=[], n_sessions=0, avg_pct=0.0, peak=0.0, low=0.0)
        pcts = [s["pct"] for s in sess]
        return dict(d, sessions=sess, n_sessions=len(sess),
                    strength=sess[0]["total"], active=sess[0]["total"],
                    avg_pct=round(sum(pcts) / len(pcts), 1),
                    peak=max(pcts), low=min(pcts))

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

    # A domain's own rooms, PLUS its slice of any complement room it sat in.
    # On the first two weekends a batch runs only Techies + everybody else, so
    # Finance never gets a room of its own and this view used to be empty for
    # those dates - the batch looked like it had no Finance sessions until week
    # three. The roster knows who is Finance, so the complement room's Finance
    # share is recoverable: `pod_split`, computed where the marks are read.
    sess = [s for s in d["sessions"] if s.get("pod") == pod]
    for s in d["sessions"]:
        if s.get("pod") == pod:
            continue
        part = (s.get("pod_split") or {}).get(pod)
        if part:
            sess.append(dict(s, pod=pod, present=part["present"],
                             total=part["total"], pct=part["pct"],
                             absent=part["total"] - part["present"],
                             within_common=True))
        elif (not s.get("pod_split") and pod in _pods.members(s.get("pod"))
              and pod in (d.get("pods") or {})):
            # A compound room ('Sales/Marketing/HR + Content Creators') this
            # POD was invited to, with NO breakdown to take a share from.
            # data._real_split drops a lone bucket covering the whole invited
            # population - which is what the split collapses to when the
            # OTHER member has nobody in this batch. The room's denominator
            # is then already this POD's strength, so the row is this POD's
            # whole session and is shown as such rather than not at all.
            sess.append(dict(s, pod=pod))
    sess.sort(key=lambda x: (x.get("mm") or "", x.get("col") or 0))
    if not sess:
        return dict(d, sessions=[], n_sessions=0, avg_pct=0.0, peak=0.0, low=0.0)
    pcts = [s["pct"] for s in sess]
    info = (d.get("pods") or {}).get(pod, {})
    return dict(d, sessions=sess, n_sessions=len(sess),
                avg_pct=round(sum(pcts) / len(pcts), 1),
                peak=max(pcts), low=min(pcts),
                strength=info.get("strength", d["strength"]),
                active=info.get("active", d["active"]))


def closing_title(pod_sel: str | None) -> tuple[str, str]:
    """The closing-types panel's heading and subtitle for the selected view.

    `closing` is built once per BATCH (data.build_batch) and `pod_view` carries
    it through untouched, so in a domain or Common view this panel is still the
    whole batch's. The panel is KEPT there — how a cohort was closed is a batch
    fact and worth reading beside any view of it — but it has to say so: a
    "share of batch" bar sitting under a heading that reads "Finance" is read
    as Finance's closing mix, which it is not.
    """
    if pod_sel:
        return ("Closing types · whole batch",
                "bar = share of the whole batch · pill = that channel's avg "
                "attendance across the batch, not the view selected above")
    return ("Closing types",
            "bar = share of batch · pill = that channel's avg attendance")


def closing_rows_html(d: dict) -> str:
    """The closing-types panel rows — shared verbatim by the app and the site.

    The 'Unknown' bucket (a blank Close Type cell) is shown LAST and called
    'Not recorded': it is the absence of a channel, not a channel, and it was
    sitting at the top of B40's panel at 57% as if it were the biggest one.
    """
    rows = []
    ordered = sorted(d["closing"], key=lambda ch: ch["type"] == "Unknown")
    for ch in ordered:
        name = "Not recorded" if ch["type"] == "Unknown" else html.escape(str(ch["type"]))
        rows.append(
            f'<div class="cl-row"><div class="cl-name">{name}</div>'
            f'<div class="cl-track"><div class="cl-fill" style="width:{ch["pct"]:.1f}%"></div></div>'
            f'<div class="cl-count">{ch["count"]:,} · {ch["pct"]:.0f}%</div>'
            f'<div>{_pill(ch["att"])}</div></div>')
    return "".join(rows)


def _num2(v) -> str:
    """A 1-5 score to 2dp, or a muted dash when no poll ran."""
    if not isinstance(v, (int, float)):
        return '<span class="muted">&mdash;</span>'
    return f"{v:.2f}"


def _signed(v) -> str:
    """NPS, always signed: -20 and +20 are opposite findings and a bare '20'
    hides which one you are looking at."""
    if not isinstance(v, (int, float)):
        return '<span class="muted">&mdash;</span>'
    return f'<span style="font-weight:600">{v:+.0f}</span>'


def _mentor(s: dict) -> str:
    """Who taught it, from L2's Mentor column.

    Blank when L2 did not record one - which is most older sessions, since the
    column only appears on the recent monthly tabs. An em dash would read as
    "nobody taught this"; empty reads as "not recorded", which is the truth.
    """
    who = (s.get("mentor") or "").strip()
    if not who:
        return '<span class="muted">-</span>'
    return f'<span style="white-space:nowrap">{html.escape(who)}</span>'


def _room_poll_note(s: dict, own: str = "") -> str:
    """"room poll, shared with B36, B37" — when the figure beside it is NOT
    this batch's own.

    A shared room's poll is normally divided per batch (pipeline `[5a.1]`), but
    an anonymous export names nobody and cannot be divided, so every sharing
    batch shows the SAME room-wide number. Unlabelled, that reads as "B35's
    students rated it 4.57" when what happened is "the room did, and we cannot
    say which batch". `own` is the batch whose table this is, so it is left out
    of the list of who else was in the room.
    """
    sh = s.get("rating_shared") or {}
    if not sh or sh.get("split"):
        return ""
    others = [b for b in (s.get("shared_batches") or []) if b and b != own]
    if not others:
        return ""
    return ('<div class="muted" style="font-size:11px;white-space:nowrap">'
            '· room poll, shared with ' + html.escape(", ".join(others))
            + "</div>")


def _rating(s: dict, own: str = "") -> str:
    """The session's poll score, or "no poll conducted" when none was run.

    Shown out of 5 with the response count, because 4.8 from 9 people and 4.8
    from 500 are not the same claim. Trainer / recommend ride in the tooltip so
    the column stays readable. A figure that is the whole room's rather than
    this batch's own says so underneath (`_room_poll_note`).
    """
    v = s.get("rating")
    if v is None:
        # Say it plainly. A dash reads as "missing data" and invites someone to
        # go looking for a number that was never collected.
        return ('<span class="muted" style="font-size:11px;font-style:italic">'
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
    return (f'<span title="{" · ".join(tips)} ({n} responses)" '
            f'style="font-weight:600">{v:.1f}</span>'
            f'<span class="muted" style="font-size:11px"> /5 ({n})</span>'
            + _room_poll_note(s, own))


# Below this many answers a domain's rating is not shown. A 5-response 5.00
# moves a full point on one more answer; the count is shown instead, so the
# thin sample is visible rather than quietly missing.
_MIN_RATING_N = 10


def domain_matrix_html(d: dict) -> str:
    """Per-domain attendance inside the sessions that mixed several domains.

    A batch's first two or three sessions are All Domains, and its early
    weekends put everyone except Techies in one room. L2 calls each of those a
    single session, so the table above shows one number for eleven very
    different turnouts - B39's 5 Sep averaged 61.2% while Content Creators came
    in at 69.7% and Students at 50.8%. The roster knows who belongs to which
    domain, so the split is recoverable; `data.build_batch` computes it and this
    lays it out as domain x date.

    Where the room's feedback poll named its respondents, each cell also
    carries that domain's own rating - so "Finance rated it 4.62" sits on the
    same row as "Finance attended 55%". A rating from fewer than
    `_MIN_RATING_N` answers is NOT shown: Content Creators returning 5.00 off
    five responses is noise presented as a perfect score, and one more answer
    moves it a full point. The response count is shown instead, so a thin
    sample is visible rather than silently absent.

    Returns "" when nothing in view carries a split - a single POD's own room
    has one domain by construction, and B17-B34 have no domains at all.
    """
    cols = [s for s in d["sessions"] if s.get("pod_split")]
    if not cols:
        return ""
    names = sorted({p for s in cols for p in s["pod_split"]})
    if len(names) < 2:
        # One bucket is not a breakdown. B17-B34 have no POD column at all, so
        # every student falls into the same "Unassigned" bin and the table would
        # restate the session's own percentage under a heading promising more.
        return ""
    head = "".join(f'<th class="num">{s["date_lbl"]}</th>' for s in cols)
    trs = []
    for p in names:
        tds = []
        for s in cols:
            part = s["pod_split"].get(p)
            if not part:
                tds.append('<td class="num">—</td>')
                continue
            rt = (s.get("pod_ratings") or {}).get(p) or {}
            n = rt.get("responses") or 0
            if rt.get("session") is not None and n >= _MIN_RATING_N:
                extra = (f'<div class="muted" style="font-size:11px">'
                         f'★ {rt["session"]:.2f} · n={n}</div>')
            elif n:
                extra = f'<div class="muted" style="font-size:11px">n={n}</div>'
            else:
                extra = ""
            tds.append(f'<td class="num">{_pill(part["pct"])}'
                       f'<div class="muted" style="font-size:11px">'
                       f'{part["present"]:,}/{part["total"]:,}</div>'
                       f'{extra}</td>')
        trs.append(f'<tr><td>{html.escape(p)}</td>{"".join(tds)}</tr>')
    return ('<table class="sess"><thead><tr><th>Domain</th>' + head +
            "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>")


def sessions_table_html(d: dict) -> str:
    """The all-sessions table — shared verbatim by the app and the site."""
    # Which batch's table this is, so a room-wide poll can name the OTHER
    # batches that sat in it. `code` is set by data.build_batch; a caller that
    # predates it simply gets the full list.
    own = str(d.get("code") or "")
    trs = []
    for s in d["sessions"]:
        if s.get("is_intro"):   # intro call: attendees + how many then joined the batch
            joined = (' <span class="flag">'
                      f'{s["total"]:,} joined this batch</span>')
            trs.append(
                f'<tr><td>{s["date_lbl"]}</td><td class="topic">{html.escape(s["topic"])}{joined}</td>'
                f'<td class="num">{s["present"]:,}</td><td class="num">—</td>'
                f'<td class="num">—</td><td>—</td><td class="num">—</td>'
                f'<td class="num">—</td><td class="num">—</td></tr>')
            continue
        absent = "—" if s["present_only"] else f'{s["absent"]:,}'
        flag = '<span class="flag">no absent logged</span>' if s["present_only"] else ""
        miss = '<span class="flag">no schedule match</span>' if s["no_l2"] else ""
        # A chip only for a SHARED room, and then L2's own wording of who was in
        # it ("AI CAP B35 , B36 , B37 - Techies") - so two identically-named
        # sessions can be told apart. A room this batch had to itself shows the
        # topic alone; the heading above the table already names the batch.
        shared = s.get("shared_batches") or []
        lbl = (s.get("l2_batch") or ", ".join(shared)).strip() if shared else ""
        who = f'<span class="chip">{html.escape(lbl)}</span>' if lbl else ""
        trs.append(
            f'<tr><td>{s["date_lbl"]}</td>'
            f'<td class="topic">{who}{html.escape(str(s["topic"]))}{flag}{miss}</td>'
            f'<td class="num">{s["present"]:,}</td><td class="num">{absent}</td>'
            f'<td class="num">{_pill(s["pct"])}</td>'
            f'<td>{_mentor(s)}</td>'
            f'<td class="num">{_rating(s, own)}</td>'
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


def _weekday(mm: str) -> str:
    """'10_03' -> 'Saturday'. The key carries no year: take the latest year
    that does not put the date more than a month in the future."""
    today = _dt.date.today()
    mo, dd = (int(x) for x in mm.split("_"))
    for y in (today.year, today.year - 1):
        try:
            day = _dt.date(y, mo, dd)
        except ValueError:
            continue
        if day <= today + _dt.timedelta(days=31):
            return day.strftime("%A")
    return ""


def weekend_table_html(w: dict) -> str:
    """One weekend of a B41+ batch: each room per day, then each room and the
    whole batch counted ONCE over both days (present in either room)."""
    def _row(label, p, t, strong=False):
        pct = f"{p / t * 100:.1f}%" if t else "—"
        st_ = (' style="font-weight:700;background:var(--brand-accent-soft)"'
               if strong else "")
        return (f'<tr{st_}><td>{html.escape(label)}</td>'
                f'<td class="num">{p:,}</td><td class="num">{t:,}</td>'
                f'<td class="num">{pct}</td></tr>')
    trs = []
    # A batch with one room (BSIAI: no Techies split) needs no room name on
    # its rows, and its per-room "unique" line would just repeat the overall.
    one = len(w["rooms"]) == 1
    for i, mm in enumerate(w["days"]):
        for rm in w["rooms"]:
            dd = rm["days"][i]
            who = "" if one else f'{rm["room"]} '
            trs.append(_row(f'{who}{_weekday(mm)} ({dd["date_lbl"]})',
                            dd["present"], dd["total"]))
    both = "both days" if len(w["days"]) > 1 else "counted once"
    if not one:
        for rm in w["rooms"]:
            trs.append(_row(f'Unique {rm["room"]} {both}', rm["present"], rm["total"], True))
    trs.append(_row(f"Unique overall {both}" if one else "Unique overall",
                    w["present"], w["total"], True))
    return ('<table class="sess"><thead><tr><th></th><th class="num">Attended</th>'
            '<th class="num">Enrolled</th><th class="num">% of enrolled</th>'
            '</tr></thead><tbody>' + "".join(trs) + "</tbody></table>")


def weekend_pods_html(w: dict) -> str:
    """The same weekend by roster POD, each person counted once."""
    if not w.get("pods"):
        return ""
    trs = [f'<tr><td>{html.escape(p)}</td><td class="num">{v["present"]:,}</td>'
           f'<td class="num">{v["total"]:,}</td><td class="num">{_pill(v["pct"])}</td></tr>'
           for p, v in w["pods"].items()]
    trs.append(f'<tr style="font-weight:700;background:var(--brand-accent-soft)">'
               f'<td>Total</td><td class="num">{w["present"]:,}</td>'
               f'<td class="num">{w["total"]:,}</td><td class="num">{w["pct"]:.1f}%</td></tr>')
    return ('<table class="sess"><thead><tr><th>Domain</th><th class="num">Attended</th>'
            '<th class="num">Enrolled</th><th class="num">% of enrolled</th>'
            '</tr></thead><tbody>' + "".join(trs) + "</tbody></table>")


def sessions_subtitle(d: dict) -> str:
    """Row count, plus how many session columns L2 does not register.

    Under `data.REQUIRE_L2` those are filtered out before they get here, so the
    honest thing to report is that they are HIDDEN - otherwise the page silently
    shows a shorter list than the workbook contains. `no_l2` rows are still
    counted for any caller that keeps them."""
    n_hidden = int(d.get("hidden_no_l2") or 0)
    n_missing = sum(1 for s in d["sessions"] if s.get("no_l2"))
    sub = f' <span class="dh-sub">· {len(d["sessions"])} rows'
    if n_hidden:
        sub += (f' · {n_hidden} session column(s) hidden - not in the schedule')
    elif n_missing:
        sub += f' · {n_missing} without a schedule topic match'
    return sub + "</span>"


# ── Streamlit-side helpers ────────────────────────────────────────────────────
def _dash(st, fragment: str) -> None:
    """Emit a dashboard HTML fragment under the `.dash` scope."""
    st.markdown(f'<div class="dash">{fragment}</div>', unsafe_allow_html=True)


def _title(st, text: str, sub: str = "", size: int = 14) -> None:
    _dash(st, f'<div class="panel-title" style="font-size:{size}px">{text}'
              + (f' <span class="dh-sub">· {sub}</span>' if sub else "") + "</div>")


def _mini_list(items: list) -> str:
    """Rows of (who, what, value) as a compact list."""
    lis = []
    for who, what, val in items:
        lis.append(f'<li><span><span class="who">{html.escape(who)}</span> '
                   f'{html.escape(what)}</span><span class="val">{html.escape(val)}</span></li>')
    return '<ul class="mini-list">' + "".join(lis) + "</ul>"


def _last_weekend(st, store: dict, key_prefix: str = "aicap") -> bool:
    """The newest Sat/Sun in the store, in four small pieces. Every figure is
    one the Sessions / Recap tabs already show: per-batch attendance is
    `recap._agg` over that batch's weekend rows (the same pooled present ÷
    invited the Browse tab's Attendance tile uses), the best / worst lists
    read the per-session `index` the pipeline stored, and the weekly line is
    `recap.weeks` — the Week-by-week table's Attendance % column."""
    sessions = store.get("sessions") or []
    wk = T.latest_weekend(s.get("date") for s in sessions)
    if not wk:
        return False
    sat, sun = wk
    rows = weekend_rows(sessions, sat, sun)
    if not rows:
        return False
    agg = _recap._agg(rows)
    label = T.weekend_label(sat, sun)
    st.markdown(
        f'<div class="section-title">Last weekend · {label} · '
        f'{agg["sessions"]} sessions · {len(agg["batches"])} batches</div>'
        '<div class="section-sub">Present % of strength per batch, the sessions '
        'furthest above and below what the decay curve expected, and the weekly '
        'attendance across every batch.</div>', unsafe_allow_html=True)

    per_batch = []
    for b in sorted(agg["batches"], key=_dc.batch_key):
        pct = _recap._agg([r for r in rows if r.get("batch") == b])["pct"]
        if pct is not None:
            per_batch.append((b, pct))
    _title(st, "Attendance by batch", "this weekend, present % of strength")
    st.plotly_chart(_weekend_bar(per_batch), width="stretch",
                    config={"displayModeBar": False}, key=f"{key_prefix}_lw_bar")

    indexed = sorted((r for r in rows if r.get("index") is not None),
                     key=lambda r: r["index"], reverse=True)

    def _row(r):
        who = r["batch"] + (f" · {r['pod']}" if r.get("pod") else "")
        return (who, (r.get("topic") or "Session")[:60], T.fmt_index(r["index"]))

    weeks = (store.get("recap") or {}).get("weeks") or []
    c1, c2, c3 = st.columns(3)
    with c1:
        _title(st, "Weekly attendance", f"all batches, last {len(weeks)} weeks")
        if weeks:
            st.plotly_chart(_weeks_line(weeks), width="stretch",
                            config={"displayModeBar": False}, key=f"{key_prefix}_lw_weeks")
        else:
            st.caption("No weekly rollup in this store yet.")
    with c2:
        _title(st, "Best vs expected", "1.00× = exactly what the curve predicts")
        if indexed:
            _dash(st, _mini_list([_row(r) for r in indexed[:3]]))
    with c3:
        _title(st, "Furthest below expected")
        if indexed:
            _dash(st, _mini_list([_row(r) for r in indexed[::-1][:3]]))
    return True


def render(DATA: dict, summary: dict, source_note: str = "",
           store: dict | None = None, rerun_scope: str = "app",
           key_prefix: str = "aicap") -> None:
    """Draw the dashboard.

    `key_prefix` names every widget and session-state key this draws
    ("aicap_batch", "aicap_bar", ...). A second programme's dashboard in the
    SAME script (the BSIAI tab) passes its own prefix, or Streamlit raises a
    duplicate-key error on the first shared widget. The default keeps every
    existing key spelling unchanged.

    `rerun_scope` is what the bar-click rerun asks Streamlit to repaint. The
    app calls this from inside an `@st.fragment`, where "fragment" repaints
    the Dashboard alone; the default stays "app" so any caller OUTSIDE a
    fragment still works — `st.rerun(scope="fragment")` raises there, and
    catching it is not an option because the rerun itself is an exception.
    """
    import streamlit as st
    store = store or {}

    # ── last weekend ──
    if _last_weekend(st, store, key_prefix):
        st.divider()
        st.markdown('<div class="section-title">All time</div>', unsafe_allow_html=True)

    # ── KPIs ──
    T.tiles([
        {"label": "Enrolled", "value": f"{summary['enrolled']:,}"},
        {"label": "Active", "value": f"{summary['active']:,}"},
        {"label": "Batches", "value": summary["batches"]},
        {"label": "Sessions logged", "value": summary["sessions"]},
    ])
    if source_note:
        st.caption(source_note)

    if not DATA:
        st.warning("No batch tabs found in the sheet.")
        return

    codes = list(DATA)
    order = batch_order(codes)
    default = newest_batch(DATA, store.get("sessions")) or order[0]
    k_batch, k_applied = f"{key_prefix}_batch", f"_{key_prefix}_bar_applied"
    if st.session_state.get(k_batch) not in codes:
        st.session_state[k_batch] = default
    sel = st.session_state[k_batch]

    # ── cross-batch comparison (click a bar to drill in) ──
    # The click is read back from the chart's selection event and applied
    # only when it is a NEW click. The chart's widget state keeps the last
    # clicked bar across reruns (and re-registers it whenever the figure
    # changes, e.g. when the accent moves), so an `on_select` callback would
    # fire again with the stale click and override a pick in the selectbox.
    _title(st, "Average attendance by batch",
           "present % of strength, all time · click a bar to drill in")
    ev = st.plotly_chart(_comparison_bar(DATA, sel), width="stretch",
                         config={"displayModeBar": False}, key=f"{key_prefix}_bar",
                         on_select="rerun", selection_mode="points")
    pts = list(((ev or {}).get("selection") or {}).get("points") or [])
    clicked = str(pts[0]["x"]) if pts and pts[0].get("x") is not None else None
    if clicked in codes and clicked != st.session_state.get(k_applied):
        st.session_state[k_applied] = clicked
        if clicked != sel:
            st.session_state[k_batch] = clicked         # the selectbox follows
            st.rerun(scope=rerun_scope)                 # and the bar re-paints

    # ── batch selector (drives drill-down; kept in step with the bar) ──
    sel = st.selectbox("Batch", order, key=k_batch)
    d = DATA[sel]

    # ── domain filter (B35+ run domain PODs; earlier batches have none) ──
    plist = pod_names(d)
    pod_sel = None
    if plist:
        pinfo = d.get("pods") or {}
        n_whole = sum(1 for s in d["sessions"] if not s.get("pod") and s.get("mm"))
        whole_lbl = f"All Domains ({n_whole})" if n_whole else None
        # B40/B41 run two parallel rooms: Techies, and everybody else. That
        # second room is a session property rather than a roster POD, so it is
        # missing from pinfo and has to be offered separately or the split is
        # invisible - picking "Generalist" just finds no sessions.
        common = [s for s in d["sessions"] if s.get("pod") == _pods.COMMON]
        common_lbl = f"Common ({common[0]['total']:,})" if common else None
        opts = (["All sessions"] + ([whole_lbl] if whole_lbl else [])
                + ([common_lbl] if common_lbl else [])
                + [f"{p} ({pinfo[p]['strength']:,})" for p in plist])
        picked = st.segmented_control("Domain", opts, default=opts[0],
                                      key=f"{key_prefix}_batch_pod")
        if picked and picked != "All sessions":
            if whole_lbl and picked == whole_lbl:
                # not a POD - the sessions the whole batch was invited to
                pod_sel = _pods.WHOLE_BATCH
            elif common_lbl and picked == common_lbl:
                pod_sel = _pods.COMMON
            else:
                pod_sel = plist[opts.index(picked)
                                - 1 - bool(whole_lbl) - bool(common_lbl)]
        if d.get("pod_guessed"):
            st.caption(f"{d['pod_guessed']} student(s) list more than one domain; the "
                       "last one in the cell was used.")
    d = pod_view(d, pod_sel)
    scope_lbl = _SCOPE_LABEL.get(pod_sel, pod_sel) if pod_sel else None

    if not d["sessions"]:
        st.info(f"No sessions logged for {scope_lbl} yet."
                if pod_sel else "No sessions logged for this batch yet.")
        return

    _title(st, f"Batch {html.escape(sel)}" + (f" · {html.escape(scope_lbl)}" if scope_lbl else ""),
           f'{d["n_sessions"]} sessions logged · strength {d["strength"]:,}', size=18)

    # ── metric cards ── (intro-call rows excluded: they measure a different thing)
    real_sess = [s for s in d["sessions"] if not s.get("is_intro")] or d["sessions"]
    peak_s = max(real_sess, key=lambda s: s["pct"])
    low_s = min(real_sess, key=lambda s: s["pct"])
    is_pod = pod_sel not in (None, _pods.WHOLE_BATCH, _pods.COMMON)
    T.tiles([
        {"label": "Total strength", "value": f"{d['strength']:,}",
         "sub": ("in this domain" if is_pod else
                 "invited to the common room" if pod_sel == _pods.COMMON else "all enrolled")},
        {"label": "Active", "value": f"{d['active']:,}",
         # data.is_active: a BLANK Payment is inactive too, and blanks are the
         # majority of what this tile leaves out — saying only "refund or
         # unidentified" made the gap between Enrolled and Active look like a
         # refund count.
         "help": "Enrolled students whose Payment is recorded and is not a "
                 "refund or unidentified. A blank Payment counts as inactive "
                 "as well: no payment recorded is not the same thing as a "
                 "refund, but it is not active either."},
        {"label": "Avg attendance", "value": f"{d['avg_pct']:.1f}%",
         "sub": "of domain strength" if is_pod else "of batch strength, per week"},
        {"label": "Peak → lowest", "value": f"{d['peak']:.0f}% → {d['low']:.0f}%",
         "sub": f"{peak_s['date_lbl']} → {low_s['date_lbl']}"},
    ])

    # ── attendance by date ── (B41+: by weekend, each person counted once)
    wk = d.get("weekends") if pod_sel is None else None
    _title(st, "Attendance by weekend" if wk else "Attendance by date",
           "Saturday and Sunday run the same session; each person counted once"
           if wk else "")
    st.plotly_chart(_date_line(d), width="stretch", config={"displayModeBar": False},
                    key=f"{key_prefix}_line")

    # ── closing types ── (whole-batch even in a domain view — `closing_title`)
    _cl_title, _cl_sub = closing_title(pod_sel)
    _title(st, _cl_title, _cl_sub)
    _dash(st, closing_rows_html(d))

    # ── sessions table ──
    _title(st, f"All sessions{sessions_subtitle(d)}")
    _dash(st, f'<div class="dash-scroll">{sessions_table_html(d)}</div>')

    # ── one weekend, day by day and counted once (B41+) ──
    if wk:
        labels = [w["date_lbl"] for w in wk][::-1]          # newest first
        pick = st.selectbox("Weekend", labels, key=f"{key_prefix}_wk_{sel}")
        w = wk[len(wk) - 1 - labels.index(pick)]
        _title(st, f"Weekend {html.escape(w['date_lbl'])}",
               html.escape(w["topic"]))
        _dash(st, f'<div class="dash-scroll">{weekend_table_html(w)}</div>')
        pods_tbl = weekend_pods_html(w)
        if pods_tbl:
            _title(st, "By domain, both days",
                   "each person counted once, present in either room")
            _dash(st, f'<div class="dash-scroll">{pods_tbl}</div>')

    # ── who actually came, inside the sessions that mixed domains ──
    matrix = domain_matrix_html(d)
    if matrix:
        _title(st, "By domain",
               "inside the All Domains, compound and shared rooms above, which "
               "the schedule records as one session each")
        _dash(st, f'<div class="dash-scroll">{matrix}</div>')
