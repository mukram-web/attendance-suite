"""Publishing a PARALLEL dataset without letting it become the weekly record.

`STORE_OUT` builds a second store beside the real one. Until now it could only
ever stay on the machine that built it, which is why the deployed app had no
"Data set" control at all: nothing uploaded `attendance_lms.duckdb` to Drive,
and the app decided availability by looking at its own disk.

Making it publishable reopens the question the old blanket refusal closed — the
upload at [8/8], next week's frozen base at [8a] and the immutable archive
snapshot at [8b] all name their file by the REAL constants, so a parallel run
that reaches any of them hands the weekly record a workbook built from a
different roster. `parallel_store_name` is where that is decided, once, and
these are the combinations it has to get right.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_data  # noqa: E402
import pipeline  # noqa: E402

LMS = "attendance_lms.duckdb"


class ParallelStoreName(unittest.TestCase):
    def test_normal_run_is_not_parallel(self):
        """No STORE_OUT: the ordinary weekly publish, unchanged."""
        self.assertEqual(
            pipeline.parallel_store_name("", no_upload=False,
                                         publish_parallel=False), "")

    def test_store_out_equal_to_the_real_name_is_not_parallel(self):
        """`STORE_OUT=attendance.duckdb` names the real store, so it IS the
        normal path — not a parallel one that may skip [8a] and [8b]."""
        self.assertEqual(
            pipeline.parallel_store_name(pipeline.STORE_NAME, no_upload=False,
                                         publish_parallel=False), "")

    def test_parallel_publishes_under_its_own_name(self):
        self.assertEqual(
            pipeline.parallel_store_name(LMS, no_upload=False,
                                         publish_parallel=True), LMS)

    def test_parallel_upload_without_the_flag_is_still_refused(self):
        """The original guard. Uploading this run would publish the parallel
        dataset AS the dashboard and archive it as that week's history."""
        with self.assertRaises(SystemExit) as cm:
            pipeline.parallel_store_name(LMS, no_upload=False,
                                         publish_parallel=False)
        self.assertIn("PARALLEL", str(cm.exception))
        self.assertIn("--publish-parallel", str(cm.exception))

    def test_publish_parallel_without_store_out_is_refused(self):
        """The new way to get it wrong: the real store down the parallel path
        moves the dashboard while [8a] and [8b] silently do not run."""
        with self.assertRaises(SystemExit) as cm:
            pipeline.parallel_store_name("", no_upload=False,
                                         publish_parallel=True)
        self.assertIn("STORE_OUT", str(cm.exception))

    def test_publish_parallel_naming_the_real_store_is_refused(self):
        with self.assertRaises(SystemExit):
            pipeline.parallel_store_name(pipeline.STORE_NAME, no_upload=False,
                                         publish_parallel=True)

    def test_no_upload_never_raises(self):
        """--no-upload is the documented local flow and must keep working
        whatever STORE_OUT says; nothing is published, so nothing is at risk."""
        for out in ("", LMS, pipeline.STORE_NAME):
            with self.subTest(store_out=out):
                pipeline.parallel_store_name(out, no_upload=True,
                                             publish_parallel=False)


class StoreExists(unittest.TestCase):
    """The app asks this on every page load to decide whether to OFFER the
    second data set, so it must be cheap and must never raise."""

    def setUp(self):
        self._real = live_data._drive_service
        self.addCleanup(lambda: setattr(live_data, "_drive_service", self._real))

    def _service_returning(self, files):
        class _Files:
            def list(self, **kw):
                class _R:
                    def execute(_):
                        return {"files": files}
                return _R()

        class _Svc:
            def files(self):
                return _Files()

        return lambda: _Svc()

    def test_present(self):
        live_data._drive_service = self._service_returning([{"id": "x",
                                                            "name": LMS}])
        self.assertTrue(live_data.store_exists("folder", LMS))

    def test_absent(self):
        live_data._drive_service = self._service_returning([])
        self.assertFalse(live_data.store_exists("folder", LMS))

    def test_drive_error_hides_the_control_rather_than_raising(self):
        """A 500 from Drive must not paint a traceback over the dashboard, and
        must not offer a data set whose file cannot then be downloaded."""
        def _boom():
            raise RuntimeError("Drive is having a day")
        live_data._drive_service = _boom
        self.assertFalse(live_data.store_exists("folder", LMS))


if __name__ == "__main__":
    unittest.main()
