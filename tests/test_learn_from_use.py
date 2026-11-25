"""learn_from_use.py: the sentence of a run, offline."""
import unittest

import learn_from_use as L


class Line(unittest.TestCase):
    def test_counts_only(self):
        self.assertEqual(L.line({"searches": 1200, "documents": 1500, "up": 40, "down": 3, "written": True}),
                         "1,200 searches with a mark or an opening, 1,500 documents among them: 40 would move up, 3 down. Written.")
        self.assertTrue(L.line({}).endswith("Dry run: nothing written."))


if __name__ == "__main__":
    unittest.main()
