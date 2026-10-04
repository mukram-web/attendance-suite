"""
ui_theme.py — the ONE place the dashboard's look is defined.

Brand assets (colour, logo, font) arrive later. Until they do, BRAND holds a
neutral, colour-blind-safe default taken from the dataviz reference palette
(accent = categorical slot 1, positive/negative = the fixed status ramp's
success text and critical). To rebrand: change the values in BRAND and the
matching `primaryColor` in `.streamlit/config.toml`; nothing else references
a hex code for the app's chrome.

Everything in here is presentation. No number is computed in this module —
the tile helper prints the strings it is given, exactly as they were computed
by the caller.

Pure helpers (no Streamlit) live at the top so they can be unit-tested and so
`site_build.py` can reuse the dashboard's CSS rules under its own wrapper.
"""
from __future__ import annotations

import datetime as _dt
import html as _html

# ── brand tokens ──────────────────────────────────────────────────────────────
BRAND = {
    # The one series colour: selected marks, the single-series line, links.
    "accent": "#2a78d6",
    # The accent washed to ~10%: area fills and the selected tile. Filled in
    # from "accent" itself just below the dict - see `_soft` - so the owner's
    # colour, when it lands, is ONE edit here plus .streamlit/config.toml
    # (which a test pins to this value, so the two cannot drift).
    "accent_soft": "",
    # Delta pill text. Status colours never carry meaning alone — every pill
    # also carries its sign and an arrow glyph.
    "positive": "#006300",
    "negative": "#d03b3b",
    # The de-emphasis colour: every mark that is not the one being pointed at.
    # It reads as gray on purpose (emphasis form: highlight one, gray the rest).
    "neutral": "#9aa3b1",
    "font": 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    # The wordmark the owner sent 2026-09-30: monochrome, two files.
    # `logo_light` is black ink for a light surface and `logo_dark` white
    # ink for a dark one -- the white file is white-on-transparent, so the
    # two are never interchangeable. `favicon` is the "10X" disc cropped
    # out of the black PNG: the full 750x432 wordmark is a smear at 16px.
    "logo_light": "assets/logo_black.svg",
    "logo_dark": "assets/logo_white.svg",
    "favicon": "assets/favicon_64.png",
    # How wide the MARK reads in the header. 140px of wordmark, not 140px of
    # file -- see `logo_ink` below.
    "logo_width_px": 140,
    # Both wordmark files are the mark centred in a 1080x1080 canvas, and 60%
    # of that canvas is empty: measured in a browser 2026-09-30 (getBBox over
    # every drawn element in logo_black.svg), the ink runs x 150.5-900 and
    # y 324-756 -- a 749.5x432 mark with 30% dead space above and below. Sized
    # by width alone the header would carry a 140px-TALL block, 60% padding,
    # which reads as a broken image rather than a logo. `.brandbar .mark`
    # therefore crops to this rect. Replace the files and re-measure these
    # four numbers; nothing else in the app moves.
    "logo_viewbox": (1080.0, 1080.0),
    "logo_ink": (150.5, 324.0, 749.5, 432.0),      # x, y, w, h in viewBox units
}


def _soft(hex_colour: str, alpha: float = 0.10) -> str:
    """`#rrggbb` as an `rgba(...)` wash. One line, one reason: the accent is a
    placeholder until the owner sends a colour, and restating its channels by
    hand a second time would mean their edit silently half-applied."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha:g})"


BRAND["accent_soft"] = _soft(BRAND["accent"])

# Chart chrome and ink — text is never in the series colour.
INK = {
    "primary": "#0b0b0b",
    "secondary": "#52514e",
    # 5.4:1 at 11px. The old #898781 measured 3.59:1 - recessive to the point
    # of unreadable on an axis. Still a clear step below `secondary`, and still
    # lighter than the #c3c2b7 axis rule it sits beside.
    "muted": "#6f6d67",
    "grid": "#e1e0d9",           # hairline, solid, one step off the surface
    "axis": "#c3c2b7",
    "surface": "#fcfcfb",
}

# Attendance status bands (pills and the closing-type panel). Status colours
# only ever say good / fair / low — never which series a mark belongs to.
STATUS = {
    "high": {"bg": "#e3f4ec", "fg": "#0f6e56", "hex": "#1a9e75"},
    "mid":  {"bg": "#fbeedd", "fg": "#8a5108", "hex": "#c98500"},
    "low":  {"bg": "#fae4dd", "fg": "#993c1d", "hex": "#d8543a"},
}


# ── plain words ──────────────────────────────────────────────────────────────
# Ruling F renames (residual → "vs expected", POD → Domain, joint → "Whole
# room", L2 → schedule, "excl." dropped, "Play Simulive" kept) are written
# by hand at each UI label, caption and column header in attendance_app.py
# and dash_view.py — never applied to data keys or CSV exports — and
# tests/test_ui_layout.py lints the two files for any that slipped back.
# The two helpers below cover the dynamic strings: a residual index read as
# "1.04× expected", and recap's "Beat the curve" award value.


def fmt_index(v, dash: str = "—") -> str:
    """A residual index as the team reads it: 1.04 -> '1.04× expected'."""
    if not isinstance(v, (int, float)):
        return dash
    return f"{v:.2f}× expected"


# What a change of each kind of figure is worth on screen. The unit is part of
# the number: the Weekend Recap used to print a bare "+3" under both NPS and
# Learners, where one means three points of NPS and the other three people.
# `None` is not zero -- a first week has no comparison, so it gets no pill.
_DELTA_FMT = {
    "present":        ("{:+,.0f}", ""),
    "learners":       ("{:+,.0f}", ""),
    "sessions":       ("{:+,.0f}", ""),
    "pct":            ("{:+.1f}", "%"),
    "index":          ("{:+.2f}", "×"),
    "rating":         ("{:+.2f}", ""),
    "rating_trainer": ("{:+.2f}", ""),
    "nps":            ("{:+.0f}", " pts"),
    # 1.3 points is not 1 point: the two weeks this is read against are
    # 59.1% and 60.4%, so rounding to whole points understates the move by a
    # quarter. NPS above is a whole-point figure and stays at .0f.
    "stickiness":     ("{:+.1f}", " pts"),
}


def delta_text(kind: str, value) -> str | None:
    """One signed change string, with its unit, for a tile's delta pill.

    `kind` names the FIGURE, not the format, so every place that shows a
    week-on-week change of the same thing shows it the same way. An unknown
    kind falls back to one decimal and no unit rather than raising — a new
    recap field must not be able to blank a tile.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    fmt, unit = _DELTA_FMT.get(kind, ("{:+.1f}", ""))
    return fmt.format(value) + unit


def award_value(a: dict) -> str:
    """An award card's value string. recap.py emits the residual award as the
    bare '1.05x'; on screen it reads '1.05× expected'. Everything else is
    printed exactly as recap wrote it."""
    v = str((a or {}).get("value") or "")
    if (a or {}).get("award") == "Beat the curve" and v.endswith("x"):
        return v[:-1] + "× expected"
    return v


# ── the latest weekend in a set of dated sessions ─────────────────────────────
def latest_weekend(dates) -> tuple[_dt.date, _dt.date] | None:
    """The Sat/Sun pair of the newest weekend session.

    Sessions run on Saturday and Sunday; the newest such date and its partner
    day make the weekend. A newer mid-week date does not move the window — it
    is not a weekend session — and a set with no weekend date at all yields
    None rather than an invented one.
    """
    days = set()
    for d in dates or ():
        if not d:
            continue
        try:
            days.add(d if isinstance(d, _dt.date) else _dt.date.fromisoformat(str(d)[:10]))
        except ValueError:
            continue
    weekend = [d for d in days if d.weekday() >= 5]
    if not weekend:
        return None
    newest = max(weekend)
    sat = newest if newest.weekday() == 5 else newest - _dt.timedelta(days=1)
    return sat, sat + _dt.timedelta(days=1)


def weekend_label(sat: _dt.date, sun: _dt.date) -> str:
    """'26–27 Sep 2026', or '31 Aug – 1 Sep 2026' across a month boundary."""
    if sat.year != sun.year:
        return f"{sat:%d %b %Y} – {sun:%d %b %Y}".replace(" 0", " ")
    if sat.month != sun.month:
        return f"{sat.day} {sat:%b} – {sun.day} {sun:%b %Y}"
    return f"{sat.day}–{sun.day} {sun:%b %Y}"


def ecap_tick(code: str) -> str:
    """Tick label that stays horizontal: 'ECAP B1' wraps to two lines."""
    return str(code).replace("ECAP ", "ECAP<br>")


# ── KPI tiles ────────────────────────────────────────────────────────────────
def _delta_class(delta: str, delta_color: str) -> str:
    """Same rule as st.metric: a delta starting with '-' is a fall."""
    neg = str(delta).strip().startswith("-")
    if delta_color == "inverse":
        neg = not neg
    return "neg" if neg else "pos"


def tile_html(label: str, value, delta=None, delta_color: str = "normal",
              help: str | None = None, sub: str | None = None) -> str:
    """One stat tile. Every string is printed as given — nothing is computed."""
    lbl = _html.escape(str(label))
    if help:
        lbl += (f' <span class="kpi-help" title="{_html.escape(str(help), quote=True)}"'
                f' aria-label="{_html.escape(str(help), quote=True)}">i</span>')
    parts = [f'<div class="kpi"><div class="kpi-label">{lbl}</div>',
             f'<div class="kpi-value">{_html.escape(str(value))}</div>']
    if delta not in (None, ""):
        if delta_color == "off":
            parts.append(f'<div class="kpi-sub">{_html.escape(str(delta))}</div>')
        else:
            cls = _delta_class(delta, delta_color)
            arrow = "▼" if str(delta).strip().startswith("-") else "▲"
            parts.append(f'<div class="kpi-delta {cls}">{arrow} '
                         f'{_html.escape(str(delta))}</div>')
    if sub:
        parts.append(f'<div class="kpi-sub">{_html.escape(str(sub))}</div>')
    parts.append("</div>")
    return "".join(parts)


def tiles_html(items) -> str:
    """A responsive grid of tiles: 4 → 2 → 1 across as the width shrinks.

    `items` is a list of dicts with keys label, value and optionally delta,
    delta_color ('normal' | 'inverse' | 'off'), help, sub.
    """
    inner = "".join(tile_html(**it) for it in items)
    return f'<div class="kpi-grid">{inner}</div>'


def tiles(items) -> None:
    """Render a tile grid in the current Streamlit container."""
    import streamlit as st
    st.markdown(tiles_html(items), unsafe_allow_html=True)


# ── CSS ──────────────────────────────────────────────────────────────────────
def dash_rules(prefix: str = ".dash ") -> str:
    """The dashboard fragments' rules (pills, closing rows, the sessions table),
    scoped under `prefix`. The app scopes them under `.dash`; the static site
    scopes the same rules under its own `.aicap` wrapper."""
    p = prefix
    a, s = BRAND["accent"], STATUS
    return f"""
  {p}.panel-title{{font-size:14px;font-weight:600;margin:18px 0 10px;}}
  {p}.dh-sub{{opacity:.7;font-weight:400;font-size:13px;}}
  {p}.pill{{padding:2px 9px;border-radius:999px;font-weight:600;font-size:12px;
    font-variant-numeric:tabular-nums;display:inline-block;}}
  {p}.cl-row{{display:grid;grid-template-columns:minmax(90px,150px) minmax(60px,1fr) auto auto;
    align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid rgba(128,128,128,.18);}}
  {p}.cl-name{{font-size:13px;}}
  {p}.cl-track{{height:6px;background:rgba(128,128,128,.16);border-radius:6px;overflow:hidden;}}
  {p}.cl-fill{{height:100%;background:{a};opacity:.55;border-radius:6px;}}
  {p}.cl-count{{font-size:12px;opacity:.75;text-align:right;font-variant-numeric:tabular-nums;}}
  {p}table.sess{{width:100%;border-collapse:collapse;font-size:13px;}}
  {p}table.sess th,{p}table.sess td{{padding:7px 10px;border-bottom:1px solid rgba(128,128,128,.18);
    text-align:left;vertical-align:top;}}
  {p}table.sess td.topic{{white-space:normal;overflow-wrap:anywhere;}}
  {p}table.sess th.num,{p}table.sess td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}}
  {p}table.sess thead th{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;opacity:.7;
    white-space:normal;}}
  {p}.flag{{font-size:10px;color:{s['mid']['fg']};background:{s['mid']['bg']};padding:1px 6px;
    border-radius:6px;margin-left:6px;white-space:nowrap;}}
  /* The shared-room label. It is DATA - the only thing telling two
     identically-titled sessions apart - so it is not in the series colour
     (see INK's note above) and not in the accent's 10% wash, which measured
     3.89:1 composited over white, under AA's 4.5 for 11px text. A neutral
     tint under secondary ink measures 6.95:1. */
  {p}.chip{{font-size:11px;color:{INK['secondary']};background:rgba(128,128,128,.12);
    padding:1px 7px;border-radius:6px;margin-right:6px;white-space:nowrap;
    display:inline-block;}}
  /* .55 measured 3.23:1 at 12px. .70 is 4.9:1 - still plainly recessive,
     but readable. The same reason lifts .kpi-sub and .mini-list .who. */
  {p}.muted{{opacity:.70;}}
  {p}.dash-scroll{{overflow-x:auto;}}
"""


def _mark_rules() -> str:
    """The header wordmark's crop, computed from BRAND's measured ink rect.

    The two logo files are the mark centred in a 1080x1080 canvas with 30%
    dead space above and below (BRAND['logo_ink']). Sizing the <img> by width
    alone therefore buys a square block that is mostly padding, so the <img>
    is blown up to `scale` and offset inside a window the shape of the ink.
    Everything here is arithmetic on BRAND — no hand-tuned pixel survives a
    change of asset.
    """
    vw, vh = BRAND["logo_viewbox"]
    ix, iy, iw, ih = BRAND["logo_ink"]
    w = BRAND["logo_width_px"]
    return (
        f'  .brandbar .mark{{--mark-w:{w}px;'
        f'--logo-w:calc(var(--mark-w)*{vw / iw:.5f});'
        f'width:var(--mark-w);height:calc(var(--mark-w)*{ih / iw:.5f});'
        'position:relative;overflow:hidden;flex:none;display:block;}\n'
        '  .brandbar .mark img{position:absolute;width:var(--logo-w);'
        f'height:calc(var(--logo-w)*{vh / vw:.5f});'
        f'left:calc(var(--logo-w)*{-ix / vw:.5f});'
        f'top:calc(var(--logo-w)*{-iy / vw:.5f});display:block;}}')


def css() -> str:
    """The single stylesheet the app injects once per run."""
    b = BRAND
    return f"""<style>
  :root{{--brand-accent:{b['accent']};--brand-accent-soft:{b['accent_soft']};
    --brand-positive:{b['positive']};--brand-negative:{b['negative']};--brand-neutral:{b['neutral']};}}
  /* KPI tiles: a grid that wraps 4 → 2 → 1 by width instead of stacking. */
  .kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:4px 0 14px;}}
  .kpi{{border:1px solid rgba(128,128,128,.28);border-radius:10px;padding:12px 14px 12px;min-width:0;}}
  .kpi-label{{font-size:12px;opacity:.75;line-height:1.3;}}
  .kpi-help{{display:inline-block;width:14px;height:14px;line-height:12px;border:1px solid currentColor;
    border-radius:50%;font-size:10px;text-align:center;margin-left:5px;cursor:help;opacity:.8;
    font-family:Georgia,serif;font-style:italic;vertical-align:1px;}}
  .kpi-value{{font-size:28px;font-weight:600;line-height:1.15;margin-top:4px;
    font-variant-numeric:normal;overflow-wrap:anywhere;}}
  .kpi-delta{{display:inline-block;margin-top:6px;font-size:12px;font-weight:600;padding:1px 8px;
    border-radius:999px;font-variant-numeric:tabular-nums;}}
  .kpi-delta.pos{{color:{b['positive']};background:rgba(0,99,0,.10);}}
  .kpi-delta.neg{{color:{b['negative']};background:rgba(208,59,59,.10);}}
  .kpi-sub{{margin-top:6px;font-size:12px;opacity:.72;line-height:1.3;}}
  /* Section titles and small lists used by the Last weekend block. */
  .section-title{{font-size:18px;font-weight:600;margin:6px 0 2px;}}
  .section-sub{{font-size:13px;opacity:.7;margin:0 0 10px;}}
  .mini-list{{list-style:none;padding:0;margin:2px 0 10px;}}
  .mini-list li{{display:flex;justify-content:space-between;gap:12px;padding:6px 0;
    border-bottom:1px solid rgba(128,128,128,.18);font-size:13px;line-height:1.3;}}
  .mini-list li .who{{opacity:.72;font-size:12px;}}
  .mini-list li .val{{white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:600;}}
  /* Expander spacing and long headers. */
  div[data-testid="stExpander"]{{margin:2px 0 10px;}}
  div[data-testid="stExpander"] summary p{{font-size:13px;}}
  h1,h2,h3{{overflow-wrap:anywhere;}}
  /* Header: the wordmark beside the page name. `.mark` is a window onto the
     file's ink rect (BRAND['logo_ink']) so --mark-w is the width of the MARK,
     not of the mostly-empty 1080px canvas it ships in. */
  .brandbar{{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin:0 0 2px;}}
{_mark_rules()}
  .brandbar .t{{font-size:1.5rem;font-weight:700;letter-spacing:-.01em;line-height:1.15;}}
  @media (max-width:640px){{.brandbar .mark{{--mark-w:{b['logo_width_px'] * 0.7:.0f}px;}}
    .brandbar .t{{font-size:1.15rem;}}}}
{dash_rules('.dash ')}
</style>"""


def inject_css() -> None:
    """Inject the stylesheet. Call ONCE per run, near the top of the page."""
    import streamlit as st
    st.markdown(css(), unsafe_allow_html=True)


# ── Plotly base layout ────────────────────────────────────────────────────────
def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def plotly_layout(height: int, **overrides) -> dict:
    """Quiet chrome: transparent surface, recessive hairline grid, no legend
    for a single series, axis text in the muted ink."""
    base = dict(
        height=height, margin=dict(l=8, r=12, t=12, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=BRAND["font"], color=INK["secondary"], size=12),
        showlegend=False,
        hoverlabel=dict(font=dict(family=BRAND["font"], size=12)),
        # tickangle 0 is the DEFAULT here, not a per-chart opt-in: ruling D
        # bans rotated tick labels, and Plotly's own default ("auto", with
        # autotickangles [0, 30, 90]) stands them on end the moment they stop
        # fitting. A chart with more labels than fit thins them (tickvals or
        # nticks); it does not turn them.
        xaxis=dict(title=None, showgrid=False, showline=True, linecolor=INK["axis"],
                   linewidth=1, tickfont=dict(size=11, color=INK["muted"]),
                   tickangle=0, fixedrange=True),
        yaxis=dict(title=None, showgrid=True, gridcolor=INK["grid"], gridwidth=1,
                   griddash="solid", zeroline=False, showline=False,
                   tickfont=dict(size=11, color=INK["muted"]), fixedrange=True),
    )
    return _merge(base, overrides)
