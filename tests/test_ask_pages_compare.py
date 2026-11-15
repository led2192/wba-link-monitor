import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ask_pages_compare as A


class Compare(unittest.TestCase):
    def test_the_years_of_the_pages_newest_first(self):
        rows = [("a", 1, 2022)] * 6 + [("b", 2, 2025), ("c", 3, None)]
        self.assertEqual(A.years(rows), "2025 x1, 2022 x6, no year x1")
        self.assertEqual(A.years([]), "none")

    def test_recent_pages_are_of_the_newest_main_report_or_the_year_before(self):
        rows = [("a", 1, 2022), ("b", 2, 2025), ("c", 3, 2026), ("d", 4, None)]
        self.assertEqual((A.recent(rows, 2026), A.recent(rows, None)), (2, 0))

    def test_with_ask_later_a_case_is_asked_about_the_year_two_before_the_newest_report(self):
        self.assertEqual((A.year_asked("ask_later", 2026), A.year_asked("ask_later", None), A.year_asked("ask_latest", 2026)), (2024, None, None))
        rows = [("a", 1, 2024), ("b", 2, 2025), ("c", 3, 2026), ("d", 4, None), ("e", 5, 2023)]
        self.assertEqual(A.counted("ask_later", rows, 2026), 2)             # 2025 and 2026: later than 2024
        self.assertEqual(A.counted("ask_latest", rows, 2026), A.recent(rows, 2026))
        src = open(A.__file__, encoding="utf-8").read()
        self.assertIn("(words, company, asked, asked, words)", src)

    def test_it_never_calls_the_model_and_prints_no_page_text(self):
        src = open(A.__file__, encoding="utf-8").read()
        self.assertNotIn("public.ask(", src); self.assertNotIn("excerpt", src)
        self.assertIn("ask_pages_compare.py", open(os.path.join(os.path.dirname(A.__file__), ".github", "workflows", "search_check.yml")).read())


if __name__ == "__main__":
    unittest.main()
