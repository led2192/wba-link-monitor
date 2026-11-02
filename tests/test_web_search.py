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
        self.assertEqual(self.j("https://s2.q4cdn.com/123/files/doc_downloads/2025/ESG.pdf", "Acme ESG Report")[1],
                         "on a shared host, to be opened (q4cdn.com)")                                 # IR host, named: opened
        self.assertEqual(self.j("https://www.other.com/acme-sr-2025.pdf", "Acme Sustainability Report 2025")[1], "another site (other.com)")
        self.assertEqual(self.j("https://www.acme.com/r/2023.pdf", "Sustainability Report 2023")[1], "not one of the missing years (2023)")
        self.assertEqual(self.j("https://www.acme.com/r/menu-2025.pdf", "Canteen menu 2025")[1], "does not name the report")
        self.assertTrue(self.j("https://www.acme.com/gb2025.pdf", "Geschäftsbericht 2025", fam="annual")[0])  # another language
        # a shared host needs the company's name in the result
        self.assertFalse(self.j("https://bucket.s3.amazonaws.com/sr-2025.pdf", "Sustainability report 2025")[0])
        self.assertTrue(self.j("https://bucket.s3.amazonaws.com/acme-sr-2025.pdf", "Acme Sustainability report 2025")[1]
                        .startswith("on a shared host, to be opened"))


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
                                      {"link": "https://news.example.com/acme-esg-2025.pdf", "title": "Acme ESG 2025"}]})
        # every file is opened (2 Nov 2026), the company's own too: the own-site report passes on its cover, the
        # two-page news item elsewhere does not
        covers = {"https://www.acme.com/esg-2025.pdf": ("Acme Group\nESG Report 2025", 80),
                  "https://news.example.com/acme-esg-2025.pdf": ("News about Acme Group 2025", 2)}
        res = W.search_company(self.F, [], YEARS, api, reg_domain, opener=lambda u: covers[u])
        self.assertEqual(len(api.asked), 2)
        self.assertEqual(res["found"], [("https://www.acme.com/esg-2025.pdf", "sustainability 2025, checked on the file's first pages")])
        self.assertEqual(res["not_added"], [("https://news.example.com/acme-esg-2025.pdf",
                                             "another site (example.com); opened: 2 pages, not a report")])
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
                               "ACME GROUP - ESG Report 2024")[1].startswith("on a shared host, to be opened"))

    def test_name_in_a_snippet_is_not_enough(self):
        # a NIQ annual report on q4cdn passed for SHEIN: the snippet named SHEIN, the title and address did not
        self.assertFalse(self.j("https://s206.q4cdn.com/369/files/doc_financials/2025/ar/NIQ-2025-Annual-Report.pdf",
                                "NIQ 2025 Annual Report", snippet="clients such as Acme Group", fam="annual")[0])
        self.assertTrue(self.j("https://acme-prod-asset.s3.us-east-2.amazonaws.com/uploads/2024-03/Acme_Sustainability_Report_2024.pdf",
                               "Sustainability Report 2024")[1].startswith("on a shared host, to be opened"))  # the name in the address

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
        self.assertIn("industry association", self.check(f, doc)[1])          # another organisation, by its own words
        doc = {"front": "ADIA Annual Report 2025\nDental supplies, Australia", "more": "www.adia.com.au", "meta": "", "file": "2025-ADIA-Annual-Report.pdf"}
        self.assertIn("not in the file", self.check(f, doc)[1])               # and by the website missing
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


class SecondWaveCases(unittest.TestCase):
    """1 Nov 2026, the second committed wave (100 companies, 33 documents, about 12 wrong)."""
    def test_a_government_subdomain_is_the_site_not_the_whole_domain(self):
        f = {"company_name": "Punjab Transport Company", "website": "https://ptc.punjab.gov.pk/node/472"}
        self.assertEqual(W.company_domains(f, [], reg_domain), ["ptc.punjab.gov.pk"])
        f = {"company_name": "Sistema de Transporte Colectivo", "website": "https://www.metro.cdmx.gob.mx"}
        self.assertEqual(W.company_domains(f, [], reg_domain), ["metro.cdmx.gob.mx"])
        f = {"company_name": "Transmilenio", "website": "https://www.transmilenio.gov.co"}
        self.assertEqual(W.company_domains(f, [], reg_domain), ["transmilenio.gov.co"])   # its own domain
        self.assertTrue(W.on_sites("https://www.transmilenio.gov.co/files/informe-2025.pdf", ["transmilenio.gov.co"]))
        self.assertTrue(W.on_sites("https://ptc.punjab.gov.pk/files/ar.pdf", ["ptc.punjab.gov.pk"]))
        self.assertFalse(W.on_sites("https://ppdb.punjab.gov.pk/system/files/PPDB_AnualReport_2025.pdf", ["ptc.punjab.gov.pk"]))
        self.assertIn("site:ptc.punjab.gov.pk", W.query("annual", [2025], domains=["ptc.punjab.gov.pk"]))
        r = {"link": "https://ppdb.punjab.gov.pk/system/files/Annex-C_PPDB_AnualReport_2025.pdf", "title": "Annual Report 2025"}
        self.assertEqual(W.judge(r, "annual", [2025], ["ptc.punjab.gov.pk"], "Punjab Transport Company", reg_domain)[1],
                         "not the company's: a government site (punjab.gov.pk)")        # and never opened (2 Nov 2026)

    def test_more_things_that_are_not_a_report(self):
        for title in ("Dodge & Cox Worldwide Funds Semi-Annual Report", "Worldwide Funds Annual Report", "JSS Analytics Sample Report",
                      "apd_sriinternational plan document", "DownLoadPublicCommunicationFile"):
            r = {"link": "https://www.dodgeandcox.com/x/" + title.replace(" ", "_") + ".pdf", "title": title}
            self.assertFalse(W.judge(r, "annual", [2025, 2024], ["dodgeandcox.com"], "Dodge & Cox", reg_domain)[0], title)

    def test_written_by_an_organisation_about_the_company(self):
        f = {"company_name": "Tsingshan Holding", "website": "tsingshan.com"}
        doc = {"front": "Mandates of the Special Rapporteur on the human rights to safe drinking water...\nTsingshan Holding Group sustainability 2025 report",
               "more": "Tsingshan Holding", "meta": "", "file": "x.pdf"}
        self.assertIn("written by someone else", W.cover_check(doc, 20, f, "sustainability", [2025], own_site=False)[1])
        f = {"company_name": "Deichmann SE", "website": "deichmann.com"}
        doc = {"front": "World Federation of Advertisers\nAnnual Report 2024\nMembers: ... Deichmann SE ... Deichmann SE", "more": "", "meta": "", "file": "wfa_annual_report_2024.pdf"}
        self.assertIn("written by someone else", W.cover_check(doc, 60, f, "annual", [2024], own_site=False)[1])


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


class SharedHostCases(unittest.TestCase):
    """2 Nov 2026 wave: two Football Association of Wales files on amazonaws.com and cloudfront.net were passed on
    for FAW (the carmaker) because "faw" was in their addresses. A file on a shared host is now always opened."""
    F = {"company_name": "FAW", "website": "http://www.faw.com", "sust_years": "", "coverage_breakdown": ""}

    def run_with(self, hits, cover):
        api = Stub({"site:faw.com": [], '"FAW"': hits})
        opened = []
        def opener(u):
            opened.append(u); return cover, 60
        return W.search_company(self.F, [], YEARS, api, reg_domain, opener=opener), opened

    def test_another_organisation_with_the_same_acronym_is_not_passed(self):
        hits = [{"link": "https://media-faw-cymru.s3.eu-west-2.amazonaws.com/faw/20240905130454/"
                         "FAW-Mens-Tier-1-Club-Licensing-and-Financial-Sustainability-Regulations-2024.pdf",
                 "title": "FAW Mens Tier 1 Club Licensing and Financial Sustainability Regulations 2024"},
                {"link": "https://d1zqj8o9na6wgl.cloudfront.net/faw/20260120161259/FAW-Anti-Doping-Annual-Report-2024-25-Website20-Read-Only.pdf",
                 "title": "FAW Anti-Doping Annual Report 2024-25"}]
        cover = {"front": "FAW Football Association of Wales\nAnti-Doping Annual Report 2024-25", "more": "www.faw.cymru",
                 "meta": "", "file": "FAW-Anti-Doping-Annual-Report-2024-25.pdf"}
        res, opened = self.run_with(hits, cover)
        self.assertEqual(res["found"], [])
        self.assertTrue(opened)                              # opened, not trusted from the address

    def test_the_companys_own_file_on_a_cdn_still_passes(self):
        hits = [{"link": "https://d1zqj8o9na6wgl.cloudfront.net/uploads/2025/FAW-Annual-Report-2025.pdf",
                 "title": "FAW Annual Report 2025"}]
        cover = {"front": "FAW\nChina FAW Group Co., Ltd.\nAnnual Report 2025", "more": "www.faw.com", "meta": "", "file": "x.pdf"}
        res, _ = self.run_with(hits, cover)
        self.assertEqual([u for u, _ in res["found"]], [hits[0]["link"]])

    def test_another_companys_report_on_an_ir_host_is_not_passed(self):
        # Morningstar's annual report on q4cdn.com was passed on for Mesirow Financial (1 Nov 2026 wave)
        f = {"company_name": "Mesirow Financial", "website": "https://www.mesirow.com"}
        doc = {"front": "2025 Annual Report\nMorningstar, Inc.\nLetter to Shareholders", "more": "", "meta": "", "file": "2025AnnualReport.pdf"}
        self.assertFalse(W.cover_check(doc, 120, f, "annual", [2025], own_site=False)[0])


class ThirdWaveCases(unittest.TestCase):
    """2 Nov 2026: 37 of the 167 documents the waves of 1 and 2 Nov brought in were other organisations' files
    or not reports. Every kind is a case here. Own-site files were never opened; now every file is."""
    def j(self, link, title, name, domains, fam="annual", years=(2025, 2024, 2023)):
        return W.judge({"link": link, "title": title}, fam, list(years), list(domains), name, reg_domain)

    def test_own_site_files_that_are_not_reports_by_their_title(self):
        cases = [("https://www.invesco.com/content/dam/invesco/apac/en/pdf/insights/2025/january/invesco-sustainable-investing-outlook-for-jan25.pdf",
                  "Invesco sustainable investing outlook for jan25", "Invesco", ["invesco.com"], "sustainability", "outlook for"),
                 ("https://www.teliacompany.com/assets/x/The_Enablement_Effect_Telia_Trend_Report_Sustainability_2026.pdf",
                  "The Enablement Effect Telia Trend Report Sustainability 2026", "Telia", ["teliacompany.com"], "sustainability", "trend report"),
                 ("https://media.amtrak.com/wp-content/uploads/2023/06/FY22-Sustainability-Report-News-Release-1.pdf",
                  "FY22 Sustainability Report News Release", "Amtrak", ["amtrak.com"], "sustainability", "news release"),
                 ("https://media.amtrak.com/wp-content/uploads/2024/06/FY23-Sustainability-Report-cover.pdf",
                  "FY23 Sustainability Report cover", "Amtrak", ["amtrak.com"], "sustainability", "cover"),
                 ("https://packagingcoatings.akzonobel.com/x/akzonobel-packaging-coatings-sustainability-whitepaper-2025.pdf",
                  "AkzoNobel sustainability whitepaper 2025", "AkzoNobel", ["akzonobel.com"], "sustainability", "whitepaper"),
                 ("http://www.posco-inc.com/poscoinc/servlet/FileDown?file=x.pdf&filename=POSCO_Holdings_consolidated_FY22_4Q_E.pdf",
                  "POSCO Holdings consolidated FY22 4Q E annual report 2023", "POSCO Holdings", ["posco-inc.com"], "annual", "4q"),
                 ("https://www.spic.in/wp-content/uploads/2024/08/Intimation_on_BRSR.pdf", "Intimation on BRSR sustainability report 2024",
                  "SPIC", ["spic.in"], "sustainability", "intimation"),
                 ("https://www.sama.gov.sa/en-US/Documents/Financial_Sector_Development_Program_Annual_Report-2024-EN.pdf",
                  "Financial Sector Development Program Annual Report 2024", "SAMA", ["sama.gov.sa"], "annual", "program annual report"),
                 ("https://www.cofcointernational.com/media/rupkzhxi/soft-commodities-forum-scf24_annualreport-2024-en-1.pdf",
                  "Soft Commodities Forum SCF24 annual report 2024", "COFCO", ["cofco.com", "cofcointernational.com"], "annual", "forum"),
                 ("https://www.hanafn.com:8002/download/10072085/crossDownload.do", "2026 Allocation & Impact Report (English)",
                  "Hana Financial Group", ["hanafn.com"], "sustainability", "allocation & impact"),
                 ("https://www.storaenso.com/x/green-and-sustainability-linked-financing-report-2025.pdf",
                  "Green and Sustainability-Linked Financing Report 2025", "Stora Enso", ["storaenso.com"], "sustainability", "financing report")]
        for link, title, name, doms, fam, word in cases:
            ok, why = self.j(link, title, name, doms, fam)
            self.assertFalse(ok, link)
            self.assertIn(word, why, link)
        # on another site such a title is never opened either (a rating agency's rationale for Bank of India)
        self.assertEqual(W.not_a_report("BoI ESG Rating Rationale", "Bank of India"), "rating rationale")
        # a word of the company's own name does not make its report "not a report"
        self.assertTrue(self.j("https://www.futurefund.gov.au/x/future-fund-annual-report-2025.pdf", "Future Fund Annual Report 2025",
                               "Future Fund", ["futurefund.gov.au"])[0])
        self.assertTrue(self.j("https://www.postholdings.com/x/post-holdings-annual-report-2025.pdf", "Post Holdings Annual Report 2025",
                               "Post Holdings", ["postholdings.com"])[0])

    def test_sites_that_never_hold_the_companys_report_are_not_opened(self):
        cases = [("https://www.jica.go.jp/english/x/Result_of_Monitoring_2025.pdf", "Klabin", ["klabin.com.br"], "government"),
                 ("http://www.sasac.gov.cn/n4470048/x/33812651.pdf", "China Railway Construction", ["crcc.cn"], "government"),
                 ("https://academica-e.unavarra.es/bitstreams/cfcacad0/download", "Mercadona", ["mercadona.es"], "academic"),
                 ("https://www.digicelfoundation.org/api/assets/x/2022-2023-annual-report.pdf", "Digicel", ["digicelgroup.com"], "foundation"),
                 ("https://www.theclimategroup.org/hubfs/Under2/Future%20Fund%202025.pdf", "Future Fund", ["futurefund.gov.au"], ".org"),
                 ("https://regnskaber.cvrapi.dk/xhtml/28960101/abc.pdf", "Lactalis", ["lactalis.fr"], "registry"),
                 ("https://www.wwf.de/fileadmin/x/EDEKA-WWF-Progress-Report-2023-2024.pdf", "Edeka Zentrale", ["verbund.edeka"], "not a company")]
        for link, name, doms, kind in cases:
            ok, why = self.j(link, "Annual Report 2025", name, doms)
            self.assertFalse(ok, link)
            self.assertTrue(why.startswith("not the company's"), why)
            self.assertIn(kind, why, link)
            self.assertFalse(why.startswith(W.CHECKABLE), why)                   # never opened
        self.assertEqual(W.site_class("https://www.jica.go.jp/x.pdf", ["klabin.com.br"], reg_domain), "never")
        self.assertEqual(W.site_class("https://www.virginaustralia.com/x.pdf", ["qatarairways.com"], reg_domain), "third")
        self.assertEqual(W.site_class("https://s3.amazonaws.com/x.pdf", ["qatarairways.com"], reg_domain), "doc_host")
        self.assertEqual(W.site_class("https://www.qatarairways.com/x.pdf", ["qatarairways.com"], reg_domain), "own")

    def test_a_shared_domain_and_a_government_folder_are_not_the_companys_site(self):
        # bank.in is every Indian bank's: the company's site under it is its host
        f = {"company_name": "Bank of India", "website": "https://www.bankofindia.co.in", "hq_country": "IND"}
        self.assertEqual(W.company_domains(f, ["https://bankofindia.bank.in/investor-relations"], reg_domain),
                         ["bankofindia.co.in", "bankofindia.bank.in"])
        self.assertEqual(self.j("https://www.axis.bank.in/docs/brsr-2024-25.pdf", "Business Responsibility Sustainability Report 2024-25",
                                "Bank of India", ["bankofindia.co.in", "bankofindia.bank.in"], "sustainability")[1], "another site (bank.in)")
        # a folder of a bare government domain is the site, not the whole domain
        f = {"company_name": "Michigan Retirement Systems", "website": "https://www.michigan.gov/ors/", "hq_country": "USA"}
        self.assertEqual(W.company_domains(f, [], reg_domain), ["michigan.gov/ors"])
        self.assertTrue(W.on_sites("https://www.michigan.gov/ors/-/media/x/SERS-Summary-Annual-Report-2025.pdf", ["michigan.gov/ors"]))
        self.assertFalse(W.on_sites("https://www.michigan.gov/ok2say/-/media/x/2025-OK2SAY-Annual-Report.pdf", ["michigan.gov/ors"]))
        self.assertIn("site:michigan.gov/ors", W.query("annual", [2025], domains=["michigan.gov/ors"]))
        self.assertTrue(self.j("https://www.michigan.gov/ok2say/-/media/x/2025-OK2SAY-Annual-Report.pdf", "2025 OK2SAY Annual Report",
                               "Michigan Retirement Systems", ["michigan.gov/ors"])[1].startswith("not the company's: a government site"))

    def test_a_local_affiliates_domain_is_not_the_groups(self):
        f = {"company_name": "McCain Foods", "website": "https://www.mccain.com", "hq_country": "CAN"}
        self.assertEqual(W.company_domains(f, ["https://www.mccainindia.com/x", "https://www.mccain.com/y"], reg_domain), ["mccain.com"])
        f = {"company_name": "Novo Nordisk", "website": "https://www.novonordisk.com", "hq_country": "DNK"}
        self.assertEqual(W.company_domains(f, ["https://www.novonordisk-us.com/x"], reg_domain), ["novonordisk.com"])
        f = {"company_name": "AS Watson", "website": "https://www.aswatson.com/", "hq_country": "HKG"}
        self.assertEqual(W.company_domains(f, ["https://bulten.watsons.com.tr/x", "https://www.aswatson.com/y"], reg_domain), ["aswatson.com"])
        f = {"company_name": "Digicel", "website": "https://www.digicelgroup.com", "hq_country": "JAM"}
        self.assertEqual(W.company_domains(f, ["https://www.digicelfoundation.org/x"], reg_domain), ["digicelgroup.com"])
        # the group's own second domain in its own country still counts
        f = {"company_name": "Telenor", "website": "https://www.telenor.com", "hq_country": "NOR"}
        self.assertEqual(W.company_domains(f, ["https://www.telenor.no/x"], reg_domain), ["telenor.com", "telenor.no"])

    def check(self, f, front, more="", meta="", fname="x.pdf", fam="annual", site="third", years=(2025, 2024, 2023), pages=120):
        return W.cover_check({"front": front, "more": more, "meta": meta, "file": fname}, pages, f, fam, list(years), site=site)

    def test_another_companys_report_that_names_the_company_further_down(self):
        f = {"company_name": "Qatar Airways Group", "website": "https://www.qatarairways.com", "hq_country": "QAT"}
        front = "Virgin Australia\nSustainability Report 2025\n" + "x " * 200 + "Our shareholders Bain Capital and Qatar Airways Group ... Qatar Airways Group"
        self.assertIn("does not head the file", self.check(f, front, fam="sustainability")[1])
        f = {"company_name": "New Balance", "website": "https://www.newbalance.com", "hq_country": "USA"}
        front = "JD Sports Fashion plc\nAnnual Report and Accounts 2026\n" + "x " * 200 + "brands such as Nike, adidas and New Balance ... New Balance"
        self.assertIn("does not head the file", self.check(f, front)[1])
        f = {"company_name": "SC Johnson", "website": "https://www.scjohnson.com", "hq_country": "USA"}
        front = "Johnson Outdoors Inc.\n2025 Annual Report\nJohnson Outdoors ... Johnson Outdoors"
        self.assertIn("whole name", self.check(f, front)[1])                     # "johnson" alone is not the name
        f = {"company_name": "Apax Partners", "website": "https://www.apax.com", "hq_country": "GBR"}
        front = "EARPS Midco 1 Limited\nAnnual Report 2025\n" + "x " * 200 + "funds advised by Apax Partners ... Apax Partners LLP"
        self.assertIn("does not head the file", self.check(f, front)[1])

    def test_a_subsidiarys_or_affiliates_document(self):
        f = {"company_name": "Lactalis", "website": "https://www.lactalis.fr", "hq_country": "FRA", "corporate_name": "Groupe Lactalis"}
        front = "Lactalis Danmark A/S\nIndustrivej 14C, DK-3320 Skaevinge\nAnnual report 2025\nLactalis Danmark A/S"
        self.assertIn("subsidiary's or affiliate's document (lactalis danmark", self.check(f, front)[1])
        f = {"company_name": "REMONDIS", "website": "https://www.remondis.com", "hq_country": "DEU", "corporate_name": "REMONDIS SE & Co. KG"}
        front = "Remondis A/S\nAnnual report 2025\nIndustrikrogen 18, 2635 Ishoj\nRemondis A/S CVR No. 19012345"
        self.assertIn("subsidiary's or affiliate's document (remondis a s", self.check(f, front)[1])
        f = {"company_name": "SC Johnson", "website": "https://www.scjohnson.com", "hq_country": "USA", "corporate_name": "S.C. Johnson & Son, Inc."}
        front = "SC Johnson Professional A/S\nSverigesgade 26, 5000 Odense C\nAnnual report 2023\nSC Johnson Professional A/S"
        self.assertIn("subsidiary's or affiliate's document", self.check(f, front)[1])
        f = {"company_name": "McCain Foods", "website": "https://www.mccain.com", "hq_country": "CAN", "corporate_name": "McCain Foods Limited"}
        front = "Project Shakti\nAnnual Report FY26\nMcCain Foods (India) Private Limited\nMcCain Foods India"
        self.assertIn("subsidiary's or affiliate's document (mccain foods india", self.check(f, front, fname="project-shakti-f26-annual-report.pdf")[1])
        f = {"company_name": "Novo Nordisk", "website": "https://www.novonordisk.com", "hq_country": "DNK", "corporate_name": "Novo Nordisk A/S"}
        front = "Novo Nordisk Inc.\nSocial Impact Report 2023\nNovo Nordisk Inc., Plainsboro, New Jersey"
        self.assertIn("subsidiary's or affiliate's document (novo nordisk inc", self.check(f, front, fam="sustainability")[1])
        # a relative that names its parent on the cover
        f = {"company_name": "China Datang", "website": "http://en.china-cdt.com", "hq_country": "CHN", "corporate_name": "China Datang Corporation Ltd."}
        front = "Datang International Power Generation Co., Ltd.\nEnvironmental, Social and Governance Report 2023\nA subsidiary of China Datang Corporation"
        self.assertIn("about a relative of the company (a subsidiary of", self.check(f, front, fam="sustainability", site="doc_host")[1])

    def test_the_groups_own_report_still_passes(self):
        f = {"company_name": "Novo Nordisk", "website": "https://www.novonordisk.com", "hq_country": "DNK", "corporate_name": "Novo Nordisk A/S"}
        self.assertTrue(self.check(f, "Novo Nordisk\nAnnual Report 2025\nNovo Nordisk A/S", site="doc_host")[0])
        f = {"company_name": "Telenor", "website": "https://www.telenor.com", "hq_country": "NOR", "corporate_name": "Telenor ASA"}
        self.assertTrue(self.check(f, "Telenor Group\nAnnual Report 2025\nTelenor ASA ... Telenor Norge AS is our Norwegian business")[0])
        f = {"company_name": "Koc Holding", "website": "https://www.koc.com.tr", "hq_country": "TUR", "corporate_name": "Koç Holding A.Ş."}
        self.assertTrue(self.check(f, "Koç Holding A.Ş.\nAnnual Report 2025\nKoç Holding A.Ş.", site="own")[0])
        f = {"company_name": "Nomura Holdings", "website": "https://www.nomura.com", "hq_country": "JPN", "corporate_name": "Nomura Holdings, Inc."}
        self.assertTrue(self.check(f, "Nomura Holdings, Inc.\nIntegrated Report 2025\nNomura Holdings, Inc.", site="own")[0])
        f = {"company_name": "FAW", "website": "http://www.faw.com", "hq_country": "CHN", "corporate_name": "CHINA FAW GROUP CO., LTD."}
        self.assertTrue(self.check(f, "China FAW Group Co., Ltd.\nAnnual Report 2025", more="www.faw.com", site="doc_host")[0])
        f = {"company_name": "Dole", "website": "https://www.doleplc.com", "hq_country": "IRL", "corporate_name": "Dole plc"}
        self.assertTrue(self.check(f, "UNITED STATES SECURITIES AND EXCHANGE COMMISSION\n" + "x " * 150 + "FORM 10-K\nANNUAL REPORT\nDole plc",
                                   fname="Dole-plc-Form-10-K-Annual-Report-2025.pdf", site="doc_host")[0])

    def test_a_files_first_lines_say_it_is_something_else(self):
        f = {"company_name": "Hana Financial Group", "website": "https://www.hanafn.com", "hq_country": "KOR"}
        front = "DNV\nSecond Party Opinion\nHana Financial Group 2026 Allocation & Impact Report\nsustainability report"
        self.assertIn("something else (second party opinion)", self.check(f, front, fam="sustainability", site="own")[1])
        f = {"company_name": "Fidelity Investments", "website": "https://www.fidelity.com", "hq_country": "USA"}
        front = "Fidelity Enhanced Emerging Markets ETF\nAnnual Report\nFebruary 28, 2025\nFidelity Investments"
        self.assertIn("something else (etf annual report)", self.check(f, front, site="own")[1])
        f = {"company_name": "Nordzucker", "website": "https://www.nordzucker.com", "hq_country": "DEU"}
        front = "Nordzucker Post\nDas Magazin\nsustainability report 2023 ... Nordzucker"
        self.assertIn("something else (magazin)", self.check(f, front, fam="sustainability", site="own")[1])

    def test_a_name_of_common_words_needs_the_website_off_the_companys_site(self):
        f = {"company_name": "Future Fund", "website": "https://www.futurefund.gov.au", "hq_country": "AUS"}
        front = "Future Fund 2025\nUnder2 Coalition\nannual report of the Under2 Future Fund ... Future Fund"
        self.assertIn("a shared name, and futurefund.gov.au is not in the file", self.check(f, front)[1])
        self.assertTrue(self.check(f, "Future Fund\nAnnual Report 2025", more="www.futurefund.gov.au")[0])

    def test_own_site_files_are_opened_and_a_refused_one_is_passed_from_its_title(self):
        F = {"wba_id": "PT_1", "company_name": "Acme Group", "website": "acme.com", "sust_years": "", "coverage_breakdown": "", "hq_country": "USA"}
        hits = [{"link": "https://www.acme.com/ir/acme-annual-report-2025.pdf", "title": "Acme Annual Report 2025"},
                {"link": "https://www.acme.com/ir/acme-annual-report-2024.pdf", "title": "Acme Annual Report 2024"},
                {"link": "https://www.acme.com/ir/acme-integrated-report-2023.pdf", "title": "Acme Integrated Report 2023"}]
        covers = {hits[0]["link"]: ({"front": "Acme Group\nAnnual Report 2025", "more": "", "meta": "", "file": "acme-annual-report-2025.pdf"}, 150),
                  hits[1]["link"]: (None, "HTTP 403"),
                  hits[2]["link"]: ({"front": "Acme Group\nIntegrated Report", "more": "", "meta": "", "file": "r.pdf"}, 90)}
        api = Stub({'site:acme.com ("annual report"': hits})
        res = W.search_company(F, [], YEARS, api, reg_domain, opener=lambda u: covers[u])
        self.assertEqual(res["opened"], 3)
        self.assertEqual(res["found"], [(hits[0]["link"], "annual 2025, checked on the file's first pages"),
                                        (hits[1]["link"], "annual 2024; could not be opened (HTTP 403), passed on from its title"),
                                        (hits[2]["link"], "annual 2023, checked on the file's first pages")])   # the title's year when the cover has none


class ModelJudge(unittest.TestCase):
    """2 Nov 2026: the dry run of the rules on the 34 audited companies still passed a listed subsidiary's ESG
    report, an Indian subsidiary's BRSR, a forum's report, fund reports and a magazine. The model reads every
    cover the rules passed and must agree."""
    class Sess:
        def __init__(self, reply):
            self.reply, self.sent = reply, []

        def post(self, url, headers=None, json=None, timeout=None):
            self.sent.append(json)
            import json as J
            body = {"content": [{"type": "text", "text": J.dumps(self.reply)}], "usage": {"input_tokens": 900, "output_tokens": 40}}
            return type("R", (), {"status_code": 200, "json": lambda s: body, "text": "", "headers": {}})()

    F = {"company_name": "China Datang", "corporate_name": "China Datang Corporation Ltd.", "website": "http://en.china-cdt.com", "hq_country": "CHN"}
    TEXT = {"front": "Datang International Power Generation Co., Ltd.\nEnvironmental, Social and Governance Report 2023", "meta": "", "file": "e00991__ESG.pdf"}

    def run_judge(self, reply, fam="sustainability", years=(2025, 2024, 2023)):
        os.environ.setdefault("ANTHROPIC_API_KEY", "test")
        sess = self.Sess(reply)
        return W.model_judge(sess=sess)(self.F, fam, list(years), "https://mmx.prnewswire.com/media/2369954/e00991__ESG.pdf", self.TEXT), sess

    def test_a_subsidiary_is_refused(self):
        (ok, why), sess = self.run_judge({"publisher": "Datang International Power Generation Co., Ltd.", "is_company": False,
                                          "kind": "esg report", "period_end": "2023-12", "why": "listed subsidiary"})
        self.assertFalse(ok); self.assertIn("published by Datang International Power", why)
        prompt = sess.sent[0]["messages"][0]["content"]
        self.assertIn("legal name: China Datang Corporation Ltd.", prompt); self.assertIn("e00991__ESG.pdf", prompt)
        self.assertEqual(sess.sent[0]["system"], W.JUDGE_SYSTEM)

    def test_not_a_report_and_the_wrong_year_are_refused(self):
        (ok, why), _ = self.run_judge({"publisher": "China Datang", "is_company": True, "kind": "other", "period_end": "2023-07"})
        self.assertFalse(ok); self.assertIn("the judge: other", why)
        (ok, why), _ = self.run_judge({"publisher": "China Datang", "is_company": True, "kind": "esg report", "period_end": "2021-12"})
        self.assertFalse(ok); self.assertIn("not a missing year", why)
        (ok, why), _ = self.run_judge({"publisher": "China Datang", "is_company": True, "kind": "annual report", "period_end": "2024-12"})
        self.assertFalse(ok)                                         # an annual report is not a sustainability report
        (ok, why), sess = self.run_judge("not json")
        self.assertFalse(ok); self.assertTrue(why.startswith("the judge's reply was not JSON"), why)
        self.assertEqual(len(sess.sent), 2)                          # asked twice before refusing

    def test_the_companys_own_report_passes(self):
        (ok, why), _ = self.run_judge({"publisher": "China Datang Corporation Ltd.", "is_company": True, "kind": "sustainability report",
                                       "period_end": "2024-12"})
        self.assertTrue(ok); self.assertEqual(why, "the judge: sustainability report by China Datang Corporation Ltd.")
        (ok, _), _ = self.run_judge({"publisher": "China Datang", "is_company": True, "kind": "integrated report", "period_end": "2025-03"},
                                    fam="annual")
        self.assertTrue(ok)                                          # March 2025 is reporting year 2024

    def test_search_company_asks_the_judge_after_the_rules(self):
        F = {"wba_id": "PT_1", "company_name": "Acme Group", "website": "acme.com", "sust_years": "", "coverage_breakdown": "", "hq_country": "USA"}
        hits = [{"link": "https://www.acme.com/ir/acme-annual-report-2025.pdf", "title": "Acme Annual Report 2025"},
                {"link": "https://www.acme.com/ir/acme-annual-report-2024.pdf", "title": "Acme Annual Report 2024"}]
        cover = lambda u: ({"front": "Acme Group\nAnnual Report " + u[-8:-4], "more": "", "meta": "", "file": u.rsplit("/", 1)[1]}, 150)
        asked = []
        def cover_judge(f, fam, ys, link, text):
            asked.append(link)
            return ("2025" in link, "the judge: annual report by Acme Group" if "2025" in link else "the judge: published by Acme Foundation")
        res = W.search_company(F, [], YEARS, Stub({'site:acme.com ("annual report"': hits}), reg_domain, opener=cover, cover_judge=cover_judge)
        self.assertEqual(asked, [h["link"] for h in hits])
        self.assertEqual([u for u, _ in res["found"]], [hits[0]["link"]])
        self.assertIn("published by Acme Foundation", dict(res["not_added"])[hits[1]["link"]])

    def test_a_refused_file_on_a_page_domain_is_not_passed_from_its_title(self):
        F = {"wba_id": "PT_1", "company_name": "Fidelity Investments", "website": "https://www.fidelity.com", "sust_years": "",
             "coverage_breakdown": "", "hq_country": "USA"}
        hit = [{"link": "https://www.fidelityinvestments-reports.com/ar/fidelity-investments-annual-report-2025.pdf",
                "title": "Fidelity Investments Annual Report 2025"}]
        api = Stub({'(site:fidelity.com OR site:fidelityinvestments-reports.com) ("annual report"': hit})
        res = W.search_company(F, ["https://www.fidelityinvestments-reports.com/x"], YEARS, api, reg_domain,
                               opener=lambda u: (None, "HTTP 403"))
        self.assertEqual(res["found"], [])
