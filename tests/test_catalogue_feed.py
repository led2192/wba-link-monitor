"""catalogue_feed.py: when the night's readings let companies in, and why not, offline."""
import unittest

import catalogue_feed as F


class Decide(unittest.TestCase):
    def test_room_and_digestion(self):
        self.assertEqual(F.decide(120.0, 0.84, 40, 31, 250), (True, None))
        self.assertEqual(F.decide(120.0, 0.84, 0, 0, 0)[1], "no wanted candidate is left to add")
        self.assertIn("ceiling", F.decide(F.MAX_DB_GB, 0.84, 0, 0, 5)[1]); self.assertFalse(F.decide(None, 0.84, 0, 0, 5)[0])
        self.assertIn("could not be read", F.decide(100.0, None, 0, 0, 5)[1])          # the base unknown: nothing is added
        self.assertIn("full", F.decide(100.0, F.MAX_BASE_FULL, 0, 0, 5)[1])
        self.assertIn("not keeping up", F.decide(100.0, 0.5, 20, 9, 5)[1])          # fewer than half of twenty old ones have a document
        self.assertTrue(F.decide(100.0, 0.5, 19, 0, 5)[0]); self.assertTrue(F.decide(100.0, 0.5, 20, 10, 5)[0])          # too few to judge; exactly half


if __name__ == "__main__":
    unittest.main()
