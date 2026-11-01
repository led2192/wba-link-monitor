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
        q = W.query("sustainability", [2021, 2020], domains=doms)               # not the latest year: no publication year added
        self.assertTrue(q.startswith("(site:acme.com OR site:acme-investors.com) (sustainability OR ESG"))
        self.assertTrue(q.endswith("(2021 OR 2020) filetype:pdf"))
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
        res = W.search_company(self.F, [], YEARS, api, reg_domain, opener=None)
        self.assertEqual(res["found"], [("https://www.acme.com/sr2025.pdf", "sustainability 2025")])
        self.assertEqual(len(api.asked), 1)                                          # own site answered: no question by name
        api = Stub({'"Acme Group"': [{"link": "https://www.acme.com/esg-2025.pdf", "title": "ESG report 2025"},
                                      {"link": "https://news.example.org/acme-esg-2025.pdf", "title": "Acme ESG 2025"}]})
        res = W.search_company(self.F, [], YEARS, api, reg_domain, opener=lambda u: ("News about Acme Group 2025", 2))
        self.assertEqual(len(api.asked), 2)
        self.assertEqual([u for u, _ in res["found"]], ["https://www.acme.com/esg-2025.pdf"])
        self.assertEqual(res["not_added"], [("https://news.example.org/acme-esg-2025.pdf",
                                             "another site (example.org); opened: 2 pages, not a report")])
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

    def test_name_in_a_snippet_is_not_enough(self):
        # a NIQ annual report on q4cdn passed for SHEIN: the snippet named SHEIN, the title and address did not
        self.assertFalse(self.j("https://s206.q4cdn.com/369/files/doc_financials/2025/ar/NIQ-2025-Annual-Report.pdf",
                                "NIQ 2025 Annual Report", snippet="clients such as Acme Group", fam="annual")[0])
        self.assertTrue(self.j("https://acme-prod-asset.s3.us-east-2.amazonaws.com/uploads/2024-03/Acme_Sustainability_Report_2024.pdf",
                               "Sustainability Report 2024")[0])                     # the name in the address

    def test_not_a_report_or_not_a_pdf(self):
        self.assertEqual(self.j("https://www.acme.com/2023-ifpr-disclosures/", "2023 IFPR disclosures")[1], "not a PDF")
        self.assertTrue(self.j("https://www.acme.com/files/Comunicado-de-Prensa-COP30-2025.pdf",
                               "Comunicado de Prensa sustainability 2025")[1].startswith("not a report"))
        self.assertTrue(self.j("https://www.acme.com/q/Conference_Call_1Q26_fv.pdf", "Conference call ESG 2025")[1].startswith("not a report"))
        self.assertTrue(self.j("https://www.acme.com/bonds/2023/Sustainability-Linked-Finance-Framework.pdf",
                               "Sustainability-Linked Finance Framework 2023")[1].startswith("not a report"))


class PublicationYear(unittest.TestCase):
    """1 Nov 2026: reporting year 2025 is published in 2026, often titled 2026 or FY2026."""
    def test_2026_is_searched_and_counts_as_2025(self):
        real = W.expected_year
        W.expected_year = lambda today=None: 2025
        try:
            self.assertTrue(W.query("sustainability", [2025, 2024], domains=["acme.com"]).endswith("(2026 OR 2025 OR 2024) filetype:pdf"))
            self.assertEqual(W.judge({"link": "https://www.acme.com/r/acme-sustainability-report-2026.pdf",
                                      "title": "Acme Sustainability Report 2026"}, "sustainability", [2025, 2024],
                                     ["acme.com"], "Acme", reg_domain), (True, "sustainability 2025"))
            self.assertTrue(W.judge({"link": "https://www.acme.com/ir/integrated-report-fy2026.pdf", "title": "Integrated Report FY2026"},
                                    "annual", [2025], ["acme.com"], "Acme", reg_domain)[0])
            # a March year end: the report for FY ending March 2026 is held as 2026, which is reporting year 2025
            self.assertEqual(W.missing({"sust_years": "2023; 2024; 2026", "coverage_breakdown": "Annual Report: 3 (latest 2026)"},
                                       [2025, 2024, 2023]), {"sustainability": [], "annual": []})
        finally:
            W.expected_year = real


class CoverCheck(unittest.TestCase):
    """1 Nov 2026: a report on another server (CDN, group site, archive) is kept when its first pages show
    the company, the report and a missing year."""
    F = {"wba_id": "PT_1", "company_name": "Arcos Dorados Holdings Inc.", "website": "arcosdorados.com",
         "sust_years": "2023; 2024", "coverage_breakdown": "Annual Report: 3 (latest 2025)"}

    def test_cover_rules(self):
        cover = "Arcos Dorados\nSocial Impact and Sustainability Report 2025\nRecipe for the Future"
        self.assertEqual(W.cover_check(cover, 120, self.F, "sustainability", [2025]),
                         (True, "sustainability 2025, checked on the file's first pages"))
        self.assertEqual(W.cover_check("McDonald's Purpose & Impact Report 2025", 90, self.F, "sustainability", [2025])[1],
                         "the company's name is not on its first 15 pages or in its properties")
        self.assertEqual(W.cover_check("Arcos Dorados press note sustainability 2025", 2, self.F, "sustainability", [2025])[1],
                         "2 pages, not a report")
        self.assertEqual(W.cover_check("Arcos Dorados Sustainability Report 2022", 80, self.F, "sustainability", [2025])[1],
                         "no missing year on its first pages (2022)")
        # dry run 1 Nov 2026: a 2022 report whose cover names its 2025 targets is a 2022 report
        self.assertEqual(W.cover_check("Arcos Dorados\nSustainability Report 2022\nOur 2025 goals", 80, self.F, "sustainability", [2025])[1],
                         "no missing year on its first pages (2022)")
        self.assertEqual(W.cover_check({"front": "Arcos Dorados Sustainability Report", "file": "AD_2022_ENG.pdf"}, 80,
                                       self.F, "sustainability", [2025])[1], "no missing year on its first pages (2022)")

    def test_not_only_the_cover(self):
        # a cover with only a logo: the name is further in, or in the file's properties
        doc = {"front": "Sustainability Report 2025\nBuilding a better future", "more": "Message from the CEO of Arcos Dorados ...",
               "meta": "", "file": "SR2025.pdf"}
        self.assertTrue(W.cover_check(doc, 120, self.F, "sustainability", [2025])[0])
        doc = {"front": "Sustainability Report 2025", "more": "", "meta": "Arcos Dorados Holdings Inc.", "file": "x.pdf"}
        self.assertTrue(W.cover_check(doc, 120, self.F, "sustainability", [2025])[0])
        # a year only deep inside does not count (every report prints its comparison years)
        doc = {"front": "Arcos Dorados Sustainability Report", "more": "x" * 7000 + " 2025 ", "meta": "", "file": "sr.pdf"}
        self.assertEqual(W.cover_check(doc, 120, self.F, "sustainability", [2025])[1], "no missing year on its first pages")

    def test_a_cdn_file_is_opened_and_kept(self):
        hit = [{"link": "https://ddhnb9vx8lz57.cloudfront.net/wp-content/uploads/sites/3/2026/07/01100613/AD_2025_ENG.pdf",
                "title": "AD_2025_ENG"}]
        api = Stub({"site:arcosdorados.com": [], '"Arcos Dorados': hit})
        opened = []
        def opener(u):
            opened.append(u); return ("ARCOS DORADOS Social Impact and Sustainability Report 2025", 150)
        res = W.search_company(self.F, [], YEARS, api, reg_domain, opener=opener)
        self.assertEqual(res["found"], [(hit[0]["link"], "sustainability 2025, checked on the file's first pages")])
        self.assertEqual(len(opened), 1)

    def test_at_most_four_files_opened_per_family(self):
        hits = [{"link": f"https://other{i}.com/esg-{i}.pdf", "title": "ESG"} for i in range(8)]
        api = Stub({"site:arcosdorados.com": [], '"Arcos Dorados': hits})
        opened = []
        W.search_company(self.F, [], YEARS, api, reg_domain, opener=lambda u: opened.append(u) or ("nothing", 1))
        self.assertEqual(len(opened), W.CHECK_MAX)


class FirstWaveCases(unittest.TestCase):
    """1 Nov 2026, the first committed wave: files on other sites passed because they mentioned the company
    somewhere and spoke of reports. Each kind is a case here."""
    def check(self, f, doc, fam="annual", own=False, pages=120, years=(2025, 2024, 2023)):
        return W.cover_check(doc, pages, f, fam, list(years), own_site=own)

    def test_a_relatives_report_is_not_the_companys(self):
        f = {"company_name": "China Post Group", "website": "chinapost.com.cn"}
        doc = {"front": "Postal Savings Bank of China Co., Ltd.\nAnnual Report 2025", "more": "controlling shareholder China Post Group ...",
               "meta": "", "file": "P020260415322721338496.pdf"}
        self.assertFalse(self.check(f, doc)[0])
        self.assertIn("whole name", self.check(f, doc)[1])

    def test_written_by_someone_else(self):
        f = {"company_name": "BetAgro", "website": "betagro.com"}
        doc = {"front": "BetAgro (BTG TB) M&A is coming; Maintain HOLD TP THB23.00\nAnnual results 2024 equity research", "more": "", "meta": "", "file": "x.pdf"}
        self.assertIn("written by someone else", self.check(f, doc)[1])
        f = {"company_name": "Boehringer Ingelheim", "website": "boehringer-ingelheim.com"}
        doc = {"front": "Boehringer Ingelheim\nPostdoctoral Fellowship Brochure 2022-2024\nresponsibility report", "more": "", "meta": "", "file": "BIFellowshipBrochure-2022-2024.pdf"}
        self.assertFalse(self.check(f, doc, fam="sustainability")[0])

    def test_a_report_word_must_stand_next_to_the_family(self):
        f = {"company_name": "Bridgewater Associates", "website": "bridgewater.com"}
        doc = {"front": "Bridgewater Associates\nIs the Green Energy Transition Dead? 2025\nsustainable investing research", "more": "", "meta": "", "file": "x"}
        self.assertEqual(self.check(f, doc, fam="sustainability", own=True)[1], "its title page does not name the report")

    def test_a_short_name_needs_its_website_in_the_file(self):
        f = {"company_name": "ADIA", "website": "adia.ae"}
        doc = {"front": "ADIA Annual Report 2025\nAustralian Dental Industry Association", "more": "www.adia.com.au", "meta": "", "file": "2025-ADIA-Annual-Report.pdf"}
        self.assertIn("not in the file", self.check(f, doc)[1])
        doc = {"front": "ADIA Annual Review 2025 Annual Report", "more": "Abu Dhabi Investment Authority www.adia.ae", "meta": "", "file": "ar.pdf"}
        self.assertTrue(self.check(f, doc)[0])

    def test_the_companys_report_elsewhere_still_passes(self):
        f = {"company_name": "Arcos Dorados Holdings Inc.", "website": "arcosdorados.com"}
        # a logo-only cover: the name twice on pages 1 to 3
        doc = {"front": "Social Impact and Sustainability Report 2025\nAbout Arcos Dorados ... Arcos Dorados is the largest ...",
               "more": "", "meta": "", "file": "AD_2025_ENG.pdf"}
        self.assertTrue(self.check(f, doc, fam="sustainability")[0])
        # the name only on deeper pages is not enough off the company's site
        doc = {"front": "Social Impact and Sustainability Report 2025", "more": "Arcos Dorados", "meta": "", "file": "sr.pdf"}
        self.assertFalse(self.check(f, doc, fam="sustainability")[0])
        self.assertTrue(self.check(f, doc, fam="sustainability", own=True)[0])     # on its own site it is


class Writes(unittest.TestCase):
    """1 Nov 2026: the first committed wave spent 384 credits, then the single write at the end called
    airtable_request with json= (it takes payload=) and nothing was written."""
    def test_patch_uses_payload_in_tens(self):
        import inspect
        from monitor_core import airtable_request
        self.assertIn("payload", inspect.signature(airtable_request).parameters)
        calls = []
        W.WRITE_PAUSE, pause = 0, W.WRITE_PAUSE
        try:
            n = W.write_companies("appX", {"Authorization": "Bearer t"},
                                  [{"id": f"rec{i}", "fields": {"web_searched_on": "2026-11-01"}} for i in range(23)],
                                  request=lambda *a, **k: calls.append((a, k)))
        finally:
            W.WRITE_PAUSE = pause
        self.assertEqual(n, 23)
        self.assertEqual([len(k["payload"]["records"]) for _, k in calls], [10, 10, 3])
        self.assertTrue(all(a[0] == "PATCH" and a[1].endswith("/appX/companies") and k["payload"]["typecast"] is False
                            for a, k in calls))
        self.assertEqual(calls[0][0][2]["Content-Type"], "application/json")

    def test_main_writes_as_it_goes(self):
        with open(W.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("if a.commit and len(updates) >= 10:", src)          # not only once at the end
        self.assertNotIn("json={", src)


if __name__ == "__main__":
    unittest.main()
