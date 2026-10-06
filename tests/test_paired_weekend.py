import unittest

import data as D

HDR = ["Contry code", "Registered Number", "Registered mail", "Payment",
       "Closing Type", "POD Prefrence",
       "2026_10_03", "2026_10_03 | Techies", "2026_10_04", "2026_10_04 | Techies"]


def _row(i, pod, marks):
    return ["91", f"9100000000{i:02d}", f"p{i}@x.com", "Full Paid", "BDA Closing",
            pod] + marks


P, A = "Present", "Absent"
ROWS = [HDR,
        # General: Sat only, Sun only, both, neither, and one who joined Techies Sat
        _row(1, "Finance", [P, A, A, A]),
        _row(2, "Finance", [A, A, P, A]),
        _row(3, "Generalist", [P, A, P, A]),
        _row(4, "Generalist", [A, A, A, A]),
        _row(5, "Generalist", [A, P, A, A]),
        # Techies
        _row(6, "Techies", [A, P, A, A]),
        _row(7, "Techies", [A, A, A, A])]


class PairedWeekendTest(unittest.TestCase):
    def setUp(self):
        self.b = D.build_batch(ROWS, "B43", None)

    def test_only_from_b41(self):
        self.assertTrue(D.paired("B41"))
        self.assertFalse(D.paired("B40"))
        self.assertFalse(D.paired("ECAP B3"))
        self.assertEqual(D.build_batch(ROWS, "B40", None)["weekends"], [])

    def test_one_weekend_counted_once(self):
        (w,) = self.b["weekends"]
        self.assertEqual(w["date_lbl"], "3–4 Oct")
        self.assertEqual((w["present"], w["total"]), (5, 7))
        rooms = {r["room"]: r for r in w["rooms"]}
        self.assertEqual(list(rooms), ["General", "Techies"])
        g = rooms["General"]
        self.assertEqual((g["present"], g["total"]), (4, 5))   # #5 counts via the Techies room
        self.assertEqual([(x["present"], x["total"]) for x in g["days"]], [(3, 5), (2, 5)])
        self.assertEqual((rooms["Techies"]["present"], rooms["Techies"]["total"]), (1, 2))

    def test_pods_and_headline(self):
        (w,) = self.b["weekends"]
        self.assertEqual(w["pods"]["Finance"]["present"], 2)
        self.assertEqual(w["pods"]["Generalist"]["total"], 3)
        self.assertEqual(self.b["avg_pct"], round(5 / 7 * 100, 1))

    def test_one_room_batch_table_has_no_room_names(self):
        # BSIAI Accelerator batches have no POD column: one room, so the rows
        # are "Saturday (3 Oct)" not "General Saturday (3 Oct)", and the only
        # bold line is the overall one.
        import dash_view as V
        w = {"days": ["10_03", "10_04"], "present": 3, "total": 6,
             "rooms": [{"room": "General", "present": 3, "total": 6,
                        "days": [{"mm": "10_03", "date_lbl": "3 Oct", "present": 3, "total": 6},
                                 {"mm": "10_04", "date_lbl": "4 Oct", "present": 0, "total": 6}]}]}
        html_ = V.weekend_table_html(w)
        self.assertNotIn("General", html_)
        self.assertIn("(3 Oct)", html_)
        self.assertEqual(html_.count("font-weight:700"), 1)
        self.assertIn("Unique overall both days", html_)

    def test_span_label(self):
        self.assertEqual(D.span_label(["10_31", "11_01"]), "31 Oct – 1 Nov")
        self.assertEqual(D._weekend_groups(["10_03", "10_04", "10_10"]),
                         [["10_03", "10_04"], ["10_10"]])


if __name__ == "__main__":
    unittest.main()
