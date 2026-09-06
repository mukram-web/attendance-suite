"""Per-student records out of the grid.

Four of these tests exist because the first version of students.py shipped the
bug they pin, and an adversarial review found each one against the live store:

  * the pod cell and the pod column header are different vocabularies, so raw
    comparison matched ZERO students and dropped every pod session;
  * older batch tabs carry hand-typed headers ('9th May'), so an ISO-only
    pattern dropped 59 real session columns;
  * the grid keeps sessions REQUIRE_L2 hides, so counting them all deflated
    attendance by 13-22 points against the dashboard;
  * a student with no recorded pod must be whole-batch only, not in every pod.
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

# What the roster ACTUALLY holds - not the bare canonical name.
TECHIE = "Techies - Ai Career Accelerator Program B35"
FINANCE = "Finance - Ai Career Accelerator Program B35"

ALL_VISIBLE = students.visible_keys([
    {"mm": "08_27", "pod": ""}, {"mm": "08_29", "pod": ""},
    {"mm": "09_05", "pod": ""}, {"mm": "09_05", "pod": "Techies"},
    {"mm": "09_06", "pod": "Techies"}])


def row(email, pod, **marks):
    r = {"Email": email, "Phone": "919000000001", "Active": "Yes",
         "Present": 0, "POD Prefrence": pod}
    for c in COLS[5:]:
        r[c] = marks.get(c, "")
    return r


class TestColumnDetection(unittest.TestCase):
    def test_iso_columns(self):
        self.assertEqual(students.parse_col("2026_09_05"), ("09_05", ""))
        self.assertEqual(students.parse_col("2026_09_05 | Techies"),
                         ("09_05", "Techies"))

    def test_legacy_hand_typed_headers_are_sessions_too(self):
        """B17-B24 carry whatever their owners typed. An ISO-only pattern
        dropped 59 real marked columns and halved those batches."""
        for label, mm in (("9th May", "05_09"), ("10thMay", "05_10"),
                          ("16th May", "05_16"), ("31st may", "05_31")):
            self.assertEqual(students.parse_col(label), (mm, ""), label)

    def test_the_pod_preference_column_is_NOT_a_session(self):
        for spelling in ("POD Prefrence", "POD pref", "POD Pref", "Pod Preference"):
            self.assertIsNone(students.parse_col(spelling), spelling)

    def test_meta_columns_are_not_sessions(self):
        for c in ("Email", "Phone", "Active", "Present"):
            self.assertIsNone(students.parse_col(c))

    def test_the_header_pod_is_canonicalised(self):
        # so it can be compared with a canonicalised roster cell
        self.assertEqual(students.parse_col("2026_09_05 | Ai Generalist"),
                         ("09_05", "Generalist"))

    def test_a_pod_session_column_is_never_taken_as_the_pod_column(self):
        cols = ["Email", "Phone", "Active", "Present", "2026_09_05 | Techies"]
        self.assertIsNone(students.pod_column(cols))


class TestPodMatching(unittest.TestCase):
    """The bug that dropped 7,330 Present marks across B35-B38."""

    def test_the_roster_cell_is_canonicalised_before_comparison(self):
        self.assertEqual(students.student_pod({"p": TECHIE}, "p"), "Techies")

    def test_a_real_roster_cell_matches_a_real_column_header(self):
        p = students.profile(row("a@x.com", TECHIE), COLS, visible=ALL_VISIBLE)
        self.assertEqual(p["invited"], 5, "3 whole-batch + 2 Techies")

    def test_aliases_fold(self):
        # The suffix has to be the real one - pods.from_roster_cell returns None
        # for '- AI CAP B35', which is worth knowing but is not this module's job.
        self.assertEqual(
            students.student_pod(
                {"p": "Operations/Supply Chain - Ai Career Accelerator Program B35"},
                "p"),
            "Ops/Supply Chain")

    def test_an_unrecognised_roster_suffix_yields_no_pod(self):
        # Documented, not endorsed: pods.from_roster_cell needs the programme
        # name it knows. A cell it cannot read makes the student whole-batch
        # only, which under-counts them rather than mis-assigning them.
        self.assertEqual(
            students.student_pod({"p": "Techies - AI CAP B35"}, "p"), "")

    def test_a_non_techie_gets_only_whole_batch_sessions(self):
        p = students.profile(row("b@x.com", FINANCE), COLS, visible=ALL_VISIBLE)
        self.assertEqual(p["invited"], 3)

    def test_a_student_with_no_pod_is_whole_batch_only(self):
        p = students.profile(row("c@x.com", ""), COLS, visible=ALL_VISIBLE)
        self.assertEqual(p["invited"], 3)

    def test_a_techies_mark_is_actually_counted(self):
        # Before the fix this student's two pod attendances were invisible.
        r = row("a@x.com", TECHIE, **{"2026_09_05 | Techies": "Present",
                                      "2026_09_06 | Techies": "Present",
                                      "2026_08_27": "Present"})
        p = students.profile(r, COLS, visible=ALL_VISIBLE)
        self.assertEqual(p["attended"], 3)
        self.assertEqual(p["pct"], 60.0)


class TestHiddenSessions(unittest.TestCase):
    """REQUIRE_L2 hides sessions from the dashboard but not from the grid."""

    def test_a_column_the_dashboard_hides_is_not_counted(self):
        visible = students.visible_keys([
            {"mm": "08_27", "pod": ""}, {"mm": "08_29", "pod": ""}])
        p = students.profile(row("b@x.com", FINANCE), COLS, visible=visible)
        self.assertEqual(p["invited"], 2, "09_05 is not in L2, so not a session")

    def test_attendance_matches_the_visible_denominator(self):
        visible = students.visible_keys([{"mm": "08_27", "pod": ""},
                                         {"mm": "08_29", "pod": ""}])
        r = row("b@x.com", FINANCE, **{"2026_08_27": "Present",
                                       "2026_08_29": "Absent",
                                       "2026_09_05": "Absent"})
        p = students.profile(r, COLS, visible=visible)
        self.assertEqual((p["attended"], p["invited"]), (1, 2))
        self.assertEqual(p["pct"], 50.0)      # not 33.3 from the hidden column

    def test_no_visible_set_means_count_everything(self):
        # "cannot tell" is not "nothing ran" - the same rule REQUIRE_L2 follows.
        p = students.profile(row("b@x.com", FINANCE), COLS, visible=None)
        self.assertEqual(p["invited"], 3)

    def test_summarise_reports_how_many_it_hid(self):
        visible = students.visible_keys([{"mm": "08_27", "pod": ""}])
        s = students.summarise(COLS, [row("a@x.com", FINANCE)], visible=visible)
        self.assertEqual(s["n_sessions"], 1)
        self.assertEqual(s["n_hidden"], 4)


class TestAttendance(unittest.TestCase):
    def test_unmarked_is_reported_separately_from_absent(self):
        r = row("a@x.com", FINANCE, **{"2026_08_27": "Present"})
        p = students.profile(r, COLS, visible=ALL_VISIBLE)
        self.assertEqual(p["unmarked"], 2)
        self.assertEqual(p["attended"], 1)

    def test_no_invited_sessions_gives_None_not_zero_percent(self):
        p = students.profile(row("a@x.com", FINANCE), ["Email", "Phone"])
        self.assertEqual(p["invited"], 0)
        self.assertIsNone(p["pct"])

    def test_the_timeline_only_carries_sessions_they_were_invited_to(self):
        p = students.profile(row("a@x.com", TECHIE), COLS, visible=ALL_VISIBLE)
        self.assertEqual(len(p["timeline"]), 5)
        p2 = students.profile(row("b@x.com", FINANCE), COLS, visible=ALL_VISIBLE)
        self.assertTrue(all(t["pod"] == "" for t in p2["timeline"]))


class TestSummarise(unittest.TestCase):
    def _rows(self):
        return [
            row("core@x.com", TECHIE, **{c: "Present" for c in COLS[5:]}),
            row("half@x.com", FINANCE, **{"2026_08_27": "Present",
                                          "2026_08_29": "Present",
                                          "2026_09_05": "Absent"}),
            row("never@x.com", FINANCE, **{c: "Absent" for c in COLS[5:8]}),
        ]

    def test_counts_and_buckets(self):
        s = students.summarise(COLS, self._rows(), visible=ALL_VISIBLE)
        self.assertEqual(s["n_students"], 3)
        self.assertEqual(s["n_sessions"], 5)
        self.assertEqual(s["attended_any"], 2)
        self.assertEqual(s["never_attended"], 1)
        # core is >=75%, so 100% is core and 2-of-3 (66.7%) is regular.
        self.assertEqual(s["buckets"]["core"], 1)
        self.assertEqual(s["buckets"]["regular"], 1)
        self.assertEqual(s["buckets"]["never"], 1)

    def test_it_reports_which_columns_it_skipped(self):
        s = students.summarise(COLS, self._rows(), visible=ALL_VISIBLE)
        self.assertIn("POD Prefrence", s["skipped_columns"])
        self.assertEqual(s["pod_column"], "POD Prefrence")

    def test_pod_membership_is_reported(self):
        s = students.summarise(COLS, self._rows(), visible=ALL_VISIBLE)
        self.assertEqual(s["pods"].get("Techies"), 1)
        self.assertEqual(s["pods"].get("Finance"), 2)

    def test_truncating_the_list_does_not_change_the_headlines(self):
        full = students.summarise(COLS, self._rows(), visible=ALL_VISIBLE)
        cut = students.summarise(COLS, self._rows(), visible=ALL_VISIBLE, top=1)
        self.assertEqual(len(cut["students"]), 1)
        for k in ("n_students", "attended_any", "never_attended", "buckets"):
            self.assertEqual(cut[k], full[k])

    def test_an_empty_batch_does_not_explode(self):
        s = students.summarise(COLS, [], visible=ALL_VISIBLE)
        self.assertEqual(s["n_students"], 0)
        self.assertEqual(s["buckets"], {})


if __name__ == "__main__":
    unittest.main()
