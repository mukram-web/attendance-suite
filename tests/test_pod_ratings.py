"""Per-domain poll ratings: dividing a multi-domain room's feedback by POD.

A room with no pod of its own - an All Domains session, or the complement room
everyone outside the day's PODs sits in - holds several domains and has ONE poll
file. Its rating used to be found by the pod name L2 wrote, which meant a
complement room ("Common", "Common , BSIAI Accelerator B1", or blank) was looked
up under a key nothing was filed under, and the dashboard printed "no poll
conducted" over a poll with 424 answers.

Membership comes from the roster instead, so nothing here depends on how the
schedule spells a room. Measured on the live store 2026-09-22: 12 of 16
complement rooms had a poll, all 12 splittable, none anonymous.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dash_view  # noqa: E402
import data as ddata  # noqa: E402
import polls  # noqa: E402


def resp(email, session=5, trainer=5, recommend=5):
    return {"email": email,
            "scores": {"session": [session], "trainer": [trainer],
                       "recommend": [recommend]}}


PODS = {
    "Techies": frozenset({"t1@x.com", "t2@x.com"}),
    "Finance": frozenset({"f1@x.com", "f2@x.com"}),
    "Data": frozenset({"d1@x.com"}),
}


class TestSplitByPod(unittest.TestCase):
    def test_each_domain_gets_its_own_students_answers(self):
        out = polls.split_by_pod(
            [resp("f1@x.com", 4), resp("f2@x.com", 4),
             resp("d1@x.com", 2), resp("t1@x.com", 5)], PODS)
        self.assertEqual(out["Finance"]["responses"], 2)
        self.assertEqual(out["Finance"]["session"], 4.0)
        self.assertEqual(out["Data"]["session"], 2.0)
        self.assertEqual(out["Techies"]["session"], 5.0)

    def test_a_domain_nobody_answered_for_is_an_empty_result_not_a_missing_one(self):
        # Zero answers is a real fact about the session - the UI must be able to
        # say "nobody from Data rated this" rather than "no data".
        out = polls.split_by_pod([resp("f1@x.com")], PODS)
        self.assertEqual(out["Data"]["responses"], 0)
        self.assertIsNone(out["Data"]["session"])

    def test_a_respondent_on_no_roster_row_is_counted_not_dropped(self):
        # 32 of B39's 424 respondents were on no B39 roster row. They belong to
        # the room's headline but to no domain, so they must be visible.
        out = polls.split_by_pod([resp("f1@x.com"), resp("stranger@x.com")], PODS)
        self.assertEqual(out["_unmatched"], 1)
        self.assertEqual(out["Finance"]["responses"], 1)

    def test_nobody_is_silently_lost(self):
        # The accounting identity split_by_roster pins, one level down.
        rows = [resp("f1@x.com"), resp("t1@x.com"), resp("nobody@x.com")]
        out = polls.split_by_pod(rows, PODS)
        total = sum(v["responses"] for k, v in out.items()
                    if not k.startswith("_"))
        self.assertEqual(total + out["_unmatched"] - out["_multi"], len(rows))


class TestApplyPodSplit(unittest.TestCase):
    """The wiring: which rooms get a breakdown, and what happens when they cannot."""

    def _ratings(self, pod=""):
        return {("B41", "09_19", pod): {"_wid": "999", "session": 4.0,
                                        "_batches": ["B41"]}}

    def test_a_multi_domain_room_gets_a_per_domain_breakdown(self):
        r = self._ratings(pod="")
        out, stats = polls.apply_pod_split(
            r, {"999": polls_text()}, {"B41": PODS})
        self.assertEqual(stats["split"], 1)
        pr = out[("B41", "09_19", "")]["pod_ratings"]
        self.assertEqual(pr["Finance"]["responses"], 1)
        self.assertEqual(pr["Techies"]["responses"], 1)

    def test_a_named_pods_own_room_is_left_alone(self):
        # One domain by construction - there is nothing to divide.
        r = self._ratings(pod="Techies")
        out, stats = polls.apply_pod_split(r, {"999": polls_text()}, {"B41": PODS})
        self.assertEqual(stats["rooms"], 0)
        self.assertNotIn("pod_ratings", out[("B41", "09_19", "Techies")])

    def test_a_batch_with_no_pods_keeps_its_single_figure(self):
        # B17-B34: the POD era starts at B35, so this is the normal case for
        # most of the corpus and must not be reported as a failure.
        r = self._ratings()
        out, stats = polls.apply_pod_split(r, {"999": polls_text()}, {})
        self.assertEqual(stats["split"], 0)
        self.assertEqual(stats["kept"], {"no-pods": 1})
        self.assertNotIn("pod_ratings", out[("B41", "09_19", "")])

    def test_an_anonymous_poll_cannot_be_split_and_says_so(self):
        anon = "\n".join([
            "Poll Report", "",
            "#,User Name,Email Address,Submitted Date and Time,"
            "What was your overall session feedback?",
            "1,Person 1,anonymous,09/19/2026 20:11:04,5",
        ])
        r = self._ratings()
        out, stats = polls.apply_pod_split(r, {"999": anon}, {"B41": PODS})
        self.assertEqual(stats["split"], 0)
        self.assertNotIn("pod_ratings", out[("B41", "09_19", "")])

    def test_a_poll_that_was_never_fetched_is_reported_not_crashed(self):
        r = self._ratings()
        out, stats = polls.apply_pod_split(r, {}, {"B41": PODS})
        self.assertEqual(stats["kept"], {"no-bytes": 1})


def polls_text():
    """A wide-form poll naming two respondents, one Finance and one Techies.

    The real Zoom shape, not an invented one: a title row, a blank, then the
    header. parse_responses returns [] for anything else - an earlier version
    of this fixture handed the split nothing and made the test look like a bug
    in the code under test.
    """
    return "\n".join([
        "Poll Report", "",
        "#,User Name,Email Address,Submitted Date and Time,"
        "What was your overall session feedback?,How would you rate the trainer?",
        "1,Person 1,f1@x.com,09/19/2026 20:11:04,5,5",
        "2,Person 2,t1@x.com,09/19/2026 20:11:09,3,3",
    ])


class TestRosterPodEmails(unittest.TestCase):
    def _tabs(self, pod_for_second="Finance"):
        return {"AI CAP B41": [
            ["Country", "Registered Number", "Registered Mail", "WhatsApp",
             "Broadcast", "Batch", "Amount", "Payment", "Close Type",
             "POD Prefrence"],
            [91, "919000000001", "t1@x.com", "", "", "B41", 0, "Full Paid",
             "BDA Closing", "Techies"],
            [91, "919000000002", "f1@x.com", "", "", "B41", 0, "Full Paid",
             "BDA Closing", pod_for_second],
        ]}

    def test_emails_are_grouped_by_canonical_pod(self):
        out = ddata.roster_pod_emails(self._tabs())
        self.assertEqual(out["B41"]["Techies"], frozenset({"t1@x.com"}))
        self.assertEqual(out["B41"]["Finance"], frozenset({"f1@x.com"}))

    def test_the_roster_cells_long_form_is_canonicalised(self):
        # The header says `Techies`, the cell says `Techies - Ai Career
        # Accelerator Program B35`. Comparing raw matched ZERO of 3,236
        # students once already (CLAUDE.md 4e).
        out = ddata.roster_pod_emails(
            self._tabs("Finance - Ai Career Accelerator Program B41"))
        self.assertEqual(out["B41"]["Finance"], frozenset({"f1@x.com"}))

    def test_a_student_with_no_pod_is_omitted_not_bucketed(self):
        # They are in the room but in no domain. Putting them in one would be
        # an invention; putting them in all of them would double-count.
        out = ddata.roster_pod_emails(self._tabs(""))
        self.assertNotIn("f1@x.com", set().union(*out["B41"].values()))
        self.assertEqual(out["B41"]["Techies"], frozenset({"t1@x.com"}))


if __name__ == "__main__":
    unittest.main()


class TestRenderedTable(unittest.TestCase):
    """What the By-domain table actually shows. A rating off a handful of
    answers is noise presented as a score: Content Creators returned 5.00 from
    five responses on B39's 12 Sep, and one more answer moves that a full
    point."""

    def _batch(self, n_finance, n_data):
        return {"sessions": [{
            "date_lbl": "12 Sep",
            "pod_split": {"Finance": {"present": 47, "total": 90, "pct": 52.2},
                          "Data": {"present": 5, "total": 10, "pct": 50.0}},
            "pod_ratings": {
                "Finance": {"session": 4.62, "responses": n_finance},
                "Data": {"session": 5.0, "responses": n_data},
            },
        }]}

    def test_a_well_answered_domain_shows_its_rating(self):
        html = dash_view.domain_matrix_html(self._batch(47, 40))
        self.assertIn("4.62", html)
        self.assertIn("n=47", html)

    def test_a_thin_sample_shows_the_COUNT_and_not_the_score(self):
        html = dash_view.domain_matrix_html(self._batch(47, 5))
        self.assertIn("n=5", html, "the count must still be visible")
        self.assertNotIn("5.00", html, "but not presented as a rating")

    def test_attendance_still_renders_when_there_are_no_ratings_at_all(self):
        d = self._batch(47, 40)
        del d["sessions"][0]["pod_ratings"]
        html = dash_view.domain_matrix_html(d)
        self.assertIn("47/90", html)
        self.assertNotIn("n=", html)


COMPOUND = "Sales/Marketing/HR + Content Creators"


def compound_polls_text():
    """The 27 Sep 2026 room in miniature: two S/M/HR respondents, one Content
    Creator and one stranger - in the real Zoom shape (title row, blank line,
    then the header), which `parse_responses` insists on."""
    return "\n".join([
        "Poll Report", "",
        "#,User Name,Email Address,Submitted Date and Time,"
        "What was your overall session feedback?,How would you rate the trainer?",
        "1,Person 1,s1@x.com,09/27/2026 20:11:04,5,5",
        "2,Person 2,s2@x.com,09/27/2026 20:11:06,3,3",
        "3,Person 3,c1@x.com,09/27/2026 20:11:09,4,4",
        "4,Person 4,nobody@x.com,09/27/2026 20:11:12,1,1",
    ])


class TestCompoundRoomPollSplit(unittest.TestCase):
    """27 Sep 2026: 'AI CAP B35 , B36 , B37 , B38 - S/M/HR + Content Creators'
    was ONE webinar, ONE poll, TWO PODs. Keyed under the compound string the
    marker writes, its poll must still be divided by domain: the live store
    already divides that day's poll (B35: S/M/HR 13 answers, Content Creators
    9; B36 23/3; B37 26/10; B38 18/4) and the By-domain table prints the
    rating beneath each attendance row. A filter reading "any pod key means
    one domain" dropped the entry before looking at it, and the two rows came
    back with no rating at all."""
    PODS = {
        "Sales/Marketing/HR": frozenset({"s1@x.com", "s2@x.com"}),
        "Content Creators": frozenset({"c1@x.com"}),
        "Techies": frozenset({"t1@x.com"}),
    }

    def _ratings(self, pod):
        return {("B35", "09_27", pod): {"_wid": "555", "session": 3.25,
                                        "responses": 4, "_batches": ["B35"]}}

    def test_multi_domain_is_the_one_rule_for_the_split_and_the_fetch(self):
        # pipeline [5a.2] fetches the poll bytes of exactly the rooms
        # apply_pod_split will divide; both read this predicate.
        self.assertTrue(polls.multi_domain(""))
        self.assertTrue(polls.multi_domain(None))
        self.assertTrue(polls.multi_domain(COMPOUND))
        self.assertFalse(polls.multi_domain("Techies"))
        self.assertFalse(polls.multi_domain("Sales/Marketing/HR"))

    def test_the_compound_room_gains_a_per_pod_breakdown(self):
        out, stats = polls.apply_pod_split(
            self._ratings(COMPOUND), {"555": compound_polls_text()},
            {"B35": self.PODS})
        self.assertEqual(stats, {"rooms": 1, "split": 1, "kept": {}})
        pr = out[("B35", "09_27", COMPOUND)]["pod_ratings"]
        self.assertEqual(pr["Sales/Marketing/HR"]["responses"], 2)
        self.assertEqual(pr["Sales/Marketing/HR"]["session"], 4.0)
        self.assertEqual(pr["Content Creators"]["responses"], 1)
        self.assertEqual(pr["Content Creators"]["session"], 4.0)
        self.assertEqual(pr["_unmatched"], 1)          # the stranger
        # the room's own headline is untouched
        self.assertEqual(out[("B35", "09_27", COMPOUND)]["session"], 3.25)

    def test_the_breakdown_is_keyed_like_pod_split_so_the_table_joins_them(self):
        """domain_matrix_html looks up pod_ratings[p] for every p in
        pod_split, so the keys are the canonical POD names - never the
        compound string itself."""
        out, _ = polls.apply_pod_split(
            self._ratings(COMPOUND), {"555": compound_polls_text()},
            {"B35": self.PODS})
        pr = out[("B35", "09_27", COMPOUND)]["pod_ratings"]
        self.assertNotIn(COMPOUND, pr)
        self.assertTrue({"Sales/Marketing/HR", "Content Creators"} <= set(pr))

    def test_a_single_named_room_is_still_left_alone(self):
        out, stats = polls.apply_pod_split(
            self._ratings("Sales/Marketing/HR"), {"555": compound_polls_text()},
            {"B35": self.PODS})
        self.assertEqual(stats["rooms"], 0)
        self.assertNotIn("pod_ratings",
                         out[("B35", "09_27", "Sales/Marketing/HR")])

    def test_an_unlabelled_room_is_still_divided(self):
        out, stats = polls.apply_pod_split(
            self._ratings(""), {"555": compound_polls_text()}, {"B35": self.PODS})
        self.assertEqual(stats["split"], 1)
        self.assertIn("pod_ratings", out[("B35", "09_27", "")])

    def test_the_rendered_table_prints_the_rating_under_each_attendance_row(self):
        """The symptom, end to end: a compound session whose pod_ratings came
        from apply_pod_split renders a per-POD count beneath each share."""
        out, _ = polls.apply_pod_split(
            self._ratings(COMPOUND), {"555": compound_polls_text()},
            {"B35": self.PODS})
        d = {"sessions": [{
            "date_lbl": "27 Sep", "pod": COMPOUND,
            "pod_split": {"Sales/Marketing/HR": {"present": 2, "total": 3, "pct": 66.7},
                          "Content Creators": {"present": 1, "total": 2, "pct": 50.0}},
            "pod_ratings": out[("B35", "09_27", COMPOUND)]["pod_ratings"],
        }]}
        html = dash_view.domain_matrix_html(d)
        self.assertIn("2/3", html)
        self.assertIn("1/2", html)
        self.assertIn("n=2", html)      # S/M/HR answered twice
        self.assertIn("n=1", html)      # Content Creators once
