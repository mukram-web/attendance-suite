"""Domain-POD tests: name reconciliation, per-POD denominators, date rollup."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data  # noqa: E402
import pods  # noqa: E402
from tests.test_data import make_tab  # noqa: E402


class TestCanon(unittest.TestCase):
    def test_every_roster_spelling_seen_in_b35_b38(self):
        for raw, want in [
            ("AI Generalist", "Generalist"),
            ("Techies", "Techies"),
            ("Finance", "Finance"),
            ("Business Owners/Enterpreneurs", "Business Owners"),
            ("Sales/Marketing/HR", "Sales/Marketing/HR"),
            ("Operations/Supply Chain", "Ops/Supply Chain"),
            ("Educators", "Educators"),
            ("Students", "Students"),
            ("Healthcare", "Healthcare"),
            ("Data", "Data"),
            ("Content Creators", "Content Creators"),
            ("Techies - AI Career Accelerator Program B35", "Techies"),
        ]:
            self.assertEqual(pods.from_roster_cell(raw)[0], want, raw)

    def test_blank_and_unidentified_are_a_real_bucket(self):
        for raw in ("", None, "Unidentified", "   "):
            self.assertEqual(pods.from_roster_cell(raw)[0], pods.UNKNOWN, repr(raw))

    def test_multi_valued_cell_takes_the_last_and_flags_itself(self):
        got, multi = pods.from_roster_cell(
            "Finance - AI Career Accelerator Program B36; Business Owners/Enterpreneurs")
        self.assertEqual(got, "Business Owners")
        self.assertTrue(multi)

    def test_every_l2_spelling_seen_on_b35_plus(self):
        for raw, want in [
            ("AI CAP B35 - Techies", "Techies"),
            ("AI CAP B35 - Generalist", "Generalist"),
            ("AICAPB35, B36-S/M/HR", "Sales/Marketing/HR"),
            ("AICAPB35, B36-Ops/SC", "Ops/Supply Chain"),
            ("AICAPB35, B36-BusinessOwners", "Business Owners"),
            ("AICAPB35, B36-ContentCreators", "Content Creators"),
            ("AI CAP B35 - Ops/Supply Chain", "Ops/Supply Chain"),
        ]:
            self.assertEqual(pods.from_l2_label(raw), want, raw)

    def test_a_label_naming_no_domain_is_a_whole_batch_session(self):
        for raw in ("AI CAP B35", "AI CAP B17", "B37-42", "AI CAP B35 - All Domains",
                    "AICAPB37- Common"):
            self.assertEqual(pods.from_l2_label(raw), pods.WHOLE_BATCH, raw)

    def test_general_is_the_whole_batch_but_generalist_is_the_pod(self):
        """B36 uses both, so they cannot mean the same thing: 'General' on 22 Aug
        (whole batch) and 'Generalist' on 30 Aug (the AI Generalist POD, one of
        eleven that day). Folding them together scored a whole-batch session
        against a POD - B37's 22 Aug read 1,631 present out of 571."""
        self.assertEqual(pods.from_l2_label("AI CAP B36 - General"), pods.WHOLE_BATCH)
        self.assertEqual(pods.from_l2_label("AI CAP B37 - General"), pods.WHOLE_BATCH)
        self.assertEqual(pods.from_l2_label("AICAPB35, B36-Generalist"), "Generalist")
        self.assertEqual(pods.from_roster_cell("AI Generalist")[0], "Generalist")

    def test_an_unknown_domain_is_none_so_the_caller_can_warn(self):
        """Never invent a bucket: a new POD silently folded into another would
        move a denominator with nothing on screen to say so."""
        self.assertIsNone(pods.from_l2_label("AI CAP B35 - Robotics"))
        self.assertIsNone(pods.canon("Robotics"))

    def test_a_topic_in_the_domain_slot_is_not_read_as_a_domain(self):
        self.assertIsNone(pods.from_folder(
            "2026-08-02 - AI CAP B33 - Office Productivity & Communication"))
        self.assertEqual(pods.from_folder(
            "2026-08-23 - AI CAP B35 - Techies - Python with AI"), "Techies")


class TestRunTogetherFolderNames(unittest.TestCase):
    """'AICAPB37- Techies' has no space before the dash and no word boundary
    before B37. The folder resolved to NO batch, so the session was never even
    downloaded and L2 was never consulted - it vanished with no warning."""

    def test_run_together_folder_resolves_to_its_batch(self):
        import attendance_core as ac
        for name, want in [
            ("2026-08-30 - AICAPB37- Techies - Cursor AI: Build Software", ("CAP", 37)),
            ("2026-08-30 - AICAPB38- AllDomains - Your AI Employee at work", ("CAP", 38)),
            ("2026-08-30 - AICAPB37- Common - Excel with AI", ("CAP", 37)),
        ]:
            self.assertIn(want, ac._folder_batches(name), name)

    def test_spaced_forms_still_resolve(self):
        import attendance_core as ac
        self.assertEqual(ac._folder_batches(
            "2026-08-23 - AI CAP B35 - Techies - Python with AI"), {("CAP", 35)})
        self.assertEqual(ac._folder_batches(
            "2026-08-02 - AI CAP B33 - Office Productivity"), {("CAP", 33)})


def _pod_tab(pod_rows, marks_by_col, headers):
    """A roster tab with a POD column and one column per (date, POD) session.

    pod_rows      : list of POD cell values, one per student
    marks_by_col  : list of columns, each a list of 'Present'/'Absent'/'' per student
    headers       : row-1 header for each session column
    """
    head = ["CountryCode", "Registered Number", "Registered mail", "CountryCode",
            "Whatsaap Number", "broadcast mail", "batch name", "Amount",
            "Payment", "Closing Type", "POD pref"] + headers
    rows = [head]
    for i, pod in enumerate(pod_rows):
        r = [91, 900000000 + i, f"s{i}@x.com", 91, 900000000 + i, f"s{i}@x.com",
             "AI CAP B35", 1000, "Full Paid", "BDA Closing", pod]
        r += [col[i] for col in marks_by_col]
        rows.append(r)
    return rows


class TestPerPodDenominator(unittest.TestCase):
    """10 students: 6 Techies, 4 Generalist. One Techies-only session on 23 Aug
    that 3 of the 6 attended."""

    def setUp(self):
        self.pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        marks = [["Present"] * 3 + ["Absent"] * 3 + [""] * 4]
        self.rows = _pod_tab(self.pod_rows, marks, ["2026_08_23 | Techies"])

    def test_a_pod_session_is_scored_against_that_pod_not_the_batch(self):
        b = data.build_batch(self.rows, "B35", {("B35", "08_23"): "Python with AI"})
        s = b["sessions"][0]
        self.assertEqual(s["pod"], "Techies")
        self.assertEqual(s["total"], 6)        # the POD, not the 10-strong batch
        self.assertEqual(s["present"], 3)
        self.assertEqual(s["pct"], 50.0)       # 3/6, NOT 3/10 = 30%
        self.assertEqual(s["absent"], 3)

    def test_pod_membership_is_reported_per_batch(self):
        b = data.build_batch(self.rows, "B35", {("B35", "08_23"): "T"})
        self.assertEqual(b["pods"]["Techies"]["strength"], 6)
        self.assertEqual(b["pods"]["Generalist"]["strength"], 4)

    def test_a_small_pod_session_survives_the_validity_threshold(self):
        """Thresholds scale to the POD. Against the batch, a 2-member POD could
        never clear '30% of strength marked' and its session would vanish."""
        pod_rows = ["Data"] * 2 + ["AI Generalist"] * 18
        marks = [["Present", "Absent"] + [""] * 18]
        rows = _pod_tab(pod_rows, marks, ["2026_08_23 | Data"])
        b = data.build_batch(rows, "B35", {("B35", "08_23"): "VBA with AI"})
        self.assertEqual(b["n_sessions"], 1)
        self.assertEqual(b["sessions"][0]["total"], 2)
        self.assertEqual(b["sessions"][0]["pct"], 50.0)


class TestDateRollup(unittest.TestCase):
    """Two PODs meeting the same day: the batch line must weight by POD size."""

    def setUp(self):
        pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        techies = ["Present"] * 3 + ["Absent"] * 3 + [""] * 4      # 3 of 6
        general = [""] * 6 + ["Present"] * 4                       # 4 of 4
        self.rows = _pod_tab(pod_rows, [techies, general],
                             ["2026_08_23 | Techies", "2026_08_23 | Generalist"])
        self.l2 = {("B35", "08_23"): "Same-day sessions"}

    def test_both_sessions_survive_instead_of_merging_into_one_column(self):
        b = data.build_batch(self.rows, "B35", self.l2)
        self.assertEqual(b["n_sessions"], 2)
        self.assertEqual(sorted(s["pod"] for s in b["sessions"]),
                         ["Generalist", "Techies"])

    def test_the_batch_headline_is_weighted_by_pod_size(self):
        b = data.build_batch(self.rows, "B35", self.l2)
        self.assertEqual(len(b["by_date"]), 1)
        d = b["by_date"][0]
        self.assertEqual(d["present"], 7)       # 3 Techies + 4 Generalist
        self.assertEqual(d["n_pods"], 2)
        self.assertEqual(d["pct"], 70.0)        # 7 of the 10-strong batch
        self.assertEqual(b["avg_pct"], 70.0)    # NOT the 75% mean of 50% and 100%


class TestPodPercentCannotExceedItsPod(unittest.TestCase):
    def test_marks_outside_the_pod_do_not_inflate_the_numerator(self):
        """B37's 22 Aug read 1,631 present out of 571 - 285.6% - because the
        column carried marks for the whole batch while the denominator was the
        POD. Numerator and denominator must span the same people."""
        pod_rows = ["AI Generalist"] * 4 + ["Techies"] * 6
        # a Generalist-labelled column that (wrongly) marks everyone present
        marks = [["Present"] * 10]
        rows = _pod_tab(pod_rows, marks, ["2026_08_22 | Generalist"])
        b = data.build_batch(rows, "B37", {("B37", "08_22"): "Prompt Engineering"})
        s = b["sessions"][0]
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["present"], 4)      # the 4 Generalists, not all 10
        self.assertEqual(s["pct"], 100.0)
        self.assertLessEqual(s["pct"], 100.0)

    def test_no_session_anywhere_can_report_over_100_percent(self):
        pod_rows = ["Data"] * 2 + ["AI Generalist"] * 18
        rows = _pod_tab(pod_rows, [["Present"] * 20], ["2026_08_23 | Data"])
        b = data.build_batch(rows, "B35", {("B35", "08_23"): "x"})
        for s in b["sessions"]:
            self.assertLessEqual(s["present"], s["total"], s)
            self.assertLessEqual(s["pct"], 100.0, s)


class TestOnlyInvitedStudentsCount(unittest.TestCase):
    def test_a_day_only_one_pod_met_is_scored_against_that_pod(self):
        """B35's 22 Aug: the Generalist POD alone met. The other 2,291 students
        were never invited, so they are not absentees - dividing by the whole
        batch reported 12% for a session 41% of its POD attended."""
        pod_rows = ["AI Generalist"] * 4 + ["Techies"] * 6
        marks = [["Present", "Present", "Absent", "Absent"] + [""] * 6]
        rows = _pod_tab(pod_rows, marks, ["2026_08_22 | Generalist"])
        b = data.build_batch(rows, "B35", {("B35", "08_22"): "Agents Part 1"})
        d = b["by_date"][0]
        self.assertEqual(d["total"], 4)     # the Generalist POD, not all 10
        self.assertEqual(d["present"], 2)
        self.assertEqual(d["pct"], 50.0)    # not 2/10 = 20%

    def test_someone_in_both_a_pod_and_a_whole_batch_session_counts_once(self):
        """A Techies student attending both that day's whole-batch session and
        their POD session is one person present, not two."""
        pod_rows = ["Techies"] * 4 + ["AI Generalist"] * 6
        whole = ["Present"] * 4 + ["Absent"] * 6          # everyone invited
        techies = ["Present"] * 4 + [""] * 6              # same 4 again
        rows = _pod_tab(pod_rows, [whole, techies],
                        ["2026_08_15", "2026_08_15 | Techies"])
        b = data.build_batch(rows, "B35", {("B35", "08_15"): "x"})
        d = b["by_date"][0]
        self.assertEqual(d["total"], 10)    # a whole-batch session invites all
        self.assertEqual(d["present"], 4)   # NOT 8
        self.assertEqual(d["pct"], 40.0)


class TestClosingTypeIsPerPerson(unittest.TestCase):
    def test_a_student_is_scored_only_on_sessions_their_pod_ran(self):
        """6 Techies, 4 Generalist, one session each. Every student attended
        their own. Dividing by all 2 sessions would report 50%; the honest
        number is 100% - nobody could attend the other POD's session."""
        pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        techies = ["Present"] * 6 + [""] * 4
        general = [""] * 6 + ["Present"] * 4
        rows = _pod_tab(pod_rows, [techies, general],
                        ["2026_08_23 | Techies", "2026_08_23 | Generalist"])
        b = data.build_batch(rows, "B35", {("B35", "08_23"): "x"})
        self.assertEqual(len(b["closing"]), 1)            # all 'BDA Closing'
        self.assertEqual(b["closing"][0]["att"], 100.0)


class TestPodViewHandlesProgrammesWithoutPods(unittest.TestCase):
    """BSIAI builds no `by_date` rollup and no PODs. pod_view assumed both and
    returned an empty session list, blanking the whole BSIAI tab with
    "No sessions logged for None yet"."""

    def test_a_batch_with_no_by_date_is_returned_untouched(self):
        import dash_view
        d = {"code": "B1", "strength": 10, "active": 10, "n_sessions": 1,
             "avg_pct": 50.0, "peak": 50.0, "low": 50.0, "closing": [],
             "sessions": [{"col": None, "mm": "08_15", "date_lbl": "15 Aug",
                           "topic": "x", "present": 5, "absent": 5, "total": 10,
                           "pct": 50.0, "present_only": False, "no_l2": False}]}
        self.assertEqual(dash_view.pod_names(d), [])
        v = dash_view.pod_view(d, None)
        self.assertEqual(len(v["sessions"]), 1)
        self.assertEqual(v["sessions"][0]["date_lbl"], "15 Aug")


class TestPodViewRowContract(unittest.TestCase):
    """Every row pod_view emits is rendered by the same table as a real session,
    so it must carry the same keys. A rollup row missing "absent" crashed the
    whole Dashboard tab with a KeyError."""

    KEYS = {"date_lbl", "topic", "present", "absent", "total", "pct",
            "present_only", "no_l2"}

    def test_all_pods_rollup_rows_carry_every_key_the_table_reads(self):
        import dash_view
        pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        techies = ["Present"] * 3 + ["Absent"] * 3 + [""] * 4
        general = [""] * 6 + ["Present"] * 4
        rows = _pod_tab(pod_rows, [techies, general],
                        ["2026_08_23 | Techies", "2026_08_23 | Generalist"])
        d = data.build_batch(rows, "B35", {("B35", "08_23"): "x"})
        for sel in [None] + dash_view.pod_names(d):
            for row in dash_view.pod_view(d, sel)["sessions"]:
                self.assertFalse(self.KEYS - set(row),
                                 f"pod={sel!r} missing {sorted(self.KEYS - set(row))}")
            dash_view.sessions_table_html(dash_view.pod_view(d, sel))

    def test_rollup_absent_is_consistent_with_present_and_total(self):
        import dash_view
        pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        rows = _pod_tab(pod_rows, [["Present"] * 3 + ["Absent"] * 3 + [""] * 4],
                        ["2026_08_23 | Techies"])
        d = data.build_batch(rows, "B35", {("B35", "08_23"): "x"})
        row = dash_view.pod_view(d, None)["sessions"][0]
        self.assertEqual(row["present"] + row["absent"], row["total"])


class TestNoPodBatchIsUnchanged(unittest.TestCase):
    def test_a_batch_with_no_pod_column_behaves_exactly_as_before(self):
        """The regression guarantee for B17-B34: no POD column, no PODs, and the
        weighted rollup collapses to one session per date - the old number."""
        rows = make_tab(
            [{"mail": f"s{i}@x.com", "payment": "Full Paid", "closing": "BDA Closing",
              "marks": ["Present" if i < 6 else "Absent"]} for i in range(10)],
            session_dates=["2026_04_04"], session_topics=["Topic"],
        )
        b = data.build_batch(rows, "B17", {("B17", "04_04"): "Topic"})
        self.assertEqual(b["pods"], {})
        self.assertEqual(b["sessions"][0]["pod"], "")
        self.assertEqual(b["sessions"][0]["total"], 10)
        self.assertEqual(b["avg_pct"], 60.0)


if __name__ == "__main__":
    unittest.main()


class TestPollRatings(unittest.TestCase):
    """Ratings come from the session's own feedback poll, joined on Webinar ID."""

    WIDE = ("Overview\n"
            "Generate Time,Meeting Topic,Meeting/Webinar ID,Actual Start Time\n"
            "08/29/2026,Demand Forecasting,92569378808,08/29/2026\n\n"
            "Launched Polls\n#,Poll Name,Questions,Responses\n1,Feedback,4,3\n\n"
            "Feedback\n"
            "#,User Name,Email Address,Submitted Date and Time,"
            "What was your overall session feedback? ,How would you rate the trainer?,"
            "How likely would you recommend it to your friends?,"
            "What can we improve in future sessions? (Description)\n"
            "1,A,a@x.com,08/29/2026,5,5,5,all good\n"
            "2,B,b@x.com,08/29/2026,4,3,4,more practice\n"
            "3,C,c@x.com,08/29/2026,3,4,2,\n")

    def test_wide_export_averages_each_question(self):
        import polls
        r = polls.parse(self.WIDE)
        self.assertEqual(r["responses"], 3)
        self.assertAlmostEqual(r["session"], 4.0)
        self.assertAlmostEqual(r["trainer"], 4.0)
        self.assertAlmostEqual(r["recommend"], 3.67, places=2)

    def test_free_text_column_is_never_read_as_a_score(self):
        import polls
        self.assertIsNone(polls._classify(
            "What can we improve in future sessions? (Description)"))

    def test_question_wording_variants_all_classify(self):
        import polls
        for q, want in [
            ("What was your overall session feedback?", "session"),
            ("How was your overall experience in today's project session?", "session"),
            ("How satisfied are you with today's project session?", "session"),
            ("How would you rate the trainer? Please note: 1 = Very Poor", "trainer"),
            ("How likely would you recommend it to your friends?", "recommend"),
        ]:
            self.assertEqual(polls._classify(q), want, q)

    def test_out_of_range_values_are_dropped(self):
        import polls
        self.assertIsNone(polls._score("great"))
        self.assertIsNone(polls._score("9"))
        self.assertEqual(polls._score("5 - Excellent"), 5.0)

    def test_duplicate_exports_keep_the_one_with_more_responses(self):
        import polls
        small = self.WIDE
        big = self.WIDE + "4,D,d@x.com,08/29/2026,5,5,5,\n"
        got = polls.parse_files([("poll_111_2026_08_29.csv", small.encode()),
                                 ("poll_111_2026_08_29.csv", big.encode())])
        self.assertEqual(got["111"]["responses"], 4)

    def test_a_poll_with_no_rating_question_yields_nothing(self):
        import polls
        txt = ("Overview\n\nLaunched Polls\n#,Poll Name,Questions,Responses\n"
               "1,Quiz,1,2\n\nQuiz\n#,User Name,Email Address,"
               "Submitted Date and Time,Which tool did you use?\n"
               "1,A,a@x.com,08/29/2026,Excel\n")
        self.assertEqual(polls.parse_files([("poll_222_2026_08_29.csv", txt.encode())]), {})


class TestRatingSurvivesTheRollup(unittest.TestCase):
    """The default view rolls sessions up per date. The rating has to travel with
    them - it was dropped there, so every AI CAP session showed a dash while the
    data was sitting in the store."""

    def _batch(self):
        pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        techies = ["Present"] * 3 + ["Absent"] * 3 + [""] * 4
        general = [""] * 6 + ["Present"] * 4
        rows = _pod_tab(pod_rows, [techies, general],
                        ["2026_08_23 | Techies", "2026_08_23 | Generalist"])
        ratings = {("B35", "08_23", "Techies"): {"session": 4.0, "trainer": 4.1,
                                                 "recommend": 4.2, "responses": 100},
                   ("B35", "08_23", "Generalist"): {"session": 5.0, "trainer": 5.0,
                                                    "recommend": 5.0, "responses": 300}}
        return data.build_batch(rows, "B35", {("B35", "08_23"): "x"}, None, ratings)

    def test_ratings_reach_the_individual_sessions(self):
        b = self._batch()
        got = {s["pod"]: s["rating"] for s in b["sessions"]}
        self.assertEqual(got, {"Techies": 4.0, "Generalist": 5.0})

    def test_rollup_row_averages_by_responses_not_by_session(self):
        import dash_view
        row = dash_view.pod_view(self._batch(), None)["sessions"][0]
        # (4.0*100 + 5.0*300) / 400 = 4.75, NOT the flat mean of 4.5
        self.assertAlmostEqual(row["rating"], 4.75, places=2)
        self.assertEqual(row["rating_n"], 400)

    def test_a_date_with_no_poll_shows_no_rating_rather_than_zero(self):
        import dash_view
        pod_rows = ["Techies"] * 6 + ["AI Generalist"] * 4
        rows = _pod_tab(pod_rows, [["Present"] * 3 + ["Absent"] * 3 + [""] * 4],
                        ["2026_08_23 | Techies"])
        b = data.build_batch(rows, "B35", {("B35", "08_23"): "x"})
        for sel in [None, "Techies"]:
            for s in dash_view.pod_view(b, sel)["sessions"]:
                self.assertIsNone(s["rating"])
                self.assertIn("rating", s)


class TestPollReportExportShape(unittest.TestCase):
    """Zoom's "Poll Report" export puts one ROW per answer behind a header that
    LOOKS wide ('#, User Name, User Email, Submitted Time, Question, Answer').
    It defeated both other readers, so B38's 30 Aug showed no rating while a
    657-response poll sat on Drive."""

    REPORT = chr(10).join([
        "Poll Report",
        "Report generated time,08/30/2026 06:18:23 PM",
        "",
        "Topic,Webinar ID,Actual Start Time,# Poll",
        "Your AI Employee at work,99299344465,08/30/2026 11:00:00 AM,1190",
        "",
        "#,User Name,User Email,Submitted Time (America/Los_Angeles),Question,Answer",
        "1,A,a@x.com,08/30/2026,have you open claude ?,Yes",
        "2,A,a@x.com,08/30/2026,What was your overall session feedback? ,4",
        "3,A,a@x.com,08/30/2026,How would you rate the trainer?,5",
        "4,A,a@x.com,08/30/2026,What can we improve in future sessions? (Description),NA",
        "5,B,b@x.com,08/30/2026,What was your overall session feedback? ,2",
        "6,B,b@x.com,08/30/2026,How would you rate the trainer?,3",
    ])

    def test_ratings_are_read_from_the_row_per_answer_shape(self):
        import polls
        r = polls.parse(self.REPORT)
        self.assertAlmostEqual(r["session"], 3.0)     # (4 + 2) / 2
        self.assertAlmostEqual(r["trainer"], 4.0)     # (5 + 3) / 2

    def test_respondents_are_counted_as_people_not_answers(self):
        import polls
        self.assertEqual(polls.parse(self.REPORT)["responses"], 2)

    def test_non_rating_questions_in_that_shape_are_ignored(self):
        import polls
        self.assertIsNone(polls.parse(self.REPORT)["recommend"])

    def test_alternate_rating_wording_is_recognised(self):
        import polls
        self.assertEqual(polls._classify("Please rate the today's class"), "session")


class TestPollFileNaming(unittest.TestCase):
    """Two conventions exist on Drive. Reading only Zoom's own left 59
    webinars' feedback stranded."""

    def test_both_conventions_yield_the_same_key(self):
        import polls
        a = polls.name_key("poll_99299344465_2026_08_30.csv")
        b = polls.name_key("99299344465 - 2026-08-30 - Poll Report-2 (1).csv")
        self.assertEqual(a, ("99299344465", "08_30"))
        self.assertEqual(a, b)

    def test_a_name_with_no_webinar_id_is_ignored(self):
        import polls
        self.assertIsNone(polls.name_key("report_poll (2).csv"))
        self.assertIsNone(polls.name_key("Session_Links.txt"))

    def test_a_folder_path_prefix_does_not_break_the_key(self):
        import polls
        self.assertEqual(
            polls.name_key("2026-08-30 - AI CAP B38/poll_99299344465_2026_08_30.csv"),
            ("99299344465", "08_30"))


class TestNoPollLabel(unittest.TestCase):
    def test_an_unrated_session_says_so_rather_than_showing_a_dash(self):
        import dash_view
        html = dash_view._rating({"rating": None})
        self.assertIn("no poll conducted", html)

    def test_a_rated_session_shows_the_score_and_response_count(self):
        import dash_view
        html = dash_view._rating({"rating": 4.56, "rating_n": 657,
                                  "rating_trainer": 4.75, "rating_recommend": 4.51})
        self.assertIn("4.6", html)
        self.assertIn("657", html)
        self.assertNotIn("no poll conducted", html)
