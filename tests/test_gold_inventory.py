import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gold_inventory as G  # noqa: E402
from gold_eval import gold_id  # noqa: E402


class Kind(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(G.kind("513727000000 ARS"), "number")
        self.assertEqual(G.kind("1234"), "number")
        self.assertEqual(G.kind("12.5 %"), "percent")
        self.assertEqual(G.kind("Yes"), "yes/no")
        self.assertEqual(G.kind("The board oversees climate risk"), "text")


class Tally(unittest.TestCase):
    def test_counts_answers_companies_and_kinds(self):
        rows = [{"fields": {"Metadata ID": "PT_00001-GMC_00064.1-2024", "Selected Value": "12 EUR"}},
                {"fields": {"Metadata ID": "PT_00001-GMC_00064.1-2023", "Selected Value": "11 EUR"}},
                {"fields": {"Metadata ID": "PT_00002-GMC_00064.1-2024", "Selected Value": "Yes"}},
                {"fields": {"Metadata ID": "not an id", "Selected Value": "1"}}]
        t = G.tally(rows, gold_id)
        self.assertEqual(list(t), ["GMC_00064.1"])
        c = t["GMC_00064.1"]
        self.assertEqual((c["answers"], c["companies"], c["number"], c["yes/no"]), (3, 2, 2, 1))


if __name__ == "__main__":
    unittest.main()
