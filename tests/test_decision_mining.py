"""Offline tests for decision_mining.py: the line and column of a figure, the competing lines, the derivation
tests and their chance check. No network, no database."""
import os
import sys
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import decision_mining as D  # noqa: E402

PAGE = """Workforce 2024 2023
Average number of employees 95,154 93,100
Employees at year end 94,236 92,300
Employees of the parent company 12,040 11,900
Revenue 26,100 25,300"""


class Lines(unittest.TestCase):
    def test_the_line_and_column_of_a_figure(self):
        got = D.lines_with(PAGE, 94236, 0)
        self.assertEqual(len(got), 1)
        self.assertIn("year end", got[0][0])
        self.assertEqual(got[0][1], 1)                        # years in the header are not columns
        self.assertEqual(D.lines_with(PAGE, 92300, 0)[0][1], 2)

    def test_competing_lines_name_the_attribute_and_have_a_figure_of_the_same_size(self):
        chosen = {"Average number of employees 95,154 93,100"}
        comp = D.competitors(PAGE, chosen, ["employees"], 95154)
        self.assertEqual(len(comp), 2)                        # year end and parent; not revenue, not the header
        self.assertNotIn("Revenue 26,100 25,300", comp)

    def test_line_words_drop_stop_words(self):
        self.assertEqual(D.line_words("Average number of employees 95,154"), {"average", "number"})


class Derivations(unittest.TestCase):
    LINES = [[120.0, 80.0], [50.0, 400.0], [333.0]]

    def test_sum_average_close(self):
        self.assertIn("sum of two figures", D.derivations(200.0, self.LINES, False))      # 120 + 80
        self.assertIn("average of two on one line", D.derivations(100.0, self.LINES, False))
        self.assertIn("close (within 1%)", D.derivations(335.0, self.LINES, False))
        self.assertEqual(D.derivations(9999.0, self.LINES, False), set())

    def test_a_share_for_percentages(self):
        self.assertIn("one figure as a share of another", D.derivations(12.5, self.LINES, True))  # 50 / 400

    def test_percent_units(self):
        self.assertTrue(D.is_percent("%"))
        self.assertTrue(D.is_percent("Percent"))
        self.assertFalse(D.is_percent("EUR"))


if __name__ == "__main__":
    unittest.main()
