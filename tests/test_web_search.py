"""Offline tests for web_search.py: the missing years, the questions asked, which results are passed on,
and the budget. No network: Serper is a stub."""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_search as W  # noqa: E402
from monitor_core import reg_domain  # noqa: E402

TODAY = date(2026, 11, 1)
YEARS = W.recent_years(TODAY)


class Stub:
    def __init__(self, answers, budget=50):
        self.answers, self.asked, self.left, self.used, self.stopped = answers, [], budget, 0, ""

    def search(self, q):
        if self.left <= 0:
            self.stopped = "the query budget is spent"; return None
        self.asked.append(q); self.left -= 1; self.used += 1
        for k, v in self.answers.items():
            if k in q:
                return v
        return []


class Years(unittest.TestCase):
    def test_recent_and_missing(self):
        self.assertEqual(YEARS, [2025, 2024, 2023])
        f = {"sust_years": "2021; 2023; 2024", "coverage_breakdown": "Annual Report: 4 (latest 2024)\nPolicy Documents: 3"}
        self.assertEqual(W.missing(f, YEARS), {"sustainability": [2025], "annual": [2025]})
        f = {"sust_years": "", "coverage_breakdown": "Integrated Report: 2 (latest 2025)"}
        self.assertEqual(W.missing(f, YEARS), {"sustainability": [2024, 2023], "annual": []})
        self.assertEqual(W.missing({}, YEARS), {"sustainability": [2025, 2024, 2023], "annual": [2025, 2024, 2023]})

    def test_gap_mode(self):
        self.assertTrue(W.in_gap({"gap_pages": ["Annual report (recent)"]}))
        self.assertTrue(W.in_gap({"overdue_reports": ["Sustainability report"]}))
        self.assertFalse(W.in_gap({"gap_pages": ["Code of Conduct"]}))


class Questions(unittest.TestCase):
    def test_query_and_domains(self):
        f = {"website": "www.acme.com", "company_name": 'Acme "Group" plc'}
        doms = W.company_domains(f, ["https://ir.acme-investors.com/x", "https://www.acme.com/y", "https://s2.q4cdn.com/z"], reg_domain)
        self.assertEqual(doms, ["acme.com", "acme-investors.com"])               # document hosts are not the company's sites
        q = W.query("sustainability", [2025, 2024], domains=doms)
        self.assertTrue(q.startswith("(site:acme.com OR site:acme-investors.com) (sustainability OR ESG"))
        self.assertTrue(q.endswith("(2025 OR 2024) filetype:pdf"))
        self.assertTrue(W.query("annual", [2025], name=f["company_name"]).startswith('"Acme Group plc" ("annual report"'))


class Judge(unittest.TestCase):
    def j(self, link, title, fam="sustainability", years=(2025,), snippet=""):
        return W.judge({"link": link, "title": title, "snippet": snippet}, fam, list(years), ["acme.com"], "Acme Group", reg_domain)

    def test_rules(self):
        self.assertEqual(self.j("https://www.acme.com/docs/acme-sustainability-report-2025.pdf", "Sustainability Report 2025"),
                         (True, "sustainability 2025"))
        self.assertTrue(self.j("https://s2.q4cdn.com/123/files/doc_downloads/2025/ESG.pdf", "Acme ESG Report")[0])   # IR host, named
        self.assertEqual(self.j("https://www.other.com/acme-sr-2025.pdf", "Acme Sustainability Report 2025")[1], "another site (other.com)")
        self.assertEqual(self.j("https://www.acme.com/r/2023.pdf", "Sustainability Report 2023")[1], "not one of the missing years (2023)")
        self.assertEqual(self.j("https://www.acme.com/r/menu-2025.pdf", "Canteen menu 2025")[1], "does not name the report")
        self.assertTrue(self.j("https://www.acme.com/gb2025.pdf", "Geschäftsbericht 2025", fam="annual")[0])  # another language
        # a shared host needs the company's name in the result
        self.assertFalse(self.j("https://bucket.s3.amazonaws.com/sr-2025.pdf", "Sustainability report 2025")[0])
        self.assertTrue(self.j("https://bucket.s3.amazonaws.com/acme-sr-2025.pdf", "Acme Sustainability report 2025")[0])


class Search(unittest.TestCase):
    F = {"wba_id": "PT_1", "company_name": "Acme Group", "website": "acme.com", "sust_years": "2023; 2024",
         "coverage_breakdown": "Annual Report: 3 (latest 2025)"}

    def test_own_sites_first_then_by_name_only_when_needed(self):
        hit = [{"link": "https://www.acme.com/sr2025.pdf", "title": "Acme Sustainability Report 2025"}]
        api = Stub({"site:acme.com": hit})
        res = W.search_company(self.F, [], YEARS, api, reg_domain)
        self.assertEqual(res["found"], [("https://www.acme.com/sr2025.pdf", "sustainability 2025")])
        self.assertEqual(len(api.asked), 1)                                          # own site answered: no question by name
        api = Stub({'"Acme Group"': [{"link": "https://www.acme.com/esg-2025.pdf", "title": "ESG report 2025"},
                                      {"link": "https://news.example.org/acme-esg-2025.pdf", "title": "Acme ESG 2025"}]})
        res = W.search_company(self.F, [], YEARS, api, reg_domain)
        self.assertEqual(len(api.asked), 2)
        self.assertEqual([u for u, _ in res["found"]], ["https://www.acme.com/esg-2025.pdf"])
        self.assertEqual(res["not_added"], [("https://news.example.org/acme-esg-2025.pdf", "another site (example.org)")])
        self.assertIn("passed to research_link: https://www.acme.com/esg-2025.pdf", W.note("2026-11-01", res))

    def test_budget_cut_is_flagged(self):
        api = Stub({}, budget=1)
        res = W.search_company(dict(self.F, coverage_breakdown=""), [], YEARS, api, reg_domain)
        self.assertTrue(res.get("cut"))

    def test_nothing_missing_asks_nothing(self):
        api = Stub({})
        res = W.search_company(dict(self.F, sust_years="2023; 2024; 2025"), [], YEARS, api, reg_domain)
        self.assertEqual((api.asked, res["looked_for"]), ([], []))


class DryRunCases(unittest.TestCase):
    """Regression cases from the first dry run on 30 companies (1 Nov 2026)."""
    def j(self, link, title, snippet="", fam="sustainability", years=(2025, 2024, 2023), domains=("acme.com",), name="Acme Group"):
        return W.judge({"link": link, "title": title, "snippet": snippet}, fam, list(years), list(domains), name, reg_domain)

    def test_not_the_companys_site(self):
        # a monitored SBTi page made sciencebasedtargets.org look like the company's site
        doms = W.company_domains({"company_name": "DMK", "website": "dmk.de"},
                                 ["https://files.sciencebasedtargets.org/x", "https://dmk.de/a", "https://lobbymap.org/b",
                                  "https://static1.squarespace.com/c", "https://psc.wb.gov.in/d", "https://www.dmk-group.com/e"], reg_domain)
        self.assertEqual(doms, ["dmk.de", "dmk-group.com"])

    def test_year_from_title_and_file_not_snippet_or_upload_folder(self):
        self.assertEqual(self.j("https://www.acme.com/media/x/acme-global-sustainability-report-2019.pdf",
                                "Acme global sustainability report 2019", snippet="... in 2025 we ...")[1],
                         "not one of the missing years (2019)")
        self.assertEqual(self.j("https://www.acme.com/wp-content/uploads/2025/07/Acme-Relatorio-Sustentavel-2020.pdf",
                                "Relatório de Desenvolvimento Sustentável 2020")[1], "not one of the missing years (2020)")
        self.assertTrue(self.j("https://www.acme.com/wp-content/uploads/2025/07/Acme-sustainability-report.pdf",
                               "Acme Sustainability Report 2024")[0])

    def test_shared_hosts_need_the_name(self):
        self.assertFalse(self.j("https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0325/2025032501525.pdf",
                                "ESG Report 2024")[0])
        self.assertTrue(self.j("https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0325/2025032501525.pdf",
                               "ACME GROUP - ESG Report 2024")[0])

    def test_not_a_report_or_not_a_pdf(self):
        self.assertEqual(self.j("https://www.acme.com/2023-ifpr-disclosures/", "2023 IFPR disclosures")[1], "not a PDF")
        self.assertTrue(self.j("https://www.acme.com/files/Comunicado-de-Prensa-COP30-2025.pdf",
                               "Comunicado de Prensa sustainability 2025")[1].startswith("not a report"))
        self.assertTrue(self.j("https://www.acme.com/q/Conference_Call_1Q26_fv.pdf", "Conference call ESG 2025")[1].startswith("not a report"))


if __name__ == "__main__":
    unittest.main()
