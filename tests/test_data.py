"""
Unit tests for data.py — synthetic fixtures only (no real student data).
Run:  python -m unittest tests.test_data    (from the project root)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data  # noqa: E402

FIXED = ["Contry code", "Registered Number", "Registered mail", "Contry code",
         "Whatsaap Number", "broadcast mail", "batch name", "amount",
         "Payment", "Closing Type"]


def make_tab(students, session_dates, session_topics, header_row=1, junk_rows=0):
    """Build one roster tab as rows (list of lists).

    students: list of dict(mail, payment, closing, marks=[per-session str])
    session_dates: list of date strings placed in the row directly above header
    session_topics: list of header-row cells for the session columns
    header_row: 0-based index where the field-name header sits
    junk_rows: extra rows above everything (to test 'header not in row 1')
    """
    n = len(session_dates)
    width = 10 + n
    rows = []
    for _ in range(junk_rows):
        rows.append(["AI CAP — roster"] + [None] * (width - 1))
    # rows between junk and header; the one directly above header holds the dates
    pad = header_row - junk_rows
    for i in range(pad):
        r = [None] * width
        if i == pad - 1:
            for j, d in enumerate(session_dates):
                r[10 + j] = d
        rows.append(r)
    rows.append(list(FIXED) + list(session_topics))           # header row
    for s in students:
        rows.append([91, "999", s["mail"], 91, "999", s["mail"], "AI CAP B17",
                     1000, s["payment"], s["closing"]] + list(s["marks"]))
    return rows


def ten_students(marks_a, marks_b, payments=None, closings=None):
    """10 students with given per-session mark lists (len 10 each)."""
    payments = payments or ["Full Paid"] * 10
    closings = closings or ["BDA Closing"] * 10
    return [dict(mail=f"s{i}@x.com", payment=payments[i], closing=closings[i],
                 marks=[marks_a[i], marks_b[i]]) for i in range(10)]


class TestPureHelpers(unittest.TestCase):
    def test_normalize_closing(self):
        self.assertEqual(data.normalize_closing("BDA Closimg"), "BDA Closing")  # typo
        self.assertEqual(data.normalize_closing("bda closing"), "BDA Closing")
        self.assertEqual(data.normalize_closing("System Generated"), "System")
        self.assertEqual(data.normalize_closing("BDA Collection"), "BDA Collection")
        self.assertEqual(data.normalize_closing("LWB"), "LWB Resume")
        self.assertEqual(data.normalize_closing("resume"), "LWB Resume")
        self.assertEqual(data.normalize_closing(""), "Unknown")
        self.assertEqual(data.normalize_closing(None), "Unknown")
        self.assertEqual(data.normalize_closing("Partner Referral"), "Partner Referral")  # unknown kept

    def test_is_active(self):
        for ok in ["Full Paid", "Booking", "Partial", "Full"]:
            self.assertTrue(data.is_active(ok), ok)
        for bad in ["", "Refund", "refunded", "undifined", "unidetified",
                    "undefined", "Not Paid", "cancelled", "unidentified"]:
            self.assertFalse(data.is_active(bad), bad)

    def test_batch_label(self):
        self.assertEqual(data.batch_label("AI CAP B17"), "B17")
        self.assertEqual(data.batch_label("AICAP B29"), "B29")
        self.assertEqual(data.batch_label("ai cap b7"), "B7")
        self.assertIsNone(data.batch_label("AICAP B17 Att"))
        self.assertIsNone(data.batch_label("AI CAP B17 Att"))
        self.assertIsNone(data.batch_label("Summary"))

    def test_band(self):
        self.assertEqual(data.band(60), "high")
        self.assertEqual(data.band(45), "high")
        self.assertEqual(data.band(44.9), "mid")
        self.assertEqual(data.band(30), "mid")
        self.assertEqual(data.band(29.9), "low")

    def test_date_label(self):
        self.assertEqual(data.date_label("04_27"), "27 Apr")
        self.assertEqual(data.date_label("12_05"), "5 Dec")
        self.assertIsNone(data.date_label(None))


class TestBuildBatch(unittest.TestCase):
    def _batch(self, **kw):
        students = ten_students(**kw.pop("marks"))
        rows = make_tab(students, **kw)
        return data.build_batch(rows, "B17", kw.get("l2_lookup"))

    def test_strength_active_and_pct(self):
        # 10 enrolled; 3 not active (refund/undifined/blank); session A = 6 present
        pays = ["Full Paid"] * 7 + ["Refund", "undifined", ""]
        rows = make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present"] * 5 + [""] * 5, payments=pays),
            session_dates=["2026_04_04", "2026_04_05"],
            session_topics=["Make Money using AI", "2026_04_05"],
        )
        b = data.build_batch(rows, "B17", None)
        self.assertEqual(b["strength"], 10)
        self.assertEqual(b["active"], 7)
        self.assertEqual(b["sessions"][0]["present"], 6)
        self.assertEqual(b["sessions"][0]["pct"], 60.0)       # present / strength, not / active
        self.assertEqual(b["n_sessions"], 2)
        self.assertEqual(b["avg_pct"], 55.0)                  # (60 + 50)/2

    def test_invalid_session_skipped(self):
        # session B has only 3 marked (=30% of 10, NOT >30%) -> dropped
        rows = make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present", "Present", "Absent"] + [""] * 7),
            session_dates=["2026_04_04", "2026_04_05"],
            session_topics=["A", "B"],
        )
        b = data.build_batch(rows, "B17", None)
        self.assertEqual(b["n_sessions"], 1)                  # only session A is valid

    def test_present_only_flag(self):
        rows = make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present"] * 5 + [""] * 5),
            session_dates=["2026_04_04", "2026_04_05"],
            session_topics=["A", "B"],
        )
        b = data.build_batch(rows, "B17", None)
        s_b = [s for s in b["sessions"] if s["mm"] == "04_05"][0]
        self.assertTrue(s_b["present_only"])                  # 5 present, 0 absent
        self.assertFalse(b["sessions"][0]["present_only"])

    def test_chronological_sort(self):
        rows = make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present"] * 6 + ["Absent"] * 4),
            session_dates=["2026_05_31", "2026_04_04"],      # out of order in the sheet
            session_topics=["Later", "Earlier"],
        )
        b = data.build_batch(rows, "B17", None)
        self.assertEqual([s["mm"] for s in b["sessions"]], ["04_04", "05_31"])

    def test_l2_topic_join_and_fallbacks(self):
        rows = make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present"] * 6 + ["Absent"] * 4),
            session_dates=["2026_04_04", "2026_04_05"],
            session_topics=["Roster Topic A", "2026_04_05"],  # B's header is a date (no roster topic)
        )
        l2 = {("B17", "04_04"): "L2 Topic A"}                 # only A has an L2 match
        b = data.build_batch(rows, "B17", l2)
        s = {x["mm"]: x for x in b["sessions"]}
        self.assertEqual(s["04_04"]["topic"], "L2 Topic A")   # L2 wins over roster header
        self.assertFalse(s["04_04"]["no_l2"])
        self.assertEqual(s["04_05"]["topic"], "Session on 5 Apr")  # no L2, header is a date -> fallback
        self.assertTrue(s["04_05"]["no_l2"])

    def test_l2_join_by_mm_when_not_per_batch(self):
        rows = make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present"] * 6 + ["Absent"] * 4),
            session_dates=["2026_04_04", "2026_04_05"],
            session_topics=["2026_04_04", "2026_04_05"],
        )
        l2 = {"04_04": "Shared A", "04_05": "Shared B"}       # shared schedule (mm only)
        b = data.build_batch(rows, "B17", l2)
        self.assertEqual({x["mm"]: x["topic"] for x in b["sessions"]},
                         {"04_04": "Shared A", "04_05": "Shared B"})

    def test_closing_breakdown(self):
        closings = ["BDA Closing"] * 4 + ["BDA Closimg"] + ["System"] * 3 + ["BDA Collection"] * 2
        # session A: System buyers all present, others mixed -> System att highest
        marks_a = ["Present", "Present", "Absent", "Absent", "Present",   # closing/closimg rows
                   "Present", "Present", "Present",                       # system rows (all present)
                   "Absent", "Absent"]                                    # collection rows (absent)
        rows = make_tab(
            [dict(mail=f"s{i}@x.com", payment="Full Paid", closing=closings[i],
                  marks=[marks_a[i], "Present" if marks_a[i] == "Present" else "Absent"])
             for i in range(10)],
            session_dates=["2026_04_04", "2026_04_05"],
            session_topics=["A", "B"],
        )
        b = data.build_batch(rows, "B17", None)
        cl = {c["type"]: c for c in b["closing"]}
        self.assertEqual(cl["BDA Closing"]["count"], 5)       # 4 + 1 typo merged
        self.assertEqual(cl["System"]["count"], 3)
        self.assertEqual(cl["BDA Collection"]["count"], 2)
        self.assertEqual(cl["BDA Collection"]["pct"], 20.0)   # 2/10
        # System buyers attend most, Collection least (matches the prototype's signal)
        self.assertGreater(cl["System"]["att"], cl["BDA Collection"]["att"])


class TestDynamicDiscovery(unittest.TestCase):
    """Adapts to growth with zero code changes: new batch tab + new session column."""

    def _two_session_batch(self, dates, topics):
        return make_tab(
            ten_students(["Present"] * 6 + ["Absent"] * 4,
                         ["Present"] * 6 + ["Absent"] * 4),
            session_dates=dates, session_topics=topics)

    def test_new_batch_tab_is_discovered(self):
        sheets = {
            "AI CAP B17": self._two_session_batch(["2026_04_04", "2026_04_05"], ["A", "B"]),
            "AICAP B17 Att": [["junk"]],                       # helper tab -> ignored
            "Summary": [["not a batch"]],                      # non-batch tab -> ignored
        }
        data1, sum1 = data.build(sheets)
        self.assertEqual(list(data1), ["B17"])
        self.assertEqual(sum1["batches"], 1)

        # add a brand-new batch tab — no code change
        sheets["AI CAP B29"] = self._two_session_batch(["2026_06_27", "2026_06_28"], ["X", "Y"])
        data2, sum2 = data.build(sheets)
        self.assertEqual(list(data2), ["B17", "B29"])
        self.assertEqual(sum2["batches"], 2)

    def test_new_session_column_appears(self):
        rows = self._two_session_batch(["2026_04_04", "2026_04_05"], ["A", "B"])
        before = data.build_batch(rows, "B17", None)
        self.assertEqual(before["n_sessions"], 2)

        # append a 3rd populated session column to every row (a freshly-uploaded session)
        date_row_idx = data._find_header_row(rows) - 1
        for i, r in enumerate(rows):
            if i == date_row_idx:
                r.append("2026_05_09")
            elif i <= data._find_header_row(rows):
                r.append("New Topic")                          # header cell
            else:
                r.append("Present" if i % 2 else "Absent")     # marks for ~all rows
        after = data.build_batch(rows, "B17", None)
        self.assertEqual(after["n_sessions"], 3)               # picked up automatically
        self.assertIn("05_09", [s["mm"] for s in after["sessions"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
