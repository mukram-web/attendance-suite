"""Duration, peak, the retention curve and stickiness — from the Zoom report.

Two traps cost real numbers here and are pinned below.

The first: 'Host Details' and 'Panelist Details' carry their OWN `Attended,...`
header, so a reader that keys only on that header counts the host and a co-host
sitting in the room for the whole session. Every computed peak came out exactly
2 too high, which read as a plausible +0.2-1.3% against Zoom's own
`Max Concurrent Views` rather than as an obvious bug.

The second: a blank peak and a peak of zero are different claims. About a
quarter of reports omit `Max Concurrent Views` entirely, so `parse_header` must
return None there — a 0 would put "nobody came" against a session with hundreds.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessionmeta  # noqa: E402


HEADER = ("Topic,Webinar ID,Actual Start Time,Actual Duration (minutes),"
          "# Registrants,# Cancelled registrants,Unique Viewers,Total Users,"
          "Max Concurrent Views,Enable Registration")


def report(rows, section="Attendee Details", header=None):
    """A minimal attendee report: summary row, then one timed section.

    `rows` are (name, join, leave) with times as 'HH:MM:SS' on 08/30/2026.
    """
    out = [header if header is not None else
           HEADER + "\nPrompt Engineering,942 2996 0431,"
           "08/30/2026 20:00:00,180,900,10,540,545,,Yes", ""]
    out.append(section)
    out.append("Attended,User Name,Email,Join Time,Leave Time,Duration (Minutes)")
    for name, j, l in rows:
        out.append(f"Yes,{name},{name}@x.com,08/30/2026 {j},08/30/2026 {l},1")
    return "\n".join(out)


class TestHeader(unittest.TestCase):
    def test_the_summary_row_is_read_without_any_interval_arithmetic(self):
        h = sessionmeta.parse_header(report([]))
        self.assertEqual(h["webinar_id"], "94229960431")   # spaces stripped
        self.assertEqual(h["duration_min"], 180)
        self.assertEqual(h["duration_hrs"], 3.0)
        self.assertEqual(h["unique_viewers"], 540)
        self.assertEqual(h["registration"], "Yes")

    def test_a_missing_max_concurrent_is_None_not_zero(self):
        h = sessionmeta.parse_header(report([]))
        self.assertIsNone(h["peak"])
        self.assertIsNone(h["peak_source"])

    def test_a_present_max_concurrent_is_labelled_as_zoom_s_own(self):
        text = report([], header=HEADER + "\nT,942 2996 0431,"
                      "08/30/2026 20:00:00,180,900,10,540,545,312,Yes")
        h = sessionmeta.parse_header(text)
        self.assertEqual(h["peak"], 312)
        self.assertEqual(h["peak_source"], "zoom")

    def test_a_file_with_no_summary_row_is_None_not_a_dict_of_zeros(self):
        # A poll export, an Overview export, or a shape nobody has taught this
        # yet. Callers must be able to tell "no metadata" from "all zeros".
        self.assertIsNone(sessionmeta.parse_header(
            "#,User Name,Email Address,Q\n1,A,a@x.com,5"))
        self.assertIsNone(sessionmeta.parse_header(""))


class TestPeak(unittest.TestCase):
    def test_overlapping_stays_beat_the_headcount(self):
        # Three people, never more than two in the room at once.
        text = report([("A", "20:00:00", "20:30:00"),
                       ("B", "20:10:00", "20:40:00"),
                       ("C", "20:50:00", "21:00:00")])
        m = sessionmeta.measure(text)
        self.assertEqual(m["peak_computed"], 2)
        self.assertEqual(m["timed_rows"], 3)

    def test_the_host_section_does_not_inflate_the_peak(self):
        # THE bug this module was rewritten for: Host Details repeats the
        # 'Attended' header, and counting it added the host to every minute.
        attendees = report([("A", "20:00:00", "21:00:00"),
                            ("B", "20:00:00", "21:00:00")])
        with_host = attendees + "\n\nHost Details\n" \
            "Attended,User Name,Email,Join Time,Leave Time,Duration (Minutes)\n" \
            "Yes,Host,host@x.com,08/30/2026 19:55:00,08/30/2026 21:05:00,1"
        self.assertEqual(sessionmeta.measure(attendees)["peak_computed"],
                         sessionmeta.measure(with_host)["peak_computed"])

    def test_a_handoff_at_the_same_instant_is_not_an_extra_viewer(self):
        # B joins exactly as A leaves. Sorting leaves before joins keeps the
        # peak at 1 rather than reading a spurious 2.
        m = sessionmeta.measure(report([("A", "20:00:00", "20:30:00"),
                                        ("B", "20:30:00", "21:00:00")]))
        self.assertEqual(m["peak_computed"], 1)

    def test_rows_with_no_usable_times_are_skipped_not_counted_as_zero_length(self):
        text = report([("A", "20:00:00", "21:00:00")])
        text += "\nYes,B,b@x.com,,,1"
        m = sessionmeta.measure(text)
        self.assertEqual(m["timed_rows"], 1)
        self.assertEqual(m["peak_computed"], 1)

    def test_a_report_with_nobody_gives_None_rather_than_zero(self):
        m = sessionmeta.measure(report([]))
        self.assertIsNone(m["peak_computed"])
        self.assertIsNone(m["curve"])
        self.assertIsNone(m["t0"])


class TestCurve(unittest.TestCase):
    def test_the_curve_is_per_minute_concurrency_from_the_first_join(self):
        m = sessionmeta.measure(report([("A", "20:00:00", "20:05:00"),
                                        ("B", "20:02:00", "20:05:00")]))
        self.assertEqual(m["curve"], [1, 1, 2, 2, 2])
        self.assertEqual(m["span_hrs"], 0.1)

    def test_t0_is_the_first_join_so_anything_else_can_share_the_axis(self):
        # The poll marker is placed at (first submission - t0) in minutes. If
        # t0 were the host's start the marker would sit minutes off.
        m = sessionmeta.measure(report([("A", "20:07:00", "20:30:00")]))
        self.assertEqual(m["t0"], "2026-08-30T20:07:00")

    def test_an_overnight_session_is_capped_rather_than_storing_a_thousand_zeros(self):
        m = sessionmeta.measure(report([("A", "20:00:00", "23:59:00"),
                                        ("B", "20:00:00", "20:01:00")]))
        self.assertLessEqual(len(m["curve"]), sessionmeta.MAX_MINUTES)

    def test_the_curve_does_not_end_on_a_phantom_empty_minute(self):
        # The last leave always lands in the final bucket, so the raw sweep
        # always ended in a zero -- 521 of 521 curves in the live store. That
        # minute is an artefact of the bucketing, and counting it made every
        # stickiness figure divide a real tail by one minute too many.
        for rows in ([("A", "20:00:00", "20:05:00")],
                     [("A", "20:00:00", "20:05:30")],
                     [("A", "20:00:00", "20:30:00"), ("B", "20:01:00", "20:29:00")]):
            m = sessionmeta.measure(report(rows))
            self.assertNotEqual(m["curve"][-1], 0, rows)

    def test_the_curve_never_goes_negative(self):
        m = sessionmeta.measure(report([("A", "20:00:00", "20:03:00")]))
        self.assertTrue(all(v >= 0 for v in m["curve"]))


class TestStickiness(unittest.TestCase):
    def test_a_room_that_holds_to_the_end_scores_100(self):
        m = sessionmeta.measure(report([("A", "20:00:00", "20:20:00"),
                                        ("B", "20:00:00", "20:20:00")]))
        self.assertEqual(m["stick10"], 100.0)

    def test_a_room_that_empties_scores_low(self):
        rows = [("A", "20:00:00", "20:20:00")]
        rows += [(f"P{i}", "20:00:00", "20:02:00") for i in range(9)]
        m = sessionmeta.measure(report(rows))
        self.assertEqual(m["peak_computed"], 10)
        self.assertLess(m["stick10"], 20)

    def test_stickiness_is_None_when_there_is_no_peak_to_divide_by(self):
        m = sessionmeta.measure(report([]))
        self.assertIsNone(m["stick10"])
        self.assertIsNone(m["stick30"])


class TestPollMarkerPlacement(unittest.TestCase):
    """The arithmetic pipeline.py does to put the marker on the curve.

    Kept here because it is the join between two modules and neither owns it:
    polls.py supplies the timestamp, sessionmeta supplies t0 and the curve.
    """

    @staticmethod
    def place(poll_at, t0, curve):
        from datetime import datetime
        mins = int((datetime.fromisoformat(poll_at)
                    - datetime.fromisoformat(t0)).total_seconds() // 60)
        return mins if 0 <= mins < len(curve) else None

    def test_a_poll_answered_mid_session_lands_on_its_minute(self):
        m = sessionmeta.measure(report([("A", "20:00:00", "20:30:00")]))
        self.assertEqual(self.place("2026-08-30T20:12:30", m["t0"], m["curve"]), 12)

    def test_a_poll_answered_before_the_first_join_is_dropped(self):
        # Happens when an early bird answers a poll left open from a previous
        # session. A negative minute would be drawn off the left of the chart.
        m = sessionmeta.measure(report([("A", "20:00:00", "20:30:00")]))
        self.assertIsNone(self.place("2026-08-30T19:50:00", m["t0"], m["curve"]))

    def test_a_poll_answered_after_the_room_emptied_is_dropped(self):
        m = sessionmeta.measure(report([("A", "20:00:00", "20:30:00")]))
        self.assertIsNone(self.place("2026-08-30T21:30:00", m["t0"], m["curve"]))


class TestNameKey(unittest.TestCase):
    def test_the_filename_yields_the_same_shape_polls_uses(self):
        self.assertEqual(sessionmeta.name_key("attendee_91695866411_2026_08_30.csv"),
                         ("91695866411", "08_30"))
        self.assertEqual(sessionmeta.name_key("x/attendee_916_2026-08-05.csv"),
                         ("916", "08_05"))
        self.assertIsNone(sessionmeta.name_key("poll_916_2026_08_30.csv"))


class TestCollect(unittest.TestCase):
    def test_the_copy_with_the_most_viewers_wins(self):
        # A webinar sits on both Shared Drives and the copies differ by a row
        # or two -- the same tie-break the marker and the poll reader use.
        small = {"webinar_id": "916", "unique_viewers": 100}
        big = {"webinar_id": "916", "unique_viewers": 540}
        self.assertEqual(sessionmeta.collect(
            [("a.csv", small), ("b.csv", big)])["916"]["unique_viewers"], 540)
        self.assertEqual(sessionmeta.collect(
            [("b.csv", big), ("a.csv", small)])["916"]["unique_viewers"], 540)

    def test_headers_that_failed_to_parse_are_skipped(self):
        self.assertEqual(sessionmeta.collect([("a.csv", None)]), {})


if __name__ == "__main__":
    unittest.main()
