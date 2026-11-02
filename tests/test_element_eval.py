"""Offline tests for element_eval.py: the analysts' evidence is split into quotes with their document type and
pages, from the shapes the 2025 Indicators bases use."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import element_eval as E  # noqa: E402

EVIDENCE = ("The Sustainability Committee advises the Supervisory Board ... [Annual Report, p.171]\n"
            "------------------------------\n"
            "1.3 E1-1 Transition plan for climate change mitigation ... aligned activities. [Annual Report, p.92,93]\n"
            "------------------------------\n"
            "Impact materiality Starting with a long list based on the ESRS topics[.] The assessment ... [Annual Report, p.75]")


class Quotes(unittest.TestCase):
    def test_split_with_type_and_pages(self):
        q = E.quotes(EVIDENCE)
        self.assertEqual([(k, p) for _, k, p in q], [("Annual Report", [171]), ("Annual Report", [92, 93]), ("Annual Report", [75])])
        self.assertTrue(q[0][0].startswith("The Sustainability Committee advises"))
        self.assertNotIn("[.]", q[2][0]); self.assertNotIn("[Annual Report", q[2][0])

    def test_one_quote_without_separator_and_without_tag(self):
        q = E.quotes("Suppliers must comply with applicable laws.[.] Suppliers must not engage in forced labour. [Code of Conduct, p.3,7]")
        self.assertEqual([(k, p) for _, k, p in q], [("Code of Conduct", [3, 7])])
        self.assertEqual(E.quotes("No relevant disclosure was found for the element."),
                         [("No relevant disclosure was found for the element.", "", [])])
        self.assertEqual(E.quotes(""), [])

    def test_ids_and_shares(self):
        self.assertEqual(E.element_of("PT_00201-GEC_00527.2"), ("PT_00201", "GEC_00527.2"))
        self.assertEqual(E.element_of("x"), (None, None))
        self.assertEqual(E.name_of({"name": "Met"}), "Met"); self.assertEqual(E.name_of([{"name": "Social"}]), "Social")
        import collections
        self.assertEqual(E.shares(collections.Counter({"Sustainability Report": 3, "Annual Report": 1})),
                         "Sustainability Report 75%, Annual Report 25%")

    def test_summary_groups(self):
        rows = [{"benchmark": "Food", "score": "Met", "quotes": [1, 2], "sources": [1], "source_types": ["Sustainability Report"]},
                {"benchmark": "Food", "score": "Unmet", "quotes": [1], "sources": [1, 2], "source_types": ["Annual Report", "Webpage"]}]
        s = E.summarize(rows, "benchmark")["Food"]
        self.assertEqual((s["elements"], s["score Met"], s["quotes"], s["sources"], s["type Webpage"]), (2, 1, 3, 3, 1))


class Looser(unittest.TestCase):
    def test_same_kind_held(self):
        typed = {("PT_1", "SUST"): {2024}, ("PT_1", "Code of Conduct"): {0, 2023}}
        self.assertTrue(E.same_kind_held({"type": "ESG Report", "year": "2024"}, "PT_1", typed))        # same family
        self.assertTrue(E.same_kind_held({"type": "Sustainability Report", "year": "2023"}, "PT_1", typed))  # the next year's
        self.assertFalse(E.same_kind_held({"type": "Sustainability Report", "year": "2021"}, "PT_1", typed))
        self.assertTrue(E.same_kind_held({"type": "Code of Conduct", "year": "2023"}, "PT_1", typed))
        self.assertFalse(E.same_kind_held({"type": "Human Rights Policy", "year": "2023"}, "PT_1", typed))


if __name__ == "__main__":
    unittest.main()
