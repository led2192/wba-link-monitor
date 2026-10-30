"""Offline tests for vocab_mining.py: page terms and the weighting of terms against ordinary pages."""
import collections, os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vocab_mining as V  # noqa: E402


class Vocab(unittest.TestCase):
    def test_terms(self):
        t = V.terms("Scope 2 location based emissions (tCO2e) of the group")
        for x in ("scope", "2", "scope 2", "location based", "tco2e"):
            self.assertIn(x, t)
        self.assertNotIn("of", t); self.assertNotIn("of the", t)

    def test_weigh_keeps_frequent_distinctive_terms(self):
        pos = collections.Counter({"location based": 40, "scope 2": 45, "annual": 50, "rare": 2})
        neg = collections.Counter({"location based": 5, "scope 2": 20, "annual": 480})
        comp = collections.defaultdict(set, {k: set(range(10)) for k in pos})
        got = dict(V.weigh(pos, neg, 50, 500, comp))
        self.assertIn("location based", got); self.assertIn("scope 2", got)
        self.assertNotIn("annual", got)                 # everywhere: no weight
        self.assertNotIn("rare", got)                   # too rare to trust
        self.assertGreater(got["location based"], got["scope 2"])


if __name__ == "__main__":
    unittest.main()
