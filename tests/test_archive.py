"""
Unit tests for archive.py — fakes for Drive, no network and no real data.
Run:  python -m unittest tests.test_archive    (from the project root)
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import archive  # noqa: E402


class FakeDrive:
    """The slice of the Drive API archive.py touches, backed by a dict."""

    def __init__(self, folders=(), files=()):
        self.folders = {name: f"folder_{i}" for i, name in enumerate(folders)}
        self.stored = {name: b"x" for name in files}
        self.created_folders = []
        self.uploads = []

    # -- files() surface -------------------------------------------------
    def files(self):
        return self

    def list(self, q="", **kw):
        self._q = q
        return self

    def create(self, body=None, **kw):
        self._created = body
        return self

    def execute(self):
        if getattr(self, "_created", None):
            body, self._created = self._created, None
            self.created_folders.append(body["name"])
            fid = f"folder_new_{len(self.created_folders)}"
            self.folders[body["name"]] = fid
            return {"id": fid}
        q = getattr(self, "_q", "")
        if archive.FOLDER_MIME in q:
            for name, fid in self.folders.items():
                if f"name = '{name}'" in q:
                    return {"files": [{"id": fid}]}
            return {"files": []}
        return {"files": []}


class FakeLiveData:
    """Stand-in for live_data, recording what would have been uploaded."""

    def __init__(self, existing=(), fetch_fails=()):
        self.existing = set(existing)
        self.fetch_fails = set(fetch_fails)
        self.uploaded = []

    def find_in_folder(self, svc, folder, name):
        return {"id": "x"} if name in self.existing else None

    def upload_to_folder(self, svc, folder, name, data, mime="x"):
        self.uploaded.append(name)
        self.existing.add(name)
        return "id"

    def fetch_sheet_cached(self, svc, fid):
        if fid in self.fetch_fails:
            raise RuntimeError("boom")
        return b"sheet-bytes", "stamp"


class ArchiveTest(unittest.TestCase):
    WHEN = date(2026, 9, 5)
    CFG = {"l2_id": "L2", "roster_id": "R",
           "curriculum_id": "C", "bsiai_roster_id": "B"}

    def setUp(self):
        self.fake = FakeLiveData()
        self._real = sys.modules.get("live_data")
        sys.modules["live_data"] = self.fake

    def tearDown(self):
        if self._real is not None:
            sys.modules["live_data"] = self._real
        else:
            sys.modules.pop("live_data", None)

    def _run(self, drive=None, cfg=None, **kw):
        return archive.run(drive or FakeDrive(folders=["archive"]),
                           self.CFG if cfg is None else cfg,
                           "store", when=self.WHEN, log=lambda *a: None, **kw)


class TestNaming(unittest.TestCase):
    def test_name_is_date_stamped_and_sorts_chronologically(self):
        names = [archive.snapshot_name("Roster", date(2026, 9, d)) for d in (5, 12, 1)]
        self.assertIn("Roster_2026-09-05.xlsx", names)
        self.assertEqual(sorted(names), [
            "Roster_2026-09-01.xlsx", "Roster_2026-09-05.xlsx",
            "Roster_2026-09-12.xlsx"])

    def test_extension_override(self):
        self.assertEqual(
            archive.snapshot_name("attendance_store", date(2026, 9, 5), "duckdb"),
            "attendance_store_2026-09-05.duckdb")


class TestFolder(unittest.TestCase):
    def test_existing_folder_is_reused(self):
        d = FakeDrive(folders=["archive"])
        self.assertEqual(archive.ensure_folder(d, "parent"), "folder_0")
        self.assertEqual(d.created_folders, [])

    def test_folder_created_when_missing(self):
        d = FakeDrive()
        archive.ensure_folder(d, "parent")
        self.assertEqual(d.created_folders, ["archive"])


class TestRun(ArchiveTest):
    def test_every_configured_sheet_is_saved(self):
        out = self._run()
        self.assertEqual(len(out["saved"]), 4)
        self.assertEqual(out["errors"], [])
        for label in ("L2_Weekly_Live_Sessions", "Master_Batch_Rosters",
                      "Master_Curriculum_Schedule", "BSIAI_Roster"):
            self.assertIn(f"{label}_2026-09-05.xlsx", self.fake.uploaded)

    def test_rerunning_the_same_day_uploads_nothing(self):
        """Snapshots are immutable: a second run must not replace the morning's
        copy with one taken after somebody edited the sheet."""
        self._run()
        first = list(self.fake.uploaded)
        out = self._run()
        self.assertEqual(self.fake.uploaded, first)
        self.assertEqual(out["saved"], [])
        self.assertEqual(len(out["skipped"]), 4)

    def test_a_later_date_makes_a_new_snapshot(self):
        self._run()
        archive.run(FakeDrive(folders=["archive"]), self.CFG, "store",
                    when=date(2026, 9, 12), log=lambda *a: None)
        self.assertIn("Master_Batch_Rosters_2026-09-12.xlsx", self.fake.uploaded)
        self.assertIn("Master_Batch_Rosters_2026-09-05.xlsx", self.fake.uploaded)

    def test_unconfigured_sheet_is_skipped_not_an_error(self):
        out = self._run(cfg={"l2_id": "L2"})          # only L2 configured
        self.assertEqual(len(out["saved"]), 1)
        self.assertEqual(out["errors"], [])
        self.assertTrue(any("not configured" in s for s in out["skipped"]))

    def test_one_failing_sheet_does_not_stop_the_others(self):
        self.fake.fetch_fails.add("R")                # roster unreadable
        out = self._run()
        self.assertEqual(len(out["saved"]), 3)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("Master_Batch_Rosters", out["errors"][0])
        self.assertIn("L2_Weekly_Live_Sessions_2026-09-05.xlsx", self.fake.uploaded)

    def test_store_is_archived_when_given(self):
        out = self._run(store_bytes=b"duckdb-bytes")
        self.assertIn("attendance_store_2026-09-05.duckdb", self.fake.uploaded)
        self.assertEqual(len(out["saved"]), 5)

    def test_store_omitted_when_not_given(self):
        self._run()
        self.assertFalse(any(n.endswith(".duckdb") for n in self.fake.uploaded))

    def test_unreachable_folder_reports_and_returns(self):
        """A broken archive must never raise into the weekly run."""
        class Broken(FakeDrive):
            def execute(self):
                raise RuntimeError("no drive")
        out = self._run(drive=Broken())
        self.assertEqual(out["saved"], [])
        self.assertTrue(out["errors"])
        self.assertEqual(self.fake.uploaded, [])

    def test_run_never_raises(self):
        class Broken(FakeDrive):
            def execute(self):
                raise RuntimeError("nope")
        try:
            self._run(drive=Broken())
        except Exception as e:                          # pragma: no cover
            self.fail(f"archive.run raised {e!r}; it must always return")


if __name__ == "__main__":
    unittest.main()
