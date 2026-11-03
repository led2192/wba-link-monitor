"""Offline tests for entity_audit.py: the company's names as patterns (3 Nov 2026: documents of other
entities filed under JSW Steel, COFCO, the OBB group and MN)."""
import os, re, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import entity_audit as E  # noqa: E402


def matches(ws, text, ci=True):
    # Postgres \\m \\M are word starts and ends; Python's \\b stands in for both here
    p = E.pattern(ws).replace(r"\m", r"\b").replace(r"\M", r"\b")
    return bool(re.search(p, text, re.I if ci else 0))


class Names(unittest.TestCase):
    def test_names_with_and_without_legal_words(self):
        ns = E.names_of("JSW Steel", "JSW Steel Limited")
        self.assertIn(("JSW", "Steel", "Limited"), ns); self.assertIn(("JSW", "Steel"), ns)

    def test_a_subsidiary_cover_does_not_name_the_parent(self):
        ws = ("JSW", "Steel")
        self.assertTrue(matches(ws, "JSW STEEL LIMITED Integrated Report 2024-25"))
        self.assertTrue(matches(ws, "JSW-Steel annual report"))
        self.assertFalse(matches(ws, "JSW Infrastructure Limited Integrated Report 2024-25"))
        self.assertFalse(matches(("COFCO", "International"), "COFCO Corporation annual report"))

    def test_short_names_are_matched_on_capitals(self):
        self.assertTrue(E.master())            # the master list is in the repository
        self.assertFalse(matches(("MN",), "State of Minnesota comprehensive report, mn.gov", ci=False))
        self.assertTrue(matches(("MN",), "MN Jaarverslag 2024", ci=False))


if __name__ == "__main__":
    unittest.main()
