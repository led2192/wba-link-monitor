import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

from monitor_core import BLOCKED_CODES


class BlockedCodes(unittest.TestCase):
    def test_firewall_codes_are_refusals_not_dead_pages(self):
        # CNPC answers 412 and Central Pattana 405 to robots while the pages open in a browser (Nov 2026)
        for code in (401, 403, 429, 405, 412, 418, 444, 567):
            self.assertIn(code, BLOCKED_CODES)

    def test_gone_and_server_errors_stay_dead(self):
        for code in (400, 404, 410, 500, 502, 503, 520):
            self.assertNotIn(code, BLOCKED_CODES)


if __name__ == "__main__":
    unittest.main()
