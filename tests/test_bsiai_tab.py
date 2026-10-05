"""The BSIAI tab inside the main app, and the standalone BSIAI page.

Both draw `bsiai_view.render` over the BSIAI store. The tab must coexist with
the AI CAP Dashboard and Roster in ONE Streamlit script — which is what the
`key_prefix` on `dash_view.render` and the `key` on every bsiai_view widget
exist for — and it must never touch the AI CAP numbers: the main Dashboard's
batch list is the same with and without a BSIAI store on disk.

The AppTest cases skip when a local store is missing (CI has none), like the
other AppTest files do. The gate is bypassed the same way `test_ui_layout`
does it: the session is marked `_authed`, no password is ever typed.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

APP = os.path.join(ROOT, "attendance_app.py")
BSIAI_APP = os.path.join(ROOT, "bsiai_app.py")
STORE = os.path.join(ROOT, ".cache", "attendance.duckdb")
BSIAI_STORE = os.path.join(ROOT, ".cache", "bsiai.duckdb")


class TestKeyPrefix(unittest.TestCase):
    """dash_view's widget keys follow the prefix, and the default spelling is
    the one the existing tests and session state use."""

    def test_default_prefix_keeps_the_aicap_keys(self):
        with open(os.path.join(ROOT, "dash_view.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('key_prefix: str = "aicap"', src)
        self.assertIn('f"{key_prefix}_batch"', src)
        self.assertIn('f"{key_prefix}_bar"', src)
        # no literal aicap_ widget key survives — every one goes through the prefix
        self.assertNotIn('key="aicap_', src)
        self.assertNotIn('["aicap_', src)

    def test_bsiai_view_prefixes_every_widget(self):
        import ast
        with open(os.path.join(ROOT, "bsiai_view.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        bare = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                        bare.append((node.lineno, kw.value.value))
        self.assertEqual(bare, [], "a widget key that does not carry the page prefix")


@unittest.skipUnless(os.path.exists(STORE) and os.path.exists(BSIAI_STORE),
                     "needs both local stores")
class TestBsiaiTabInMainApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(APP, default_timeout=300)
        at.session_state["_authed"] = True
        at.secrets = {"drive": {}}
        cls.at = at.run()

    def test_runs_without_an_exception(self):
        self.assertFalse(self.at.exception, [e.value for e in self.at.exception])

    def test_the_tab_and_its_pages_render(self):
        labels = [t.label for t in self.at.tabs]
        self.assertIn("💼 BSIAI", labels)
        import bsiai_view
        for page in bsiai_view.PAGES:
            self.assertIn(page, labels)

    def test_the_bsiai_dashboard_draws_its_own_batches(self):
        import bsiai_build as bb
        sel = self.at.selectbox(key="bsiai_batch")
        self.assertEqual(set(sel.options), set(bb.CODES))

    def test_the_aicap_dashboard_is_untouched(self):
        sel = self.at.selectbox(key="aicap_batch")
        self.assertTrue(all(o.startswith(("B", "ECAP")) for o in sel.options))
        self.assertFalse(any("Accelerator" in o or o.startswith("B3-") for o in sel.options))

    def test_every_fragment_painted_once(self):
        painted = self.at.session_state["_painted"]
        self.assertEqual(painted.get("bsiai"), 1)
        self.assertEqual(painted.get("dashboard"), 1)

    def test_the_roster_grids_mask_contact_details(self):
        """Both Roster grids (AI CAP's and BSIAI's) are dataframes with an
        Email column; by default every address on them is masked."""
        grids = [d.value for d in self.at.dataframe
                 if "Email" in list(d.value.columns) and "Phone" in list(d.value.columns)]
        self.assertGreaterEqual(len(grids), 2, "expected the AI CAP and the BSIAI roster grids")
        for g in grids:
            emails = [e for e in g["Email"].astype(str) if e]
            self.assertTrue(emails)
            self.assertTrue(all("…@" in e or e == "•••" for e in emails),
                            [e for e in emails if "…@" not in e][:3])


@unittest.skipUnless(os.path.exists(BSIAI_STORE), "no local BSIAI store")
class TestStandalonePage(unittest.TestCase):
    def test_runs_and_shows_every_page(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(BSIAI_APP, default_timeout=300).run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        import bsiai_view
        labels = [t.label for t in at.tabs]
        for page in bsiai_view.PAGES:
            self.assertIn(page, labels)


if __name__ == "__main__":
    unittest.main()
