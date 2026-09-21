"""One webinar in two folders: the copy naming the MOST people wins.

The two Shared Drives name the same session differently, so the same attendee
export arrives twice and the copies are not identical.
`attendance_core.process_files` marks both and the LAST one wins, so which copy
reached the published column depended on Drive's listing order: B35's 23 Aug
Educators column read 62 present in one run and 59 in the next, and GATE 6
refused the drop. It was right to, but the gate compares against the PUBLISHED
store, so an unstable column wedges every later run.

Byte size was tried as the tie-break and is a BAD proxy: B26's 22 Aug had a
bigger file with 22 fewer people present, so ranking on size fixed one session
and moved three others the wrong way. The count of people the file actually
names is the rule `bsiai.sessions_from_files` already uses here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_data  # noqa: E402

A = "2026-08-23 - AI CAP B35 - Blueprint to Launch: Designing Courses with AI"
B = "2026-08-23 - AI CAP B35 - Educators - Blueprint to Launch Designing Cour"
NAME = "attendee_91641846331_2026_08_23.csv"

HEAD = "Attended,First Name,Last Name,Email,Phone\n"


def report(n, pad=""):
    """An attendee report naming n people, optionally padded to be BIGGER on
    disk while naming FEWER people - which is exactly the B26 case."""
    rows = "".join(f"Yes,A,B,s{i}@x.com,90000000{i:02d}\n" for i in range(n))
    return (HEAD + rows + pad).encode()


class DedupeByWebinar(unittest.TestCase):
    def test_the_fuller_copy_wins_whatever_the_order(self):
        small = (f"{A}/{NAME}", report(4))
        big = (f"{B}/{NAME}", report(9))
        for files in ([small, big], [big, small]):
            with self.subTest(order=[f[0][:20] for f in files]):
                got = live_data.dedupe_by_webinar(files)
                self.assertEqual(len(got), 1)
                self.assertIn(B, got[0][0])

    def test_a_bigger_file_naming_fewer_people_loses(self):
        """The B26 regression, pinned. Ranking on bytes picked this one."""
        fat_but_emptier = (f"{A}/{NAME}", report(3, pad="#" * 50_000))
        lean_but_fuller = (f"{B}/{NAME}", report(30))
        got = live_data.dedupe_by_webinar([fat_but_emptier, lean_but_fuller])
        self.assertEqual(len(got), 1)
        self.assertIn(B, got[0][0])

    def test_different_webinars_on_one_date_are_both_kept(self):
        """Eleven PODs can meet on one day, each its own webinar."""
        got = live_data.dedupe_by_webinar([
            (f"{B}/attendee_91641846331_2026_08_23.csv", report(5)),
            (f"{A}/attendee_93538394941_2026_08_23.csv", report(5))])
        self.assertEqual(len(got), 2)

    def test_the_same_webinar_on_two_dates_is_two_sessions(self):
        got = live_data.dedupe_by_webinar([
            (f"{B}/attendee_91641846331_2026_08_23.csv", report(5)),
            (f"{B}/attendee_91641846331_2026_08_30.csv", report(7))])
        self.assertEqual(len(got), 2)

    def test_a_tie_keeps_the_incumbent_so_weeks_agree(self):
        got = live_data.dedupe_by_webinar([
            (f"{A}/{NAME}", report(6)), (f"{B}/{NAME}", report(6))])
        self.assertEqual(len(got), 1)
        self.assertIn(A, got[0][0])

    def test_an_unparseable_copy_never_beats_a_readable_one(self):
        got = live_data.dedupe_by_webinar([
            (f"{A}/{NAME}", b"\x00\x01 not a report at all"),
            (f"{B}/{NAME}", report(2))])
        self.assertEqual(len(got), 1)
        self.assertIn(B, got[0][0])

    def test_names_with_no_webinar_id_are_never_deduped(self):
        """'attendee_Rag beginner.csv' and friends are real files on the drive."""
        got = live_data.dedupe_by_webinar([
            ("f/attendee_Rag beginner.csv", report(1)),
            ("g/attendee_Intro to Agentic AI with n8n.csv", report(1))])
        self.assertEqual(len(got), 2)


if __name__ == "__main__":
    unittest.main()
