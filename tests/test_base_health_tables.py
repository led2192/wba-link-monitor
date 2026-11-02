"""Base health counts every Disclosure Library table. 2 Nov 2026: attribute_values was missing, so records_total
read 377,628 when Airtable showed 389,675, and the Source Library import's whole-base check undercounted too."""
import os, re, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import base_health  # noqa: E402


class Tables(unittest.TestCase):
    def test_every_documented_table_is_counted(self):
        text = open(os.path.join(ROOT, "docs", "fields.md"), encoding="utf-8").read()
        section = text.split("## Disclosure Library", 1)[1].split("\n## ", 1)[0]
        documented = set(re.findall(r"^### (\w+)", section, re.M))
        self.assertTrue(documented)
        self.assertEqual(documented - set(base_health.TABLES), set())


if __name__ == "__main__":
    unittest.main()
