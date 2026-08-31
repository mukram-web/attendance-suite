"""Unit tests for the BSIAI programme layer (stdlib unittest, no pytest)."""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bsiai  # noqa: E402


def _roster_xlsx(tabs):
    """tabs = {title: (header_list, [row, ...])} -> .xlsx bytes."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for title, (header, rows) in tabs.items():
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEAD = ["CountryCode", "Phone", "CC+Phone", "Email", "Batch",
        "Funnel for QnA", "Zoom Link For QnA", "QnA Group Wise Form Link", "Refund"]


def _row(phone, email, batch, refund=None):
    return [91, phone, f"91{phone}", email, batch, "", "", "", refund]


def _attendee_csv(pairs):
    """Minimal but real-shaped Zoom Attendee Report."""
    out = ["Attendee Report", "Report generated time,08/16/2026 05:29:06 AM", "",
           "Attended,First Name,Last Name,Email,Phone,Join Time"]
    for email, phone in pairs:
        out.append(f"Yes,First,Last,{email},{phone},08/16/2026")
    return "\n".join(out).encode()


class TestBatchNum(unittest.TestCase):
    def test_every_spelling_in_the_three_sources(self):
        for label, want in [("BSIAI B1", 1), ("BSIAI B1GA", 1), ("BSI AI B3", 3),
                            ("BSI B1", 1), ("bsi b2", 2), ("BSI B2W1", 2),
                            ("BSI B2W3", 2), ("bsiai  b10 ", 10)]:
            self.assertEqual(bsiai.batch_num(label), want, label)

    def test_other_programmes_are_not_bsiai(self):
        for label in ["MM-AI B1", "AI CAP B17", "ECAP B1", "", None, "Pause"]:
            self.assertIsNone(bsiai.batch_num(label), label)


class TestReadRoster(unittest.TestCase):
    def setUp(self):
        self.xlsx = _roster_xlsx({
            "b1 final strength": (HEAD, [
                _row(5550000101, "a@x.com", "BSIAI B1GA"),
                _row(5550000102, "b@x.com", "BSIAI B1GA"),
                _row(5550000103, "c@x.com", "BSIAI B1GA", "TRUE"),   # refunded
                _row(1111111111, "", "BSIAI B1GA"),                  # no email -> ignored
            ]),
            "b2": (HEAD, [
                _row(5550000201, "d@x.com", "BSIAI B2"),
                _row(5550000202, "e@x.com", "BSI B2W1"),             # sub-group -> B2
                _row(5550000203, "f@x.com", "BSI B2W3"),
            ]),
            "mm-ai export": (["Customer ID", "Name", "Email", "Batches"],
                             [["id", "n", "g@x.com", "MM-AI B1"]]),  # other programme
            "Pivot Table 1": (["a", "b"], [[1, 2]]),                 # junk tab
        })

    def test_groups_and_subgroups_roll_into_their_parent_batch(self):
        roster, _ = bsiai.read_roster(self.xlsx)
        self.assertEqual(sorted(roster), [1, 2])
        self.assertEqual(len(roster[1]), 3)   # blank-email row dropped
        self.assertEqual(len(roster[2]), 3)   # B2 + B2W1 + B2W3

    def test_refund_flag_and_phone_normalisation(self):
        roster, _ = bsiai.read_roster(self.xlsx)
        self.assertEqual([s["refund"] for s in roster[1]], [False, False, True])
        self.assertEqual(roster[1][0]["phone"], "915550000101")

    def test_unusable_tabs_warn_instead_of_crashing(self):
        _, warns = bsiai.read_roster(self.xlsx)
        self.assertTrue(any("Pivot Table 1" in w for w in warns))
        self.assertTrue(any("mm-ai export" in w for w in warns))

    def test_confirmed_non_roster_tabs_are_skipped_without_warning(self):
        """The three tabs the owner confirmed are not roster data (2026-08-29)
        must not clutter the tab's warning list every week."""
        xlsx = _roster_xlsx({
            "b1": (HEAD, [_row(9000000001, "a@x.com", "BSIAI B1")]),
            "failed BSI B1": (["", "", "Group B", "Group A"], [["a@x.com", "", "b@x", "c@x"]]),
            "Sheet8": (["Email", "Phone Number"], [["d@x.com", 915550000301]]),
            "NEXT BATCH 15K MMAI": (["Customer ID", "Email", "Batches"],
                                    [["id", "e@x.com", "MM-AI B1"]]),
        })
        roster, warns = bsiai.read_roster(xlsx)
        self.assertEqual(sorted(roster), [1])
        self.assertEqual(warns, [], f"expected no warnings, got {warns}")

    def test_the_ignore_list_does_not_silence_an_unknown_tab(self):
        """A tab nobody has vetted must still warn - that is what stops a real
        roster tab from disappearing without anyone noticing."""
        xlsx = _roster_xlsx({
            "b1": (HEAD, [_row(9000000001, "a@x.com", "BSIAI B1")]),
            "Sheet8": (["Email", "Phone Number"], [["d@x.com", 1]]),
            "Sheet9": (["Email", "Phone Number"], [["e@x.com", 2]]),
        })
        _, warns = bsiai.read_roster(xlsx)
        self.assertEqual(len(warns), 1)
        self.assertIn("Sheet9", warns[0])

    def test_ignored_tab_names_match_regardless_of_case_or_padding(self):
        xlsx = _roster_xlsx({
            "b1": (HEAD, [_row(9000000001, "a@x.com", "BSIAI B1")]),
            " sheet8 ": (["Email", "Phone Number"], [["d@x.com", 1]]),
            "Failed BSI B1": (["", "", "Group B", "Group A"], [["a@x", "", "b@x", "c@x"]]),
        })
        _, warns = bsiai.read_roster(xlsx)
        self.assertEqual(warns, [])

    def test_float_phone_keeps_its_digits(self):
        xlsx = _roster_xlsx({"t": (HEAD, [[91, 5550000101.0, "915550000101.0",
                                           "z@x.com", "BSIAI B1", "", "", "", ""]])})
        roster, _ = bsiai.read_roster(xlsx)
        self.assertEqual(roster[1][0]["phone"], "915550000101")

    def test_lms_email_column_does_not_shadow_the_real_one(self):
        head = ["CountryCode", "Phone", "CC+Phone", "lms email", "Email", "Batch", "Refund"]
        xlsx = _roster_xlsx({"b3": (head, [[91, 1, "911", "lms@x.com", "real@x.com",
                                            "BSI AI B3", ""]])})
        roster, _ = bsiai.read_roster(xlsx)
        self.assertEqual(roster[3][0]["email"], "real@x.com")


class TestSessionsFromFiles(unittest.TestCase):
    L2 = {"111": (frozenset({("BSIAI", 1)}), "WhatsApp Automation"),
          "222": (frozenset({("BSIAI", 2)}), "Insights of contents"),
          "999": (frozenset({("CAP", 33)}), "An AI CAP session")}

    def test_a_webinar_missing_from_l2_is_skipped_and_warned(self):
        files = [("2026-08-08 - bsi b2 - X/attendee_777_2026_08_08.csv",
                  _attendee_csv([("a@x.com", "919000000001")]))]
        sess, warns = bsiai.sessions_from_files(files, self.L2)
        self.assertEqual(sess, {})
        self.assertTrue(any("777" in w and "not in L2" in w for w in warns))

    def test_ai_cap_sessions_are_left_alone(self):
        files = [("2026-08-02 - AI CAP B33 - X/attendee_999_2026_08_02.csv",
                  _attendee_csv([("a@x.com", "919000000001")]))]
        sess, _ = bsiai.sessions_from_files(files, self.L2)
        self.assertEqual(sess, {})

    def test_duplicate_copies_across_drives_collapse_to_the_fuller_one(self):
        small = _attendee_csv([("a@x.com", "919000000001")])
        big = _attendee_csv([("a@x.com", "919000000001"), ("b@x.com", "919000000002")])
        files = [("2026-08-15 - BSI B1 - WhatsApp/attendee_111_2026_08_15.csv", small),
                 ("2026-08-15 - BSIAI B1 - WhatsApp/attendee_111_2026_08_15.csv", big)]
        sess, _ = bsiai.sessions_from_files(files, self.L2)
        self.assertEqual(len(sess[1]), 1)
        self.assertEqual(len(sess[1]["111"]["emails"]), 2)

    def test_a_file_that_parses_to_zero_attendees_warns(self):
        flat = b"Name (original name),Email,Join time\nA,a@x.com,10:00\n"
        files = [("2026-08-15 - BSI B1 - X/attendee_111_2026_08_15.csv", flat)]
        sess, warns = bsiai.sessions_from_files(files, self.L2)
        self.assertEqual(sess, {})
        self.assertTrue(any("ZERO" in w for w in warns))


class TestBuild(unittest.TestCase):
    def _fixture(self):
        roster = {1: [
            {"email": "a@x.com", "phone": "919000000001", "refund": False, "label": "BSIAI B1"},
            {"email": "b@x.com", "phone": "919000000002", "refund": False, "label": "BSIAI B1"},
            {"email": "c@x.com", "phone": "919000000003", "refund": False, "label": "BSIAI B1"},
            {"email": "d@x.com", "phone": "919000000004", "refund": True, "label": "BSIAI B1"},
        ]}
        sessions = {1: {"111": {
            "wid": "111", "mm": "08_15", "topic": "WhatsApp Automation",
            "emails": {"a@x.com"},                 # a matches on email
            "ph_full": {"9000000002"},             # b matches on last-10 phone
            "ph10": {"9000000002", "9000000004"},  # d matches but is refunded
        }}}
        return roster, sessions

    def test_strength_excludes_refunds_from_the_denominator(self):
        DATA, summary = bsiai.build(*self._fixture())
        d = DATA["B1"]
        self.assertEqual(d["strength"], 4)      # everyone enrolled
        self.assertEqual(d["active"], 3)        # refund excluded
        self.assertEqual(d["sessions"][0]["total"], 3)
        self.assertEqual(summary["enrolled"], 4)
        self.assertEqual(summary["active"], 3)

    def test_present_matches_on_email_or_last_ten_digits(self):
        DATA, _ = bsiai.build(*self._fixture())
        s = DATA["B1"]["sessions"][0]
        self.assertEqual(s["present"], 2)       # a by email, b by phone; d refunded
        self.assertEqual(s["absent"], 1)
        self.assertAlmostEqual(s["pct"], 66.7, places=1)

    def test_shape_matches_the_ai_cap_dashboard_contract(self):
        DATA, _ = bsiai.build(*self._fixture())
        d = DATA["B1"]
        for k in ("code", "strength", "active", "n_sessions", "avg_pct",
                  "peak", "low", "sessions", "closing"):
            self.assertIn(k, d)
        # BSIAI has no Close Type, so the generic panel carries engagement bands
        self.assertTrue(all({"type", "count", "pct", "att"} <= set(x)
                            for x in d["closing"]))
        for k in ("date_lbl", "topic", "present", "absent", "total", "pct",
                  "present_only", "no_l2"):
            self.assertIn(k, d["sessions"][0])

    def test_a_batch_with_no_sessions_is_left_out(self):
        roster, sessions = self._fixture()
        roster[2] = [{"email": "z@x.com", "phone": "", "refund": False, "label": "BSIAI B2"}]
        DATA, summary = bsiai.build(roster, sessions)
        self.assertEqual(sorted(DATA), ["B1"])
        self.assertEqual(summary["batches"], 1)


class TestEngagementBands(unittest.TestCase):
    def _fx(self, marks):
        """marks: per-student list of booleans, one per session."""
        n = len(marks[0])
        students = [{"email": f"s{i}@x.com", "phone": "", "refund": False,
                     "label": "BSIAI B1"} for i in range(len(marks))]
        sessions = {}
        for j in range(n):
            sessions[str(j)] = {
                "wid": str(j), "mm": f"08_{j+1:02d}", "topic": f"S{j}",
                "emails": {f"s{i}@x.com" for i, m in enumerate(marks) if m[j]},
                "ph_full": set(), "ph10": set()}
        return students, sessions

    def test_bands_split_by_how_much_of_the_course_each_student_attended(self):
        # 1 attends all 4, 1 attends 2 of 4, 1 attends none
        students, sessions = self._fx([[1, 1, 1, 1], [1, 1, 0, 0], [0, 0, 0, 0]])
        got = {b["type"]: b["count"] for b in bsiai.engagement_bands(students, sessions)}
        self.assertEqual(got.get("Attended 80%+"), 1)
        self.assertEqual(got.get("Most sessions (50-79%)"), 1)
        self.assertEqual(got.get("Never attended"), 1)

    def test_refunded_students_are_not_counted_as_disengaged(self):
        students, sessions = self._fx([[1, 1], [0, 0]])
        students[1]["refund"] = True
        bands = bsiai.engagement_bands(students, sessions)
        self.assertEqual(sum(b["count"] for b in bands), 1)
        self.assertNotIn("Never attended", [b["type"] for b in bands])

    def test_shares_add_up_to_the_active_population(self):
        students, sessions = self._fx([[1, 1], [1, 0], [0, 0], [0, 0]])
        bands = bsiai.engagement_bands(students, sessions)
        self.assertEqual(sum(b["count"] for b in bands), 4)
        self.assertAlmostEqual(sum(b["pct"] for b in bands), 100.0, places=1)


class TestAnalyse(unittest.TestCase):
    def test_batches_awaiting_a_session_are_reported_not_hidden(self):
        xlsx = _roster_xlsx({"r": (HEAD, [_row(9000000001, "a@x.com", "BSIAI B1"),
                                          _row(9000000009, "z@x.com", "BSI AI B3")])})
        l2 = {"111": (frozenset({("BSIAI", 1)}), "WhatsApp Automation")}
        files = [("2026-08-15 - BSI B1 - X/attendee_111_2026_08_15.csv",
                  _attendee_csv([("a@x.com", "919000000001")]))]
        DATA, summary, warns = bsiai.analyse(xlsx, files, l2)
        self.assertEqual(sorted(DATA), ["B1"])
        self.assertTrue(any("B3" in w and "no L2-known session" in w for w in warns))


if __name__ == "__main__":
    unittest.main()
