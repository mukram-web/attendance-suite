"""The coverage gate, and what the store records about the memo.

Steps [5a] and [5a2] are the only two in the pipeline with no gate above them
and a blanket `except Exception` inside them. That was tolerable while their
columns were recomputed from the Zoom exports every single week — a failure cost
one column for one week and self-healed the next Monday.

It stopped being tolerable when a memoised fact could reach a published number.
"Green but blank" would then persist, and every Monday in between would archive
it immutably with nothing pruned. So the run now compares its own coverage
against last week's store and refuses to publish a collapse.

The threshold direction is the whole point: sessions do not vanish from the
past. The corpus only grows. A drop is a bug upstream, never a fact about the
week.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb  # noqa: E402

import derived_cache  # noqa: E402
import pipeline  # noqa: E402


def store_with(rows) -> str:
    """A throwaway duckdb store carrying just a `sessions` meta blob."""
    path = os.path.join(tempfile.mkdtemp(), "s.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO meta VALUES ('sessions', ?)", [json.dumps(rows)])
    con.close()
    return path


def sess(n, duration=True, rating=True):
    return [{"duration_hrs": 3.0 if duration else None,
             "rating": 4.5 if rating else None} for _ in range(n)]


class TestStoreCoverage(unittest.TestCase):
    def test_it_counts_sessions_carrying_each_fact(self):
        p = store_with(sess(10) + sess(5, duration=False) + sess(3, rating=False))
        self.assertEqual(pipeline._store_coverage(p),
                         {"duration": 13, "ratings": 15})

    def test_a_store_it_cannot_read_is_None_not_zero(self):
        # None means "no yardstick"; zero would mean "everything vanished" and
        # would fail every run against a store that merely failed to download.
        self.assertIsNone(pipeline._store_coverage("does-not-exist.duckdb"))
        empty = os.path.join(tempfile.mkdtemp(), "e.duckdb")
        con = duckdb.connect(empty)
        con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
        con.close()
        self.assertEqual(pipeline._store_coverage(empty),
                         {"duration": 0, "ratings": 0})


class TestTheGateArithmetic(unittest.TestCase):
    """The comparison itself, exactly as main() applies it."""

    @staticmethod
    def drops(prev, now):
        return [k for k in now
                if prev.get(k, 0) >= 20
                and now[k] < prev[k] * pipeline.COVERAGE_FLOOR]

    def test_a_collapse_to_a_handful_is_caught(self):
        # The fatal scenario: cached rows arrive without their name-derived key,
        # lookup_by_session drops them all, and only the week's fresh parses
        # survive. 5 of 500 sessions keep a duration.
        self.assertEqual(self.drops({"duration": 500, "ratings": 480},
                                    {"duration": 5, "ratings": 480}),
                         ["duration"])

    def test_total_blanking_is_caught(self):
        self.assertEqual(sorted(self.drops({"duration": 500, "ratings": 480},
                                           {"duration": 0, "ratings": 0})),
                         ["duration", "ratings"])

    def test_normal_weekly_growth_passes(self):
        self.assertEqual(self.drops({"duration": 500, "ratings": 480},
                                    {"duration": 543, "ratings": 521}), [])

    def test_a_handful_of_missing_exports_passes(self):
        # Real and routine: a few Zoom exports go missing, an L2 row is
        # corrected. The floor must not turn those into a red run.
        self.assertEqual(self.drops({"duration": 500, "ratings": 480},
                                    {"duration": 470, "ratings": 455}), [])

    def test_a_small_corpus_is_exempt(self):
        # Below 20 the ratios are noise, and a genuinely new deployment would
        # otherwise fail its second run.
        self.assertEqual(self.drops({"duration": 4, "ratings": 2},
                                    {"duration": 0, "ratings": 0}), [])

    def test_the_floor_is_a_real_threshold_not_equality(self):
        self.assertGreater(pipeline.COVERAGE_FLOOR, 0.5)
        self.assertLess(pipeline.COVERAGE_FLOOR, 1.0)


class TestCacheMeta(unittest.TestCase):
    def test_the_rule_hash_is_recorded_for_provenance(self):
        m = pipeline._cache_meta({"polls": {"hit": 9, "miss": 1, "listed": 10}},
                                 {"polls": "abc123", "sessionmeta": "def456"})
        self.assertEqual(m["polls"]["rule"], "abc123")
        self.assertEqual(m["polls"]["hit"], 9)

    def test_drive_file_ids_never_reach_the_store(self):
        # `keys` is working state for the hygiene pass. The store is archived
        # forever with nothing pruned, so it must not carry Drive identifiers.
        m = pipeline._cache_meta(
            {"polls": {"hit": 1, "keys": {"1abcDEF:md5:poll_9_2026_08_30.csv"}}},
            {"polls": "abc123"})
        self.assertNotIn("keys", m["polls"])
        self.assertNotIn("1abcDEF", json.dumps(m))

    def test_a_namespace_that_did_not_run_is_absent_rather_than_zeroed(self):
        m = pipeline._cache_meta({"polls": {"hit": 1}}, {"polls": "x"})
        self.assertNotIn("sessionmeta", m)

    def test_it_survives_an_empty_run(self):
        self.assertEqual(pipeline._cache_meta({}, {}), {})


class TestTheNamespacesLineUp(unittest.TestCase):
    def test_the_pipeline_and_the_cache_agree_on_the_namespace_names(self):
        # A typo here would mean a namespace that never hits and never warns.
        self.assertEqual(set(derived_cache.NAMESPACES), {"sessionmeta", "polls"})


if __name__ == "__main__":
    unittest.main()
