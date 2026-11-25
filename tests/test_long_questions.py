"""long_questions.py: the counts of a comparison, offline."""
import unittest

import long_questions as L


class Compare(unittest.TestCase):
    def test_summary(self):
        c = L.summary([(4, 40, 9.7, 1.0, 3), (0, 12, 2.0, 2.0, 0), (30, 30, 1.0, 1.0, 30), (10, 0, 1.0, 1.0, 0), (50, 20, 3.0, 2.0, 20)])
        self.assertEqual((c["questions"], c["more pages with the option"], c["fewer"], c["the same number"]), (5, 2, 2, 1))
        self.assertEqual((c["none before, some with the option"], c["some before, none with the option"], c["the same pages"]), (1, 1, 1))
        self.assertEqual((c["median seconds off"], c["median seconds on"], c["median pages off"], c["median pages on"]), (2.0, 1.0, 10, 20))
        self.assertEqual(L.summary([])["median pages on"], 0.0)

    def test_the_share_of_pages_is_sent_as_the_real_the_function_takes(self):
        import inspect
        self.assertIn("refresh_word_freq(%s::real)", inspect.getsource(L.main))
        self.assertLess(inspect.getsource(L.main).index("set statement_timeout = '30min'"), inspect.getsource(L.main).index("refresh_word_freq(%s::real)"))          # 25 Nov 2026: a float alone is double precision, and no such function exists


if __name__ == "__main__":
    unittest.main()
