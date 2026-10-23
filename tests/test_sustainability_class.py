"""Offline tests for sustainability_class.py: the class, the frameworks and the assurance read from text.

Run:  python3 -m unittest tests/test_sustainability_class.py -v
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sustainability_class as SC  # noqa: E402

D, E, S, F = SC.D, SC.E, SC.S, SC.F
SUS = ("Our Scope 1 and Scope 2 GHG emissions fell 12% against the 2019 baseline as renewable energy rose to 61% of "
       "consumption. The double materiality assessment confirmed climate change, biodiversity, human rights in the supply "
       "chain and health and safety as material topics; the Board's sustainability committee reviews the net zero targets, "
       "validated by the SBTi, twice a year. Water withdrawal in water-stressed sites and hazardous waste are reported per "
       "ESRS E3 and E5, with GRI 303 and GRI 306 cross-references in the content index. " * 3)
FIN = ("Revenue for the year rose 4.2% to EUR 1,204 million, with operating profit of EUR 188 million. Net debt was "
       "EUR 630 million and the leverage ratio 1.9x. The Board proposes a dividend of EUR 0.45 per share. Trade "
       "receivables are measured at amortised cost; impairment follows the expected credit loss model of IFRS 9. " * 4)
RISK = ("Risk factors. Our business may be affected by climate change regulation, by changes in environmental laws, "
        "by litigation, by cyber attacks, by foreign exchange rates, by interest rates, by pandemics, by the loss of key "
        "personnel, by supply chain disruption, by tax changes and by competition. " * 4)
ACEA_COVER = ("CONSOLIDATED FINANCIAL STATEMENTS ACEA GROUP 2025 Letter to Shareholders 4 1. REPORT ON OPERATIONS 7 "
              "Table of contents SUSTAINABILITY REPORT 74 1. General information 76 2. Environmental information 99 "
              "3. CONSOLIDATED FINANCIAL STATEMENTS 256")


def doc(*blocks):
    return [(i + 1, t) for i, t in enumerate(blocks)]


class Classes(unittest.TestCase):
    def test_by_type(self):
        self.assertEqual(SC.decide("Sustainability Report", doc(SUS, SUS))["class"], D)
        self.assertEqual(SC.decide("Integrated Report", doc(FIN, FIN))["class"], E)
        self.assertEqual(SC.decide("Code of Conduct", None, "Our code")["class"], S)
        self.assertEqual(SC.decide("ESG Report", None, "")["reason"], "ESG Report: a dedicated sustainability report; no text yet")

    def test_annual_report_with_section_is_embedded(self):
        r = SC.decide("Annual Report", doc(FIN, FIN, SUS, SUS, SUS, SUS, SUS, FIN, FIN, FIN))
        self.assertEqual(r["class"], E)
        self.assertIn("block of 5", r["reason"])
        self.assertEqual(r["share"], 50)

    def test_annual_report_without_section_is_financial(self):
        r = SC.decide("Annual Report", doc(FIN, RISK, FIN, FIN, SUS, FIN, FIN, RISK, FIN, FIN, FIN, FIN))
        self.assertEqual(r["class"], F)
        self.assertIn("1 of 12 pages", r["reason"])

    def test_risk_factor_pages_are_not_sustainability_pages(self):
        self.assertEqual(SC.page_flags([(1, RISK)]), [False])
        self.assertEqual(SC.page_flags([(1, SUS), (2, "short")]), [True, None])

    def test_contents_entry_with_some_pages(self):
        r = SC.decide("Annual Report", doc(ACEA_COVER, FIN, SUS, FIN, SUS, FIN, FIN, FIN, FIN, FIN, FIN, FIN))
        self.assertEqual(r["class"], E)
        self.assertIn("sustainability report 74", r["reason"])

    def test_first_pages_only(self):
        r = SC.decide("Annual Report", None, ACEA_COVER)
        self.assertEqual(r["class"], E)
        self.assertTrue(r["reason"].startswith("first pages:"))
        r = SC.decide("Annual Report", None, FIN)
        self.assertIsNone(r["class"])
        self.assertTrue(r["reason"].startswith("first pages:"))
        self.assertEqual(SC.decide("Annual Report", None, "")["reason"], "no text yet")

    def test_types_never_read_in_full_are_decided_from_first_pages(self):
        r = SC.decide("Interim Reports", None, FIN)
        self.assertEqual(r["class"], F)
        self.assertIn("not read in full", r["reason"])
        self.assertEqual(SC.decide("Company Presentations", None, "Contents ... ESG highlights 12 ...")["class"], E)
        # an annual report still waits for its full text
        self.assertIsNone(SC.decide("10K Form", None, FIN)["class"])

    def test_no_text_at_all(self):
        self.assertEqual(SC.decide("ESG Report", None, "")["class"], D)
        self.assertEqual(SC.decide("Integrated Report", None, "")["class"], E)
        r = SC.decide("Annual Report", None, "")
        self.assertIsNone(r["class"]); self.assertEqual(r["reason"], "no text yet")

    def test_other_languages(self):
        es = ("Las emisiones de gases de efecto invernadero de alcance 1 y 2 bajaron un 10%. La biodiversidad, los derechos "
              "humanos y la seguridad y salud son asuntos materiales del análisis de materialidad; la economía circular y "
              "las energías renovables guían el plan de sostenibilidad. " * 4)
        ja = "当社のサステナビリティ方針。温室効果ガスの排出削減、気候変動への対応、生物多様性の保全、人権の尊重、ダイバーシティの推進を重要課題（マテリアリティ）とする。" * 6
        self.assertEqual(SC.page_flags([(1, es), (2, ja)]), [True, True])
        self.assertEqual(SC.decide("Annual Report", None, "Índice ... Estado de información no financiera 120")["class"], E)

    def test_year_after_heading_is_not_a_page_number(self):
        self.assertIsNone(SC.section_named("see our Sustainability Report 2024 on the website"))
        self.assertEqual(SC.section_named("Contents Sustainability review 46 Financial statements 90"), "Sustainability review 46")


class Frameworks(unittest.TestCase):
    def test_named_twice(self):
        t = SC.norm("Prepared in accordance with the GRI Standards; see GRI 2-1. TCFD recommendations; the TCFD index. "
                    "ESRS E1 climate change, ESRS 2. SASB once. Sustainable Development Goals 13 and the SDGs.")
        self.assertEqual(SC.frameworks(t), ["ESRS", "GRI", "TCFD", "SDGs"])
        self.assertIn("SASB", SC.frameworks(t, 1))

    def test_cdp_needs_context(self):
        self.assertEqual(SC.frameworks(SC.norm("Cassa Depositi e Prestiti (CDP) and CDP Reti"), 1), [])
        self.assertEqual(SC.frameworks(SC.norm("CDP climate change score A-"), 1), ["CDP"])


class Assurance(unittest.TestCase):
    def test_limited(self):
        t = SC.norm("We obtained limited assurance over the sustainability statement under ISAE 3000. The procedures are less "
                    "in extent than for a reasonable assurance engagement.")
        self.assertEqual(SC.assurance(t), "limited")

    def test_financial_audit_is_not_sustainability_assurance(self):
        t = SC.norm("Our objectives are to obtain reasonable assurance about whether the financial statements as a whole are "
                    "free from material misstatement. The sustainability report is other information.")
        self.assertEqual(SC.assurance(t), "none found")

    def test_reasonable_on_ghg(self):
        t = SC.norm("Scope 1 and 2 greenhouse gas emissions were verified by BSI under ISO 14064-3, where a reasonable "
                    "assurance opinion was provided.")
        self.assertEqual(SC.assurance(t), "reasonable")

    def test_level_not_stated(self):
        self.assertEqual(SC.assurance(SC.norm("Independent assurance statement to the directors of X plc")), "assured, level not stated")
        self.assertEqual(SC.assurance(SC.norm("Internal controls provide reasonable assurance that operations are efficient.")), "none found")


if __name__ == "__main__":
    unittest.main()
