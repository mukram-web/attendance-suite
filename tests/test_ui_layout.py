"""The dashboard's layout, pinned.

Two kinds of test. The pure ones exercise ui_theme's helpers — the KPI tile
grid, the jargon map and the latest-weekend rule — with no Streamlit at all.
The AppTest ones run the real attendance_app.py headlessly against the local
prebuilt store (.cache/attendance.duckdb) and assert the SHAPE of the page:
every tab and sub-tab renders without an exception, the "Last weekend"
section leads the Dashboard, the marked-roster download lives in the Roster
tab and nowhere above the tab strip, the technical data-source text sits in a
collapsed Admin expander, and the batch drill-in opens on the newest batch.

The gate: attendance_app's password check returns early when the session is
already marked `_authed`, so the test sets that flag on the AppTest session
state and never touches a password. Secrets are replaced with an empty-ish
dict so the run is hermetic — no Drive, local-only store mode — which is
exactly the mode this machine runs the app in.

These AppTest cases skip when there is no local store (CI has none), the
same way the sample-roster tests skip without their fixture.
"""
import ast
import datetime as dt
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ui_theme as T  # noqa: E402

STORE = os.path.join(ROOT, ".cache", "attendance.duckdb")
APP = os.path.join(ROOT, "attendance_app.py")

# The jargon lint (TestPlainWords): what must NOT appear in an on-screen
# string, and the strings that are data rather than labels — the CSV
# export's column keys (a file format, kept verbatim) and dict keys.
_JARGON = re.compile(r"\bvs\.? curve\b|\bresiduals?\b|\bPODs?\b|\b[Jj]oint\b|"
                     r"\bL2\b|\bexcl\.")
_CSV_KEYS = {"L2 batch", "POD", "vs curve", "Joint trainer rating", "Joint overall",
             "Joint NPS", "Joint responses"}
_DATA_KEY = re.compile(r"[a-z_0-9]+")


def _string_literals(path):
    """(lineno, text) for every string literal in a source file that is not a
    docstring — i.e. everything that could reach the screen."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            yield node.lineno, node.value


TAB_LABELS = ["📊 Dashboard", "📚 Sessions", "🎬 Weekend Recap",
              "📋 Roster (marked attendance)", "🔮 Forecast", "➕ Add data"]
SUB_TAB_LABELS = ["🔎 Browse", "🏆 This week", "🎓 Trainers"]


# ── pure helpers ─────────────────────────────────────────────────────────────
class TestTileGrid(unittest.TestCase):
    def test_grid_wraps_every_item_and_uses_auto_fit_columns(self):
        html = T.tiles_html([{"label": "A", "value": "1"}, {"label": "B", "value": "2"},
                             {"label": "C", "value": "3"}, {"label": "D", "value": "4"}])
        self.assertEqual(html.count('class="kpi"'), 4)
        self.assertIn("kpi-grid", html)
        self.assertIn("repeat(auto-fit,minmax(150px,1fr))", T.css())

    def test_value_and_label_are_printed_verbatim_and_escaped(self):
        html = T.tile_html("Learners <b>", "14,641")
        self.assertIn("14,641", html)
        self.assertIn("Learners &lt;b&gt;", html)
        self.assertNotIn("<b>", html)

    def test_delta_pill_is_green_for_a_rise_and_red_for_a_fall(self):
        self.assertIn('kpi-delta pos', T.tile_html("x", "1", delta="+2.0"))
        self.assertIn('kpi-delta neg', T.tile_html("x", "1", delta="-3.1%"))
        # 'inverse' flips the colour but not the arrow, like st.metric
        inv = T.tile_html("x", "1", delta="-3.1%", delta_color="inverse")
        self.assertIn('kpi-delta pos', inv)
        self.assertIn("▼", inv)

    def test_off_delta_and_sub_render_as_muted_text_not_a_pill(self):
        off = T.tile_html("x", "1", delta="all enrolled", delta_color="off")
        self.assertIn("kpi-sub", off)
        self.assertNotIn("kpi-delta", off)
        sub = T.tile_html("x", "1", sub="of batch strength")
        self.assertIn("of batch strength", sub)
        self.assertNotIn("kpi-delta", sub)

    def test_help_becomes_a_title_tooltip(self):
        html = T.tile_html("x", "1", help='pooled, "not" a mean')
        self.assertIn("kpi-help", html)
        self.assertIn('title="pooled, &quot;not&quot; a mean"', html)

    def test_no_delta_means_no_pill(self):
        html = T.tile_html("x", "1")
        self.assertNotIn("kpi-delta", html)
        self.assertNotIn("kpi-sub", html)


class TestPlainWords(unittest.TestCase):
    """Ruling F. The renames are written by hand at each label, so what is
    pinned here is (a) the two dynamic mappings that ship as code and (b) a
    lint of every string the two UI files would put on screen."""

    def test_index_reads_as_times_expected(self):
        self.assertEqual(T.fmt_index(1.039), "1.04× expected")
        self.assertEqual(T.fmt_index(None), "—")
        self.assertEqual(T.award_value({"award": "Beat the curve", "value": "1.05x"}),
                         "1.05× expected")
        self.assertEqual(T.award_value({"award": "Biggest room", "value": "1,204"}), "1,204")

    def test_no_jargon_in_any_on_screen_string(self):
        """Every string literal in the two UI files except docstrings, the
        CSV export's column keys, lower-case data keys and file names."""
        hits = []
        for name in ("attendance_app.py", "dash_view.py"):
            for lineno, text in _string_literals(os.path.join(ROOT, name)):
                if text in _CSV_KEYS or _DATA_KEY.fullmatch(text):
                    continue
                if text.endswith((".xlsx", ".zip", ".csv", ".py")):
                    continue
                if _JARGON.search(text):
                    hits.append(f"{name}:{lineno}: {text[:70]!r}")
        self.assertEqual(hits, [])


class TestChartAxes(unittest.TestCase):
    """Ruling D: the batch labels are pinned horizontal (tickangle 0) rather
    than left to Plotly, which rotates array ticks to 30° / 90° as the plot
    narrows; value labels sit only on the selected, best and worst bars."""

    def test_cross_batch_bar_pins_ticks_horizontal_and_labels_three_bars(self):
        import dash_view
        DATA = {"B17": {"avg_pct": 5.7}, "B40": {"avg_pct": 41.2},
                "B41": {"avg_pct": 30.0}, "ECAP B1": {"avg_pct": 22.5}}
        fig = dash_view._comparison_bar(DATA, sel="B41")
        self.assertEqual(fig.layout.xaxis.tickangle, 0)
        self.assertEqual(list(fig.layout.xaxis.ticktext), ["B17", "B40", "B41", "ECAP<br>B1"])
        self.assertEqual([t for t in fig.data[0].text if t], ["6%", "41%", "30%"])
        self.assertAlmostEqual(fig.layout.yaxis.range[1], 41.2 * 1.15)

    def test_weekend_bar_pins_ticks_horizontal(self):
        import dash_view
        fig = dash_view._weekend_bar([("B40", 12.0), ("ECAP B2", 3.5), ("B42", 20.0)])
        self.assertEqual(fig.layout.xaxis.tickangle, 0)
        self.assertEqual([t for t in fig.data[0].text if t], ["4%", "20%"])

    @staticmethod
    def _dates(labels):
        return {"sessions": [{"date_lbl": lb, "pct": 10.0 + i, "topic": "T",
                              "present": 1, "total": 10, "present_only": False}
                             for i, lb in enumerate(labels)]}

    def test_the_date_line_pins_its_ticks_and_thins_them_to_a_budget(self):
        """Ruling D again, on the chart the first tab exists to show. This one
        passed NO xaxis at all, so 21 of the 29 batches — the ones with 17 to
        36 dated sessions — inherited Plotly's autotickangles [0, 30, 90] and
        stood every date label on its end. The two bars beside it were pinned;
        this was not. Thinning beats rotating here because the sessions table
        below is this chart's table-view twin: no value is only on the axis."""
        import dash_view
        fig = dash_view._date_line(self._dates(
            [f"{d} May" for d in range(1, 37)]))
        self.assertEqual(fig.layout.xaxis.tickangle, 0)
        ticks = list(fig.layout.xaxis.tickvals or ())
        self.assertLessEqual(len(ticks), dash_view._TICK_BUDGET)
        self.assertEqual(ticks[0], "1 May", "the first date must carry a tick")
        self.assertTrue(set(ticks) <= {f"{d} May" for d in range(1, 37)})

    def test_the_newest_date_keeps_its_label(self):
        """The thinning's first cut anchored at index 0 (`uniq[::stride]`), so
        the LAST label survived only when (len(uniq)-1) % stride == 0 — and
        with tickmode="array" an unlisted tick simply does not render. 13 of
        the 29 batches lost the newest point that way, B17/B19/B24/B26 by a
        full week. The right-hand end is the end a time series is read from,
        so it is anchored there now; the first is kept as well."""
        import dash_view
        # 36 dates, budget 14 -> stride 3: 35 % 3 != 0, the case that failed.
        labels = [f"{d} May" for d in range(1, 37)]
        fig = dash_view._date_line(self._dates(labels))
        ticks = list(fig.layout.xaxis.tickvals)
        self.assertEqual(ticks[-1], "36 May", "the newest date lost its label")
        self.assertEqual(ticks[0], "1 May", "the oldest date lost its label")
        self.assertLessEqual(len(ticks), dash_view._TICK_BUDGET)
        self.assertEqual(ticks, sorted(ticks, key=labels.index),
                         "the labels must stay in date order")

    def test_thinning_never_moves_the_line_itself(self):
        """Thinning is an AXIS operation: the trace keeps one point per class
        session, in order, with its hover — that is what makes dropping labels
        an acceptable cure for rotation in the first place."""
        import dash_view
        labels = [f"{d} May" for d in range(1, 37)]
        fig = dash_view._date_line(self._dates(labels))
        self.assertEqual(list(fig.data[0].x), labels)
        self.assertEqual(len(fig.data[0].y), len(labels))

    def test_a_short_batch_keeps_every_one_of_its_date_labels(self):
        """Thinning is a ceiling, not a stride applied for its own sake."""
        import dash_view
        labels = ["6 Sep", "13 Sep", "20 Sep", "27 Sep"]
        fig = dash_view._date_line(self._dates(labels))
        self.assertEqual(list(fig.layout.xaxis.tickvals), labels)

    def test_repeated_dates_count_once_towards_the_tick_budget(self):
        """A batch with domains sends several rows per date (B35: 64 rows over
        13 dates). Budgeting on ROWS would thin 13 real labels down to 2."""
        import dash_view
        labels = [f"{d} Sep" for d in range(1, 14) for _ in range(5)]
        fig = dash_view._date_line(self._dates(labels))
        # 65 rows, 13 dates, a budget of 14: every date keeps its tick.
        # Budgeting on rows would have thinned these 13 down to 2.
        self.assertEqual(list(fig.layout.xaxis.tickvals),
                         [f"{d} Sep" for d in range(1, 14)])

    def test_every_chart_inherits_flat_ticks_by_default(self):
        """The reason the date line could regress at all: the shared layout let
        the axis fall through to Plotly's "auto". Pinned in the base now, so a
        chart added later cannot inherit the bug."""
        self.assertEqual(T.plotly_layout(200)["xaxis"]["tickangle"], 0)
        merged = T.plotly_layout(200, xaxis=dict(tickfont=dict(size=10)))
        self.assertEqual(merged["xaxis"]["tickangle"], 0)
        self.assertEqual(merged["xaxis"]["tickfont"]["size"], 10)

    def test_the_week_by_week_line_is_flat_too(self):
        import dash_view
        fig = dash_view._weeks_line([{"week": "2026-09-21", "pct": 15.7},
                                     {"week": "2026-09-14", "pct": 14.1}])
        self.assertEqual(fig.layout.xaxis.tickangle, 0)

    @unittest.skipUnless(os.path.exists(STORE), "no local prebuilt store")
    def test_no_batch_in_the_store_loses_its_newest_date_label(self):
        """The synthetic case above pins the rule; this one pins that the rule
        holds for the shapes the store actually has. Every batch's figure is
        built by the shipped helper and read back off the figure."""
        import json
        import duckdb
        import dash_view
        con = duckdb.connect(STORE, read_only=True)
        DATA = json.loads(con.execute(
            "SELECT value FROM meta WHERE key = 'DATA'").fetchone()[0])
        con.close()
        lost_newest, lost_first, over_budget, checked = [], [], [], 0
        for code, d in DATA.items():
            sess = [s for s in d["sessions"] if not s.get("is_intro")]
            if not sess:
                continue
            checked += 1
            uniq = list(dict.fromkeys(s["date_lbl"] for s in sess))
            ticks = list(dash_view._date_line(d).layout.xaxis.tickvals)
            if ticks[-1] != uniq[-1]:
                lost_newest.append(f"{code}: ends {uniq[-1]}, last label {ticks[-1]}")
            if ticks[0] != uniq[0]:
                lost_first.append(code)
            if len(ticks) > dash_view._TICK_BUDGET:
                over_budget.append(f"{code}: {len(ticks)}")
            self.assertTrue(set(ticks) <= set(uniq), f"{code}: invented a tick")
        self.assertGreater(checked, 20, "the store fed almost no date lines")
        self.assertEqual(lost_newest, [])
        self.assertEqual(lost_first, [])
        self.assertEqual(over_budget, [])


class TestLatestWeekend(unittest.TestCase):
    def test_sunday_newest_gives_that_saturday_and_sunday(self):
        got = T.latest_weekend(["2026-09-19", "2026-09-26", "2026-09-27"])
        self.assertEqual(got, (dt.date(2026, 9, 26), dt.date(2026, 9, 27)))
        self.assertEqual(T.weekend_label(*got), "26–27 Sep 2026")

    def test_saturday_newest_still_names_the_whole_weekend(self):
        got = T.latest_weekend(["2026-09-20", "2026-09-26"])
        self.assertEqual(got, (dt.date(2026, 9, 26), dt.date(2026, 9, 27)))

    def test_a_newer_midweek_date_does_not_move_the_weekend(self):
        got = T.latest_weekend(["2026-09-26", "2026-09-27", "2026-09-30"])
        self.assertEqual(got, (dt.date(2026, 9, 26), dt.date(2026, 9, 27)))

    def test_no_weekend_dates_means_none(self):
        self.assertIsNone(T.latest_weekend(["2026-09-30", None, ""]))
        self.assertIsNone(T.latest_weekend([]))

    def test_label_across_a_month_boundary(self):
        self.assertEqual(T.weekend_label(dt.date(2026, 8, 29), dt.date(2026, 8, 30)),
                         "29–30 Aug 2026")
        self.assertEqual(T.weekend_label(dt.date(2026, 10, 31), dt.date(2026, 11, 1)),
                         "31 Oct – 1 Nov 2026")

    def test_ecap_tick_wraps_instead_of_rotating(self):
        self.assertEqual(T.ecap_tick("ECAP B1"), "ECAP<br>B1")
        self.assertEqual(T.ecap_tick("B40"), "B40")


class TestThemePlumbing(unittest.TestCase):
    def test_config_toml_uses_the_brand_accent_and_holds_no_secret(self):
        """`primaryColor` is parsed, not just searched for: the accent is a
        placeholder until the owner sends a colour, and the two files have to
        move together or half the page keeps the old one."""
        path = os.path.join(ROOT, ".streamlit", "config.toml")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r'primaryColor\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(m, "config.toml sets no primaryColor")
        self.assertEqual(m.group(1).lower(), T.BRAND["accent"].lower())
        for forbidden in ("password", "private_key", "token", "api_key", "[drive]",
                          "[gcp_service_account]"):
            self.assertNotIn(forbidden, text.lower())

    def test_the_accent_wash_is_derived_from_the_accent(self):
        """It used to be a second hand-written rgba() of the same channels, so
        "the owner sends a colour" was a two-place edit with one of the two
        silent when missed."""
        self.assertEqual(T.BRAND["accent_soft"], T._soft(T.BRAND["accent"]))
        self.assertEqual(T._soft("#2a78d6"), "rgba(42,120,214,0.1)")
        self.assertEqual(T._soft("#ffffff", 0.5), "rgba(255,255,255,0.5)")

    def test_no_text_is_painted_in_the_series_colour(self):
        """ui_theme's one rule about itself, stated beside INK. The shared-room
        chip broke it: accent ink on the accent's 10% wash measured 3.89:1,
        under AA's 4.5 for 11px — and that chip is the only thing telling two
        identically-titled sessions apart, so it is data, not chrome."""
        sheet = T.css()
        self.assertNotRegex(sheet, r"[^-]color:\s*" + re.escape(T.BRAND["accent"]))
        chip = next(r for r in T.dash_rules(".dash ").split("}") if ".chip{" in r)
        self.assertNotIn(T.BRAND["accent"], chip)
        self.assertIn(T.INK["secondary"], chip)

    def test_no_inline_style_paints_text_in_the_series_colour_either(self):
        """The guard above lints ui_theme's stylesheet ONLY, and the Weekend
        Recap kicker slipped straight past it: an inline `color:` in
        attendance_app.py, fed from T.BRAND so the bare-hex lint below missed
        it too. Accent ink on the accent's own 10% wash measured the same
        3.89:1 the chip did. The wash may stay — as a SURFACE. Only `color:`
        is forbidden; `background:` and Plotly's `color=` are the mark."""
        # The accent reaches an inline style as a TOKEN, never as a hex, so
        # this reads the source rather than the parsed literals: an f-string
        # splits around `{T.BRAND['accent']}` and the name lands outside the
        # literal. Adjacent literals are joined first, for a style broken
        # across two source lines.
        pat = re.compile(r"[^-\w]color:\s*\{?\s*"
                         r"(T\.BRAND\[[\"']accent[\"']\]|_ACCENT\b)")
        for name in ("attendance_app.py", "dash_view.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                src = fh.read()
            flat = re.sub(r"[\"']\s*\n\s*[frb]*[\"']", "", src)
            hit = pat.search(flat)
            self.assertIsNone(hit, f"{name} sets an inline TEXT colour to the "
                                   f"accent: {hit.group(0) if hit else ''}")

    def test_the_recessive_text_tokens_stay_readable(self):
        """The whole muted family was set by eye and landed under AA at the
        sizes it renders at (.muted 3.2:1 at 12px, the axis ink 3.6:1 at 11px).
        These are the floors it was raised to; loosening one again is a
        deliberate act, not a typo."""
        sheet = T.css()
        self.assertIn(".dash .muted{opacity:.70;}", sheet)
        self.assertIn("opacity:.72", sheet)                  # .kpi-sub
        self.assertEqual(T.INK["muted"], "#6f6d67")
        with open(os.path.join(ROOT, "dash_view.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("font-size:10px", src,
                         "a muted inline style is back below 11px")

    def test_ui_files_carry_no_hex_colour_of_their_own(self):
        """R2: a rebrand edits ui_theme.BRAND / STATUS and nothing else — the
        Roster tab's Present / Absent cells included."""
        for name in ("attendance_app.py", "dash_view.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                hexes = re.findall(r"#[0-9a-fA-F]{6}\b", fh.read())
            self.assertEqual(hexes, [], f"{name} paints with its own hex: {hexes}")

    def test_brand_has_the_agreed_slots(self):
        """The owner's wordmark arrived 2026-09-30, so `logo_path` (the single
        placeholder) is now the three real slots: a black file for a light
        surface, a white one for a dark surface, and the cropped disc used as
        the tab icon. They are separate keys because the white file is
        white-on-transparent and is invisible on white — picking the wrong one
        is a blank header, not a tinted one."""
        for k in ("accent", "accent_soft", "positive", "negative", "neutral",
                  "font", "logo_light", "logo_dark", "favicon"):
            self.assertIn(k, T.BRAND)

    def test_every_brand_asset_exists_on_disk(self):
        """A missing file would make `set_page_config` raise and take the whole
        app down for a cosmetic asset, so the app falls back — but if the files
        are gone from the repo we want a red test, not a silent emoji."""
        for k in ("logo_light", "logo_dark", "favicon"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, T.BRAND[k])),
                            f"BRAND[{k!r}] = {T.BRAND[k]!r} is not on disk")

    def test_site_css_is_scoped_and_app_css_is_one_block(self):
        import dash_view
        self.assertIn(".aicap .pill", dash_view._CSS)
        self.assertEqual(T.css().count("<style>"), 1)
        self.assertIn(".dash .pill", T.css())

    def test_the_app_never_scopes_a_rule_under_the_site_wrapper(self):
        """P1. The dashboard's rules used to be emitted under `.aicap` while
        the app opened `<div class="aicap">` in one `st.markdown` and closed it
        115 lines later in another — and each `st.markdown` is its own DOM
        fragment, so the div was empty and NONE of the rules ever matched.

        The fix keeps the prefix parameterised rather than dropping it: a rule
        with no container at all would be a global `.pill` / `table.sess`
        leaking into Streamlit's own DOM and into the Browse tab's table, which
        is the second half of the same bug. So the app scopes under `.dash`, the
        static site keeps `.aicap` (`site_templates/dashboard.html` wraps the
        whole page in one string, where the split cannot happen), and what is
        pinned here is that the SITE's wrapper never reaches the app.
        """
        self.assertNotIn(".aicap", T.css())
        for name in ("attendance_app.py",):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                self.assertNotIn("aicap", fh.read().replace("aicap_", ""))

    def test_a_dashboard_fragment_opens_and_closes_its_wrapper_in_one_write(self):
        """The actual P1 defect: the wrapper has to be opened and closed by the
        SAME st.markdown, or the styled content sits outside it."""
        import dash_view
        wrote = []

        class _Stub:
            @staticmethod
            def markdown(body, **kw):
                wrote.append(body)

        dash_view._dash(_Stub, '<span class="pill">41%</span>')
        self.assertEqual(len(wrote), 1, "the wrapper was split across writes")
        self.assertTrue(wrote[0].startswith('<div class="dash">'))
        self.assertTrue(wrote[0].endswith("</div>"))
        self.assertIn('class="pill"', wrote[0])

    def test_browse_table_rules_are_scoped_to_their_own_container(self):
        """The Browse tab's `table.sess` rules were global and restyled the
        Dashboard's own sessions table (12px, no wrapping). Both tables are
        called `sess`; what keeps them apart is the container."""
        with open(os.path.join(ROOT, "attendance_app.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(".sess-wrap table.sess", src)
        # Every rule for either table names its container first.
        for text, scope in ((src, ".sess-wrap"), (T.css(), ".dash")):
            rules = re.findall(r"([^\n{};]*?)table\.sess[^\n{};]*\{", text)
            self.assertTrue(rules)
            for sel in rules:
                self.assertIn(scope, sel, f"unscoped table.sess rule: {sel!r}")

    def test_header_wordmark_is_sized_by_the_marks_own_width(self):
        """P11. Both logo files are the mark centred in a 1080x1080 canvas with
        30% dead space above and below, so `width:140px` alone buys a 140px-TALL
        block that is mostly padding. BRAND carries the measured ink rect and
        ui_theme does the arithmetic."""
        self.assertEqual(T.BRAND["logo_width_px"], 140)
        vw, vh = T.BRAND["logo_viewbox"]
        ix, iy, iw, ih = T.BRAND["logo_ink"]
        self.assertTrue(0 < ix and 0 < iy and ix + iw <= vw and iy + ih <= vh)
        css = T.css()
        self.assertIn("--mark-w:140px", css)
        # the box is the INK's aspect, not the file's
        self.assertIn(f"{ih / iw:.5f}", css)
        self.assertIn(".brandbar .mark img", css)

    def test_delta_text_carries_the_unit_of_the_figure_that_moved(self):
        """P8. One helper replaces `_d` (This week) and `_dtxt` (Weekend
        Recap); the latter printed a bare "+3" under both NPS and Learners."""
        self.assertEqual(T.delta_text("nps", 3), "+3 pts")
        self.assertEqual(T.delta_text("present", 1234), "+1,234")
        self.assertEqual(T.delta_text("pct", -1.23), "-1.2%")
        self.assertEqual(T.delta_text("index", 0.04), "+0.04×")
        self.assertEqual(T.delta_text("rating", 0.125), "+0.12")
        self.assertEqual(T.delta_text("stickiness", -4), "-4.0 pts")
        # Stickiness is a percentage of a percentage, and the store carries it
        # to a decimal: the weeks it is read against are 59.1% and 60.4%, so
        # rounding +1.3 to "+1 pts" understates the move by a quarter. NPS is a
        # whole-point figure and stays whole.
        self.assertEqual(T.delta_text("stickiness", 1.3), "+1.3 pts")
        self.assertEqual(T.delta_text("nps", 3.4), "+3 pts")
        # None is not zero: a first week has no comparison, so it gets no pill.
        self.assertIsNone(T.delta_text("nps", None))
        self.assertIsNone(T.delta_text("nps", "n/a"))
        # An unknown figure degrades rather than raising.
        self.assertEqual(T.delta_text("whatever", 2), "+2.0")

    def test_a_plain_subtitle_draws_no_arrow(self):
        """P3. `delta_color='off'` only greyed st.metric's arrow, it did not
        remove it, so every subtitle came with a meaningless ▼/▲."""
        for html in (T.tile_html("x", "1", sub="all enrolled"),
                     T.tile_html("x", "1", delta="all enrolled", delta_color="off")):
            self.assertNotIn("▲", html)
            self.assertNotIn("▼", html)
        self.assertIn("▲", T.tile_html("x", "1", delta="+2"))

    def test_no_st_metric_is_left_in_the_ui_files(self):
        """P3 the other way round: nothing can regress to st.metric and get an
        arrow back, because the tile helper is now the only stat surface."""
        for name in ("attendance_app.py", "dash_view.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
                self.assertNotIn("st.metric(", fh.read())


class TestSessionsTable(unittest.TestCase):
    """dash_view's drill-in table — pure HTML, no Streamlit."""

    @staticmethod
    def _d(sessions, code="B35"):
        return {"code": code, "sessions": sessions}

    @staticmethod
    def _cells(html, tag):
        """Count real <th>/<td> cells — `<thead>` also starts with '<th'."""
        return len(re.findall(rf"<{tag}[ >]", html))

    def test_the_intro_row_has_one_cell_per_header(self):
        """P2. The intro-call row emitted 7 cells under 9 headers, so every
        column after Present was shifted left by two for that one row."""
        import dash_view
        html = dash_view.sessions_table_html(self._d([{
            "is_intro": True, "date_lbl": "Intro call", "topic": "Intro call",
            "present": 1753, "total": 2461,
        }]))
        n_head = self._cells(html, "th")
        body = html.split("<tbody>")[1]
        self.assertEqual(n_head, 9)
        self.assertEqual(self._cells(body, "td"), n_head)

    def test_a_class_row_has_one_cell_per_header_too(self):
        import dash_view
        html = dash_view.sessions_table_html(self._d([{
            "date_lbl": "27 Sep", "topic": "Prompt Engineering", "present": 400,
            "absent": 2061, "total": 2461, "pct": 16.3, "present_only": False,
            "no_l2": False, "rating": None,
        }]))
        body = html.split("<tbody>")[1]
        self.assertEqual(self._cells(body, "td"), self._cells(html, "th"))

    def test_an_undivided_shared_poll_says_it_is_the_rooms(self):
        """P7. An anonymous export cannot be split per batch, so every sharing
        batch shows the SAME number. Unlabelled it reads as this batch's own."""
        import dash_view
        s = {"date_lbl": "4 Jul", "topic": "Finance", "present": 10, "absent": 1,
             "total": 11, "pct": 90.9, "present_only": False, "no_l2": False,
             "rating": 4.17, "rating_n": 220,
             "shared_batches": ["B15", "B16", "B17"],
             "rating_shared": {"split": False, "reason": "no-emails"}}
        html = dash_view.sessions_table_html(self._d([s], code="B17"))
        self.assertIn("room poll, shared with B15, B16", html)
        # its own batch is not in the list of who else was in the room
        self.assertNotIn("shared with B15, B16, B17", html)

    def test_a_divided_poll_and_a_lone_room_carry_no_note(self):
        import dash_view
        base = {"date_lbl": "4 Jul", "topic": "Finance", "present": 10,
                "absent": 1, "total": 11, "pct": 90.9, "present_only": False,
                "no_l2": False, "rating": 4.17, "rating_n": 220}
        split = dict(base, shared_batches=["B16", "B17"],
                     rating_shared={"split": True})
        alone = dict(base, shared_batches=[], rating_shared={})
        for s in (split, alone):
            self.assertNotIn("room poll", dash_view.sessions_table_html(
                self._d([s], code="B17")))

    @staticmethod
    def _rolled(code="B17"):
        """One batch as the Dashboard OPENS on it: no domain picked, so the
        rows are rebuilt from `by_date` by `pod_view(d, None)`."""
        import dash_view
        s = {"mm": "2026-07-04", "date_lbl": "4 Jul", "topic": "Finance",
             "present": 10, "absent": 1, "total": 11, "pct": 90.9,
             "present_only": False, "no_l2": False, "is_intro": False,
             "rating": 4.17, "rating_n": 220, "pod": "", "l2_batch": "",
             "mentor": "R", "rating_dist": {}, "shared_batches": ["B15", "B16"],
             "rating_shared": {"split": False, "reason": "no-emails"}}
        d = {"code": code, "sessions": [s],
             "by_date": [{"mm": "2026-07-04", "date_lbl": "4 Jul", "present": 10,
                          "total": 11, "pct": 90.9, "n_pods": 1}]}
        return d, s, dash_view.pod_view(d, None)

    def test_the_default_view_marks_an_undivided_room_poll_too(self):
        """P7's last clause shipped only half reachable. With no domain picked
        the drill-in rebuilds every row from `by_date`, and that rollup carried
        `shared_batches` but not `rating_shared` — so the note returned "" on
        the one page nearly everyone looks at. It bites hardest where there is
        no way round it: B17-B34 have no domains, hence no domain selector, so
        for them the default view is the ONLY view."""
        import dash_view
        _d, _s, rolled = self._rolled()
        self.assertIn("room poll, shared with B15, B16",
                      dash_view.sessions_table_html(rolled))

    def test_a_rollup_of_several_domain_rooms_claims_no_room_poll(self):
        """The other half of the same call: several rooms on one date average
        by responses, and calling a weighted mean "the room's poll" would be a
        different wrong claim, so the provenance is dropped above n == 1."""
        import dash_view
        d, s, _r = self._rolled()
        d = dict(d, sessions=[s, dict(s, pod="Students", rating=4.4)],
                 by_date=[dict(d["by_date"][0], n_pods=2)])
        rolled = dash_view.pod_view(d, None)
        self.assertEqual(rolled["sessions"][0]["rating_shared"], {})
        self.assertNotIn("room poll", dash_view.sessions_table_html(rolled))

    def test_the_closing_panel_says_whole_batch_in_a_domain_view(self):
        """P7. `closing` is a BATCH fact that pod_view carries through
        unchanged, so under a domain heading it has to name its own scope."""
        import dash_view
        import pods
        plain_t, plain_s = dash_view.closing_title(None)
        self.assertEqual(plain_t, "Closing types")
        self.assertNotIn("whole batch", plain_s)
        for scope in ("Finance", pods.WHOLE_BATCH, pods.COMMON):
            t, s = dash_view.closing_title(scope)
            self.assertIn("whole batch", t)
            self.assertIn("whole batch", s)

    def test_both_pod_sentinels_have_a_label(self):
        """P7. `pods.WHOLE_BATCH` is the string '*all*' and was printed raw."""
        import dash_view
        import pods
        for sentinel in (pods.WHOLE_BATCH, pods.COMMON):
            self.assertIn(sentinel, dash_view._SCOPE_LABEL)
            self.assertNotIn("*", dash_view._SCOPE_LABEL[sentinel])


# ── fragments, read off the source ───────────────────────────────────────────
# Fragment isolation cannot be observed through AppTest: it builds a fresh
# LocalScriptRunner (and therefore a fresh MemoryFragmentStorage) for every
# `.run()` and never passes `fragment_id_queue`, so every AppTest interaction
# is a FULL script rerun and all seven bodies repaint whatever the decorators
# say. The counter the app keeps (`_painted`) is still asserted below — it
# proves each body runs exactly once per pass, which is the precondition — and
# what proves the wrapping itself is this AST read of the source.
_FRAGMENT_BODIES = {"_tab_dashboard", "_tab_roster", "_tab_forecast",
                    "_sub_browse", "_sub_this_week", "_sub_trainers",
                    "_tab_weekend"}


def _decorated_fragments(path):
    """Names of module-level functions decorated with @st.fragment."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    out = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Attribute) and dec.attr == "fragment"
                    and isinstance(dec.value, ast.Name) and dec.value.id == "st"):
                out.add(node.name)
    return out


class TestFragments(unittest.TestCase):
    """P9. Every tab body was top-level script, so any widget anywhere re-ran
    all six tabs and all three sub-tabs — the Roster grid rebuilt because
    somebody changed the Dashboard's batch."""

    def test_every_tab_and_sub_tab_body_is_a_fragment(self):
        got = _decorated_fragments(APP)
        self.assertEqual(got, _FRAGMENT_BODIES)
        self.assertGreaterEqual(len(got), 6)

    def test_the_gate_the_loader_and_add_data_stay_in_the_main_script(self):
        """The password gate must run before anything else on every pass, the
        store load is shared by all the fragments, and the Add-data tab
        dispatches a build and then clears the cache for the whole page — none
        of the three can live inside a fragment."""
        with open(APP, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), APP)
        frag_spans = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in _FRAGMENT_BODIES:
                frag_spans.append((node.lineno, node.end_lineno))

        def inside_a_fragment(lineno):
            return any(a <= lineno <= b for a, b in frag_spans)

        calls = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.setdefault(node.func.id, []).append(node.lineno)
        self.assertTrue(calls.get("_password_ok"))
        for name in ("_password_ok", "_load_store"):
            for lineno in calls.get(name, ()):
                self.assertFalse(inside_a_fragment(lineno),
                                 f"{name}() is inside a fragment at line {lineno}")
        with open(APP, encoding="utf-8") as fh:
            add_line = next(i + 1 for i, ln in enumerate(fh)
                            if ln.startswith("with tab_add:"))
        self.assertFalse(inside_a_fragment(add_line))

    def test_the_dashboards_own_rerun_is_fragment_scoped(self):
        """Clicking a bar reruns to repaint the accent. Left at app scope that
        one click re-ran every other tab, which is most of what the fragments
        were added to stop. dash_view keeps "app" as its default so a caller
        outside a fragment still works — `st.rerun(scope="fragment")` raises
        there, and it cannot be caught, because the rerun IS an exception."""
        with open(APP, encoding="utf-8") as fh:
            self.assertIn('rerun_scope="fragment"', fh.read())
        with open(os.path.join(ROOT, "dash_view.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("st.rerun(scope=rerun_scope)", src)
        self.assertIn('rerun_scope: str = "app"', src)


# ── the running app ──────────────────────────────────────────────────────────
def _walk(block, path=()):
    """Yield (node, path-of-container-labels) for every node under `block`."""
    for child in block.children.values():
        label = getattr(child, "label", None)
        here = path + ((label,) if label else ())
        yield child, here
        if hasattr(child, "children"):
            yield from _walk(child, here)


@unittest.skipUnless(os.path.exists(STORE), "no local prebuilt store")
class TestAppLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(APP, default_timeout=300)
        # The gate returns early for an authenticated session; no password.
        at.session_state["_authed"] = True
        # Hermetic: a non-empty dict replaces st.secrets, so no Drive id, no
        # service account — the app runs in local-only store mode.
        at.secrets = {"drive": {}}
        cls.at = at.run()

    def test_runs_without_an_exception(self):
        self.assertFalse(self.at.exception, [e.value for e in self.at.exception])

    def test_all_six_tabs_and_the_three_sub_tabs_render(self):
        labels = [t.label for t in self.at.tabs]
        for want in TAB_LABELS + SUB_TAB_LABELS:
            self.assertIn(want, labels)

    def test_last_weekend_section_leads_the_dashboard(self):
        dash = next(t for t in self.at.tabs if t.label == "📊 Dashboard")
        md = [m.value for m in dash.markdown]
        titles = [m for m in md if "Last weekend" in m]
        self.assertTrue(titles, "no 'Last weekend' title on the Dashboard tab")
        self.assertRegex(titles[0], r"Last weekend · \d+[––]?\d* \w+ \d{4} · \d+ sessions · \d+ batches")
        # It comes BEFORE the all-time KPI tiles.
        first_kpi = next(i for i, m in enumerate(md) if 'class="kpi-grid"' in m)
        first_title = next(i for i, m in enumerate(md) if "Last weekend" in m)
        self.assertLess(first_title, first_kpi)

    def test_last_weekend_numbers_match_the_recap(self):
        """The title's session and batch counts are the same figures the
        This-week tab's recap carries for that week."""
        import json
        import duckdb
        import recap
        con = duckdb.connect(STORE, read_only=True)
        rows = {k: json.loads(v) for k, v in con.execute(
            "SELECT key, value FROM meta WHERE key IN ('sessions','recap')").fetchall()}
        con.close()
        wk = T.latest_weekend(s["date"] for s in rows["sessions"])
        import dash_view
        agg = recap._agg(dash_view.weekend_rows(rows["sessions"], *wk))
        dash = next(t for t in self.at.tabs if t.label == "📊 Dashboard")
        title = next(m.value for m in dash.markdown if "Last weekend" in m.value)
        self.assertIn(f"{agg['sessions']} sessions", title)
        self.assertIn(f"{len(agg['batches'])} batches", title)

    def test_download_button_is_in_the_roster_tab_and_not_above_the_tabs(self):
        marked = ("marked roster",)
        inside_roster, above_tabs = [], []
        for node, path in _walk(self.at.main):
            if getattr(node, "type", None) not in ("button", "download_button"):
                continue
            label = str(getattr(node, "label", "") or "")
            if not any(m in label.lower() for m in marked):
                continue
            if "📋 Roster (marked attendance)" in path:
                inside_roster.append(label)
            elif not any(p in TAB_LABELS for p in path):
                above_tabs.append(label)
        self.assertTrue(inside_roster, "no marked-roster button in the Roster tab")
        self.assertEqual(above_tabs, [])

    def test_no_success_banner_above_the_tabs(self):
        self.assertEqual([s.value for s in self.at.success], [])
        stamp = [c.value for c in self.at.caption
                 if "Data as of" in c.value]
        self.assertTrue(stamp)
        self.assertNotIn("carried forward", stamp[0])

    def _all_text(self):
        """Every string this page put on screen, main and sidebar.

        `.value` on a widget element reads session state and raises for one
        that has no entry (a plotly_chart with no selection), so each read is
        guarded — an element with no readable value simply has no text. The
        strings of ONE node are de-duplicated, because a markdown's `.value`
        and `.body` are the same string and counting both would make a single
        caption look like two.
        """
        out = []
        for root in (self.at.main, self.at.sidebar):
            for node, _path in _walk(root):
                here = set()
                for attr in ("value", "label", "body"):
                    try:
                        v = getattr(node, attr, None)
                    except Exception:
                        continue
                    if isinstance(v, str):
                        here.add(v)
                out.extend(sorted(here))
        return out

    def test_the_build_stamp_is_printed_exactly_once(self):
        """P4. "Data as of" was printed three times on first paint — the
        sidebar's Admin caption, the caption under the title, and the
        Dashboard's own header — so three places had to be kept in step and a
        screenshot of any one of them was ambiguous."""
        texts = self._all_text()
        self.assertEqual(sum(t.count("Data as of") for t in texts), 1)
        self.assertEqual(sum(t.lower().count("data as of") for t in texts), 1)
        stamp = next(t for t in texts if "Data as of" in t)
        self.assertRegex(stamp, r"Data as of .+ · sessions through \d+ \w+ \d{4}")

    def test_the_stamp_names_the_newest_session_in_the_store(self):
        """The second half of the wording is the question people actually ask
        of a dashboard: is last weekend in it? The build time alone does not
        answer that — a run that fetched nothing has a fresh build time."""
        import datetime as _d
        import json
        import duckdb
        con = duckdb.connect(STORE, read_only=True)
        rows = json.loads(con.execute(
            "SELECT value FROM meta WHERE key='sessions'").fetchone()[0])
        con.close()
        newest = max(str(s["date"]) for s in rows)
        want = f"{_d.date.fromisoformat(newest[:10]):%d %b %Y}"
        stamp = next(t for t in self._all_text() if "Data as of" in t)
        self.assertIn(f"sessions through {want}", stamp)

    def test_the_refresh_note_splits_new_remarked_and_frozen(self):
        """P5. `attendance_core` tags every report row NEW / re-mark / frozen
        and the pipeline counts the third separately. The caption lumped frozen
        in with re-marked, so the live store — 36 frozen columns, nothing
        re-marked — announced "36 re-marked" on a run that marked nothing."""
        import json
        import duckdb
        con = duckdb.connect(STORE, read_only=True)
        report = json.loads(con.execute(
            "SELECT value FROM meta WHERE key='report'").fetchone()[0])
        con.close()
        want = {k: sum(1 for r in report if r.get("kind") == k)
                for k in ("NEW", "re-mark", "frozen")}
        stamp = next(t for t in self._all_text() if "last refresh" in t)
        self.assertIn(f"{want['NEW']} new", stamp)
        self.assertIn(f"{want['re-mark']} re-marked", stamp)
        self.assertIn(f"{want['frozen']} frozen", stamp)
        self.assertRegex(
            stamp,
            r"last refresh: \d+ new, \d+ re-marked, \d+ frozen session column\(s\)")

    def test_the_stores_own_warnings_are_listed_in_admin(self):
        """P6. `store["warnings"]` was read into a variable and rendered
        nowhere, so nobody has ever seen one. They belong with the operational
        detail, not as yellow boxes over the numbers."""
        import json
        import duckdb
        con = duckdb.connect(STORE, read_only=True)
        warns = json.loads(con.execute(
            "SELECT value FROM meta WHERE key='warnings'").fetchone()[0])
        con.close()
        if not warns:
            self.skipTest("this store published no warnings")
        admin = next(e for e in self.at.sidebar.expander if e.label == "Admin")
        text = " ".join(c.value for c in admin.caption)
        self.assertIn(f"Notes from the last refresh ({len(warns)})", text)
        for w in warns:
            self.assertIn(str(w), text)
        # and NOT as a page-level warning
        self.assertNotIn(str(warns[0]), " ".join(
            w.value for w in self.at.warning))

    def test_no_sentinel_string_reaches_the_screen(self):
        """P7. `pods.WHOLE_BATCH` is literally the string '*all*' and the
        drill-in heading printed it raw when "All Domains" was picked."""
        import pods
        for t in self._all_text():
            self.assertNotIn(pods.WHOLE_BATCH, t)

    def test_below_the_curve_names_the_weeks_worst_three(self):
        """P8. "Beat the curve" named the week's best session against
        expectation and nothing named the worst, so a session that drew half
        what its cohort normally does left no trace on the page."""
        import json
        import duckdb
        con = duckdb.connect(STORE, read_only=True)
        meta = {k: json.loads(v) for k, v in con.execute(
            "SELECT key, value FROM meta WHERE key IN ('sessions','recap')"
        ).fetchall()}
        con.close()
        week = meta["recap"]["weeks"][-1]["week"]
        rows = [r for r in meta["sessions"] if r.get("week") == week]
        worst = sorted((r for r in rows if r.get("index") is not None),
                       key=lambda r: r["index"])[:3]
        recap = next(t for t in self.at.tabs if t.label == "🎬 Weekend Recap")
        heads = [h.value for h in recap.subheader]
        self.assertIn("Below the curve", heads)
        cards = " ".join(m.value for m in recap.markdown
                         if "BELOW THE CURVE" in m.value)
        self.assertEqual(cards.count("BELOW THE CURVE"), len(worst))
        for r in worst:
            self.assertIn(T.fmt_index(r["index"]), cards)

    def test_both_leaderboards_rank_on_the_same_key(self):
        """P8, owner's ruling. Weekend Recap sorted its trainer board on the
        raw poll score while Sessions›This week sorted on vs expected, so the
        same week had two different best trainers depending on which tab you
        opened."""
        import json
        import duckdb
        import trainers as TR
        con = duckdb.connect(STORE, read_only=True)
        meta = {k: json.loads(v) for k, v in con.execute(
            "SELECT key, value FROM meta WHERE key IN ('sessions','recap')"
        ).fetchall()}
        con.close()
        week = meta["recap"]["weeks"][-1]["week"]
        rows = [r for r in meta["sessions"] if r.get("week") == week]
        board = TR.build(rows).get("trainers") or []
        board = sorted(board, key=lambda t: (t.get("index") is None,
                                             -(t.get("index") or 0)))
        this_week = meta["recap"]["leaderboard"]
        if not (board and this_week):
            self.skipTest("no trainer board for the latest week")
        self.assertEqual(board[0]["trainer"], this_week[0]["mentor"])

        recap = next(t for t in self.at.tabs if t.label == "🎬 Weekend Recap")
        frames = [d.value for d in recap.dataframe
                  if "Trainer" in getattr(d.value, "columns", [])]
        lb = next(f for f in frames if "vs expected" in f.columns)
        self.assertEqual(lb.iloc[0]["Trainer"], board[0]["trainer"])

    def test_browse_carries_the_vs_expected_column(self):
        """P8. The ranking key the whole app sorts on was missing from the one
        table people actually sort, so Browse could only be ordered on raw
        attendance — which always favours the youngest cohort in view."""
        browse = next(t for t in self.at.tabs if t.label == "🔎 Browse")
        frames = [d.value for d in browse.dataframe
                  if "Att %" in getattr(d.value, "columns", [])]
        self.assertTrue(frames)
        self.assertIn("vs expected", list(frames[0].columns))

    def test_roster_opens_on_the_newest_batch(self):
        """P10. The Roster tab's list came straight off `batch_key`, so it
        opened on B17 — a cohort that finished months ago."""
        import dash_view
        sel = self.at.selectbox(key="roster_pick")
        self.assertEqual(sel.value, sel.options[0])
        self.assertEqual(list(sel.options),
                         dash_view.batch_order(list(sel.options)))
        self.assertNotEqual(sel.value, "B17")

    def test_roster_cells_are_glyphs_and_identity_is_pinned(self):
        """P10. Forty repetitions of the words Present / Absent across a row
        is a wall of text nobody reads a pattern out of, and each column had
        to be wide enough to spell "Present"."""
        roster = next(t for t in self.at.tabs
                      if t.label == "📋 Roster (marked attendance)")
        grid = next(d.value for d in roster.dataframe
                    if "Email" in getattr(d.value, "columns", []))
        sess = [c for c in grid.columns
                if c not in ("Email", "Phone", "Active", "Present")]
        self.assertTrue(sess)
        seen = set()
        for c in sess:
            seen |= set(grid[c].unique())
        self.assertTrue(seen)
        self.assertEqual(seen - {"✓", "✗", "—"}, set())
        with open(APP, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('st.column_config.TextColumn("Email", pinned=True)', src)
        self.assertIn("@st.cache_data(show_spinner=False)\ndef _roster_display", src)

    def test_each_tab_body_paints_exactly_once_per_pass(self):
        """P9's precondition, and the counter a live session can be watched
        with. In a real browser a widget inside one fragment repaints that
        fragment alone; AppTest always reruns the whole script (see the note
        above TestFragments), so what is checked here is that wrapping the
        bodies in functions did not make any of them run twice or not at all."""
        painted = dict(self.at.session_state["_painted"])
        self.assertEqual(len(painted), len(_FRAGMENT_BODIES),
                         f"one stamp per fragment body, got {sorted(painted)}")
        self.assertEqual(sorted(set(painted.values())), [1], painted)

    def test_the_favicon_the_page_config_asks_for_is_on_disk(self):
        """P11. A missing page_icon makes set_page_config raise and takes the
        whole app down for a cosmetic asset."""
        self.assertEqual(T.BRAND["favicon"], "assets/favicon_64.png")
        self.assertTrue(os.path.exists(os.path.join(ROOT, T.BRAND["favicon"])))
        head = next(m.value for m in self.at.main.markdown
                    if "brandbar" in m.value)
        self.assertIn('<span class="mark">', head)
        self.assertIn("image/svg+xml", head)

    def test_admin_expander_holds_the_data_source_controls(self):
        exp = [e for e in self.at.sidebar.expander if e.label == "Admin"]
        self.assertEqual(len(exp), 1)
        admin = exp[0]
        self.assertTrue(any("Refresh from Google" in b.label for b in admin.button))
        self.assertTrue(any("Matching rule" in c.value for c in admin.caption))

    def test_default_batch_is_the_newest_with_sessions(self):
        import json
        import duckdb
        import dash_view
        con = duckdb.connect(STORE, read_only=True)
        rows = {k: json.loads(v) for k, v in con.execute(
            "SELECT key, value FROM meta WHERE key IN ('DATA','sessions')").fetchall()}
        con.close()
        want = dash_view.newest_batch(rows["DATA"], rows["sessions"])
        sel = self.at.selectbox(key="aicap_batch")
        self.assertEqual(sel.value, want)
        self.assertEqual(sel.options[0], want, "newest batch should head the list")
        self.assertNotEqual(sel.value, "B17")

    def test_no_st_metric_left_anywhere(self):
        self.assertEqual(len(self.at.metric), 0)

    def test_closing_types_put_not_recorded_last(self):
        dash = next(t for t in self.at.tabs if t.label == "📊 Dashboard")
        panel = next((m.value for m in dash.markdown if 'class="cl-row"' in m.value), None)
        self.assertIsNotNone(panel)
        names = [seg.split("<")[0] for seg in panel.split('class="cl-name">')[1:]]
        self.assertNotIn("Unknown", names)
        if "Not recorded" in names:
            self.assertEqual(names[-1], "Not recorded")

    def test_prose_moved_into_expanders(self):
        labels = [e.label for _n, _p in _walk(self.at.main)
                  for e in [_n] if getattr(e, "type", "") == "expander"]
        self.assertGreaterEqual(labels.count("How this is calculated"), 3)


# ── P9, measured rather than inferred ────────────────────────────────────────
# AppTest builds a fresh LocalScriptRunner (and a fresh MemoryFragmentStorage)
# per .run() and never passes a fragment_id_queue, so every ordinary AppTest
# interaction is a FULL rerun — which is why TestFragments above can only read
# the decorators off the source. Appending a fragment rerun to a finished pass
# does not work either: the Add-data tab ends the script with st.stop() when no
# upload_password is configured, and st.stop() latches the runner at STOP.
#
# This seam seeds a NEW runner's fragment storage from the previous full run and
# makes its FIRST request the fragment run. Nothing in the app is patched, only
# the test runner, and the patch is installed and removed around this one class.
def _fragment_body_name(frag):
    """Which tab body a stored fragment wraps, found through its closure."""
    for cell in (getattr(frag, "__closure__", None) or ()):
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and getattr(value, "__name__", "").startswith(
                ("_tab_", "_sub_")):
            return value.__name__
    return None


def _seeded_runner_class():
    """The LocalScriptRunner subclass, built lazily so importing this module
    never depends on Streamlit's private test plumbing."""
    from streamlit.runtime.scriptrunner import RerunData
    from streamlit.testing.v1.element_tree import parse_tree_from_messages
    from streamlit.testing.v1.local_script_runner import (
        LocalScriptRunner, require_widgets_deltas)

    class _Seeded(LocalScriptRunner):
        LAST: dict = {}
        ARM: dict = {}      # {"body": "_tab_dashboard"} -> next run is that

        def run(self, widget_state=None, query_params=None, timeout=3,
                page_hash=""):
            arm = dict(_Seeded.ARM)
            _Seeded.ARM.clear()
            store = self._fragment_storage
            if not arm:
                tree = super().run(widget_state, query_params, timeout, page_hash)
                _Seeded.LAST = {
                    "fragments": dict(store._fragments),
                    "parents": dict(store._parent_by_id),
                    "seq": dict(store._registration_sequence_by_id),
                    "names": {fid: _fragment_body_name(f)
                              for fid, f in store._fragments.items()},
                }
                return tree
            store._fragments.update(_Seeded.LAST["fragments"])
            store._parent_by_id.update(_Seeded.LAST["parents"])
            store._registration_sequence_by_id.update(_Seeded.LAST["seq"])
            fid = next(k for k, v in _Seeded.LAST["names"].items()
                       if v == arm["body"])
            _Seeded.LAST["fid"] = fid
            self.request_rerun(RerunData(widget_states=widget_state,
                                         fragment_id_queue=[fid],
                                         is_fragment_scoped_rerun=True))
            if not self._script_thread:
                self.start()
            require_widgets_deltas(self, timeout)
            return parse_tree_from_messages(self.forward_msgs())

    return _Seeded


@unittest.skipUnless(os.path.exists(STORE), "no local prebuilt store")
class TestFragmentIsolation(unittest.TestCase):
    """The claim the fragments were added for, asserted end to end: changing
    the Dashboard's batch repaints the Dashboard and NOTHING else. The AST test
    above cannot catch a body that is decorated but whose widgets still trigger
    an app-scoped rerun; this can."""

    @classmethod
    def setUpClass(cls):
        from streamlit.testing.v1 import AppTest
        from streamlit.testing.v1 import app_test as _app_test
        try:
            seeded = _seeded_runner_class()
        except ImportError as exc:                       # pragma: no cover
            raise unittest.SkipTest(f"streamlit test plumbing moved: {exc}")
        cls._seeded = seeded
        cls._orig_runner = _app_test.LocalScriptRunner
        cls._app_test = _app_test
        _app_test.LocalScriptRunner = seeded
        try:
            at = AppTest.from_file(APP, default_timeout=300)
            at.session_state["_authed"] = True
            at.secrets = {"drive": {}}
            first = at.run()
            probe = next(iter(seeded.LAST.get("fragments", {})), None)
            if probe is None:
                raise unittest.SkipTest("no fragment storage to seed from")
            cls.exc_first = [e.value for e in first.exception]
            cls.before = dict(at.session_state["_painted"])
            sel = at.selectbox(key="aicap_batch")
            cls.old, cls.new = sel.value, next(o for o in sel.options
                                               if o != sel.value)
            sel.set_value(cls.new)
            seeded.ARM = {"body": "_tab_dashboard"}
            cls.at = at.run()
            cls.after = dict(at.session_state["_painted"])
        except AttributeError as exc:                    # pragma: no cover
            _app_test.LocalScriptRunner = cls._orig_runner
            raise unittest.SkipTest(f"streamlit internals moved: {exc}")
        except Exception:
            _app_test.LocalScriptRunner = cls._orig_runner
            raise

    @classmethod
    def tearDownClass(cls):
        app_test = getattr(cls, "_app_test", None)
        if app_test is not None:
            app_test.LocalScriptRunner = cls._orig_runner

    def test_the_first_paint_was_clean(self):
        self.assertEqual(self.exc_first, [])

    def test_the_rerun_really_was_fragment_scoped(self):
        last = self._seeded.LAST
        self.assertEqual(last["names"][last["fid"]], "_tab_dashboard")
        self.assertEqual([e.value for e in self.at.exception], [])

    def test_only_the_dashboard_repainted(self):
        delta = {k: self.after.get(k, 0) - self.before.get(k, 0)
                 for k in self.before}
        self.assertEqual(delta.pop("dashboard"), 1, "the Dashboard did not repaint")
        self.assertEqual(delta, {k: 0 for k in delta},
                         f"a Dashboard widget re-ran other tabs: {delta}")

    def test_the_roster_grid_was_not_rebuilt(self):
        """The strongest form of the same claim: the fragment run's own output
        holds the repainted Dashboard and no roster grid at all."""
        self.assertEqual(self.after["roster"], self.before["roster"])
        painted = [m.value for m in self.at.main.markdown
                   if "kpi-grid" in m.value or "panel-title" in m.value]
        self.assertTrue(painted, "the fragment rerun painted nothing")
        rosters = [d for d in self.at.main.dataframe
                   if "Email" in list(getattr(d.value, "columns", []))]
        self.assertEqual(rosters, [],
                         "the Roster grid was rebuilt by a Dashboard-only rerun")

    def test_the_dashboard_repainted_with_the_new_batch(self):
        heads = [m.value for m in self.at.main.markdown
                 if f"Batch {self.new}" in m.value]
        self.assertTrue(heads, f"the drill-in heading does not name {self.new}")


if __name__ == "__main__":
    unittest.main()
