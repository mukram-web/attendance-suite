"""The BSIAI pages — drawn inside the main app's BSIAI tab AND by the standalone
`bsiai_app.py`, from the store `bsiai_build.py` writes.

Pure presentation over an already-loaded store dict (the `meta` table of
`bsiai.duckdb`, as `attendance_app._load_store` returns it). Nothing is
fetched or computed here beyond filtering rows for display. The pages reuse
the main app's own renderers (`dash_view.render`, `ui_theme`) and read the
same section names (`sessions`, `recap`, `trainers`, `grid_<code>`), so a
BSIAI number is drawn by the same code as an AI CAP one.

Every widget key is prefixed with `key` so the pages can sit beside the AI CAP
ones in a single Streamlit script without a duplicate-key error — the Roster
page's "Batch" selectbox, for instance, would otherwise collide with the main
Roster tab's.

Wording follows the main app's plain-words rule: Domain not POD, "vs expected"
not residual, "schedule" not L2, "Whole room" not joint.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import json
import pathlib

import pandas as pd
import streamlit as st

import dash_view
import polls as _polls
import recap as _recap
import ui_theme as T

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MARK_PRESENT, _MARK_ABSENT, _MARK_BLANK = "✓", "✗", "—"

PAGES = ["📊 Dashboard", "🔎 Sessions", "🏆 This week", "🎓 Trainers",
         "📋 Roster (marked attendance)", "🧾 Build notes"]


# ───────────────────────────── loading ───────────────────────────────────────
@st.cache_data(show_spinner="Loading the BSIAI store…")
def load_store(path: str, mtime: float) -> dict:
    """The store's meta table as one dict. `mtime` keys the cache so a rebuild
    is picked up without restarting the server. Used by the standalone page;
    the main app loads through its own `_load_store` (Drive-aware)."""
    import duckdb
    con = duckdb.connect(path, read_only=True)
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
    finally:
        con.close()
    out = {k: json.loads(v) for k, v in rows}
    out["path"] = path
    return out


def _grid(path: str, batch: str):
    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
        try:
            return con.execute(f'SELECT * FROM "grid_{batch}"').df()
        finally:
            con.close()
    except Exception:                      # noqa: BLE001 — a missing grid is "nothing to show"
        return None


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


@st.cache_data(show_spinner=False)
def _roster_display(_g, batch, active_only, show_pii, stamp):
    g = _g[_g["Active"].astype(bool)] if active_only else _g
    sess_cols = [c for c in g.columns if c not in ("Email", "Phone", "Active", "Present")]
    n_students = len(g)
    n_present_any = int((g["Present"] > 0).sum())
    disp = g.copy()
    if not show_pii:
        disp["Email"] = disp["Email"].map(mask_email)
        disp["Phone"] = disp["Phone"].map(mask_phone)
    glyph = {"present": _MARK_PRESENT, "absent": _MARK_ABSENT}
    for c in sess_cols:
        disp[c] = disp[c].map(lambda v: glyph.get(str(v).strip().lower(), _MARK_BLANK))
    disp = disp[["Email", "Phone", "Active", "Present"] + sess_cols]
    return disp, sess_cols, n_students, n_present_any


def _f(v, fmt="{:.1f}", dash="—"):
    return fmt.format(v) if isinstance(v, (int, float)) else dash


def caption(store: dict) -> str:
    return (f"Built {store.get('generated_at', '?')} · {store.get('source', '')} · "
            f"Build Side Income Using AI: {len(store.get('batches') or [])} batches")


# ───────────────────────────── pages ─────────────────────────────────────────
def page_dashboard(store: dict, key: str) -> None:
    # A deploy that updates the main script in place can leave an ALREADY
    # IMPORTED dash_view in memory — the one without `key_prefix` — while this
    # module, imported for the first time, is new. Seen on Streamlit Cloud the
    # day the tab shipped: TypeError at this call, no frame inside render.
    # Re-executing the module from disk is the same thing a reboot does.
    import importlib
    import inspect
    if "key_prefix" not in inspect.signature(dash_view.render).parameters:
        importlib.reload(dash_view)
    dash_view.render(store["DATA"], store["summary"], "", store=store,
                     rerun_scope="fragment", key_prefix=key)


def page_sessions(store: dict, key: str) -> None:
    S = store.get("sessions") or []
    if not S:
        st.warning("No sessions in this store.")
        return
    st.caption("Every logged session across the seven batches. Filter, then pick one for its breakdown.")
    with st.expander("How this is calculated", expanded=False):
        st.markdown(
            "**Attendance % = present ÷ batch strength** (everyone enrolled with an email), the same "
            "denominator the AI CAP dashboard uses. **Present** counts a learner whose email or phone "
            "appears in the Zoom attendee report with *Attended = Yes*. **vs expected** compares the "
            "session with the decay curve fitted on these seven batches: 1.00× is exactly what a cohort "
            "of that age normally draws. Ratings come from the session's feedback poll; in a room shared "
            "with an AI CAP batch the figure is **this batch's own students' answers**, and *Whole room* "
            "is everyone who answered. **Duration** is first-join to last-leave; **Peak** is the most "
            "people in the room at once, swept from join/leave times.")
    dates = sorted({s["date"] for s in S})
    f1, f2, f3 = st.columns([2, 1, 2])
    dr = f1.date_input("Date range",
                       value=(_dt.date.fromisoformat(dates[0]), _dt.date.fromisoformat(dates[-1])),
                       min_value=_dt.date.fromisoformat(dates[0]),
                       max_value=_dt.date.fromisoformat(dates[-1]), key=f"{key}_dates")
    fb = f2.multiselect("Batch", store["batches"], key=f"{key}_batch_filter")
    ft = f3.multiselect("Trainer", sorted({t for s in S for t in (s.get("trainers") or [])}),
                        key=f"{key}_trainer")
    fq = st.text_input("Search title", key=f"{key}_q").strip().lower()
    v = S
    if isinstance(dr, (tuple, list)) and len(dr) == 2:
        a, b = dr[0].isoformat(), dr[1].isoformat()
        v = [s for s in v if a <= s["date"] <= b]
    if fb:
        v = [s for s in v if s["batch"] in fb]
    if ft:
        v = [s for s in v if set(s.get("trainers") or []) & set(ft)]
    if fq:
        v = [s for s in v if fq in (s.get("topic") or "").lower()]
    if not v:
        st.info("No sessions match these filters.")
        return
    G = _recap.group_sessions(v)
    rated = [g for g in G if g.get("rating") is not None]
    rn = sum(g["rating_n"] for g in rated)
    trd = [g for g in G if g.get("rating_trainer") is not None]
    trn = sum(g["rating_n"] for g in trd)
    merged = _polls.merge_dists(g.get("dist") for g in G)
    pres, inv = sum(s["present"] for s in v), sum(s["total"] for s in v)
    nps = _polls.nps_from_dist(merged.get("recommend"))
    T.tiles([
        {"label": "Sessions", "value": f"{len(G):,}"},
        {"label": "Attendance", "value": f"{pres / inv * 100:.1f}%" if inv else "—",
         "help": f"{pres:,} present of {inv:,} invited, pooled."},
        {"label": "Avg overall",
         "value": (f"{sum(g['rating'] * g['rating_n'] for g in rated) / rn:.2f}" if rn else "—"),
         "help": "Weighted by responses."},
        {"label": "Avg trainer",
         "value": (f"{sum(g['rating_trainer'] * g['rating_n'] for g in trd) / trn:.2f}" if trn else "—")},
        {"label": "NPS", "value": f"{nps:+d}" if nps is not None else "—",
         "help": "Promoter 5, passive 4, detractor 1-3, from the summed histograms of the sessions shown."},
    ])
    rows = sorted(v, key=lambda s: (s["date"], s["batch"]), reverse=True)
    df = pd.DataFrame([{
        "Date": s["date"], "Batch": s["batch"], "Title": s.get("topic") or "",
        "Trainer": s.get("trainer") or "", "Present": s["present"], "Absent": s.get("absent"),
        "Att %": s.get("pct"), "vs expected": s.get("index"),
        "Rating": s.get("rating"), "Trainer rating": s.get("rating_trainer"),
        "Responses": s.get("rating_n") or 0, "NPS": s.get("nps"),
        "Duration (hrs)": s.get("duration_hrs"), "Peak": s.get("peak"),
        "Stickiness %": s.get("stick30"), "Schedule label": s.get("l2_batch") or "",
    } for s in rows])
    st.dataframe(df, width="stretch", hide_index=True, height=min(520, 40 + 35 * len(df)),
                 column_config={
                     "Att %": st.column_config.NumberColumn(format="%.1f%%"),
                     "vs expected": st.column_config.NumberColumn(
                         format="%.2f×",
                         help="1.00 = exactly what the decay curve predicts for a batch of that age."),
                     "Rating": st.column_config.NumberColumn(format="%.2f"),
                     "Trainer rating": st.column_config.NumberColumn(format="%.2f"),
                     "Duration (hrs)": st.column_config.NumberColumn(format="%.1f"),
                     "Stickiness %": st.column_config.NumberColumn(
                         format="%.0f%%", help="Mean of the closing 30 minutes over the session's peak."),
                 })
    labels = [f"{s['date']} · {s['batch']} · {(s.get('topic') or '')[:60]}" for s in rows]
    pick = st.selectbox("Session detail", labels, key=f"{key}_pick")
    s = rows[labels.index(pick)]
    st.markdown(f'<div class="section-title">{_html.escape(s.get("topic") or "Session")}</div>'
                f'<div class="section-sub">{_html.escape(s["batch"])} · {s["date_lbl"]}'
                + (f" · {_html.escape(s['trainer'])}" if s.get("trainer") else "")
                + (f" · schedule: {_html.escape(s['l2_batch'])}" if s.get("l2_batch") else "")
                + "</div>", unsafe_allow_html=True)
    sh = s.get("rating_shared") or {}
    T.tiles([
        {"label": "Present", "value": f"{s['present']:,}", "sub": f"of {s['total']:,} enrolled"},
        {"label": "Attendance", "value": f"{_f(s.get('pct'))}%"},
        {"label": "vs expected", "value": T.fmt_index(s.get("index")),
         "sub": f"curve expected {_f(s.get('expected_pct'))}%" if s.get("expected_pct") else None},
        {"label": "Rating", "value": _f(s.get("rating"), "{:.2f}"),
         "sub": f"{s.get('rating_n') or 0} responses" + (" · own students" if sh.get("split") else "")},
        {"label": "Trainer rating", "value": _f(s.get("rating_trainer"), "{:.2f}")},
        {"label": "NPS", "value": f"{s['nps']:+d}" if isinstance(s.get("nps"), int) else "—"},
        {"label": "Duration", "value": f"{_f(s.get('duration_hrs'))} h", "sub": "first join to last leave"},
        {"label": "Peak in room", "value": _f(s.get("peak"), "{:,}"),
         "sub": f"{s['unique_viewers']:,} unique viewers" if s.get("unique_viewers") else None},
    ])
    if sh:
        room = sh.get("room") or sh.get("joint") or {}
        others = [b for b in (sh.get("batches") or []) if b != s["batch"]]
        if sh.get("split"):
            st.caption(f"Shared room with {', '.join(others)}: this batch's rating is its own students' "
                       f"answers ({s.get('rating_n') or 0}); the whole room rated it "
                       f"{_f(room.get('session'), '{:.2f}')} from {room.get('responses') or 0} responses "
                       f"({sh.get('unmatched', 0)} not on this roster).")
        else:
            st.caption(f"Shared room with {', '.join(others)}: the poll could not be divided "
                       f"({sh.get('reason', '?')}) — the figure shown is the whole room's.")
    curve = s.get("retention")
    if curve:
        import plotly.graph_objects as go
        fig = go.Figure(go.Scatter(x=list(range(len(curve))), y=curve, mode="lines",
                                   line=dict(color=T.BRAND["accent"], width=2),
                                   fill="tozeroy", fillcolor=T.BRAND["accent_soft"],
                                   hovertemplate="minute %{x}<br>%{y} in the room<extra></extra>"))
        if s.get("poll_at_min") is not None:
            fig.add_vline(x=s["poll_at_min"], line_dash="dot", line_color=T.INK["muted"],
                          annotation_text="poll", annotation_position="top")
        fig.update_layout(T.plotly_layout(220, xaxis=dict(ticksuffix=" min")))
        st.markdown('<div class="panel-title">People in the room, minute by minute</div>',
                    unsafe_allow_html=True)
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"{key}_curve")


def page_this_week(store: dict, key: str) -> None:
    r = store.get("recap")
    if not r or not r.get("weeks"):
        st.warning("No weekly recap in this store.")
        for w in (r or {}).get("warnings") or []:
            st.caption("• " + str(w))
        return
    lat = r["latest"]
    with st.expander("How this is calculated", expanded=False):
        st.markdown(
            "Every headline here is **attendance vs expected** — what a session drew over what the "
            "decay curve (fitted on these seven BSIAI batches) says a cohort of that age should draw. "
            f"1.00× expected is exactly on curve. Week of {lat['week']}; built {store['generated_at']}.")
    dl0 = lat["delta"]
    T.tiles([
        {"label": "Sessions", "value": f"{lat['sessions']:,}"},
        {"label": "Learners present", "value": f"{lat['present']:,}",
         "delta": T.delta_text("present", dl0["present"])},
        {"label": "Attendance", "value": f"{lat['pct']:.1f}%" if lat["pct"] else "—",
         "delta": T.delta_text("pct", dl0["pct"])},
        {"label": "vs expected", "value": T.fmt_index(lat["index"]) if lat["index"] else "—",
         "delta": T.delta_text("index", dl0["index"])},
        {"label": "NPS", "value": f"{lat['nps']:+d}" if lat["nps"] is not None else "—",
         "delta": T.delta_text("nps", dl0["nps"])},
    ])
    if r.get("awards"):
        st.subheader("This week")
        for a, col in zip(r["awards"], st.columns(len(r["awards"]))):
            with col:
                e = _html.escape
                st.markdown(f"**{e(a['award'])}**  \n### {e(T.award_value(a))}  \n"
                            f"{e(a['batch'])} · {e(a['topic'][:44])}"
                            + (f"  \n_{e(a['mentor'])}_" if a.get("mentor") else "")
                            + f"  \n<span style='opacity:.65;font-size:12px'>{e(a['why'])}</span>",
                            unsafe_allow_html=True)
    idx_col = st.column_config.NumberColumn("vs expected", format="%.2f×")
    st.subheader("Week by week")
    st.dataframe(pd.DataFrame([{
        "Week of": w["week"], "Sessions": w["sessions"], "Batches": len(w["batches"]),
        "Present": w["present"], "Invited": w["invited"], "Attendance %": w["pct"],
        "vs expected": w["index"], "Rating": w["rating"], "NPS": w["nps"],
    } for w in reversed(r["weeks"])]), width="stretch", hide_index=True,
        column_config={"vs expected": idx_col})
    if r.get("leaderboard"):
        st.subheader("Trainers, this week")
        st.dataframe(pd.DataFrame([{
            "Trainer": b["mentor"], "Sessions": b["sessions"], "Present": b["present"],
            "Attendance %": b["pct"], "vs expected": b["index"], "Rating": b["rating"], "NPS": b["nps"],
        } for b in r["leaderboard"]]), width="stretch", hide_index=True,
            column_config={"vs expected": idx_col})


def page_trainers(store: dict, key: str) -> None:
    t = store.get("trainers")
    if not t or not t.get("trainers"):
        st.warning("No trainer data in this store.")
        return
    with st.expander("How this is calculated", expanded=False):
        st.markdown(
            f"**{t['n_raw']} spellings in the schedule resolve to {t['n_people']} people.** The Mentor "
            "cell is hand-typed; where the schedule carries an email it is trusted, otherwise a short "
            "name joins a longer one only when it is an unambiguous prefix. Attendance pools every "
            "session the trainer taught against each batch's own roster; the rating counts each room's "
            "poll once.")
    mn = st.slider("Minimum sessions", 1, 15, 2, key=f"{key}_min_sessions")
    rows = [x for x in t["trainers"] if x["sessions"] >= mn]
    st.dataframe(pd.DataFrame([{
        "Trainer": x["trainer"], "Type": x.get("type") or "", "Sessions": x["sessions"],
        "Batches": ", ".join(x["batches"]), "Present": x["present"], "Invited": x["invited"],
        "Attendance %": x["pct"], "vs expected": x["index"], "Rating": x["rating"],
        "Responses": x["rating_n"], "NPS": x["nps"], "First": x["first"], "Last": x["last"],
    } for x in rows]), width="stretch", hide_index=True,
        height=min(520, 40 + 35 * max(1, len(rows))),
        column_config={"vs expected": st.column_config.NumberColumn("vs expected", format="%.2f×")})
    st.caption(f"{len(rows)} of {t['n_people']} shown.")
    if t.get("merged"):
        with st.expander(f"Spellings merged ({len(t['merged'])} people)"):
            st.dataframe(pd.DataFrame([{"Shown as": k, "Merged from": ", ".join(v)}
                                       for k, v in sorted(t["merged"].items())]),
                         width="stretch", hide_index=True)


def _file_download(store: dict, key: str, label: str, local_key: str, drive_key: str,
                   fname: str, primary: bool = False) -> None:
    """A workbook download: straight from disk on the machine that built the
    store, otherwise fetched once from the private Drive folder (where
    `bsiai_build.py --upload` put it) and offered from session state."""
    p = store.get(local_key)
    if p and pathlib.Path(p).exists():
        st.download_button(label, data=pathlib.Path(p).read_bytes(), file_name=pathlib.Path(p).name,
                           mime=XLSX_MIME, type="primary" if primary else "secondary", key=f"{key}_dl")
        return
    fid = store.get(drive_key) or ""
    if not fid:
        st.caption(f"{fname} is not on Drive yet — run `bsiai_build.py --upload`.")
        return
    tok = f"{fid}@{store.get('generated_at_iso', '')}"
    if st.session_state.get(f"{key}_tok") != tok:
        if st.button(f"⬇️ Prepare {fname} (.xlsx)", key=f"{key}_prep",
                     help="Fetches the workbook from Drive"):
            try:
                import live_data
                with st.spinner("Fetching from Drive…"):
                    st.session_state[f"{key}_bytes"] = live_data.fetch_file_bytes(
                        live_data._drive_service(), fid)
                    st.session_state[f"{key}_tok"] = tok
            except Exception as e:             # noqa: BLE001 — say why, keep the page
                st.error(f"Couldn’t fetch it from Drive: {e}")
            else:
                st.rerun(scope="fragment")
    if st.session_state.get(f"{key}_tok") == tok:
        st.download_button(label, data=st.session_state[f"{key}_bytes"], file_name=fname,
                           mime=XLSX_MIME, type="primary" if primary else "secondary", key=f"{key}_dl")


def page_roster(store: dict, key: str) -> None:
    c1, c2 = st.columns(2)
    with c1:
        _file_download(store, f"{key}_marked", "⬇️ Marked attendance — all batches, all sessions (.xlsx)",
                       "marked_xlsx", "marked_xlsx_file_id", "BSIAI_marked_attendance.xlsx", primary=True)
    with c2:
        _file_download(store, f"{key}_roster", "⬇️ Roster extract in the house format (.xlsx)",
                       "roster_xlsx", "roster_xlsx_file_id", "BSIAI_roster_format.xlsx")
    st.caption("The marked attendance, learner by learner, exactly like the roster sheet.")
    batches = dash_view.batch_order(store["batches"])
    ctop1, ctop2 = st.columns([3, 2])
    pick = ctop1.selectbox("Batch", batches, index=0, key=f"{key}_roster_pick")
    show_pii = ctop2.checkbox("Show full contact details (real PII)", value=False,
                              key=f"{key}_pii", help="Off by default — emails/phones are masked.")
    g = _grid(store["path"], pick) if pick else None
    if g is None:
        st.info("No roster rows to show for this batch.")
        return
    active_only = st.checkbox("Active learners only", value=True, key=f"{key}_active")
    disp, sess_cols, n_students, n_present_any = _roster_display(
        g, pick, active_only, show_pii, store.get("generated_at_iso", ""))
    T.tiles([
        {"label": "Learners shown", "value": f"{n_students:,}"},
        {"label": "Attended ≥1 session", "value": f"{n_present_any:,}"},
        {"label": "Sessions", "value": len(sess_cols)},
    ])

    def _hl(v):
        if v == _MARK_PRESENT:
            b = T.STATUS["high"]
        elif v == _MARK_ABSENT:
            b = T.STATUS["low"]
        else:
            return ""
        return f"background-color: {b['bg']}; color:{b['fg']}"

    styled = disp.style.map(_hl, subset=sess_cols)
    st.dataframe(styled, width="stretch", hide_index=True, height=520,
                 column_config={"Email": st.column_config.TextColumn("Email", pinned=True),
                                **{c: st.column_config.TextColumn(c, width="small") for c in sess_cols}})
    st.caption(f"{_MARK_PRESENT} present · {_MARK_ABSENT} absent · {_MARK_BLANK} not marked for that session.")


def page_notes(store: dict, key: str) -> None:
    st.markdown('<div class="section-title">The rules behind these numbers</div>', unsafe_allow_html=True)
    for r in store.get("rulings") or []:
        st.markdown("• " + r)
    stamps = store.get("stamps") or {}
    if stamps.get("lms"):
        st.markdown('<div class="section-title">Roster, from the LMS API</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "Batch": c, "LMS batch name": v["name"], "LMS id": v["id"],
            "Listing claimed": v["claimed"], "Roster rows": v["rows"],
        } for c, v in stamps["lms"].items()]), width="stretch", hide_index=True)
        st.caption(f"Roster fetched {stamps.get('roster', '')} · schedule modified {stamps.get('l2', '')}")
    rep = store.get("report") or []
    if rep:
        st.markdown('<div class="section-title">Every session marked</div>', unsafe_allow_html=True)
        st.dataframe(pd.DataFrame([{
            "Batch": r["batch"], "Date": r["date"], "Title": r["topic"], "Schedule label": r["label"],
            "Webinars": ", ".join(r["webinars"]), "Present": r["present"], "Enrolled": r["total"],
            "People in room": r["in_room"], "Registered, did not join": r["registered_no"],
            "Copies on Drive": r["copies"],
            "Rooms merged": ", ".join(x["label"] for x in r.get("extra_rooms") or []),
            "Other rooms (measured only)": "; ".join(
                f"{x['label']}: {x['students_only_there']} only there"
                for x in r.get("unmerged_rooms") or []),
        } for r in rep]), width="stretch", hide_index=True, height=min(600, 40 + 35 * len(rep)))
    ws = store.get("warnings") or []
    with st.expander(f"{len(ws)} warning(s) from the build", expanded=False):
        for w in ws:
            st.write("• " + str(w))


_PAGE_FN = [page_dashboard, page_sessions, page_this_week, page_trainers, page_roster, page_notes]


def render(store: dict, key: str = "bsiai") -> None:
    """Draw every BSIAI page under one strip of sub-tabs.

    Call it from inside an `@st.fragment` — the Dashboard's bar-click rerun is
    fragment-scoped, exactly as the AI CAP Dashboard's is.
    """
    st.caption(caption(store))
    tabs = st.tabs(PAGES)
    for tab, fn in zip(tabs, _PAGE_FN):
        with tab:
            fn(store, key)
