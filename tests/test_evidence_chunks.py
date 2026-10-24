import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import evidence_chunks as E

LEX = E.Lexicon(E.load_terms())


class Tagging(unittest.TestCase):
    def test_climate_planning(self):
        t = ("We have committed to reach net zero across scope 1 and scope 2 by 2040, and our science-based "
             "targets were validated by the SBTi. The Board oversees our climate transition plan.")
        tg = LEX.tag(t)
        self.assertEqual(tg["dims"], ["climate"])
        self.assertEqual(tg["area"], "planning")

    def test_climate_performance_with_numbers(self):
        t = "Scope 1 and 2 greenhouse gas emissions decreased by 18% compared to the 2019 baseline, to 1.2 million tCO2e."
        tg = LEX.tag(t)
        self.assertIn("climate", tg["dims"])
        self.assertEqual(tg["area"], "performance")
        self.assertTrue(tg["number"])

    def test_social_and_nature(self):
        s = LEX.tag("Our human rights due diligence identified forced labour risks; we strengthened the grievance mechanism for workers.")
        self.assertEqual(s["dims"], ["social"])
        n = LEX.tag("We assessed biodiversity and deforestation risks at sites near protected areas using the TNFD LEAP approach.")
        self.assertEqual(n["dims"], ["nature"])

    def test_one_term_in_passing_is_not_evidence(self):
        self.assertIsNone(LEX.tag("Revenue grew 5% thanks to strong demand; see the sustainability section for our net zero ambition."))

    def test_capital_terms_match_capitals_only(self):
        self.assertIsNone(LEX.tag("The ghg of tcfd was a typo in this sentence about nothing."))

    def test_passages_and_profile(self):
        page = " ".join(["This is a sentence about scope 1 emissions and net zero."] * 40)
        ps = E.passages(page)
        self.assertGreater(len(ps), 1)
        self.assertTrue(all(len(p) <= E.MAX_CHARS for p in ps))
        lines, summ = E.tag_document(LEX, "lib1", [(3, page)])
        self.assertEqual(summ["passages"], len(lines))
        self.assertTrue(E.profile(summ).startswith("climate"))


if __name__ == "__main__":
    unittest.main()
