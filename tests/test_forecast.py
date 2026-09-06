"""
Unit tests for forecast.py — synthetic fixtures only (no real student data).
Run:  python -m unittest tests.test_forecast    (from the project root)
"""
import math
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forecast  # noqa: E402

TODAY = date(2026, 8, 31)
PODS = ["Generalist", "Techies", "Students"]


def make_batch(start: date, pcts, strength=1000, pods=None, pod_sessions=None):
    """One DATA[code] entry: weekly sessions from `start`, attendance `pcts`."""
    by_date, sessions = [], []
    for i, p in enumerate(pcts):
        d = start + timedelta(days=7 * i)
        mm = f"{d.month:02d}_{d.day:02d}"
        by_date.append({"mm": mm, "date_lbl": f"{d.day} {d:%b}", "n_pods": 1,
                        "present": round(strength * p / 100), "total": strength,
                        "pct": p})
        sessions.append({"mm": mm, "date_lbl": f"{d.day} {d:%b}", "pod": "",
                         "topic": f"Topic {i}", "present": round(strength * p / 100),
                         "total": strength, "pct": p})
    for s in (pod_sessions or []):
        sessions.append(s)
    return {"code": "X", "strength": strength, "active": strength,
            "n_sessions": len(pcts), "avg_pct": sum(pcts) / len(pcts),
            "peak": max(pcts), "low": min(pcts),
            "by_date": by_date, "sessions": sessions,
            "pods": pods or {p: {"strength": strength // len(PODS),
                                 "active": strength // len(PODS)} for p in PODS}}


def make_tabs(dates, batches, topic_of, pod_names=PODS):
    """Curriculum tabs: {tab_name: grid}. topic_of(pod, batch, date) -> str|None."""
    tabs = {}
    for pod in pod_names:
        hdr = ["Date", "Day"] + [x for b in batches for x in (f"AI CAP {b}", "Trainer")]
        grid = [hdr]
        for d in dates:
            row = [f"{d.day} {d:%b}", f"{d:%A}"] + [""] * (len(batches) * 2)
            for i, b in enumerate(batches):
                t = topic_of(pod, b, d)
                if t:
                    row[2 + i * 2] = t
                    row[3 + i * 2] = "Trainer A"
            grid.append(row)
        tabs[pod] = grid
    return tabs


class TestDateHandling(unittest.TestCase):
    def test_walk_years_crosses_new_year(self):
        """Dec->Jan must advance the year, or the schedule sorts backwards."""
        mds = [(11, 29), (12, 27), (1, 3), (1, 31)]
        out = forecast._walk_years(mds, date(2026, 11, 29), anchor_idx=0)
        self.assertEqual(out, [date(2026, 11, 29), date(2026, 12, 27),
                               date(2027, 1, 3), date(2027, 1, 31)])
        self.assertEqual(out, sorted(out))

    def test_walk_years_backward_from_late_anchor(self):
        mds = [(12, 20), (1, 10)]
        out = forecast._walk_years(mds, date(2027, 1, 10), anchor_idx=1)
        self.assertEqual(out, [date(2026, 12, 20), date(2027, 1, 10)])

    def test_parse_daymonth_forms(self):
        for cell, want in [("8 Aug", (8, 8)), ("8-Aug", (8, 8)), ("Aug 8", (8, 8)),
                           ("30 Sep", (9, 30)), (date(2026, 7, 4), (7, 4))]:
            self.assertEqual(forecast._parse_daymonth(cell), want, cell)
        for bad in ("", None, "Week 3", "TBD"):
            self.assertIsNone(forecast._parse_daymonth(bad))

    def test_safe_date_rejects_impossible(self):
        self.assertIsNone(forecast._safe_date(2026, 2, 30))


class TestTopicKey(unittest.TestCase):
    def test_spelling_variants_collapse(self):
        """The real sheet carries both of these for the same session."""
        self.assertEqual(forecast.topic_key("Prompt Engineering 2026"),
                         forecast.topic_key("Prompt Engineering in 2026"))

    def test_genuinely_different_topics_stay_apart(self):
        self.assertNotEqual(forecast.topic_key("Excel with AI"),
                            forecast.topic_key("Mastering Claude"))

    def test_split_kind(self):
        same = ["Prompt Engineering 2026", "Prompt Engineering in 2026",
                "Prompt engineering 2026"]
        self.assertEqual(forecast._split_kind(same), "whole")
        self.assertEqual(forecast._split_kind([f"Topic {i}" for i in range(11)]), "pod")


class TestCleanTrainer(unittest.TestCase):
    """The cell right of a topic is not reliably the trainer — on the real sheet
    it also holds times, a batch code, and merged-cell topic text."""

    def test_real_names_survive(self):
        # Invented names in the SHAPES the real sheet uses (single, two-part,
        # apostrophe, initial, hyphen) — this repo is public, so no real staff.
        for name in ("Asha", "Priya", "Ravi Venkataraman", "Meera Krishnan",
                     "Sunita Balakrishnan", "O'Brien", "A. Kumar", "Jean-Luc"):
            self.assertEqual(forecast.clean_trainer(name), name, name)

    def test_times_are_dropped(self):
        for bad in ("11:00 AM", "7:30 PM", "19:30:00", "7 PM", "11.00 AM"):
            self.assertEqual(forecast.clean_trainer(bad), "", bad)

    def test_time_objects_are_dropped(self):
        from datetime import time, datetime
        self.assertEqual(forecast.clean_trainer(time(19, 30)), "")
        self.assertEqual(forecast.clean_trainer(datetime(2026, 9, 6, 19, 30)), "")

    def test_batch_code_and_topic_text_are_dropped(self):
        for bad in ("B52", "AI CAP B35", r"\[merged\] Inventory, SS, Lead time",
                    "Excel with AI, Office Prod with AI"):
            self.assertEqual(forecast.clean_trainer(bad), "", bad)

    def test_blank_and_none(self):
        for bad in (None, "", "   "):
            self.assertEqual(forecast.clean_trainer(bad), "")

    def test_a_time_in_the_sheet_yields_no_trainer_not_a_wrong_one(self):
        tabs = {"Techies": [["Date", "Day", "AI CAP B1", "Trainer"],
                            ["8 Aug", "Saturday", "Some Topic", "11:00 AM"],
                            ["9 Aug", "Sunday", "Other Topic", "Asha"]]}
        rows, _ = forecast.parse_curriculum(tabs, TODAY, PODS)
        by_topic = {r["topic"]: r["trainer"] for r in rows}
        self.assertEqual(by_topic["Some Topic"], "")
        self.assertEqual(by_topic["Other Topic"], "Asha")


class TestPodMatching(unittest.TestCase):
    def setUp(self):
        self.by_key = {forecast.pod_key(p): p for p in
                       ["Generalist", "Sales/Marketing/HR", "Ops/Supply Chain",
                        "Business Owners", "Techies"]}

    def test_general_maps_to_generalist(self):
        self.assertEqual(forecast._match_pod("General", self.by_key), "Generalist")

    def test_spacing_and_case_drift(self):
        self.assertEqual(forecast._match_pod("Sales/ Marketing/HR", self.by_key),
                         "Sales/Marketing/HR")
        self.assertEqual(forecast._match_pod("ops supply chain", self.by_key),
                         "Ops/Supply Chain")

    def test_unmatched_tab_returns_none(self):
        self.assertIsNone(forecast._match_pod("Sheet7", self.by_key))

    def test_unmatched_tab_is_warned_not_dropped_silently(self):
        tabs = {"Techies": [["Date", "Day", "AI CAP B1", "Trainer"],
                            ["8 Aug", "Saturday", "T", "A"]],
                "Pivot Table 2": [[None, None]]}
        rows, warn = forecast.parse_curriculum(tabs, TODAY, PODS)
        self.assertTrue(any("Pivot Table 2" in w for w in warn))


class TestCurveFit(unittest.TestCase):
    def test_curve_recovers_known_decay(self):
        """Two identical batches -> the curve is exactly their shared decay."""
        pcts = [50.0, 45.0, 40.0, 36.0]
        DATA = {"B1": make_batch(date(2026, 6, 6), pcts),
                "B2": make_batch(date(2026, 6, 13), pcts)}
        fit = forecast.fit_curve(DATA, TODAY)
        for wk, p in enumerate(pcts):
            self.assertAlmostEqual(math.exp(fit["curve"][wk]), p, places=6)
        for b in ("B1", "B2"):
            self.assertAlmostEqual(fit["offsets"][b], 0.0, places=9)

    def test_batch_offset_detects_a_cold_batch(self):
        """A batch running uniformly 20% low must read as a ~0.8 multiplier."""
        base = [50.0, 45.0, 40.0, 36.0]
        DATA = {"B1": make_batch(date(2026, 6, 6), base),
                "B2": make_batch(date(2026, 6, 13), base),
                "B3": make_batch(date(2026, 6, 20), [p * 0.8 for p in base])}
        fit = forecast.fit_curve(DATA, TODAY)
        self.assertLess(math.exp(fit["offsets"]["B3"]), 0.90)
        self.assertGreater(math.exp(fit["offsets"]["B1"]), 1.0)

    def test_start_dates_are_first_session(self):
        DATA = {"B1": make_batch(date(2026, 6, 6), [50.0, 45.0])}
        self.assertEqual(forecast.fit_curve(DATA, TODAY)["start"]["B1"],
                         date(2026, 6, 6))


class TestPodMultipliers(unittest.TestCase):
    def test_multiplier_is_relative_to_same_day_batch_rate(self):
        """A pod at 2x its day's batch rate, shrunk toward 1 by POD_SHRINK_K."""
        b = make_batch(date(2026, 8, 1), [40.0, 40.0])
        b["sessions"].append({"mm": "08_01", "pod": "Techies", "topic": "T",
                              "pct": 80.0, "present": 80, "total": 100})
        mult, detail = forecast._pod_multipliers({"B1": b}, TODAY)
        self.assertEqual(detail["Techies"]["raw"], 2.0)
        # n=1, K=3 -> 1 + (2-1)*1/4 = 1.25
        self.assertAlmostEqual(mult["Techies"], 1.25, places=6)
        self.assertLess(mult["Techies"], detail["Techies"]["raw"])

    def test_more_history_shrinks_less(self):
        b = make_batch(date(2026, 8, 1), [40.0, 40.0, 40.0])
        for mm in ("08_01", "08_08", "08_15"):
            b["sessions"].append({"mm": mm, "pod": "Data", "topic": "T",
                                  "pct": 80.0, "present": 80, "total": 100})
        mult, _ = forecast._pod_multipliers({"B1": b}, TODAY)
        self.assertAlmostEqual(mult["Data"], 1 + 1.0 * 3 / 6, places=6)


def decay(n, top=50.0, rate=0.92):
    return [round(top * rate ** i, 2) for i in range(n)]


class TestBuild(unittest.TestCase):
    """Mirrors the real shape: OLD batches supply the curve at weeks the YOUNG
    batches have not reached yet. Forecasting a young batch is the whole point —
    if every batch were the same age there would be no curve to lean on."""

    def setUp(self):
        # B1/B2 are old and long (weeks 0-21); B3/B4 are young (weeks 0-6).
        self.DATA = {
            "B1": make_batch(date(2026, 4, 4), decay(22)),
            "B2": make_batch(date(2026, 4, 11), decay(21)),
            "B3": make_batch(date(2026, 7, 18), decay(7)),
            "B4": make_batch(date(2026, 7, 25), decay(6)),
        }
        self.future = [TODAY + timedelta(days=7 * i) for i in range(1, 4)]

    def _whole(self, pod, batch, d):
        return "Shared Session"        # same title in every pod tab

    def _split(self, pod, batch, d):
        return f"{pod} deep dive"      # a different title per pod

    def test_whole_batch_day_yields_one_row_per_batch(self):
        tabs = make_tabs(self.future, ["B3", "B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        for d in f["by_date"]:
            self.assertEqual(d["sessions"], d["batches"])
        self.assertTrue(all(r["kind"] == "whole" for r in f["sessions"]))
        self.assertTrue(all(r["pod"] == "ALL" for r in f["sessions"]))

    def test_pod_split_day_yields_one_row_per_pod(self):
        """Three distinct titles is below SPLIT_MIN_TOPICS, so widen the fixture."""
        # Real-ish names: pod_key() strips digits, so "P0".."P5" would all
        # collapse to the same key and every tab would match the same pod.
        pods = ["Generalist", "Techies", "Students", "Finance", "Educators",
                "Healthcare"]
        DATA = {k: dict(v, pods={p: {"strength": 100, "active": 100} for p in pods})
                for k, v in self.DATA.items()}
        tabs = make_tabs(self.future, ["B4"], self._split, pod_names=pods)
        # tab names must match pod names for the mapping to resolve
        f = forecast.build(DATA, tabs, TODAY, horizon_weeks=4)
        self.assertTrue(f["sessions"])
        self.assertTrue(all(r["kind"] == "pod" for r in f["sessions"]))
        self.assertEqual({r["pod"] for r in f["sessions"]}, set(pods))

    def test_prediction_follows_the_curve_downward(self):
        tabs = make_tabs(self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        pcts = [r["pred_pct"] for r in sorted(f["sessions"], key=lambda r: r["date"])]
        self.assertEqual(pcts, sorted(pcts, reverse=True))

    def test_interval_widens_with_horizon(self):
        tabs = make_tabs(self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        rows = sorted(f["sessions"], key=lambda r: r["date"])
        widths = [(r["hi"] - r["lo"]) / r["pred"] for r in rows]
        self.assertLessEqual(widths[0], widths[-1] + 1e-9)

    def test_unstarted_batch_is_flagged_not_hidden(self):
        tabs = make_tabs(self.future, ["B4", "B9"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        self.assertIn("B9", f["assumed_batches"])
        b9 = [r for r in f["sessions"] if r["batch"] == "B9"]
        self.assertTrue(b9)
        self.assertTrue(all(r["assumed_strength"] for r in b9))
        self.assertFalse(any(r["assumed_strength"]
                             for r in f["sessions"] if r["batch"] == "B4"))
        self.assertTrue(any("have not started" in w for w in f["warnings"]))

    def test_horizon_is_respected(self):
        far = [TODAY + timedelta(days=7 * i) for i in range(1, 12)]
        tabs = make_tabs(far, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=3)
        self.assertTrue(f["sessions"])
        for r in f["sessions"]:
            self.assertLessEqual(date.fromisoformat(r["date"]),
                                 TODAY + timedelta(weeks=3))

    def test_today_is_forecast_not_dropped(self):
        """The window started strictly AFTER today, so a 03:04 run hid that same
        day's sessions and the tab jumped to the following weekend."""
        tabs = make_tabs([TODAY] + self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        self.assertTrue([r for r in f["sessions"]
                         if r["date"] == TODAY.isoformat()])

    def test_the_last_couple_of_days_stay_visible(self):
        y = TODAY - timedelta(days=1)
        tabs = make_tabs([y] + self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        self.assertTrue([r for r in f["sessions"] if r["date"] == y.isoformat()])

    def test_anything_older_is_still_excluded(self):
        """This is the current weekend, not a history panel."""
        old = TODAY - timedelta(days=7)
        tabs = make_tabs([old] + self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        self.assertFalse([r for r in f["sessions"]
                          if r["date"] == old.isoformat()])

    def test_week_beyond_observed_life_is_skipped_not_extrapolated(self):
        """No curve point => no forecast. Silence beats a made-up number.

        B1 is the OLDEST batch, so by next week it is already past week 21 —
        further into its life than any batch has ever been observed. There is
        nothing to extrapolate from and the model must decline, loudly."""
        far = [TODAY + timedelta(days=7 * i) for i in range(1, 9)]
        tabs = make_tabs(far, ["B1"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=8)
        maxwk = max(int(k) for k in f["curve"])
        self.assertTrue(all(r["wk"] <= maxwk for r in f["sessions"]))
        self.assertEqual(f["sessions"], [])
        self.assertTrue(any("beyond any batch" in w for w in f["warnings"]))

    def test_accuracy_beats_carry_forward_baseline(self):
        tabs = make_tabs(self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        a = f["accuracy"]
        self.assertGreater(a["n_scored"], 0)
        self.assertLess(a["mape_1_4wk"], a["baseline_mape_1_4wk"])

    def test_lo_le_pred_le_hi(self):
        tabs = make_tabs(self.future, ["B3", "B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        for r in f["sessions"]:
            self.assertLessEqual(r["lo"], r["pred"])
            self.assertLessEqual(r["pred"], r["hi"])

    def test_by_date_totals_match_session_rows(self):
        tabs = make_tabs(self.future, ["B3", "B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        self.assertEqual(sum(d["pred"] for d in f["by_date"]),
                         sum(r["pred"] for r in f["sessions"]))
        self.assertEqual(sum(d["sessions"] for d in f["by_date"]), len(f["sessions"]))

    def test_output_carries_no_pii(self):
        """The store is public-ish; the forecast must be aggregates only."""
        tabs = make_tabs(self.future, ["B4"], self._whole)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        allowed = {"date", "date_lbl", "batch", "pod", "topic", "trainer", "kind",
                   "wk", "horizon_wks", "denom", "pred_pct", "pred", "lo", "hi",
                   "assumed_strength"}
        for r in f["sessions"]:
            self.assertEqual(set(r), allowed)

    def test_degrades_cleanly_with_no_data(self):
        for DATA, tabs in [({}, {}), (self.DATA, {})]:
            f = forecast.build(DATA, tabs, TODAY, horizon_weeks=4)
            self.assertEqual(f["sessions"], [])
            self.assertTrue(f["warnings"])

    def test_duplicate_pod_row_is_deduped(self):
        """Merged cells in the sheet can repeat a pod on one date."""
        # Real-ish names: pod_key() strips digits, so "P0".."P5" would all
        # collapse to the same key and every tab would match the same pod.
        pods = ["Generalist", "Techies", "Students", "Finance", "Educators",
                "Healthcare"]
        DATA = {k: dict(v, pods={p: {"strength": 100, "active": 100} for p in pods})
                for k, v in self.DATA.items()}
        tabs = make_tabs(self.future, ["B4"], self._split, pod_names=pods)
        tabs["Techies"].append(list(tabs["Techies"][1]))   # duplicate a data row
        f = forecast.build(DATA, tabs, TODAY, horizon_weeks=4)
        keys = [(r["date"], r["batch"], r["pod"]) for r in f["sessions"]]
        self.assertEqual(len(keys), len(set(keys)))


class TestSessionRollup(unittest.TestCase):
    """by_session is the view the programme is actually staffed from: one row per
    (date, topic, pod), the batches inside it, and a total."""

    def setUp(self):
        self.DATA = {
            "B1": make_batch(date(2026, 4, 4), decay(22)),
            "B2": make_batch(date(2026, 4, 11), decay(21)),
            "B3": make_batch(date(2026, 7, 18), decay(7)),
            "B4": make_batch(date(2026, 7, 25), decay(6)),
        }
        self.future = [TODAY + timedelta(days=7 * i) for i in range(1, 3)]

    def _build(self, batches=("B3", "B4")):
        tabs = make_tabs(self.future, list(batches), lambda p, b, d: "Shared Session")
        return forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)

    def test_batches_on_one_date_collapse_into_one_session(self):
        f = self._build()
        self.assertTrue(f["by_session"])
        for s in f["by_session"]:
            self.assertEqual(s["n_batches"], 2)
            self.assertEqual(sorted(s["batches"]), ["B3", "B4"])

    def test_total_is_the_sum_of_its_batches(self):
        f = self._build()
        for s in f["by_session"]:
            self.assertEqual(s["pred"], sum(v["pred"] for v in s["per_batch"].values()))
            self.assertEqual(s["denom"], sum(v["denom"] for v in s["per_batch"].values()))
            self.assertEqual(s["lo"], sum(v["lo"] for v in s["per_batch"].values()))
            self.assertEqual(s["hi"], sum(v["hi"] for v in s["per_batch"].values()))

    def test_rollup_conserves_every_session_row(self):
        f = self._build()
        self.assertEqual(sum(s["n_batches"] for s in f["by_session"]),
                         len(f["sessions"]))
        self.assertEqual(sum(s["pred"] for s in f["by_session"]),
                         sum(r["pred"] for r in f["sessions"]))

    def test_spelling_variants_group_as_one_session(self):
        """B3 and B4 carry the same session under different spellings."""
        def topic_of(pod, batch, d):
            return ("Prompt Engineering 2026" if batch == "B3"
                    else "Prompt Engineering in 2026")
        tabs = make_tabs(self.future, ["B3", "B4"], topic_of)
        f = forecast.build(self.DATA, tabs, TODAY, horizon_weeks=4)
        for s in f["by_session"]:
            self.assertEqual(s["n_batches"], 2)

    def test_assumed_batch_flags_the_whole_session(self):
        f = self._build(batches=("B4", "B9"))
        for s in f["by_session"]:
            self.assertTrue(s["any_assumed"])

    def test_sorted_by_date(self):
        f = self._build()
        d = [s["date"] for s in f["by_session"]]
        self.assertEqual(d, sorted(d))


if __name__ == "__main__":
    unittest.main()
