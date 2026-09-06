"""Per-student records out of the grid.

Two traps drive most of these tests: a non-session column being counted as a
session (which inflates every denominator), and a pod session being counted
against students who were never invited to it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import students  # noqa: E402

# The real shape, taken from grid_B38 in the live store.
COLS = ["Email", "Phone", "Active", "Present", "POD Prefrence",
        "2026_08_27", "2026_08_29", "2026_09_05", "2026_09_05 | Techies",
        "2026_09_06 | Techies"]


def row(email, pod, **marks):
    r = {"Email": email, "Phone": "919000000001", "Active": "Yes",
         "Present": 0, "POD Prefrence": pod}
    for c in COLS[5:]:
        r[c] = marks.get(c, "")
    return r


class TestColumnDetection(unittest.TestCase):
    def test_a_bare_date_is_a_whole_batch_session(self):
        self.assertEqual(students.parse_col("2026_09_05"), ("2026_09_05", ""))

    def test_a_dated_pod_column_is_a_pod_session(self):
        self.assertEqual(students.parse_col("2026_09_05 | Techies"),
                         ("2026_09_05", "Techies"))

    def test_the_pod_preference_column_is_NOT_a_session(self):
        # The bug: counting this inflates every student's denominator by one.
        for spelling in ("POD Prefrence", "POD pref", "POD Pref", "Pod Preference"):
            self.assertIsNone(students.parse_col(spelling), spelling)

    def test_meta_columns_are_not_sessions(self):
        for c in ("Email", "Phone", "Active", "Present"):
            self.assertIsNone(students.parse_col(c))

    def test_session_columns_are_exactly_the_dated_ones(self):
        self.assertEqual(students.session_columns(COLS), COLS[5:])
        self.assertEqual(students.non_session_columns(COLS), COLS[:5])

    def test_the_pod_column_is_found_whatever_it_is_spelled(self):
        for spelling in ("POD Prefrence", "POD pref", "Pod Preference"):
            cols = ["Email", "Phone", "Active", "Present", spelling, "2026_09_05"]
            self.assertEqual(students.pod_column(cols), spelling)

    def test_a_pod_session_column_is_never_taken_as_the_pod_column(self):
        cols = ["Email", "Phone", "Active", "Present", "2026_09_05 | Techies"]
        self.assertIsNone(students.pod_column(cols))


class TestInvitation(unittest.TestCase):
    """You cannot be absent from a session you were not invited to."""

    def test_a_techie_is_invited_to_whole_batch_and_their_own_pod(self):
        p = students.profile(row("a@x.com", "Techies"), COLS)
        self.assertEqual(p["invited"], 5)          # 3 whole-batch + 2 Techies

    def test_a_non_techie_is_invited_only_to_whole_batch_sessions(self):
        p = students.profile(row("b@x.com", "Finance"), COLS)
        self.assertEqual(p["invited"], 3)

    def test_a_student_with_no_pod_is_whole_batch_only(self):
        # NOT a member of every pod - that would make them absent ten times a day.
        p = students.profile(row("c@x.com", ""), COLS)
        self.assertEqual(p["invited"], 3)

    def test_pod_matching_is_case_insensitive(self):
        p = students.profile(row("d@x.com", "techies"), COLS)
        self.assertEqual(p["invited"], 5)


class TestAttendance(unittest.TestCase):
    def test_percentage_is_over_invited_not_over_all_columns(self):
        r = row("a@x.com", "Finance", **{"2026_08_27": "Present",
                                         "2026_08_29": "Absent",
                                         "2026_09_05": "Present"})
        p = students.profile(r, COLS)
        self.assertEqual((p["attended"], p["invited"]), (2, 3))
        self.assertEqual(p["pct"], 66.7)
        # Counting the two Techies columns would give 2/5 = 40% - the bug.
        self.assertNotEqual(p["pct"], 40.0)

    def test_unmarked_is_reported_separately_from_absent(self):
        r = row("a@x.com", "Finance", **{"2026_08_27": "Present"})
        p = students.profile(r, COLS)
        self.assertEqual(p["unmarked"], 2)
        self.assertEqual(p["attended"], 1)

    def test_no_invited_sessions_gives_None_not_zero_percent(self):
        p = students.profile(row("a@x.com", "Finance"), ["Email", "Phone"])
        self.assertEqual(p["invited"], 0)
        self.assertIsNone(p["pct"])

    def test_the_timeline_only_carries_sessions_they_were_invited_to(self):
        p = students.profile(row("a@x.com", "Techies"), COLS)
        self.assertEqual(len(p["timeline"]), 5)
        p2 = students.profile(row("b@x.com", "Finance"), COLS)
        self.assertTrue(all(t["pod"] == "" for t in p2["timeline"]))


class TestSummarise(unittest.TestCase):
    def _rows(self):
        return [
            row("core@x.com", "Techies", **{c: "Present" for c in COLS[5:]}),
            row("half@x.com", "Finance", **{"2026_08_27": "Present",
                                            "2026_08_29": "Present",
                                            "2026_09_05": "Absent"}),
            row("never@x.com", "Finance", **{c: "Absent" for c in COLS[5:8]}),
        ]

    def test_counts_and_buckets(self):
        s = students.summarise(COLS, self._rows())
        self.assertEqual(s["n_students"], 3)
        self.assertEqual(s["n_sessions"], 5)
        self.assertEqual(s["attended_any"], 2)
        self.assertEqual(s["never_attended"], 1)
        # core is >=75%, so 100% is core and 2-of-3 (66.7%) is regular.
        self.assertEqual(s["buckets"]["core"], 1)
        self.assertEqual(s["buckets"]["regular"], 1)
        self.assertEqual(s["buckets"]["never"], 1)

    def test_it_reports_which_columns_it_skipped(self):
        # So a future non-session column shows up as a visible decision rather
        # than silently changing everybody's denominator.
        s = students.summarise(COLS, self._rows())
        self.assertIn("POD Prefrence", s["skipped_columns"])
        self.assertEqual(s["pod_column"], "POD Prefrence")

    def test_truncating_the_list_does_not_change_the_headlines(self):
        full = students.summarise(COLS, self._rows())
        cut = students.summarise(COLS, self._rows(), top=1)
        self.assertEqual(len(cut["students"]), 1)
        for k in ("n_students", "attended_any", "never_attended", "buckets"):
            self.assertEqual(cut[k], full[k])

    def test_an_empty_batch_does_not_explode(self):
        s = students.summarise(COLS, [])
        self.assertEqual(s["n_students"], 0)
        self.assertEqual(s["buckets"], {})


if __name__ == "__main__":
    unittest.main()
