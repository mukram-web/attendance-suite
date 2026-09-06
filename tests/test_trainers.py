"""Trainer identity resolution and rollups.

The clustering rule is deliberately conservative. Leaving a name unmerged shows
a small duplicate row; merging wrongly puts one person's bad week on another
person's record. These tests pin that asymmetry.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trainers  # noqa: E402


def sess(mentor, batch="B38", present=100, total=200, index=1.0,
         rating=4.5, n=50, dist=None, date="2026-09-05", pod=""):
    return {"mentor": mentor, "batch": batch, "present": present, "total": total,
            "index": index, "rating": rating, "rating_n": n, "date": date,
            "pod": pod, "dist": dist or {}}


class TestSplitting(unittest.TestCase):
    def test_a_single_name(self):
        self.assertEqual(trainers.split_mentors("Swapnil Narayan"), ["Swapnil Narayan"])

    def test_every_separator_real_cells_use(self):
        for cell in ("A Kumar, B Singh", "A Kumar & B Singh", "A Kumar + B Singh",
                     "A Kumar / B Singh", "A Kumar and B Singh"):
            self.assertEqual(len(trainers.split_mentors(cell)), 2, cell)

    def test_the_same_person_twice_in_one_cell_counts_once(self):
        self.assertEqual(trainers.split_mentors("Swapnil, Swapnil"), ["Swapnil"])

    def test_blank_and_junk(self):
        self.assertEqual(trainers.split_mentors(""), [])
        self.assertEqual(trainers.split_mentors(None), [])
        self.assertEqual(trainers.split_mentors("  ,  "), [])


class TestNormalisation(unittest.TestCase):
    def test_session_notes_typed_into_the_name_are_stripped(self):
        # 'Swapnil (Play Simulive)' is a fact about the SESSION, not the person.
        self.assertEqual(trainers.norm_name("Swapnil (Play Simulive)"), "swapnil")
        self.assertEqual(trainers.norm_name("Ravi [sub]"), "ravi")

    def test_honorifics_and_punctuation_go(self):
        self.assertEqual(trainers.norm_name("Dr. Ravi Kumar"), "ravi kumar")
        self.assertEqual(trainers.norm_name("  RAVI   KUMAR  "), "ravi kumar")


class TestResolve(unittest.TestCase):
    def test_a_short_name_joins_its_only_longer_match(self):
        r = trainers.resolve(["Swapnil", "Swapnil Narayan", "Swapnil (Play Simulive)"])
        self.assertEqual(len(set(r["canon"].values())), 1)
        self.assertEqual(set(r["canon"].values()), {"Swapnil Narayan"})

    def test_two_different_people_sharing_a_first_name_stay_apart(self):
        """The bug a first-token rule would introduce."""
        r = trainers.resolve(["Ravi Kumar", "Ravi Sharma"])
        self.assertEqual(len(set(r["canon"].values())), 2)

    def test_an_ambiguous_short_name_is_flagged_not_guessed(self):
        r = trainers.resolve(["Ravi", "Ravi Kumar", "Ravi Sharma"])
        # 'Ravi' could be either, so it stays its own entry and says so.
        self.assertEqual(r["canon"]["Ravi"], "Ravi")
        self.assertIn("Ravi", r["ambiguous"])
        self.assertEqual(len(set(r["canon"].values())), 3)

    def test_an_email_overrides_spelling_entirely(self):
        r = trainers.resolve(
            ["Ravi Kumar", "R Kumar", "Ravi K"],
            {"Ravi Kumar": "ravi@x.com", "R Kumar": "ravi@x.com", "Ravi K": "ravi@x.com"})
        self.assertEqual(len(set(r["canon"].values())), 1)

    def test_names_without_an_email_still_cluster(self):
        r = trainers.resolve(["Ravi Kumar", "Ravi"], {"Ravi Kumar": "ravi@x.com"})
        self.assertEqual(len(set(r["canon"].values())), 1)

    def test_merged_groups_are_reported(self):
        r = trainers.resolve(["Swapnil", "Swapnil Narayan"])
        self.assertEqual(r["merged"] if "merged" in r else r["groups"],
                         {"Swapnil Narayan": ["Swapnil", "Swapnil Narayan"]})


class TestBuild(unittest.TestCase):
    def test_spellings_collapse_into_one_row(self):
        out = trainers.build([sess("Swapnil"), sess("Swapnil Narayan"),
                              sess("Swapnil (Play Simulive)")])
        self.assertEqual(out["n_people"], 1)
        self.assertEqual(out["trainers"][0]["sessions"], 3)

    def test_co_taught_credits_both_and_says_so(self):
        out = trainers.build([sess("A Kumar, B Singh")])
        self.assertEqual(out["n_people"], 2)
        for t in out["trainers"]:
            self.assertEqual(t["sessions"], 1)
            self.assertEqual(t["co_taught"], 1)

    def test_attendance_is_pooled_by_headcount_not_averaged(self):
        # 100/200 and 900/1000 pools to 1000/1200 = 83.3%, not (50+90)/2 = 70%.
        out = trainers.build([sess("A", present=100, total=200),
                              sess("A", present=900, total=1000)])
        self.assertEqual(out["trainers"][0]["pct"], 83.3)

    def test_nps_comes_from_summed_histograms(self):
        big = {"recommend": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 300}}
        small = {"recommend": {"1": 3, "2": 0, "3": 0, "4": 0, "5": 0}}
        out = trainers.build([sess("A", dist=big), sess("A", dist=small)])
        self.assertEqual(out["trainers"][0]["nps"], 98)

    def test_a_trainer_with_no_index_still_appears_but_sorts_last(self):
        out = trainers.build([sess("Good", index=1.4), sess("Unknown", index=None)])
        self.assertEqual([t["trainer"] for t in out["trainers"]], ["Good", "Unknown"])

    def test_sessions_with_no_mentor_are_ignored_not_bucketed_as_blank(self):
        out = trainers.build([sess(""), sess(None), sess("A Kumar")])
        self.assertEqual(out["n_people"], 1)

    def test_empty_input(self):
        out = trainers.build([])
        self.assertEqual(out["trainers"], [])
        self.assertEqual(out["n_people"], 0)


if __name__ == "__main__":
    unittest.main()
