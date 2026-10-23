import unittest

import discover_pages as dp


class ClearWinner(unittest.TestCase):
    def test_single_candidate_with_documents(self):
        c = [("reports_hub", "https://a/reports", "why", 14)]
        self.assertEqual(dp.clear_winner(c)[1], "https://a/reports")

    def test_too_few_documents_waits_for_a_person(self):
        self.assertIsNone(dp.clear_winner([("reports_hub", "https://a/r", "why", 2)]))

    def test_close_second_is_ambiguous(self):
        c = [("reports_hub", "https://a/r1", "w", 10), ("reports_hub", "https://a/r2", "w", 7)]
        self.assertIsNone(dp.clear_winner(c))

    def test_twice_the_next_wins(self):
        c = [("reports_hub", "https://a/r2", "w", 5), ("reports_hub", "https://a/r1", "w", 12)]
        self.assertEqual(dp.clear_winner(c)[1], "https://a/r1")

    def test_plan_splits_by_type(self):
        found = [("reports_hub", "https://a/r", "w", 9), ("sustainability_page", "https://a/s1", "w", 4),
                 ("sustainability_page", "https://a/s2", "w", 4)]
        auto, amb = dp.plan(found, ["reports_hub", "sustainability_page"])
        self.assertEqual([x[1] for x in auto], ["https://a/r"])
        self.assertEqual(len(amb), 2)


class Gone(unittest.TestCase):
    def test_is_gone_only_for_monitored_report_pages_with_404_or_410(self):
        self.assertTrue(dp.is_gone({"monitor": True, "status": "dead", "http_status": 404, "type": "reports_hub"}))
        self.assertTrue(dp.is_gone({"monitor": True, "status": "dead", "http_status": 410.0, "type": "policies"}))
        self.assertFalse(dp.is_gone({"monitor": True, "status": "dead", "http_status": 403, "type": "reports_hub"}))
        self.assertFalse(dp.is_gone({"monitor": False, "status": "dead", "http_status": 404, "type": "reports_hub"}))
        self.assertFalse(dp.is_gone({"monitor": True, "status": "dead", "http_status": 404, "type": "news"}))

    def test_notes(self):
        self.assertEqual(dp.gone_note("2026-10-24", 404.0, replacement="https://a/new"),
                         "2026-10-24: gone (404); replaced by https://a/new")
        self.assertIn("another page of this type", dp.gone_note("2026-10-24", 410, other_live=True))
        self.assertIn("no replacement found", dp.gone_note("2026-10-24", 404))


if __name__ == "__main__":
    unittest.main()
