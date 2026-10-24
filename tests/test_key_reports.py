import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import key_reports as K


def cand(i, t, roles, pages=200, readable=True):
    return {"id": i, "type": t, "roles": set(roles), "pages": pages, "readable": readable, "label": i,
            "library_id": i, "entity": "unclear", "entity_name": ""}


class Waves(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(K.wave_of((2025, 12), 2026), "latest")      # calendar 2025, published 2026
        self.assertEqual(K.wave_of((2026, 3), 2026), "latest")       # April 2025 to March 2026
        self.assertEqual(K.wave_of((2025, 3), 2026), "previous")     # April 2024 to March 2025
        self.assertEqual(K.wave_of((2024, 12), 2026), "previous")
        self.assertIsNone(K.wave_of((2024, 6), 2026))
        self.assertIsNone(K.wave_of((2026, 9), 2026))

    def test_doc_end_uses_month_or_company_fye(self):
        self.assertEqual(K.doc_end({"period_end": "2025-03", "year": 2025}, 12), (2025, 3))
        self.assertEqual(K.doc_end({"period_end": "2025", "year": 2025}, 3), (2025, 3))
        self.assertEqual(K.doc_end({"year": 2025}, 12), (2025, 12))
        self.assertIsNone(K.doc_end({}, 12))
        self.assertEqual(K.company_fye([3, 3, 12, None]), 3)
        self.assertEqual(K.company_fye([None]), 12)


class Roles(unittest.TestCase):
    def test_types(self):
        self.assertEqual(K.roles({"page_count": 300}, "Integrated Report"), {"fin", "sust"})
        self.assertEqual(K.roles({"page_count": 300}, "Annual Report"), {"fin"})
        self.assertEqual(K.roles({"page_count": 80}, "Sustainability Report"), {"sust"})
        self.assertEqual(K.roles({"page_count": 12}, "Annual Report"), set())          # a summary, not the report
        self.assertEqual(K.roles({}, "Annual Report"), {"fin"})                          # pages unknown: counts
        self.assertEqual(K.roles({"page_count": 300, "scope": "Part of a report"}, "Annual Report"), set())
        self.assertEqual(K.roles({"page_count": 30}, "Financial Statement"), {"fin"})
        self.assertEqual(K.roles({"page_count": 30}, "Code of Conduct"), set())

    def test_embedded_annual_report_carries_both(self):
        f = {"page_count": 300, "sustainability_class": "E embedded",
             "class_reason": "a block of 40 sustainability pages"}
        self.assertEqual(K.roles(f, "Annual Report"), {"fin", "sust"})


class Choose(unittest.TestCase):
    def test_one_report_with_both_wins(self):
        best, key = K.choose([cand("ar", "Annual Report", ["fin"]), cand("sr", "Sustainability Report", ["sust"]),
                              cand("ir", "Integrated Report", ["fin", "sust"])])
        self.assertEqual(key, "both_one")
        self.assertEqual(best["fin"]["id"], "ir")

    def test_separate(self):
        best, key = K.choose([cand("ar", "Annual Report", ["fin"]), cand("sr", "Sustainability Report", ["sust"])])
        self.assertEqual(key, "both_sep")
        self.assertEqual((best["fin"]["id"], best["sust"]["id"]), ("ar", "sr"))

    def test_financial_statement_only_as_last_resort(self):
        best, key = K.choose([cand("fs", "Financial Statement", ["fin"], 30), cand("ar", "Annual Report", ["fin"])])
        self.assertEqual((key, best["fin"]["id"]), ("fin", "ar"))

    def test_none(self):
        self.assertEqual(K.choose([])[1], "none")


class Entity(unittest.TestCase):
    def test_subsidiaries_seen_in_the_wild(self):
        self.assertEqual(K.entity_check("BMW International Investment B.V. Annual Report 2024", "BMW")[0], "subsidiary")
        self.assertEqual(K.entity_check("AIRTEL MOBILE COMMERCE UGANDA LIMITED ANNUAL REPORT 2018", "Bharti Airtel")[0], "subsidiary")
        self.assertEqual(K.entity_check("Swiss Re Capital Markets Limited Annual Report 2019", "Swiss Re")[0], "subsidiary")

    def test_group_reports(self):
        self.assertEqual(K.entity_check("Annual Report 2024 Bayerische Motoren Werke AG", "BMW (Bayerische Motoren Werke)")[0], "group")
        self.assertEqual(K.entity_check("Mizuho Financial Group, Inc. Integrated Report 2025", "Mizuho")[0], "group")
        self.assertEqual(K.entity_check("HSBC Holdings plc Annual Report and Accounts 2024", "HSBC")[0], "group")
        self.assertEqual(K.entity_check("Swiss Re Ltd Financial Report 2024", "Swiss Re")[0], "group")

    def test_first_dry_run_cases(self):
        # 24 Oct 2026 dry run: document words before the name are not part of the entity
        self.assertEqual(K.entity_check("Annual Financial Statements 2025 ANNUAL FINANCIAL STATEMENTS PUMA SE, HERZOGENAURACH", "Puma")[0], "group")
        self.assertEqual(K.entity_check("FY25 Non-Financial Matters Report Logitech International S.A.", "Logitech International")[0], "group")
        # DNB Bank ASA is the parent; the title says it is the group report
        self.assertEqual(K.entity_check("DNB Bank ASA Annual report", "DNB", title="2025 | DNB | DNB Group Annual report 2025")[0], "group")
        self.assertEqual(K.entity_check("", "DNB", title="2025 | DNB | DNB Boligkreditt Annual Report 2025")[0], "subsidiary")
        self.assertEqual(K.entity_check("", "DNB", title="Responsible Investments Annual Report 2025 DNB Asset Management")[0], "subsidiary")
        self.assertEqual(K.entity_check("Roche Capital Market Ltd Financial Statements 2025", "Roche")[0], "subsidiary")
        self.assertEqual(K.entity_check("Taipei Fubon Commercial Bank Co., Ltd. and Subsidiaries", "Fubon Financial")[0], "subsidiary")
        self.assertEqual(K.entity_check("", "Telstra", title="Telstra Annual Report 2025")[0], "unclear")
        # second dry run: the company part of doc_label and document words are not subsidiaries
        for title, co in (("2025 | ENGIE | Annual financial report (ESEF) for fiscal year 2025", "ENGIE"),
                          ("2025 | Roche | Finance Report 2025", "Roche"),
                          ("2024 | UPM | UPM Financial Statement Release 2024", "UPM"),
                          ("2024 | UPM | UPM Green Finance Report 2024", "UPM"),
                          ("2026 | SPML Infra | Engineering India's Tomorrow ANNUAL REPORT 2025-26", "SPML Infra")):
            self.assertEqual(K.entity_check("", co, title=title)[0], "unclear", title)
        self.assertEqual(K.entity_check("", "Va Tech Wabag", title="2026 | Va Tech Wabag | VA TECH WABAG DEUTSCHLAND GMBH, GERMANY - Financial Position")[0], "subsidiary")

    def test_unclear_without_a_legal_name(self):
        self.assertEqual(K.entity_check("BMW Group Report 2024", "BMW")[0], "unclear")
        self.assertEqual(K.entity_check("", "BMW")[0], "unclear")


if __name__ == "__main__":
    unittest.main()
