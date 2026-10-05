"""bsiai_build: the pure rules that decide which room is whose and who was in it.

These pin the owner's rulings of 2026-09-28 (see the module docstring) so a
later edit cannot quietly move a session to another cohort or count a
registrant who never joined.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bsiai_build as bb  # noqa: E402
import lms_roster as lr   # noqa: E402


class TestAssign(unittest.TestCase):
    """Ruling 2: the CAP peer names the Accelerator cohort; the BSIAI number
    names the cohort only when no CAP batch shares the label."""

    def test_cohorts_by_their_own_number(self):
        self.assertEqual(bb.assign("BSIAI B1 10:30 AM"), ["B1"])
        self.assertEqual(bb.assign("Build Side Income Using AI B2"), ["B2"])
        self.assertEqual(bb.assign("Build Side Income Using AI B3-A"), ["B3-A"])
        self.assertEqual(bb.assign("Build Side Income using AI B3-B"), ["B3-B"])
        self.assertEqual(bb.assign("BSI B3-A"), ["B3-A"])

    def test_plain_b3_marks_both_halves(self):
        # 12-13 Sep ran before the A/B split; the roster has both halves.
        self.assertEqual(bb.assign("Build Side Income Using AI B3"), ["B3-A", "B3-B"])

    def test_shared_common_room_goes_to_the_cap_peer(self):
        lab = "AI CAP B41 - Common , Build Side Income Using AI Accelerator B41"
        self.assertEqual(bb.assign(lab), ["Accelerator B41"])
        lab = "AI CAP B42 - Common, Build Side Income Using AI Accelerator B42"
        self.assertEqual(bb.assign(lab), ["Accelerator B42"])

    def test_cap_peer_wins_over_the_bsiai_number(self):
        # The older naming: L2 and the folders called LMS B41 "Accelerator B2".
        # Reading the BSIAI number would credit cohort B2 with the room.
        self.assertEqual(bb.assign("AI CAP B41 - Common , BSIAI Accelerator B2"),
                         ["Accelerator B41"])
        # And "Accelerator B1" = LMS B40, which this dashboard does not track.
        self.assertEqual(bb.assign("AI CAP B40 - Common + BSIAI Accelerator B1"), [])

    def test_b43_own_room(self):
        self.assertEqual(bb.assign("Build Side Income Using AI Accelerator B43"),
                         ["Accelerator B43"])

    def test_untracked_and_non_bsiai(self):
        self.assertEqual(bb.assign("AI CAP B39 , B40 - Generalist, Build Side Income Using AI Accelerator B40"), [])
        self.assertEqual(bb.assign("AI CAP B16 + B17 + B18 11AM"), [])
        self.assertEqual(bb.assign(""), [])


class TestAttendedYes(unittest.TestCase):
    """Ruling 5: Attended == Yes only."""

    CSV = (
        "Attendee Report\n"
        "Attended,User Name (Original Name),First Name,Last Name,Email,Phone,Join Time\n"
        "Yes,A,A,X,a@x.com,919876543210,09/19/2026 10:00:00 AM\n"
        "No,B,B,Y,b@x.com,919876543211,\n"
        "Yes,C,C,Z,,9876543212,09/19/2026 10:05:00 AM\n"
    )

    def test_only_yes_rows_count(self):
        emails, full, last10, n_no = bb.parse_attended_yes(self.CSV)
        self.assertEqual(emails, {"a@x.com"})
        self.assertEqual(full, {"919876543210", "9876543212"})
        self.assertEqual(last10, {"9876543210", "9876543212"})
        self.assertEqual(n_no, 1)

    def test_no_header_is_nobody(self):
        self.assertEqual(bb.parse_attended_yes("Name,Email\nA,a@x.com\n"), (set(), set(), set(), 0))


class TestPickDate(unittest.TestCase):
    def test_l2_date_wins_over_an_early_stamp(self):
        self.assertEqual(bb.pick_l2_date("2026_09_25", {"2026_09_26"}), "2026_09_26")

    def test_reused_webinar_takes_the_nearest_l2_date(self):
        self.assertEqual(bb.pick_l2_date("2026_10_01", {"2026_09_24", "2026_10_01"}), "2026_10_01")
        self.assertEqual(bb.pick_l2_date("2026_09_23", {"2026_09_24", "2026_10_01"}), "2026_09_24")

    def test_no_l2_dates_keeps_the_stamp(self):
        self.assertEqual(bb.pick_l2_date("2026_09_25", set()), "2026_09_25")
        self.assertEqual(bb.pick_l2_date("2026_09_25", None), "2026_09_25")


class TestHit(unittest.TestCase):
    SESS = {"emails": {"a@x.com", "alt@x.com"},
            "ph_full": {"919876543210", "971506753703"},
            "ph10": {"9876543210", "1506753703"}}

    def _st(self, **kw):
        base = {"email": "", "alt_emails": [], "phone": "", "alt_phones": [], "cc": "91"}
        base.update(kw)
        return base

    def test_email_phone_and_alternates(self):
        self.assertTrue(bb.hit(self._st(email="a@x.com"), self.SESS))
        self.assertTrue(bb.hit(self._st(alt_emails=["alt@x.com"]), self.SESS))
        self.assertTrue(bb.hit(self._st(phone="9876543210"), self.SESS))        # bare 10
        self.assertTrue(bb.hit(self._st(alt_phones=["919876543210"]), self.SESS))
        self.assertFalse(bb.hit(self._st(email="z@x.com", phone="910000000000"), self.SESS))

    def test_non_91_nine_digit_fallback(self):
        # UAE: cc 971 + 9-digit national number. Last-10 of the roster value is
        # '1506753703' which happens to be in ph10 here; the fallback is for
        # when the report stored the number WITHOUT the country code.
        sess = {"emails": set(), "ph_full": {"506753703"}, "ph10": set()}
        self.assertTrue(bb.hit(self._st(phone="971506753703", cc="971"), sess))
        self.assertFalse(bb.hit(self._st(phone="919506753703", cc="91"), sess))


class TestRosterRows(unittest.TestCase):
    def test_rows_are_ten_columns_named_by_tab_and_deduped(self):
        raw = {c: {"customers": []} for c in bb.CODES}
        raw["B1"]["customers"] = [
            {"email": "a@x.com", "countryCode": "91", "number": "9876543210",
             "paymentStatus": "full_paid", "closingType": "bda_closing"},
            {"email": "A@X.com", "countryCode": "91", "number": "9876543210",
             "paymentStatus": "full_paid", "closingType": "bda_closing"},       # same person
            {"email": "", "countryCode": "", "number": "9000000001",
             "paymentStatus": "booking", "closingType": "unknown"},
        ]
        rows = bb.roster_rows(raw)
        self.assertEqual(len(rows["B1"]), 2)
        r = rows["B1"][0]
        self.assertEqual(len(r), 10)
        self.assertEqual(r[lr.I_BATCH], "BSIAI B1")
        self.assertEqual(r[lr.I_PAY], "Full Paid")
        self.assertEqual(r[lr.I_CLOSE], "BDA Closing")
        self.assertEqual(r[lr.I_NUM], "919876543210")
        self.assertEqual(rows["B2"], [])


class TestConstants(unittest.TestCase):
    def test_codes_tabs_and_ids_line_up(self):
        self.assertEqual(len(bb.CODES), 7)
        self.assertEqual(len(set(bb.LMS_ID.values())), 7)
        for c in bb.CODES:
            self.assertTrue(bb.TAB_OF[c].startswith("BSIAI "))
            self.assertEqual(bb.CODE_OF_TAB[bb.TAB_OF[c]], c)
        self.assertNotIn(40, bb.ACCEL_BY_CAP)          # not requested; never credited silently
        self.assertIn("95403362407", bb.DROP_WIDS)


if __name__ == "__main__":
    unittest.main()
