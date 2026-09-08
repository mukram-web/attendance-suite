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

    def test_a_shared_email_that_names_two_people_is_SPLIT(self):
        """kaladipti0@gmail.com is typed against 'Dipti', 'Dipti Kala',
        'Vansh Agrawal' AND 'Abhishek Raj Pramani' in the live L2. Trusting the
        address outright credited two people's work to a third."""
        em = {n: "kaladipti0@gmail.com"
              for n in ("Dipti", "Dipti Kala", "Vansh Agrawal", "Abhishek Raj Pramani")}
        r = trainers.resolve(list(em), em)
        self.assertEqual(r["canon"]["Dipti"], r["canon"]["Dipti Kala"])
        self.assertNotEqual(r["canon"]["Vansh Agrawal"], r["canon"]["Dipti Kala"])
        self.assertNotEqual(r["canon"]["Abhishek Raj Pramani"], r["canon"]["Dipti Kala"])
        self.assertIn("kaladipti0@gmail.com", r["shared_email"])

    def test_a_shared_email_across_TYPOS_of_one_name_still_merges(self):
        """The common case, and the one worth keeping: 56 addresses carry more
        than one spelling and almost all are typos of a single person."""
        for names in (["Isshita Debnath", "Ishita", "Isshita", "Ishita Debnath"],
                      ["Varun", "varun sahdev", "Varun Sahdev", "Varrun Sahdev"],
                      ["Abhisek Parmani", "Abhishek", "Abhishek Raj Parmani"]):
            em = {n: "x@y.com" for n in names}
            r = trainers.resolve(names, em)
            self.assertEqual(len(set(r["canon"].values())), 1, names)
            self.assertFalse(r["shared_email"], names)

    def test_edit_distance_only_forgives_one_character_on_real_words(self):
        self.assertTrue(trainers._edit_le1("isshita", "ishita"))
        self.assertTrue(trainers._edit_le1("varrun", "varun"))
        self.assertFalse(trainers._edit_le1("dipti", "vansh"))
        # too short to risk it: 'ravi' and 'rani' are different people
        self.assertFalse(trainers._edit_le1("ram", "raj"))

    def test_a_lone_initial_does_not_win_the_display_name(self):
        r = trainers.resolve(["Disha K", "Disha", "Disha Kharbanda"])
        self.assertEqual(set(r["canon"].values()), {"Disha Kharbanda"})

    def test_merged_groups_are_reported(self):
        r = trainers.resolve(["Swapnil", "Swapnil Narayan"])
        self.assertEqual(r["merged"] if "merged" in r else r["groups"],
                         {"Swapnil Narayan": ["Swapnil", "Swapnil Narayan"]})


class TestBuild(unittest.TestCase):
    # A batch has one session per (date, pod), so two rows on the same date in
    # the same batch ARE one session (recap.session_key). Fixtures that mean
    # "several sessions" therefore vary the date.
    def test_spellings_collapse_into_one_row(self):
        out = trainers.build([sess("Swapnil", date="2026-09-01"),
                              sess("Swapnil Narayan", date="2026-09-02"),
                              sess("Swapnil (Play Simulive)", date="2026-09-03")])
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
        out = trainers.build([sess("Ann Rao", present=100, total=200),
                              sess("Ann Rao", present=900, total=1000,
                                   date="2026-09-06")])
        self.assertEqual(out["trainers"][0]["pct"], 83.3)

    def test_nps_comes_from_summed_histograms(self):
        big = {"recommend": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 300}}
        small = {"recommend": {"1": 3, "2": 0, "3": 0, "4": 0, "5": 0}}
        out = trainers.build([sess("Ann Rao", dist=big),
                              sess("Ann Rao", dist=small, date="2026-09-06")])
        self.assertEqual(out["trainers"][0]["nps"], 98)

    def test_a_room_three_batches_sat_in_is_one_session_with_one_poll(self):
        """The 3x bug. B35, B36 and B37 share one Finance webinar; the poll
        arrives split per batch (own students) with the whole room in
        `rating_shared.joint`. The trainer taught once and was rated once."""
        joint = {"session": 4.3, "trainer": 4.3, "recommend": 4.27,
                 "responses": 220, "nps": 27,
                 "dist": {"recommend": {"1": 10, "2": 10, "3": 30, "4": 60, "5": 110}}}
        rows = []
        for b, own_r, own_n, present, total in (("B35", 4.6, 60, 117, 289),
                                                ("B36", 4.1, 70, 115, 275),
                                                ("B37", 4.2, 60, 129, 303)):
            r = sess("Aryan Patel", batch=b, present=present, total=total,
                     rating=own_r, n=own_n, pod="Finance")
            r["shared_batches"] = ["B35", "B36", "B37"]
            r["rating_shared"] = {"batches": ["B35", "B36", "B37"],
                                  "joint": joint, "split": True,
                                  "unmatched": 30, "multi": 0}
            rows.append(r)
        t = trainers.build(rows)["trainers"][0]
        self.assertEqual(t["sessions"], 1)
        self.assertEqual(t["rating"], 4.3)          # the room's, not 3 copies
        self.assertEqual(t["rating_n"], 220)        # not 190 (own parts) or 660
        self.assertEqual(t["nps"], 27)                 # (110 - 50) / 220
        self.assertEqual(t["present"], 117 + 115 + 129)   # attendance IS per batch
        self.assertEqual(t["invited"], 289 + 275 + 303)
        self.assertEqual(t["batches"], ["B35", "B36", "B37"])
        self.assertEqual(t["co_taught"], 0)

    def test_verbatim_copies_from_an_older_store_still_count_once(self):
        # A store built before the split holds three identical copies and no
        # rating_shared. They must be taken once, never summed to 3x.
        rows = []
        for b in ("B17", "B21"):
            r = sess("Ann Rao", batch=b, rating=4.5, n=757,
                     dist={"recommend": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 757}})
            r["shared_batches"] = ["B17", "B21"]
            rows.append(r)
        t = trainers.build(rows)["trainers"][0]
        self.assertEqual(t["sessions"], 1)
        self.assertEqual(t["rating_n"], 757)
        self.assertEqual(t["nps"], 100)

    def test_co_taught_shared_room_is_one_co_taught_session(self):
        rows = []
        for b in ("B35", "B36"):
            r = sess("A Kumar, B Singh", batch=b)
            r["shared_batches"] = ["B35", "B36"]
            rows.append(r)
        for t in trainers.build(rows)["trainers"]:
            self.assertEqual(t["sessions"], 1)
            self.assertEqual(t["co_taught"], 1)

    def test_a_trainer_with_no_index_still_appears_but_sorts_last(self):
        out = trainers.build([sess("Good", index=1.4), sess("Unknown", index=None)])
        self.assertEqual([t["trainer"] for t in out["trainers"]], ["Good", "Unknown"])

    def test_sessions_with_no_mentor_are_ignored_not_bucketed_as_blank(self):
        out = trainers.build([sess(""), sess(None), sess("A Kumar")])
        self.assertEqual(out["n_people"], 1)

    def test_a_one_letter_mentor_cell_is_not_a_person(self):
        # 'A' is an initial or a stray keystroke, never a trainer.
        out = trainers.build([sess("A"), sess("K"), sess("Ann Rao")])
        self.assertEqual([t["trainer"] for t in out["trainers"]], ["Ann Rao"])

    def test_empty_input(self):
        out = trainers.build([])
        self.assertEqual(out["trainers"], [])
        self.assertEqual(out["n_people"], 0)


if __name__ == "__main__":
    unittest.main()
