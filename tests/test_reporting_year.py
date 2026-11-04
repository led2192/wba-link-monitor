import unittest

import reporting_year as R


class ReportingYear(unittest.TestCase):
    def test_the_guidance_table(self):
        # SDG2000 FY2024 data collection guidance, August 2025: fiscal year 2024 corresponds to reports ending...
        for end in ("2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30", "2025-05-31", "2025-06-29",
                    "2024-06-30", "2024-07-31", "2024-08-31", "2024-09-30", "2024-10-31", "2024-11-30", "2024-12-31"):
            self.assertEqual(R.from_end_date(end), 2024, end)

    def test_month_only(self):
        self.assertEqual(R.from_end_month(2025, 3), 2024)
        self.assertEqual(R.from_end_month(2024, 6), 2024)       # Telstra, Diageo, Coty, FirstRand
        self.assertEqual(R.from_end_month(2024, 12), 2024)
        self.assertEqual(R.from_end_month(2024, None), 2024)
        self.assertTrue(R.shifted(5)); self.assertFalse(R.shifted(6)); self.assertFalse(R.shifted(None))


if __name__ == "__main__":
    unittest.main()
