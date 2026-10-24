import datetime as dt
import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import pub_dates as P

D = dt.date


class Dates(unittest.TestCase):
    def test_publication_statements(self):
        end = D(2024, 12, 31)
        t = P.find_dates([(2, "Date of publication: 4 March 2025. Contact us at ir@example.com")], end, D(2025, 6, 1))
        self.assertEqual(t[:3], (2, D(2025, 3, 4), "day"))
        t = P.find_dates([(1, "This report was published in March 2025 and covers the year")], end, None)
        self.assertEqual(t[:3], (2, D(2025, 3, 1), "month"))
        t = P.find_dates([(9, "Published on April 2, 2025 by the Group")], end, None)
        self.assertEqual(t[:3], (2, D(2025, 4, 2), "day"))

    def test_board_approval(self):
        end = D(2025, 3, 31)
        t = P.find_dates([(140, "The financial statements were authorised for issue by the Board of Directors on 27 May 2025 and signed")], end, None)
        self.assertEqual(t[:3], (3, D(2025, 5, 27), "day"))
        t = P.find_dates([(80, "On 26 February 2025, the Board of Directors approved the annual report.")], D(2024, 12, 31), None)
        self.assertEqual(t[:3], (3, D(2025, 2, 26), "day"))

    def test_publication_beats_approval_and_implausible_dates_are_ignored(self):
        end = D(2024, 12, 31)
        pages = [(3, "approved by the Board on 20 February 2025."), (2, "Publication date: 10 March 2025")]
        self.assertEqual(P.find_dates(pages, end, None)[:2], (2, D(2025, 3, 10)))
        # a date inside the period, or after the robot saw the file, is not the publication
        self.assertIsNone(P.find_dates([(1, "Published on 1 June 2024")], end, None))
        self.assertIsNone(P.find_dates([(1, "Published on 1 June 2025")], end, D(2025, 4, 1)))

    def test_period_end(self):
        self.assertEqual(P.period_end_date("2025-03", 2025), D(2025, 3, 31))
        self.assertEqual(P.period_end_date("2024-12", 2024), D(2024, 12, 31))
        self.assertEqual(P.period_end_date("", 2024), D(2024, 1, 1))


class Decide(unittest.TestCase):
    def test_regulator_filing_first(self):
        f = {"Reviewer notes": "Added from the SEC regulator database (filed 2025-02-21). ", "period_end": "2024-12"}
        self.assertEqual(P.decide(f, [(1, "Published on 1 March 2025")], None)[:3], (1, D(2025, 2, 21), "day"))

    def test_first_seen_only_after_the_initial_collection(self):
        self.assertEqual(P.decide({"period_end": "2026-03"}, [], D(2026, 8, 10))[:2], (4, D(2026, 8, 10)))
        self.assertIsNone(P.decide({"period_end": "2025-12"}, [], D(2026, 3, 10)))      # initial collection
        self.assertIsNone(P.decide({"period_end": "2024-12"}, [], D(2026, 6, 20)))


if __name__ == "__main__":
    unittest.main()
