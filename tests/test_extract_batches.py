"""Which batches a session belongs to, read off a hand-typed label.

Every one of these strings is a real L2 `Batch Name` cell or Drive folder name.
Getting this wrong is not a display bug: a batch that is not in the returned set
never has the session marked at all, and under the freeze (CLAUDE.md §4g) it is
never re-marked either — so a dropped batch is a permanently missing session.

The two cases fixed on 2026-09-14, both found on live data that week:
  * 'AI CAP B20 + 21' returned only B20, so B21 lost the session. Same for B23
    and B25 on 'AI CAP B22 + 23 + 25', and B27 on 'AI CAP B26 + 27'.
  * 'AI CAP B40 - Common + BSIAI Accelerator B1' returned BSIAI for BOTH
    numbers, inventing a 'BSIAI B40' that does not exist and losing AI CAP B40
    the first two sessions of its life.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import attendance_core as ac  # noqa: E402


class TestBareNumbers(unittest.TestCase):
    def test_a_bare_number_after_a_plus_is_a_batch(self):
        self.assertEqual(ac.extract_batches("AI CAP B20 + 21"),
                         {("CAP", 20), ("CAP", 21)})

    def test_several_bare_numbers(self):
        self.assertEqual(ac.extract_batches("AI CAP B22 + 23 + 25"),
                         {("CAP", 22), ("CAP", 23), ("CAP", 25)})
        self.assertEqual(ac.extract_batches("AI CAP B26 + 27"),
                         {("CAP", 26), ("CAP", 27)})

    def test_a_bare_number_inherits_its_segments_programme(self):
        self.assertEqual(ac.extract_batches("BSIAI B2 + 3"),
                         {("BSIAI", 2), ("BSIAI", 3)})

    def test_a_time_of_day_is_never_a_batch(self):
        """The mistake the bare-number rule could have introduced.

        A number counts only when its `+`/`,` item is NOTHING else. These have
        no separator at all, so '11AM' can never be read as B11 — which would
        silently mark a completely unrelated batch.
        """
        for label in ("AI CAP B17 11AM", "AI CAP B17 @ 11AM",
                      "AI CAP B17  11AM", "AI CAP B17 11:00 AM]"):
            self.assertEqual(ac.extract_batches(label), {("CAP", 17)}, label)

    def test_a_topic_number_in_a_folder_segment_is_not_a_batch(self):
        self.assertEqual(ac.extract_batches("AI CAP B19 + Content creation Part 1"),
                         {("CAP", 19)})


class TestProgrammes(unittest.TestCase):
    def test_a_label_naming_two_programmes_keeps_both(self):
        self.assertEqual(
            ac.extract_batches("AI CAP B40 - Common + BSIAI Accelerator  B1"),
            {("CAP", 40), ("BSIAI", 1)})

    def test_bsiai_still_wins_over_cap_within_one_item(self):
        # §4b.1: 'BSI B1' must never collide with AI CAP B1.
        for label in ("BSI B1", "BSIAI B1", "BSI AI B1"):
            self.assertEqual(ac.extract_batches(label), {("BSIAI", 1)}, label)

    def test_an_item_naming_no_programme_inherits_the_segments(self):
        self.assertEqual(ac.extract_batches("ECAP B2 + B3"),
                         {("ECAP", 2), ("ECAP", 3)})

    def test_the_default_is_still_cap(self):
        self.assertEqual(ac.extract_batches("B35 , B36"), {("CAP", 35), ("CAP", 36)})


class TestTrackNamed(unittest.TestCase):
    """`_track_named` reads a bare NAME - a roster tab or a Drive folder - with
    no batch number to lean on. `lms_roster` uses it to decide whether a tab in
    the roster workbook belongs to AI CAP at all, so the BSIAI branch here is
    what keeps a `BSIAI B1` tab from being carried as AI CAP B1. Nothing else
    pinned it: test_lms_roster.test_bsiai_tabs_are_not_cap passes with the
    branch deleted (measured 2026-09-22)."""

    def test_bsiai_spellings_name_bsiai_not_cap(self):
        for name in ("BSIAI B1", "BSI B2", "bsi b2", "BSI AI B3",
                     "BSIAI Accelerator B1"):
            self.assertEqual(ac._track_named(name), "BSIAI", name)

    def test_a_cap_tab_is_still_cap(self):
        for name in ("AI CAP B41", "AICAP B17", "ai cap b35"):
            self.assertEqual(ac._track_named(name), "CAP", name)

    def test_bsi_must_not_match_inside_a_longer_word(self):
        # The guard is `(?<![a-z])bsi`: a word CONTAINING 'bsi' is not BSIAI.
        self.assertNotEqual(ac._track_named("absinthe CAP B1"), "BSIAI")

    def test_an_unnamed_track_is_None_not_CAP(self):
        # _track() defaults to CAP; _track_named must NOT, or a stray tab like
        # `Sheet1` would silently become a batch.
        for name in ("Sheet1", "MM-AI B1", "NEXT BATCH 15K"):
            self.assertIsNone(ac._track_named(name), name)


class TestUnchanged(unittest.TestCase):
    """Shapes that already worked and must keep working."""

    def test_prefixed_lists(self):
        self.assertEqual(ac.extract_batches("AI CAP B10 + B24"),
                         {("CAP", 10), ("CAP", 24)})
        self.assertEqual(ac.extract_batches("AI CAP B14 + B15 +B17"),
                         {("CAP", 14), ("CAP", 15), ("CAP", 17)})

    def test_comma_lists_and_the_run_together_form(self):
        self.assertEqual(ac.extract_batches("AI CAP B35 , B36 , B37 - Finance"),
                         {("CAP", 35), ("CAP", 36), ("CAP", 37)})
        # no word boundary before B35 — the CAP pattern is what catches it
        self.assertEqual(ac.extract_batches("AICAPB35, B36-Generalist"),
                         {("CAP", 35), ("CAP", 36)})

    def test_a_hyphen_range_still_expands(self):
        self.assertEqual(ac.extract_batches("AI CAP B8-B10"),
                         {("CAP", 8), ("CAP", 9), ("CAP", 10)})

    def test_a_pod_suffix_is_not_a_range(self):
        self.assertEqual(ac.extract_batches("AI CAP B35 - Techies"), {("CAP", 35)})
        self.assertEqual(ac.extract_batches("AI CAP B39 - Common"), {("CAP", 39)})

    def test_nothing_recognisable(self):
        for label in ("", "Common", "Techies", None and ""):
            self.assertEqual(ac.extract_batches(label or ""), set(), repr(label))


if __name__ == "__main__":
    unittest.main()
