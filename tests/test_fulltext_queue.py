"""Offline test: the full-text queue reads newest documents first, undated next, old years last."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_fulltext as FT  # noqa: E402


class Queue(unittest.TestCase):
    def test_newest_first_undated_next_old_last(self):
        docs = [{"id": "c", "year": 2019}, {"id": "a", "year": 2026}, {"id": "u", "year": ""},
                {"id": "b", "year": 2026}, {"id": "d", "year": 2021}, {"id": "e", "year": 2012}]
        self.assertEqual([d["id"] for d in sorted(docs, key=FT.queue_key)], ["a", "b", "d", "u", "c", "e"])


if __name__ == "__main__":
    unittest.main()
