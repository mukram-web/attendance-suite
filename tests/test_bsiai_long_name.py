import unittest

import attendance_core as ac


class BsiaiLongNameTest(unittest.TestCase):
    """'Build Side Income Using AI Accelerator B43' is BSIAI's own room, not AI CAP B43."""

    def test_long_name_is_bsiai(self):
        self.assertEqual(ac.extract_batches("Build Side Income Using AI Accelerator B43"),
                         {("BSIAI", 43)})
        self.assertEqual(ac.extract_batches("Build Side Income Using AI B3-A"), {("BSIAI", 3)})

    def test_shared_room_keeps_cap(self):
        self.assertIn(("CAP", 41), ac.extract_batches(
            "AI CAP B41 - Common , Build Side Income Using AI Accelerator B41"))

    def test_topic_with_side_income_stays_cap(self):
        self.assertEqual(ac.extract_batches(
            "2026-10-04 - AI CAP B33, B34, 35, B36, B37, B38 - Build a Side Income Using AI"),
            {("CAP", n) for n in range(33, 39)})


if __name__ == "__main__":
    unittest.main()
