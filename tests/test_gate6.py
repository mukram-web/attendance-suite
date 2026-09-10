"""GATE 6 — the freeze's only detector, so it needs to be right in both directions.

A carry-forward that mismatches students writes 'Absent' over real Presents and
nothing would ever correct it, so this gate has to fire. But it must NOT fire on
an ordinary roster edit: a student who leaves, joins or switches POD changes the
published present count of every past session they touched, with nobody having
touched the data and with a full re-mark giving the identical lower number. A
gate that goes red on that is a gate somebody learns to bypass with
--allow-partial, which switches off every other gate too.

So it compares MARKS over the students both weeks' rosters share.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline  # noqa: E402

FS = frozenset


class TestLostMarks(unittest.TestCase):
    def test_nothing_changed_is_no_loss(self):
        pm = {("B35", "2026_08_23"): FS({"a@x.com", "b@x.com"})}
        lost, gone = pipeline.lost_marks(pm, dict(pm),
                                        {"B35": FS({"a@x.com", "b@x.com"})},
                                        {"B35": FS({"a@x.com", "b@x.com"})})
        self.assertEqual((lost, gone), ([], []))

    def test_a_present_turned_absent_for_a_shared_student_is_caught(self):
        """The failure the gate exists for: the carry-forward lost somebody."""
        roster = {"B35": FS({"a@x.com", "b@x.com"})}
        lost, _gone = pipeline.lost_marks(
            {("B35", "2026_08_23"): FS({"a@x.com", "b@x.com"})},
            {("B35", "2026_08_23"): FS({"a@x.com"})},
            roster, roster)
        self.assertEqual(len(lost), 1)
        (key, was, now, dropped) = lost[0]
        self.assertEqual(key, ("B35", "2026_08_23"))
        self.assertEqual((was, now, dropped), (2, 1, 1))

    def test_a_student_who_left_is_not_a_loss(self):
        """b@x.com is gone from this week's roster, so their old Present is
        simply out of scope — not evidence of anything."""
        lost, _gone = pipeline.lost_marks(
            {("B35", "2026_08_23"): FS({"a@x.com", "b@x.com"})},
            {("B35", "2026_08_23"): FS({"a@x.com"})},
            {"B35": FS({"a@x.com", "b@x.com"})},
            {"B35": FS({"a@x.com"})})
        self.assertEqual(lost, [])

    def test_a_student_who_joined_is_not_a_loss(self):
        roster_now = {"B35": FS({"a@x.com", "new@x.com"})}
        lost, _gone = pipeline.lost_marks(
            {("B35", "2026_08_23"): FS({"a@x.com"})},
            {("B35", "2026_08_23"): FS({"a@x.com"})},
            {"B35": FS({"a@x.com"})}, roster_now)
        self.assertEqual(lost, [])

    def test_a_pod_switch_does_not_trip_the_gate(self):
        """The false positive that made the first version of this gate unusable.

        The published `present` for a POD column drops when a student moves out
        of that POD — but their MARK in the workbook is untouched, and that is
        what this reads.
        """
        roster = {"B35": FS({"a@x.com", "switcher@x.com"})}
        col = ("B35", "2026_08_23 | Techies")
        lost, _gone = pipeline.lost_marks({col: FS({"a@x.com", "switcher@x.com"})},
                                          {col: FS({"a@x.com", "switcher@x.com"})},
                                          roster, roster)
        self.assertEqual(lost, [])

    def test_a_vanished_column_is_reported_not_gated(self):
        """An L2 row being corrected legitimately hides a session (§5.6), so
        this is information, not a refusal."""
        roster = {"B35": FS({"a@x.com"})}
        lost, gone = pipeline.lost_marks(
            {("B35", "2026_08_23"): FS({"a@x.com"})}, {}, roster, roster)
        self.assertEqual(lost, [])
        self.assertEqual(gone, [("B35", "2026_08_23")])

    def test_a_batch_with_no_shared_students_is_skipped_rather_than_flagged(self):
        # A brand-new roster tab, or a batch renamed: no overlap means no claim.
        lost, _gone = pipeline.lost_marks(
            {("B40", "2026_08_23"): FS({"a@x.com"})},
            {("B40", "2026_08_23"): FS()},
            {"B40": FS({"a@x.com"})}, {"B40": FS({"z@x.com"})})
        self.assertEqual(lost, [])

    def test_empty_inputs_never_raise(self):
        for args in (({}, {}, {}, {}), (None, None, None, None)):
            self.assertEqual(pipeline.lost_marks(*args), ([], []))


if __name__ == "__main__":
    unittest.main()
