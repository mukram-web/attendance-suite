"""Reading a hand-supplied week of Zoom exports.

The load-bearing test here is test_the_folder_name_round_trips: the pipeline
decides what to download from the FOLDER name, so a name this module invents
that `attendance_core._folder_batches` cannot read is a session that is
uploaded, paid for in Drive storage, and never marked.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import attendance_core as ac          # noqa: E402
import ingest                         # noqa: E402
import pods                           # noqa: E402


class TestNames(unittest.TestCase):
    def test_the_two_zoom_exports_are_recognised(self):
        self.assertEqual(ingest.classify_name("attendee_97466597585_2026_09_13.csv"),
                         ("attendee", "97466597585", "2026_09_13"))
        self.assertEqual(ingest.classify_name("poll_97466597585_2026_09_13.csv"),
                         ("poll", "97466597585", "2026_09_13"))

    def test_the_hand_saved_poll_form_is_recognised(self):
        # 59 webinars' feedback was stranded on Drive under this shape once.
        self.assertEqual(
            ingest.classify_name("97466597585 - 2026-09-13 - Poll Report.csv"),
            ("poll", "97466597585", "2026_09_13"))

    def test_a_zips_internal_folders_are_ignored(self):
        self.assertEqual(
            ingest.classify_name(
                "2026-09-13 - AI CAP B35/attendee_97466597585_2026_09_13.csv"),
            ("attendee", "97466597585", "2026_09_13"))

    def test_anything_the_parsers_would_skip_is_refused(self):
        for bad in ("Attendance report.csv", "attendee_123_2026_09_13.csv",
                    "recording.mp4", "", None, "attendee_97466597585.csv"):
            self.assertIsNone(ingest.classify_name(bad), bad)

    def test_a_hyphenated_attendee_date_is_refused_not_silently_dropped(self):
        """attendance_core._parse_filename accepts underscores ONLY.

        Accepting a hyphen here would let the file pass validation, reach Drive
        and dispatch a run, and then be skipped by the marker without a word —
        a green run that added nothing.
        """
        self.assertIsNone(
            ingest.classify_name("attendee_97466597585_2026-09-13.csv"))
        self.assertEqual(
            ac._parse_filename("attendee_97466597585_2026_09_13.csv"),
            ("97466597585", "2026_09_13"))
        self.assertIsNone(
            ac._parse_filename("attendee_97466597585_2026-09-13.csv"))

    def test_files_group_into_sessions(self):
        sessions, rejected = ingest.classify([
            "attendee_9912345678_2026_09_13.csv",
            "poll_9912345678_2026_09_13.csv",
            "attendee_8812345678_2026_09_13.csv",
            "notes.txt",
        ])
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[("9912345678", "2026_09_13")]["poll"],
                         ["poll_9912345678_2026_09_13.csv"])
        self.assertEqual(sessions[("8812345678", "2026_09_13")]["poll"], [])
        self.assertEqual(rejected, ["notes.txt"])


class TestFolderName(unittest.TestCase):
    def test_the_folder_name_round_trips(self):
        """The name must be readable by the code that decides what to download.

        `_folder_batches` recovers the batches, `pods.from_folder` the POD and
        `_mmdd` the date — all three from the string alone.
        """
        cases = [
            ("AI CAP B35 , B36 , B37 - Finance", {("CAP", 35), ("CAP", 36), ("CAP", 37)}, "Finance"),
            ("AI CAP B35 - Techies", {("CAP", 35)}, "Techies"),
            ("AI CAP B39", {("CAP", 39)}, None),
            ("AI CAP B17 + B21 11AM", {("CAP", 17), ("CAP", 21)}, None),
        ]
        for label, keys, pod in cases:
            name = ingest.folder_name("2026_09_13", label, "Financial Analysis & AI")
            self.assertTrue(name.startswith("2026-09-13 - "), name)
            self.assertEqual(ac._mmdd(name), "09_13", name)
            self.assertEqual(ac._folder_batches(name), keys, name)
            self.assertEqual(pods.from_folder(name), pod, name)

    def test_characters_drive_refuses_are_stripped_from_the_topic(self):
        n = ingest.folder_name("2026_09_13", "AI CAP B39", 'Excel: "AI/ML" <fast>')
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, n.split(" - ", 2)[-1])

    def test_a_missing_topic_still_gives_a_usable_name(self):
        n = ingest.folder_name("2026_09_13", "AI CAP B39", "")
        self.assertEqual(n, "2026-09-13 - AI CAP B39")
        self.assertEqual(ac._folder_batches(n), {("CAP", 39)})


L2 = {"9912345678": (frozenset({("CAP", 35), ("CAP", 36)}), "Financial Analysis"),
      "8812345678": (frozenset({("CAP", 39)}), "Prompt Engineering")}
LABELS = {"9912345678": "AI CAP B35 , B36 - Finance",
          "8812345678": "AI CAP B39"}


class TestPreflight(unittest.TestCase):
    def _rows(self, names):
        sessions, _rej = ingest.classify(names)
        return {r["wid"]: r for r in ingest.preflight(sessions, L2, LABELS)}

    def test_a_scheduled_session_resolves_its_batches_pod_and_topic(self):
        r = self._rows(["attendee_9912345678_2026_09_13.csv",
                        "poll_9912345678_2026_09_13.csv"])["9912345678"]
        self.assertTrue(r["in_l2"])
        self.assertEqual(r["batches"], ["B35", "B36"])
        self.assertEqual(r["pod"], "Finance")
        self.assertEqual(r["topic"], "Financial Analysis")
        self.assertEqual((r["n_attendee"], r["n_poll"]), (1, 1))
        self.assertEqual(r["date"], "2026-09-13")

    def test_a_whole_batch_session_carries_no_pod(self):
        r = self._rows(["attendee_8812345678_2026_09_13.csv"])["8812345678"]
        self.assertEqual(r["pod"], "")
        self.assertEqual(r["batches"], ["B39"])

    def test_a_session_l2_has_never_heard_of_is_flagged_not_guessed(self):
        r = self._rows(["attendee_7712345678_2026_09_13.csv"])["7712345678"]
        self.assertFalse(r["in_l2"])
        self.assertEqual(r["folder"], "")          # nothing to invent a name from
        self.assertEqual(r["batches"], [])


class TestBlockers(unittest.TestCase):
    def _blockers(self, names):
        sessions, rejected = ingest.classify(names)
        rows = ingest.preflight(sessions, L2, LABELS)
        return ingest.blockers(rows, rejected)

    def test_a_clean_upload_has_none(self):
        self.assertEqual(self._blockers(["attendee_9912345678_2026_09_13.csv"]), [])

    def test_missing_from_l2_blocks_and_names_the_webinar(self):
        """Uploading it would be a green run with no visible change.

        REQUIRE_L2 hides the session, so the only fix is the L2 row — which is
        what the message has to say.
        """
        got = self._blockers(["attendee_7712345678_2026_09_13.csv"])
        self.assertEqual(len(got), 1)
        self.assertIn("7712345678", got[0])
        self.assertIn("L2", got[0])

    def test_a_poll_with_no_attendee_report_blocks(self):
        got = self._blockers(["poll_9912345678_2026_09_13.csv"])
        self.assertTrue(any("nothing to mark" in g for g in got))

    def test_unreadable_names_block_and_show_the_expected_shape(self):
        got = self._blockers(["attendee_9912345678_2026_09_13.csv", "junk.csv"])
        self.assertTrue(any("junk.csv" in g for g in got))
        self.assertTrue(any("attendee_<webinar>" in g for g in got))


class TestTargetDrive(unittest.TestCase):
    def test_new_files_go_to_the_last_configured_drive(self):
        # process_files prefers the last-listed drive for a duplicated session,
        # so uploading anywhere else puts the new copy behind an older one.
        self.assertEqual(ingest.target_drive("old_id, new_id"), "new_id")
        self.assertEqual(ingest.target_drive("only_id"), "only_id")
        with self.assertRaises(ValueError):
            ingest.target_drive("")


if __name__ == "__main__":
    unittest.main()
