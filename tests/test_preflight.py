"""tools/preflight.py: the pure half - which weekend, and what the run will do
with each session - so the report can be trusted without touching Drive."""
import datetime
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location("preflight", os.path.join(ROOT, "tools", "preflight.py"))
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

D = datetime.date
ROSTER = {("CAP", 39), ("CAP", 40), ("CAP", 41), ("ECAP", 1)}


class TestWeekend(unittest.TestCase):
    def test_a_weekday_looks_at_the_weekend_just_gone(self):
        self.assertEqual(preflight.weekend_dates(D(2026, 9, 30)), [D(2026, 9, 26), D(2026, 9, 27)])
        self.assertEqual(preflight.weekend_dates(D(2026, 10, 2)), [D(2026, 9, 26), D(2026, 9, 27)])

    def test_a_saturday_or_sunday_is_this_weekend(self):
        self.assertEqual(preflight.weekend_dates(D(2026, 10, 3)), [D(2026, 10, 3), D(2026, 10, 4)])
        self.assertEqual(preflight.weekend_dates(D(2026, 10, 4)), [D(2026, 10, 3), D(2026, 10, 4)])

    def test_batch_names(self):
        self.assertEqual(preflight.batch_name(("CAP", 41)), "B41")
        self.assertEqual(preflight.batch_name(("ECAP", 1)), "ECAP B1")
        self.assertEqual(preflight.batch_name(41), "B41")


class TestScope(unittest.TestCase):
    def test_a_new_batch_of_a_known_programme_is_in_scope(self):
        self.assertTrue(preflight.in_scope(("CAP", 42), ROSTER))
        self.assertTrue(preflight.in_scope(("ECAP", 4), ROSTER))

    def test_below_the_lowest_tab_or_an_unknown_programme_is_not(self):
        self.assertFalse(preflight.in_scope(("CAP", 11), ROSTER))     # the roster starts at B17
        self.assertFalse(preflight.in_scope(("BSIAI", 1), ROSTER))    # removed 2026-09-22
        self.assertFalse(preflight.in_scope(("CAP", 42), set()))


def _row(dates, batches, topic="Prompting", label="AI CAP B41", not_session=False):
    return {"dates": set(dates), "batches": set(batches), "topic": topic,
            "label": label, "not_session": not_session}


def _ex(date, folder, batches, drive="Zoom extracts"):
    return {"date": date, "folder": folder, "drive": drive, "batches": set(batches)}


class TestAssess(unittest.TestCase):
    def setUp(self):
        weekend = {"2026_10_03", "2026_10_04"}
        l2rows = {
            "1": _row(["2026_10_03"], [("CAP", 41)]),                                  # ready
            "2": _row(["2026_10_03"], [("CAP", 41)], topic="Agents"),                   # missing
            "3": _row(["2026_10_04"], [("CAP", 42)], label="AI CAP B42"),              # no tab (in scope)
            "4": _row(["2026_10_03"], [("CAP", 40), ("CAP", 41)], topic="Hackathon Solution",
                      label="AI CAP B40, B41", not_session=True),                      # skipped
            "5": _row(["2026_10_04"], [("CAP", 40), ("ECAP", 1)], label="AI CAP B40 + ECAP B1"),  # shared, twins
            "6": _row(["2026_10_03"], [("CAP", 11), ("CAP", 13)], label="AI CAP B11 + B13"),      # out of scope
            "8": _row(["2026_10_04"], [("CAP", 41)], topic="Voice agents"),            # bad folder
        }
        l2_other = {"9": {"2026_09_27"}}
        exports = {
            "1": [_ex("2026_10_03", "2026-10-03 - AI CAP B41 - Prompting", [("CAP", 41)])],
            "3": [_ex("2026_10_03", "2026-10-03 - AI CAP B42 - X", [("CAP", 42)])],
            "5": [_ex("2026_10_04", "2026-10-04 - AI CAP B40 - Y", [("CAP", 40), ("ECAP", 1)]),
                  _ex("2026_10_04", "2026-10-04 - AI CAP B40 + ECAP B1 - Y", [("CAP", 40), ("ECAP", 1)])],
            "6": [_ex("2026_10_03", "2026-10-03 - AI CAP B11 + B13 - Z", [("CAP", 11), ("CAP", 13)])],
            "7": [_ex("2026_10_03", "2026-10-03 - AI CAP B41 - Mystery", [("CAP", 41)])],   # not in L2
            "8": [_ex("2026_10_04", "2026-10-04 - Voice agents", [])],                    # no batch in the name
            "9": [_ex("2026_10_04", "2026-10-04 - AI CAP B39 - Reused id", [("CAP", 39)])],  # L2: 27 Sep
            "10": [_ex("2026_10_03", "2026-10-03 - BSI B2 - W", [("BSIAI", 2)])],         # out of scope, not in L2
            "11": [_ex("2026_10_03", "2026-10-03 - AI CAP B41 - Inner Circle Walkthrough", [("CAP", 41)])],
        }
        marked = {"2026_09_27", "2026_10_03"}
        self.f = preflight.assess(weekend, l2rows, l2_other, exports, ROSTER, marked)
        self.report = preflight.render([D(2026, 10, 3), D(2026, 10, 4)], self.f, ["a note"],
                                       {"l2": "t1", "roster": "t2"},
                                       {"folders": 9, "drives": 2, "exports": 10})

    def wids(self, key):
        return sorted(r["wid"] for r in self.f[key])

    def test_each_session_lands_in_exactly_one_bucket(self):
        self.assertEqual(self.wids("ready"), ["1", "5"])
        self.assertEqual(self.wids("missing"), ["2"])
        self.assertEqual(self.wids("date_mismatch"), ["9"])
        self.assertEqual(self.wids("not_in_l2"), ["7"])
        self.assertEqual(self.wids("bad_folder"), ["8"])
        self.assertEqual(self.wids("skipped"), ["11", "4"])   # the Hackathon row, the walkthrough folder
        self.assertEqual(self.wids("out_of_scope"), ["10", "6"])

    def test_a_missing_tab_is_named_once_and_only_when_in_scope(self):
        self.assertEqual([r["batch"] for r in self.f["no_tab"]], ["B42"])
        # ...and the session for it is neither READY nor a date problem
        self.assertNotIn("3", self.wids("ready") + self.wids("date_mismatch") + self.wids("out_of_scope"))

    def test_info_buckets(self):
        self.assertEqual(self.wids("shared"), ["5"])          # across programmes only
        self.assertEqual(self.wids("twins"), ["5"])           # two folders on ONE drive
        self.assertEqual(self.f["already_marked"], ["2026_10_03"])

    def test_two_copies_on_different_drives_are_not_twins(self):
        f = preflight.assess({"2026_10_03"}, {"1": _row(["2026_10_03"], [("CAP", 41)])}, {},
                             {"1": [_ex("2026_10_03", "a", [("CAP", 41)], drive="A"),
                                    _ex("2026_10_03", "b", [("CAP", 41)], drive="B")]}, ROSTER, set())
        self.assertEqual([r["wid"] for r in f["ready"]], ["1"])
        self.assertEqual(f["twins"], [])

    def test_a_date_mismatch_shows_both_dates(self):
        r = {x["wid"]: x for x in self.f["date_mismatch"]}
        self.assertEqual(r["9"]["dates"], ["2026_09_27"])
        self.assertEqual(r["9"]["export_dates"], ["2026_10_04"])

    def test_the_report_says_what_needs_a_decision(self):
        self.assertIn("NEEDS ATTENTION", self.report)
        self.assertIn("**B42** has no tab", self.report)
        self.assertIn("BAD FOLDER", self.report)
        self.assertIn("Hackathon Solution", self.report)
        self.assertIn("skipped as always", self.report)
        self.assertIn("gh workflow run refresh.yml", self.report)
        self.assertIn("a note", self.report)

    def test_a_clean_weekend_is_ready(self):
        f = preflight.assess({"2026_10_03"}, {"1": _row(["2026_10_03"], [("CAP", 41)])}, {},
                             {"1": [_ex("2026_10_03", "f", [("CAP", 41)])]}, ROSTER, set())
        self.assertFalse(any(f[k] for k in preflight.DECISION_KEYS))
        self.assertIn("READY TO DISPATCH", preflight.render([D(2026, 10, 3)], f, [], {}, {}))


if __name__ == "__main__":
    unittest.main()
