import os
import sys
import types
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
os.environ.setdefault("AIRTABLE_TABLE", "monitored_links")
# the browser library is not needed to test the timing rules
if "playwright" not in sys.modules:
    pw = types.ModuleType("playwright"); sync = types.ModuleType("playwright.sync_api")
    sync.sync_playwright = lambda: None
    sys.modules["playwright"] = pw; sys.modules["playwright.sync_api"] = sync

import playwright_monitor as P


class ChunkWait(unittest.TestCase):
    def test_bounded_even_far_from_the_budget(self):
        # one hanging page used to keep every run alive to GitHub's job limit (Oct 2026)
        self.assertEqual(P.chunk_wait(0, 180 * 60, now=60), P.CHUNK_MAX_S)

    def test_near_the_budget_waits_only_the_grace(self):
        self.assertEqual(P.chunk_wait(0, 180 * 60, now=180 * 60 + 10 * 60), 5 * 60)

    def test_never_less_than_one_navigation(self):
        floor = P.NAV_TIMEOUT_MS / 1000 + P.SETTLE_MS / 1000 + 30
        self.assertEqual(P.chunk_wait(0, 180 * 60, now=400 * 60), floor)


if __name__ == "__main__":
    unittest.main()
