"""The derived-facts memo: what it refuses to remember, and what it forgets.

A cache that is merely fast is easy. This file pins the four ways this one could
go silently wrong, each of which has a real precedent in this repo:

1. A key that does not cover every input. Drive's "Upload new version" gives the
   same file id new bytes, and that is the documented fix for a bad Zoom export.
2. A memoised fact derived from the file NAME. The session date is parsed out of
   the filename; re-dating a mis-named export is a normal human correction.
3. A fact memoised from bytes the run never verified. The local byte cache is
   read before any network call, so a stale copy plus a fresh md5 would stamp
   old facts as current, unrecoverably.
4. A rule change that does not invalidate. The phone fix moved every session
   +8.4%; the curve trim moved stick10 -5.4 points. Either, applied to only the
   week's new files, puts two rules inside one store with nothing saying which.
"""
import gzip
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import derived_cache as dcache  # noqa: E402
import polls  # noqa: E402
import sessionmeta  # noqa: E402

RULES = {"sessionmeta": "aaaa", "polls": "bbbb"}
OLD = "2000-01-01"
FACT = {"duration_min": 180, "peak": 312, "curve": [1, 2, 3], "topic": "AI in HR"}


def row(f, name="attendee_9_2026_08_30.csv", md5="m1", mtime="2026-08-30T10:00:00Z"):
    return {"id": f, "name": name, "md5Checksum": md5, "modifiedTime": mtime}


class TestKey(unittest.TestCase):
    def test_the_signature_is_part_of_the_key(self):
        # Same id, new bytes -> different key. This is failure mode 1: Drive's
        # "Manage versions -> Upload new version" keeps the id.
        a = dcache.key_for(row("F1", md5="before"))
        b = dcache.key_for(row("F1", md5="after"))
        self.assertNotEqual(a, b)

    def test_the_name_is_part_of_the_key(self):
        # Failure mode 2: the session date comes from the filename, so a rename
        # with identical bytes must NOT hit.
        a = dcache.key_for(row("F1", name="attendee_9_2026_08_23.csv"))
        b = dcache.key_for(row("F1", name="attendee_9_2026_08_30.csv"))
        self.assertNotEqual(a, b)

    def test_md5_is_preferred_over_modified_time(self):
        k = dcache.key_for(row("F1", md5="m", mtime="t"))
        self.assertIn(":m:", k)

    def test_modified_time_is_the_fallback_when_drive_reports_no_md5(self):
        k = dcache.key_for({"id": "F1", "name": "n", "modifiedTime": "t"})
        self.assertEqual(k, "F1:t:n")

    def test_no_signature_at_all_means_no_key_and_therefore_always_a_miss(self):
        # Missing metadata must cost time, never correctness.
        self.assertIsNone(dcache.key_for({"id": "F1", "name": "n"}))
        self.assertIsNone(dcache.key_for({"name": "n", "md5Checksum": "m"}))
        self.assertIsNone(dcache.key_for({}))
        self.assertIsNone(dcache.key_for(None))


class TestRuleVersion(unittest.TestCase):
    def test_it_hashes_the_whole_module_not_one_function(self):
        # measure() depends on _ts, _TIME_FMTS and MAX_MINUTES; parse() on
        # _KINDS and nps_from_dist. Hashing one function would miss all of them.
        a = dcache.rule_version(sessionmeta)
        b = dcache.rule_version(polls)
        self.assertTrue(a and b and a != b)
        self.assertEqual(a, dcache.rule_version(sessionmeta))   # stable

    def test_it_fails_closed_when_the_source_cannot_be_read(self):
        v = dcache.rule_version(object())
        self.assertTrue(v)
        self.assertNotEqual(v, dcache.rule_version(sessionmeta))
        # and it must not equal a plausible stored value either
        self.assertNotIn(v, ("", None, "aaaa"))


class TestStageRefusals(unittest.TestCase):
    def setUp(self):
        self.c = {}

    def test_a_name_derived_field_is_refused_entry(self):
        # Failure mode 2, the other half: _mm must never be remembered.
        ok = dcache.stage(self.c, "sessionmeta", "k", dict(FACT, _mm="08_30"),
                          verified=True, today="2026-09-08")
        self.assertFalse(ok)
        self.assertIsNone(dcache.get(self.c, "sessionmeta", "k"))

    def test_unverified_bytes_are_used_but_never_remembered(self):
        # Failure mode 3. The run still gets its answer; the memo stays clean.
        self.assertFalse(dcache.stage(self.c, "sessionmeta", "k", FACT,
                                      verified=False, today="2026-09-08"))
        self.assertEqual(self.c, {})

    def test_a_row_with_no_key_is_not_stored(self):
        self.assertFalse(dcache.stage(self.c, "sessionmeta", None, FACT,
                                      verified=True, today="2026-09-08"))

    def test_an_unknown_namespace_is_refused(self):
        self.assertFalse(dcache.stage(self.c, "marks", "k", FACT,
                                      verified=True, today="2026-09-08"))

    def test_first_seen_survives_a_rewrite(self):
        # Otherwise every weekly rewrite resets the clock and the rolling
        # re-derivation silently never happens.
        dcache.stage(self.c, "polls", "k", {"nps": 50}, verified=True,
                     today="2026-01-01")
        for day in ("2026-02-01", "2026-03-01", "2026-04-01"):
            dcache.stage(self.c, "polls", "k", {"nps": 50}, verified=True,
                         today=day)
        self.assertEqual(self.c["polls"]["k"]["first_seen"], "2026-01-01")


class TestValidation(unittest.TestCase):
    """Values, not just key names — the PII and poisoned-value control."""

    def bad(self, r):
        self.assertFalse(dcache._validate(r), repr(r)[:80])

    def test_a_plausible_aggregate_passes(self):
        self.assertTrue(dcache._validate(FACT))
        self.assertTrue(dcache._validate(
            {"nps": 58, "dist": {"recommend": {"1": 0, "5": 12}}, "responses": 12}))

    def test_a_long_string_is_refused(self):
        # A name allow-list would pass {"topic": "<every attendee's email>"}.
        self.bad({"topic": "x@y.com," * 200})

    def test_a_list_of_strings_is_refused(self):
        self.bad({"curve": ["a", "b"]})
        self.bad({"attendees": ["a@b.com"]})

    def test_a_nested_structure_is_refused(self):
        self.bad({"rows": [{"email": "a@b.com"}]})
        self.bad({"x": {"y": {"z": 1}}})

    def test_an_absurd_curve_is_refused(self):
        self.bad({"curve": list(range(5000))})
        self.bad({"curve": [-1, 2]})

    def test_a_malformed_histogram_is_refused(self):
        self.bad({"dist": {"recommend": {"9": 3}}})
        self.bad({"dist": {"recommend": {"1": -2}}})
        self.bad({"dist": "5"})

    def test_empty_and_non_dict_rows_are_refused(self):
        self.bad({})
        self.bad(None)
        self.bad([1, 2])


class TestRoundTrip(unittest.TestCase):
    def _saved(self, cache):
        return dcache.dumps(cache, RULES)

    def test_a_stored_fact_comes_back(self):
        c = {}
        dcache.stage(c, "sessionmeta", "k", FACT, verified=True, today="2026-09-08")
        back, stats = dcache.loads(self._saved(c), RULES, OLD)
        self.assertEqual(dcache.get(back, "sessionmeta", "k"), FACT)
        self.assertEqual(stats["loaded"], 1)

    def test_a_changed_rule_discards_that_namespace_and_only_that_one(self):
        # Failure mode 4.
        c = {}
        dcache.stage(c, "sessionmeta", "a", FACT, verified=True, today="2026-09-08")
        dcache.stage(c, "polls", "b", {"nps": 50}, verified=True, today="2026-09-08")
        back, stats = dcache.loads(self._saved(c),
                                   {"sessionmeta": "CHANGED", "polls": "bbbb"}, OLD)
        self.assertIsNone(dcache.get(back, "sessionmeta", "a"))
        self.assertEqual(dcache.get(back, "polls", "b"), {"nps": 50})
        self.assertEqual(stats["discarded"], 1)

    def test_a_changed_schema_discards_everything(self):
        c = {}
        dcache.stage(c, "polls", "b", {"nps": 50}, verified=True, today="2026-09-08")
        raw = json.loads(gzip.decompress(self._saved(c)))
        raw["schema"] = dcache.SCHEMA + 1
        blob = gzip.compress(json.dumps(raw).encode())
        back, _ = dcache.loads(blob, RULES, OLD)
        self.assertEqual(back, {})

    def test_entries_past_the_max_age_are_dropped(self):
        c = {}
        dcache.stage(c, "polls", "old", {"nps": 1}, verified=True, today="2026-01-01")
        dcache.stage(c, "polls", "new", {"nps": 2}, verified=True, today="2026-09-01")
        back, stats = dcache.loads(self._saved(c), RULES, "2026-06-01")
        self.assertIsNone(dcache.get(back, "polls", "old"))
        self.assertIsNotNone(dcache.get(back, "polls", "new"))
        self.assertEqual(stats["expired"], 1)

    def test_a_corrupt_or_truncated_file_yields_an_empty_cache_not_an_exception(self):
        for blob in (b"", b"not gzip", gzip.compress(b"not json"),
                     self._saved({"polls": {}})[:20]):
            back, stats = dcache.loads(blob, RULES, OLD)
            self.assertEqual(back, {})
            self.assertEqual(stats["loaded"], 0)

    def test_a_poisoned_value_on_disk_is_a_miss_not_a_trusted_input(self):
        # The memo is a MUTABLE input to published numbers. Anyone with write
        # access to the Drive folder, or a bug in a past version, could leave a
        # wrong shape behind; it must not be believed on the way back in.
        c = {"polls": {"k": {"first_seen": "2026-09-01",
                             "f": {"nps": 50, "leak": ["a@b.com"]}}}}
        raw = {"schema": dcache.SCHEMA,
               "polls": {"rule": "bbbb", "rows": c["polls"]},
               "sessionmeta": {"rule": "aaaa", "rows": {}}}
        blob = gzip.compress(json.dumps(raw).encode())
        back, stats = dcache.loads(blob, RULES, OLD)
        self.assertIsNone(dcache.get(back, "polls", "k"))
        self.assertEqual(stats["invalid"], 1)

    def test_dumps_raises_rather_than_writing_an_unexpected_shape(self):
        c = {"polls": {"k": {"first_seen": "2026-09-01",
                             "f": {"emails": ["a@b.com"]}}}}
        with self.assertRaises(ValueError):
            dcache.dumps(c, RULES)


class TestPrune(unittest.TestCase):
    def test_entries_for_files_no_longer_on_the_drive_are_dropped(self):
        c = {}
        for k in ("a", "b", "c"):
            dcache.stage(c, "polls", k, {"nps": 1}, verified=True, today="2026-09-08")
        self.assertEqual(dcache.prune_to(c, "polls", {"a", "c"}), 1)
        self.assertEqual(sorted(c["polls"]), ["a", "c"])

    def test_pruning_an_empty_namespace_is_harmless(self):
        self.assertEqual(dcache.prune_to({}, "polls", set()), 0)


class TestRealFactsSurviveTheRoundTrip(unittest.TestCase):
    """The shapes sessionmeta and polls actually produce must validate."""

    def test_a_real_sessionmeta_row_validates(self):
        text = "\n".join([
            "Topic,Webinar ID,Actual Start Time,Actual Duration (minutes),"
            "# Registrants,# Cancelled registrants,Unique Viewers,Total Users,"
            "Max Concurrent Views,Enable Registration",
            "AI in HR,942 2996 0431,08/30/2026 20:00:00,180,900,10,540,545,312,Yes",
            "", "Attendee Details",
            "Attended,User Name,Email,Join Time,Leave Time,Duration (Minutes)",
            "Yes,A,a@x.com,08/30/2026 20:00:00,08/30/2026 20:30:00,30",
        ])
        h = sessionmeta.parse_header(text)
        h.update(sessionmeta.measure(text))
        self.assertTrue(dcache._validate(h), sorted(h))
        c = {}
        self.assertTrue(dcache.stage(c, "sessionmeta", "k", h,
                                     verified=True, today="2026-09-08"))
        back, _ = dcache.loads(dcache.dumps(c, RULES), RULES, OLD)
        self.assertEqual(dcache.get(back, "sessionmeta", "k")["peak_computed"], 1)

    def test_a_real_polls_row_validates(self):
        text = "\n".join([
            "Poll Report", "",
            "#,User Name,Email Address,Submitted Date and Time,"
            "How likely would you recommend it to your friends?",
            "1,A,a@x.com,08/30/2026 20:11:04,5",
            "2,B,b@x.com,08/30/2026 20:11:09,4",
        ])
        got = polls.parse(text)
        got["submitted_first"] = polls.submission_times(text)[0].isoformat()
        self.assertTrue(dcache._validate(got), sorted(got))
        c = {}
        self.assertTrue(dcache.stage(c, "polls", "k", got,
                                     verified=True, today="2026-09-08"))
        back, _ = dcache.loads(dcache.dumps(c, RULES), RULES, OLD)
        self.assertEqual(dcache.get(back, "polls", "k")["responses"], 2)

    def test_no_real_row_carries_a_per_person_field(self):
        # The PII argument, asserted rather than assumed.
        text = ("Poll Report\n\n#,User Name,Email Address,Submitted Date and Time,Q\n"
                "1,Asha,asha@x.com,08/30/2026 20:11:04,5")
        got = polls.parse(text)
        blob = json.dumps(got)
        self.assertNotIn("asha@x.com", blob)
        self.assertNotIn("Asha", blob)


if __name__ == "__main__":
    unittest.main()
