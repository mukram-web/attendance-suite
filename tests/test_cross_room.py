"""Cross-room attendance: two rooms on one date sharing their attendees.

Techies are allowed to sit in the Common session and vice versa (owner's rule,
2026-09-22). Scored per-file, a student who does that is dropped from the room
they attended - they are not its POD - and never credited to their own, so they
vanish from the numbers entirely. Measured on B41's 19 Sep: 550 non-Techies sat
in the Techies room and the Common room published 21.0% when 39.3% of its
students had attended something that day.

`attendance_core.CROSS_ROOM_BATCHES` names the batches whose rooms are matched
against the UNION of a date's attendees. The per-row POD filter is unchanged, so
every student still lands in their OWN room against a POD-sized denominator.

Note both exports are always non-empty here: an export that parses to nobody is
SKIPPED outright (the zero-attendee guard, see test_attendee_format) before the
union is ever built, so a room with no export of its own cannot be rescued.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import attendance_core as ac  # noqa: E402

TECH = ("tech@x.com", "919000000001")      # POD Prefrence: Techies
FIN = ("fin@x.com", "919000000002")        # POD Prefrence: Finance
OPS = ("ops@x.com", "919000000003")        # POD Prefrence: Ops/Supply Chain


def report_csv(rows):
    """A minimal Zoom Attendee Report. `rows` is [(email, phone)]."""
    out = ["Attendee Report", "", "Topic,Webinar ID,Actual Start Time,Unique Viewers",
           "Some Session,91695866411,09/19/2026 10:50,2", "", "Attendee Details",
           "Attended,First Name,Last Name,Email,Phone,Join Time,Leave Time,"
           "Time in Session (minutes)"]
    for em, ph in rows:
        out.append(f"Yes,A,B,{em},{ph},09/19/2026 10:52,09/19/2026 12:31,99")
    return "\n".join(out).encode()


class CrossRoomBase(unittest.TestCase):
    BATCH = "AI CAP B41"          # in CROSS_ROOM_BATCHES
    TECH_WID = "96086086337"
    COMMON_WID = "91351810967"

    def _roster(self, batch=None):
        from openpyxl import Workbook
        wb = Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet(batch or self.BATCH)
        ws.append(["Country", "Registered Number", "Registered Mail", "WhatsApp",
                   "Broadcast", "Batch", "Amount", "Payment", "Close Type",
                   "POD Prefrence"])
        for (em, ph), pod in ((TECH, "Techies"), (FIN, "Finance"),
                              (OPS, "Ops/Supply Chain")):
            ws.append([91, ph, em, "", "", "B41", 0, "Full Paid", "BDA Closing", pod])
        buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

    def _l2(self, batch=None):
        from openpyxl import Workbook
        b = batch or self.BATCH
        wb = Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet("Sep 2026")
        ws.append(["Date", "Webinar ID", "Batch Name", "Topic"])
        ws.append(["09/19/2026", self.TECH_WID, f"{b} - Techies", "Session"])
        ws.append(["09/19/2026", self.COMMON_WID, f"{b} - Common", "Session"])
        buf = io.BytesIO(); wb.save(buf); return buf.getvalue()

    def _run(self, tech_rows, common_rows, batch=None, roster=None):
        b = batch or self.BATCH
        files = [
            (f"2026-09-19 - {b} - Techies/attendee_{self.TECH_WID}_2026_09_19.csv",
             report_csv(tech_rows)),
            (f"2026-09-19 - {b} - Common/attendee_{self.COMMON_WID}_2026_09_19.csv",
             report_csv(common_rows)),
        ]
        _, report, warns = ac.process_files(roster or self._roster(b), self._l2(b),
                                            files, values_only=True)
        return {(r["pod"] or "WHOLE"): r for r in report}, warns


class TestCrossRoomUnion(CrossRoomBase):
    def test_a_techie_who_sat_in_common_is_still_present_for_techies(self):
        # The Techie sits in the COMMON room. Without the union they are dropped
        # there (not its POD) and absent from Techies - they vanish entirely.
        by, _ = self._run(tech_rows=[FIN], common_rows=[TECH, OPS])
        self.assertEqual(by["Techies"]["present"], 1, "credited back to Techies")
        self.assertEqual(by["Techies"]["total"], 1, "denominator stays POD-sized")
        self.assertEqual(by["Techies"]["crossed"], 1, "and REPORTED as crossed")

    def test_a_non_techie_who_sat_in_techies_is_still_present(self):
        # The B41 19 Sep case in miniature: Finance sits in the Techies room.
        # The complement column has no POD filter at THIS layer - it marks the
        # whole batch, and data.build_batch narrows it to everyone the day's POD
        # rooms did not invite. So 3 here becomes Finance+Ops on the dashboard.
        by, _ = self._run(tech_rows=[TECH, FIN], common_rows=[OPS])
        self.assertEqual(by["WHOLE"]["present"], 2,
                         "Finance (crossed in) + Ops (own export). NOT the "
                         "Techie: their POD ran its own room, so the union must "
                         "not also mark them here - see the complement note in "
                         "process_files")
        self.assertEqual(by["WHOLE"]["crossed"], 1,
                         "Finance sat in the other room and is reported as such")
        self.assertEqual(by["Techies"]["present"], 1, "Finance is NOT a Techie")
        self.assertEqual(by["Techies"]["total"], 1, "POD-sized denominator")

    def test_someone_in_their_own_room_is_not_reported_as_crossed(self):
        # Everybody in the room they belong to: nothing crossed anywhere.
        by, _ = self._run(tech_rows=[TECH], common_rows=[FIN, OPS])
        self.assertEqual(by["Techies"]["present"], 1)
        self.assertEqual(by["Techies"]["crossed"], 0)
        self.assertEqual(by["WHOLE"]["present"], 2, "Finance + Ops, not the Techie")
        self.assertEqual(by["WHOLE"]["crossed"], 0)

    def test_a_techie_marked_on_the_complement_column_is_not_counted_as_crossed(self):
        # The complement column marks the WHOLE batch; data.py narrows it to
        # everyone the day's POD rooms did not invite. A Techie on that column
        # is not a member of the Common room, so they must not inflate `crossed`.
        by, _ = self._run(tech_rows=[TECH], common_rows=[FIN, OPS])
        self.assertEqual(by["WHOLE"]["crossed"], 0)

    def test_the_union_does_not_mark_a_pod_member_on_the_complement(self):
        """The signal `data.build_batch` uses to RECOGNISE a complement room is
        that the day's PODs are absent from it (a 10% leak floor). If the union
        marked every Techie here, that signal reads 100% and the room silently
        reverts to a whole-batch denominator - which is exactly what happened to
        B41 (2,231 -> 2,714) before this rule existed."""
        by, _ = self._run(tech_rows=[TECH, FIN], common_rows=[OPS])
        self.assertEqual(by["Techies"]["present"], 1, "still counted in THEIR room")
        self.assertEqual(by["WHOLE"]["present"], 2, "but not a second time here")

    def test_a_pod_member_who_really_sat_in_the_common_room_is_still_marked(self):
        """Only a UNION-only hit is refused. A Techie named in the Common room's
        own export genuinely turned up, so they are marked exactly as before."""
        by, _ = self._run(tech_rows=[FIN], common_rows=[TECH, OPS])
        self.assertEqual(by["WHOLE"]["present"], 3,
                         "Techie was in this room's own export, plus Ops, plus "
                         "Finance crossing in from the Techies room")

    def test_nobody_is_counted_twice(self):
        # Attending BOTH rooms is one attendance, in the student's own room.
        by, _ = self._run(tech_rows=[TECH, FIN], common_rows=[TECH, FIN, OPS])
        self.assertEqual(by["Techies"]["present"], 1)
        self.assertEqual(by["Techies"]["crossed"], 0, "their own room named them")


class TestOutsideStillDetectsAMislabelledSession(CrossRoomBase):
    """`outside` must keep measuring the room's OWN export. Under the union every
    other room's attendees would land in it and fire the mislabel warning on a
    perfectly clean session - which it did, on B41's 20 Sep, until fixed."""

    def test_a_clean_pod_room_does_not_warn(self):
        by, warns = self._run(tech_rows=[TECH], common_rows=[FIN, OPS])
        self.assertEqual(by["Techies"]["outside"], 0)
        self.assertFalse([w for w in warns if "OUTSIDE that POD" in w],
                         f"clean session must not warn, got {warns}")

    def test_a_pod_room_the_whole_batch_attended_still_warns(self):
        # 30 non-Techies in the Techies room's OWN export: a mislabelled session.
        from openpyxl import Workbook
        wb = Workbook(); wb.remove(wb.active)
        ws = wb.create_sheet(self.BATCH)
        ws.append(["Country", "Registered Number", "Registered Mail", "WhatsApp",
                   "Broadcast", "Batch", "Amount", "Payment", "Close Type",
                   "POD Prefrence"])
        rows = []
        for i in range(30):
            em, ph = f"f{i}@x.com", f"9190000{i:05d}"
            ws.append([91, ph, em, "", "", "B41", 0, "Full Paid", "BDA Closing",
                       "Finance"])
            rows.append((em, ph))
        ws.append([91, TECH[1], TECH[0], "", "", "B41", 0, "Full Paid",
                   "BDA Closing", "Techies"])
        buf = io.BytesIO(); wb.save(buf)
        by, warns = self._run(tech_rows=rows, common_rows=[TECH],
                              roster=buf.getvalue())
        self.assertTrue([w for w in warns if "OUTSIDE that POD" in w],
                        f"a whole-batch session labelled with a domain must warn: {warns}")


class TestGating(CrossRoomBase):
    """The union is deliberately NOT global - it would restate published numbers
    for the 148 one-or-two-student cases in B35-B40."""

    def test_b41_is_in_the_allowlist(self):
        self.assertIn(("CAP", 41), ac.CROSS_ROOM_BATCHES)

    def test_a_batch_outside_the_allowlist_keeps_the_old_behaviour(self):
        by, _ = self._run(tech_rows=[FIN], common_rows=[TECH, OPS],
                          batch="AI CAP B39")
        self.assertEqual(by["Techies"]["present"], 0,
                         "B39 is not allowlisted, so the Techie is not credited")
        self.assertEqual(by["Techies"]["crossed"], 0)


if __name__ == "__main__":
    unittest.main()
