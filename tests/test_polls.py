"""Poll ratings: NPS, the 1-5 histograms, and the shapes they are read from.

The scale trap is the reason this file exists. The obvious NPS implementation
sniffs the scale from the data — "if any answer is above 5 it must be a 0-10
question" — and that silently inverts a bad session: a genuine 0-10 poll where
nobody scored above 5 is all detractors (-100), but the sniffing rule reads it
as a 1-5 poll and scores every 5 as a promoter (+100). polls.nps commits to 1-5
instead, and these tests pin that commitment.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polls  # noqa: E402


def wide(rows, question="How likely would you recommend it to your friends?"):
    """Zoom's wide export: one row per respondent, one column per question."""
    out = ["Poll Report", "",
           "#,User Name,Email Address,Submitted Date and Time," + question]
    for i, v in enumerate(rows, 1):
        out.append(f"{i},Person {i},p{i}@x.com,08/30/2026 20:11:04,{v}")
    return "\n".join(out)


class TestNps(unittest.TestCase):
    def test_all_fives_is_plus_100(self):
        self.assertEqual(polls.nps([5, 5, 5, 5]), 100)

    def test_all_ones_is_minus_100(self):
        self.assertEqual(polls.nps([1, 1, 1]), -100)

    def test_four_is_passive_and_scores_zero(self):
        self.assertEqual(polls.nps([4, 4, 4, 4]), 0)

    def test_three_is_a_detractor_not_a_passive(self):
        # The whole point of top-box: a 3 out of 5 is not neutral.
        self.assertEqual(polls.nps([3]), -100)

    def test_a_realistic_mix(self):
        # 6 promoters, 2 passives, 2 detractors over 10 -> (6-2)/10 = +40
        self.assertEqual(polls.nps([5] * 6 + [4] * 2 + [2, 1]), 40)

    def test_no_answers_is_None_not_zero(self):
        # "nobody answered" and "promoters equalled detractors" are different
        # facts and must not render as the same number.
        self.assertIsNone(polls.nps([]))
        self.assertIsNone(polls.nps(None))
        self.assertIsNone(polls.nps([None, None]))

    def test_float_drift_still_buckets_correctly(self):
        # A spreadsheet round-trip can hand back 4.999999 for a 5.
        self.assertEqual(polls.nps([4.999999, 5.0]), 100)

    def test_the_scale_is_never_inferred_from_the_values(self):
        """The trap, pinned.

        A 0-10 poll where everyone answered 5 is every respondent a detractor.
        A scale-sniffing implementation reads it as 1-5 and returns +100. This
        one returns -100 either way, because it does not guess.
        """
        self.assertEqual(polls.nps([5, 5, 5]), 100)   # genuinely 1-5: correct
        # and _score refuses to admit an out-of-range value in the first place,
        # so a 0-10 answer can never reach nps() and change the rule underneath it
        self.assertIsNone(polls._score("9"))
        self.assertIsNone(polls._score("0"))
        self.assertIsNone(polls._score("10"))


class TestDistribution(unittest.TestCase):
    def test_every_bucket_is_present_even_when_empty(self):
        d = polls._distribution([5, 5, 3])
        self.assertEqual(d, {"1": 0, "2": 0, "3": 1, "4": 0, "5": 2})

    def test_empty_input_still_gives_five_buckets(self):
        # The UI draws a five-bar chart; a short dict would silently drop bars.
        self.assertEqual(polls._distribution([]), {str(i): 0 for i in range(1, 6)})

    def test_the_histogram_and_the_mean_can_disagree_usefully(self):
        # Both average 3.0, but one room was lukewarm and one was split.
        lukewarm = polls._distribution([3, 3, 3, 3])
        polarised = polls._distribution([5, 5, 1, 1])
        self.assertEqual(sum(int(k) * v for k, v in lukewarm.items()) / 4, 3.0)
        self.assertEqual(sum(int(k) * v for k, v in polarised.items()) / 4, 3.0)
        self.assertNotEqual(lukewarm, polarised)


class TestCombiningSessions(unittest.TestCase):
    """Eleven POD sessions on one date roll up to one dashboard row."""

    def test_nps_from_dist_matches_nps_from_raw_answers(self):
        # One rule, two entry points. If these ever disagree the dashboard row
        # and the session row would show different NPS for the same poll.
        for scores in ([5, 5, 4, 1], [3, 3, 3], [5], [1, 2, 3, 4, 5]):
            self.assertEqual(polls.nps(scores),
                             polls.nps_from_dist(polls._distribution(scores)),
                             f"disagreed on {scores}")

    def test_merging_sums_histograms(self):
        a = {"recommend": {"1": 1, "2": 0, "3": 0, "4": 0, "5": 3}}
        b = {"recommend": {"1": 0, "2": 2, "3": 0, "4": 1, "5": 0}}
        m = polls.merge_dists([a, b])
        self.assertEqual(m["recommend"], {"1": 1, "2": 2, "3": 0, "4": 1, "5": 3})

    def test_a_big_pod_outweighs_a_small_one(self):
        """The bug this rule exists to prevent.

        A 300-response pod at +100 and a 3-response pod at -100 is NOT 0. Any
        implementation that averages the two percentages says 0; summing the
        histograms says +98, which is what actually happened in the room.
        """
        big = {"recommend": polls._distribution([5] * 300)}
        small = {"recommend": polls._distribution([1] * 3)}
        merged = polls.merge_dists([big, small])
        self.assertEqual(polls.nps_from_dist(merged["recommend"]), 98)
        naive_average = (100 + -100) / 2
        self.assertNotEqual(polls.nps_from_dist(merged["recommend"]), naive_average)

    def test_merging_nothing_is_None_not_zero(self):
        self.assertIsNone(polls.nps_from_dist({}))
        self.assertIsNone(polls.nps_from_dist(None))
        self.assertIsNone(polls.nps_from_dist({str(i): 0 for i in range(1, 6)}))
        self.assertEqual(polls.merge_dists([]), {})
        self.assertEqual(polls.merge_dists([None, {}]), {})

    def test_a_session_with_no_poll_does_not_drag_the_rollup(self):
        # An unrated pod contributes no counts at all rather than zeros.
        rated = {"recommend": polls._distribution([5, 5, 4])}
        merged = polls.merge_dists([rated, {}, None])
        self.assertEqual(polls.nps_from_dist(merged["recommend"]), 67)


class TestParseCarriesThem(unittest.TestCase):
    def test_wide_export_yields_avg_dist_and_nps(self):
        got = polls.parse(wide([5, 5, 4, 1]))
        self.assertEqual(got["recommend"], 3.75)
        self.assertEqual(got["responses"], 4)
        self.assertEqual(got["dist"]["recommend"], {"1": 1, "2": 0, "3": 0, "4": 1, "5": 2})
        self.assertEqual(got["nps"], 25)          # (2 - 1) / 4

    def test_indexed_long_export_yields_them_too(self):
        text = "\n".join([
            "Poll Report", "",
            "#,User Name,User Email,Submitted Date and Time,Question,Answer",
            "1,A,a@x.com,08/30/2026 20:11:04,How likely would you recommend it to your friends?,5",
            "2,B,b@x.com,08/30/2026 20:11:09,How likely would you recommend it to your friends?,2",
        ])
        got = polls.parse(text)
        self.assertEqual(got["dist"]["recommend"], {"1": 0, "2": 1, "3": 0, "4": 0, "5": 1})
        self.assertEqual(got["nps"], 0)           # one promoter, one detractor

    def test_no_recommend_question_leaves_nps_None(self):
        got = polls.parse(wide([5, 4], question="How would you rate the trainer?"))
        self.assertIsNone(got["nps"])
        self.assertEqual(got["dist"]["trainer"], {"1": 0, "2": 0, "3": 0, "4": 1, "5": 1})
        self.assertEqual(got["dist"]["recommend"], {str(i): 0 for i in range(1, 6)})

    def test_the_payload_survives_the_dedupe_and_the_session_join(self):
        # parse_files keeps the copy with the most responses; the new keys must
        # ride along rather than being dropped by the copy that wins.
        small = wide([5]).encode()
        big = wide([5, 5, 1]).encode()
        by_wid = polls.parse_files([
            ("poll_91695866411_2026_08_30.csv", small),
            ("poll_91695866411_2026_08_30 (1).csv", big),
        ])
        self.assertEqual(by_wid["91695866411"]["responses"], 3)
        self.assertEqual(by_wid["91695866411"]["nps"], 33)
        self.assertIn("dist", by_wid["91695866411"])


class TestSubmissionTimes(unittest.TestCase):
    """The poll marker's input: when the room actually started answering.

    The reference app marks the moment a moderator SAYS they circulated the
    poll. This reads the timestamp Zoom already writes against every response,
    so the marker needs no new data entry and cannot drift from what happened.
    """

    def test_times_come_back_sorted_even_when_the_export_is_not(self):
        text = "\n".join([
            "Poll Report", "",
            "#,User Name,Email Address,Submitted Date and Time,Q",
            "1,A,a@x.com,08/30/2026 20:41:00,5",
            "2,B,b@x.com,08/30/2026 20:11:04,4",
        ])
        ts = polls.submission_times(text)
        self.assertEqual([t.strftime("%H:%M:%S") for t in ts],
                         ["20:11:04", "20:41:00"])

    def test_am_pm_and_24_hour_shapes_both_parse(self):
        for stamp in ("08/30/2026 08:11:04 PM", "08/30/2026 20:11:04",
                      "2026-08-30 20:11:04"):
            ts = polls.submission_times(
                "#,User Name,Email Address,Submitted Date and Time,Q\n"
                f"1,A,a@x.com,{stamp},5")
            self.assertEqual(len(ts), 1, stamp)
            self.assertEqual(ts[0].hour, 20, stamp)

    def test_a_file_without_the_column_returns_empty_not_a_guess(self):
        # 8 of 1,113 cached poll files carry no submitted column. They must
        # yield no marker rather than a plausible-looking wrong one.
        self.assertEqual(polls.submission_times(
            "#,User Name,Email Address,Q\n1,A,a@x.com,5"), [])

    def test_unparseable_stamps_are_skipped_not_zeroed(self):
        # A zero would place the marker at minute 0 of the curve -- a claim
        # that the room answered before the session began.
        ts = polls.submission_times(
            "#,User Name,Email Address,Submitted Date and Time,Q\n"
            "1,A,a@x.com,not a date,5\n"
            "2,B,b@x.com,08/30/2026 20:11:04,4")
        self.assertEqual(len(ts), 1)

    def test_parse_files_carries_the_first_submission(self):
        by_wid = polls.parse_files([
            ("poll_91695866411_2026_08_30.csv", wide([5, 4]).encode())])
        self.assertEqual(by_wid["91695866411"]["submitted_first"],
                         "2026-08-30T20:11:04")

    def test_submitted_first_is_None_when_there_is_no_column(self):
        text = ("Poll Report\n\n#,User Name,Email Address,"
                "How likely would you recommend it to your friends?\n"
                "1,A,a@x.com,5")
        by_wid = polls.parse_files([
            ("poll_91695866411_2026_08_30.csv", text.encode())])
        self.assertIsNone(by_wid["91695866411"]["submitted_first"])


if __name__ == "__main__":
    unittest.main()
