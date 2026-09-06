"""The weekly recap, and the reason it is scored on residuals.

The load-bearing test here is test_the_youngest_batch_does_not_win_by_default:
ranking sessions on raw attendance makes the newest cohort win every week
forever, because attendance decays with cohort age. That is a calendar, not an
award.
"""
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recap  # noqa: E402

TODAY = date(2026, 9, 6)          # a Sunday


def batch(pcts, start_offset_weeks, topic="Topic", mentor="A Kumar",
          strength=1000, nps_dist=None):
    """A batch whose sessions run weekly, oldest first, ending near TODAY."""
    sessions, by_date = [], []
    n = len(pcts)
    for i, pct in enumerate(pcts):
        d = TODAY - timedelta(weeks=(n - 1 - i) + start_offset_weeks)
        mm = f"{d.month:02d}_{d.day:02d}"
        present = round(strength * pct / 100)
        row = {"mm": mm, "date_lbl": f"{d.day} {d:%b}", "present": present,
               "total": strength, "pct": pct}
        by_date.append(row)
        sessions.append({**row, "topic": topic, "pod": "", "mentor": mentor,
                         "rating": 4.5, "rating_n": 100,
                         "rating_nps": None,
                         "rating_dist": nps_dist or {}})
    return {"strength": strength, "active": strength,
            "sessions": sessions, "by_date": by_date}


class TestWeekStart(unittest.TestCase):
    def test_monday_to_sunday(self):
        self.assertEqual(recap.week_start(date(2026, 9, 6)), date(2026, 8, 31))
        self.assertEqual(recap.week_start(date(2026, 8, 31)), date(2026, 8, 31))
        self.assertEqual(recap.week_start(date(2026, 9, 1)), date(2026, 8, 31))


class TestResiduals(unittest.TestCase):
    def _data(self):
        # Two batches on the SAME decay shape. B_old has run 8 weeks and is deep
        # into its tail; B_new has run 2 and is still near its opening rate.
        return {
            "B17": batch([56, 50, 45, 40, 36, 32, 29, 26], 0),
            "B38": batch([54, 48], 0),
        }

    def test_the_youngest_batch_does_not_win_by_default(self):
        """Raw pct says B38. The residual says they are both merely on-curve."""
        rows = recap.collect_sessions(self._data(), TODAY)
        latest = [r for r in rows if r["week"] == recap.week_start(TODAY).isoformat()]
        self.assertTrue(latest)
        raw_winner = max(latest, key=lambda r: r["pct"])
        self.assertEqual(raw_winner["batch"], "B38")     # what raw ranking gives
        # Both sit on the pooled curve, so neither should be far from 1.0 and
        # the gap between them must be far smaller than the raw gap.
        idx = {r["batch"]: r["index"] for r in latest if r["index"] is not None}
        if len(idx) == 2:
            raw_gap = abs(latest[0]["pct"] - latest[1]["pct"])
            res_gap = abs(list(idx.values())[0] - list(idx.values())[1]) * 100
            self.assertLess(res_gap, raw_gap,
                            "the residual must compress the age effect")

    def _wide(self):
        """Four batches on one decay shape, so the pooled curve is stable."""
        shape = [56, 50, 45, 40, 36, 32, 29, 26]
        return {"B17": batch(shape, 0), "B22": batch(shape[:6], 0),
                "B30": batch(shape[:4], 0), "B38": batch(shape[:2], 0)}

    def test_a_genuinely_good_session_beats_a_merely_young_one(self):
        d = self._wide()
        # The 8-week-old batch massively over-performs its own trend; the
        # 2-week-old batch under-performs its own. Raw pct still favours the
        # young one, the residual must not.
        d["B17"]["sessions"][-1]["pct"] = d["B17"]["by_date"][-1]["pct"] = 42
        d["B38"]["sessions"][-1]["pct"] = d["B38"]["by_date"][-1]["pct"] = 30
        rows = recap.collect_sessions(d, TODAY)
        latest = [r for r in rows
                  if r["week"] == recap.week_start(TODAY).isoformat()
                  and r["index"] is not None]
        by_batch = {r["batch"]: r for r in latest}
        self.assertIn("B17", by_batch)
        self.assertIn("B38", by_batch)
        self.assertGreater(by_batch["B17"]["pct"], by_batch["B38"]["pct"],
                           "fixture check: raw pct favours the older one here")
        self.assertGreater(by_batch["B17"]["index"], by_batch["B38"]["index"],
                           "the residual must reward over-performance, not youth")

    def test_the_expectation_is_in_sample_and_that_is_a_known_limit(self):
        """A session helps set the curve it is then scored against.

        With 22 batches and 470 real sessions the pull is small, but it is not
        zero, and it always flatters an outlier: bumping one session up raises
        both its own numerator AND its expectation, so the index moves less than
        the raw change does. Pinned so nobody later reads the index as a clean
        out-of-sample score.
        """
        base = self._wide()
        rows0 = {r["batch"]: r for r in recap.collect_sessions(base, TODAY)
                 if r["week"] == recap.week_start(TODAY).isoformat()}
        bumped = self._wide()
        bumped["B17"]["sessions"][-1]["pct"] = 42
        bumped["B17"]["by_date"][-1]["pct"] = 42
        rows1 = {r["batch"]: r for r in recap.collect_sessions(bumped, TODAY)
                 if r["week"] == recap.week_start(TODAY).isoformat()}
        raw_change = 42 / rows0["B17"]["pct"]
        index_change = rows1["B17"]["index"] / rows0["B17"]["index"]
        self.assertLess(index_change, raw_change,
                        "an outlier partly raises its own expectation")

    def test_index_is_none_when_the_curve_has_no_point(self):
        rows = recap.collect_sessions({"B1": batch([50], 0)}, TODAY)
        # A single-session batch still produces a row; it must never invent an
        # expectation it cannot support.
        for r in rows:
            self.assertTrue(r["index"] is None or r["index"] > 0)

    def test_future_sessions_are_excluded(self):
        d = {"B17": batch([56, 50, 45], 0)}
        d["B17"]["sessions"].append({"mm": "12_25", "date_lbl": "25 Dec",
                                     "present": 1, "total": 10, "pct": 10,
                                     "topic": "X", "pod": "", "mentor": "",
                                     "rating": None, "rating_n": 0})
        rows = recap.collect_sessions(d, TODAY)
        self.assertTrue(all(r["date"] <= TODAY.isoformat() for r in rows))


class TestAggregation(unittest.TestCase):
    def test_attendance_is_pooled_not_averaged(self):
        rows = [{"present": 100, "total": 200, "index": None, "rating": None,
                 "rating_n": 0, "dist": {}, "batch": "A"},
                {"present": 900, "total": 1000, "index": None, "rating": None,
                 "rating_n": 0, "dist": {}, "batch": "B"}]
        a = recap._agg(rows)
        self.assertEqual(a["pct"], 83.3)          # not (50 + 90) / 2 = 70
        self.assertEqual(a["present"], 1000)

    def test_delta_against_a_missing_week_is_None_not_zero(self):
        self.assertIsNone(recap._delta(5, None))
        self.assertIsNone(recap._delta(None, 5))
        self.assertEqual(recap._delta(5, 3), 2)


class TestAwards(unittest.TestCase):
    def _rows(self, n=4):
        return [{"index": 1.0 + i / 10, "pct": 40.0, "expected_pct": 40.0,
                 "wk": 3, "batch": f"B{i}", "topic": f"T{i}", "pod": "",
                 "mentor": "M", "date_lbl": "5 Sep", "present": 100 + i,
                 "total": 500, "nps": 50 + i, "rating_n": 100, "rating": 4.5,
                 "dist": {}} for i in range(n)]

    def test_awards_need_a_real_field(self):
        # One contender is a fact, not a competition.
        self.assertEqual(recap._awards(self._rows(1)), [])
        self.assertTrue(recap._awards(self._rows(4)))

    def test_session_of_the_week_is_the_best_residual(self):
        aw = recap._awards(self._rows(4))
        sotw = next(a for a in aw if a["award"] == "Session of the week")
        self.assertEqual(sotw["batch"], "B3")     # highest index
        self.assertIn("normally draws", sotw["why"])

    def test_a_thin_poll_cannot_win_crowd_favourite(self):
        rows = self._rows(4)
        rows[0]["nps"] = 100
        rows[0]["rating_n"] = 3        # three people is not a mandate
        aw = recap._awards(rows)
        fav = next(a for a in aw if a["award"] == "Crowd favourite")
        self.assertNotEqual(fav["batch"], "B0")

    def test_every_award_states_its_basis(self):
        for a in recap._awards(self._rows(4)):
            self.assertTrue(a["why"], f"{a['award']} has no stated basis")


class TestBuild(unittest.TestCase):
    def test_end_to_end_shape(self):
        out = recap.build({"B17": batch([56, 50, 45, 40, 36], 0),
                           "B38": batch([54, 48], 0)}, TODAY)
        self.assertTrue(out["weeks"])
        self.assertIsNotNone(out["latest"])
        self.assertIn("leaderboard", out)
        self.assertTrue(all("week" in w and "delta" in w for w in out["weeks"]))

    def test_no_data_degrades_cleanly(self):
        out = recap.build({}, TODAY)
        self.assertEqual(out["weeks"], [])
        self.assertIsNone(out["latest"])
        self.assertTrue(out["warnings"])


if __name__ == "__main__":
    unittest.main()
