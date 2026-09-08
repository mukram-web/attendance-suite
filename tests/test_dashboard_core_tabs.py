"""compute() and roster_grid() must give the SAME answer from bytes or from rows.

build_store already materialises the whole marked workbook into `tabs` to build
DATA. It then used to hand the raw bytes to compute() and roster_grid(), each of
which re-streamed the same 80k rows out of the zip. Measured on the real 7.9 MB
marked workbook: 46.7s to materialise the tabs, then 43.7s and 46.8s to do it
twice more — 90s of a 446s pipeline spent reading a file already in memory.

Passing `tabs` skips that. It is a de-duplication, not a cache: same rows, read
from memory. This file is the proof, because "obviously identical" is exactly
the kind of claim that turns out to have an off-by-one in it — the two paths
differ in one real way, which is that openpyxl PADS short rows out to max_col
with None and a materialised row is its natural length. The session-column loop
runs to len(row1), so a width divergence would change which columns are seen.

If either function ever grows a code path that reads from the worksheet object
directly rather than through _Tab, these tests are what will catch it.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl  # noqa: E402

import dashboard_core as dc  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "sample_data", "Copy_of_Master_Batch_Rosters.xlsx")


def _tabs(b):
    wb = openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True)
    out = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
           for ws in wb.worksheets}
    wb.close()
    return out


@unittest.skipUnless(os.path.exists(SAMPLE), "sample roster not present")
class TestBytesAndTabsAgree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SAMPLE, "rb") as fh:
            cls.b = fh.read()
        cls.tabs = _tabs(cls.b)

    def test_compute_is_identical(self):
        a = dc.compute(self.b)
        c = dc.compute(self.b, tabs=self.tabs)
        self.assertFalse(a.empty, "sample produced no rows — fixture is wrong")
        self.assertEqual(list(a.columns), list(c.columns))
        self.assertEqual(len(a), len(c))
        self.assertTrue(a.equals(c), "compute() disagrees between the two paths")

    def test_every_roster_grid_is_identical(self):
        smap = dc.batch_sheet_map(self.b)
        self.assertTrue(smap, "no batch sheets found — fixture is wrong")
        checked = 0
        for sheet in smap.values():
            a = dc.roster_grid(self.b, sheet)
            c = dc.roster_grid(self.b, sheet, tabs=self.tabs)
            self.assertEqual(list(a.columns), list(c.columns), sheet)
            self.assertTrue(a.equals(c), f"roster_grid disagrees on {sheet}")
            checked += 1
        self.assertGreater(checked, 0)

    def test_the_two_paths_see_the_same_session_columns(self):
        # The failure this guards is silent: a width divergence drops session
        # columns off the right-hand end, and every percentage still adds up.
        a = dc.compute(self.b)
        c = dc.compute(self.b, tabs=self.tabs)
        by_batch = lambda df: df.groupby("Batch")["SessionIdx"].max().to_dict()
        self.assertEqual(by_batch(a), by_batch(c))


class TestTabReader(unittest.TestCase):
    """_Tab's slicing has to match openpyxl's max_col semantics."""

    def test_head_and_body_slice_to_the_requested_width(self):
        rows = [tuple(range(10)), tuple(range(10, 20)), tuple(range(20, 30))]
        t = dc._Tab(rows=rows)
        self.assertEqual(t.head(2, 4), [(0, 1, 2, 3), (10, 11, 12, 13)])
        self.assertEqual(t.body(2, 3), [(10, 11, 12), (20, 21, 22)])

    def test_an_empty_sheet_yields_no_header_rather_than_raising(self):
        # People add scratch tabs to the roster; a "Pivot Table 2" once killed
        # the whole build through an unguarded next().
        self.assertEqual(dc._find_header_row(dc._Tab(rows=[])), (1, []))

    def test_the_header_row_is_found_on_row_1_or_row_2(self):
        r1 = [("registered number", "payment"), ("x", "y")]
        self.assertEqual(dc._find_header_row(dc._Tab(rows=r1))[0], 1)
        r2 = [("batch", ""), ("registered number", "payment")]
        self.assertEqual(dc._find_header_row(dc._Tab(rows=r2))[0], 2)

    def test_a_sheet_with_no_recognisable_header_falls_back_to_row_1(self):
        self.assertEqual(dc._find_header_row(dc._Tab(rows=[("a", "b")])), (1, []))

    def test_the_wide_tab_warning_fires_before_columns_are_lost_not_after(self):
        # The read limit is a real cliff: a session column past it stops being
        # counted and nothing says so. Warn with headroom to spare.
        self.assertIsNone(dc._wide_tab_warning("AI CAP B17", 43))
        self.assertIsNotNone(dc._wide_tab_warning("AI CAP B40", dc._MAX_SCAN_COL - 8))
        self.assertIsNotNone(dc._wide_tab_warning("AI CAP B40", dc._MAX_SCAN_COL + 5))


if __name__ == "__main__":
    unittest.main()
