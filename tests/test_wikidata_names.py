"""wikidata_names.py: the match by name, offline."""
import unittest

import wikidata_names as N


class Names(unittest.TestCase):
    def test_host_key(self):
        self.assertEqual(N.host_key("https://www.Zebra.example.co.uk/about?x=1"), "example.co.uk")
        self.assertEqual(N.host_key("http://investors.zebra.com:8080/"), "zebra.com")
        self.assertEqual((N.host_key(""), N.host_key("localhost")), ("", ""))

    def test_the_name_searched(self):
        self.assertEqual(N.search_name("Zebra Motors AG"), "Zebra Motors")
        self.assertEqual(N.search_name("  Zebra   Motors "), "Zebra Motors")

    def test_pick_wants_the_country_and_a_website(self):
        facts = {"Q1": ({"FR"}, "https://a.example"), "Q2": ({"DE"}, None), "Q3": ({"DE", "US"}, "https://c.example")}
        self.assertEqual(N.pick(["Q1", "Q2", "Q3"], facts, "de"), ("Q3", "https://c.example"))          # the first of the country with a site, not the first hit
        self.assertEqual(N.pick(["Q1", "Q2"], facts, "DE"), (None, None)); self.assertEqual(N.pick(["Q3"], facts, None), (None, None))

    def test_facts(self):
        q = N.facts_query(["Q5", "x; DROP", "Q7"]); self.assertIn("wd:Q5 wd:Q7", q); self.assertNotIn("DROP", q)
        b = lambda c, iso=None, site=None: {k: {"value": v} for k, v in (("c", "http://www.wikidata.org/entity/" + c), ("iso", iso), ("site", site)) if v}
        got = N.facts_of({"results": {"bindings": [b("Q5", "de", "https://x.example"), b("Q5", "US"), b("Q7")]}})
        self.assertEqual(got, {"Q5": ({"DE", "US"}, "https://x.example"), "Q7": (set(), None)})


if __name__ == "__main__":
    unittest.main()
