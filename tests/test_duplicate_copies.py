"""One webinar in two folders: the biggest copy wins, deterministically.

The two Shared Drives name the same session differently, so the same attendee
export arrives twice and the copies are NOT identical - webinar 91641846331 on
23 Aug 2026 is 18,371 bytes in the folder that names the POD and 17,580 in the
one that does not. `attendance_core.process_files` marks both and the LAST one
wins, so which copy reached the published column depended on Drive's listing
order: B35's 23 Aug Educators column read 62 present in one run and 59 in the
next, and GATE 6 refused the drop - correctly, but that wedges every later run
until the count comes back.

`sessionmeta.by_webinar` and `bsiai.sessions_from_files` already break this tie
on size. This pins the same rule for the path that decides the MARKS.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_data  # noqa: E402

A = "2026-08-23 - AI CAP B35 - Blueprint to Launch: Designing Courses with AI"
B = "2026-08-23 - AI CAP B35 - Educators - Blueprint to Launch Designing Cour"
NAME = "attendee_91641846331_2026_08_23.csv"


class DedupeByWebinar(unittest.TestCase):
    def test_the_bigger_copy_wins_whatever_the_order(self):
        small = (f"{A}/{NAME}", "id_small", False)
        big = (f"{B}/{NAME}", "id_big", False)
        sizes = {"id_small": 17580, "id_big": 18371}
        for entries in ([small, big], [big, small]):
            with self.subTest(order=[e[1] for e in entries]):
                got = live_data.dedupe_by_webinar(entries, sizes)
                self.assertEqual([e[1] for e in got], ["id_big"])

    def test_different_webinars_on_one_date_are_both_kept(self):
        """Eleven PODs can meet on one day, each its own webinar."""
        e = [(f"{B}/attendee_91641846331_2026_08_23.csv", "a", False),
             (f"{A}/attendee_93538394941_2026_08_23.csv", "b", False)]
        got = live_data.dedupe_by_webinar(e, {"a": 10, "b": 10})
        self.assertEqual(sorted(x[1] for x in got), ["a", "b"])

    def test_the_same_webinar_on_two_dates_is_two_sessions(self):
        e = [(f"{B}/attendee_91641846331_2026_08_23.csv", "a", False),
             (f"{B}/attendee_91641846331_2026_08_30.csv", "b", False)]
        got = live_data.dedupe_by_webinar(e, {"a": 1, "b": 2})
        self.assertEqual(sorted(x[1] for x in got), ["a", "b"])

    def test_a_tie_keeps_the_incumbent_so_weeks_agree(self):
        e = [(f"{A}/{NAME}", "first", False), (f"{B}/{NAME}", "second", False)]
        got = live_data.dedupe_by_webinar(e, {"first": 9, "second": 9})
        self.assertEqual([x[1] for x in got], ["first"])

    def test_names_with_no_webinar_id_are_never_deduped(self):
        """'attendee_Rag beginner.csv' and friends are real files on the drive
        with nothing to group on."""
        e = [("f/attendee_Rag beginner.csv", "a", False),
             ("g/attendee_Intro to Agentic AI with n8n.csv", "b", False)]
        got = live_data.dedupe_by_webinar(e, {})
        self.assertEqual(sorted(x[1] for x in got), ["a", "b"])

    def test_zips_pass_through(self):
        """A zip's byte size says nothing about how many attendees are inside."""
        e = [(f"{A}/attendee_91641846331_2026_08_23.zip", "a", True),
             (f"{B}/attendee_91641846331_2026_08_23.zip", "b", True)]
        got = live_data.dedupe_by_webinar(e, {"a": 1, "b": 2})
        self.assertEqual(sorted(x[1] for x in got), ["a", "b"])

    def test_a_missing_size_loses_to_a_known_one(self):
        e = [(f"{A}/{NAME}", "unknown", False), (f"{B}/{NAME}", "known", False)]
        got = live_data.dedupe_by_webinar(e, {"known": 5})
        self.assertEqual([x[1] for x in got], ["known"])


if __name__ == "__main__":
    unittest.main()
