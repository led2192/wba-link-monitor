import datetime as dt
import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import pub_dates as P

D = dt.date


class Dates(unittest.TestCase):
    def test_publication_statements(self):
        end = D(2024, 12, 31)
        t = P.find_dates([(2, "Date of publication: 4 March 2025. Contact us at ir@example.com")], end, D(2025, 6, 1))
        self.assertEqual(t[:3], (2, D(2025, 3, 4), "day"))
        t = P.find_dates([(1, "This report was published in March 2025 and covers the year")], end, None)
        self.assertEqual(t[:3], (2, D(2025, 3, 1), "month"))
        t = P.find_dates([(5, "The Sustainability Report of Stadler Rail AG covers the reporting period from 1 January 2024 "
                               "to 31 December 2024 and was published on 19 March 2025.")], end, None)
        self.assertEqual(t[:3], (2, D(2025, 3, 19), "day"))

    def test_dates_of_other_documents_are_not_the_publication(self):
        # 24 Oct 2026 dry run: a delegated act, a press release, a bond notice, by-laws
        end = D(2024, 12, 31)
        pages = [(15, "The Omnibus I package changes are in a Delegated Act, the final version of which was published on January 8, 2025."),
                 (93, "According to the press release published March 13th, 2025, no evidence has been found."),
                 (44, "63rd Corporate Bonds (Green Bond) Publication date: May 29, 2025 Total issuance amount"),
                 (107, "On February 27, 2025, the Board approved and adopted amendments to the Company's Restated By-Laws.")]
        pages.append((4, "For example, the prior 2024 Economic Impact Report, which was published April 2025, presented data."))
        pages += [(i, "Text") for i in range(1, 5)] + [(i, "Text") for i in range(200, 204)]
        self.assertIsNone(P.find_dates(pages, end, None))

    def test_board_approval(self):
        end = D(2025, 3, 31)
        t = P.find_dates([(140, "The financial statements were authorised for issue by the Board of Directors on 27 May 2025 and signed")], end, None)
        self.assertEqual(t[:3], (3, D(2025, 5, 27), "day"))
        t = P.find_dates([(80, "On 26 February 2025, the Board of Directors approved the annual report.")], D(2024, 12, 31), None)
        self.assertEqual(t[:3], (3, D(2025, 2, 26), "day"))
        t = P.find_dates([(197, "The Standalone Financial Statements have been approved by the Board of Directors in its meeting held on 11th May, 2025.")], D(2025, 3, 31), None)
        self.assertEqual(t[:3], (3, D(2025, 5, 11), "day"))

    def test_publication_beats_approval_and_implausible_dates_are_ignored(self):
        end = D(2024, 12, 31)
        pages = [(3, "The financial statements were approved by the Board on 20 February 2025."), (2, "Publication date: 10 March 2025")]
        self.assertEqual(P.find_dates(pages, end, None)[:2], (2, D(2025, 3, 10)))
        # a date inside the period, or after the robot saw the file, is not the publication
        self.assertIsNone(P.find_dates([(1, "Published on 1 June 2024")], end, None))
        self.assertIsNone(P.find_dates([(1, "Published on 1 June 2025")], end, D(2025, 4, 1)))

    def test_period_end(self):
        self.assertEqual(P.period_end_date("2025-03", 2025), D(2025, 3, 31))
        self.assertEqual(P.period_end_date("2024-12", 2024), D(2024, 12, 31))
        self.assertEqual(P.period_end_date("", 2024), D(2024, 1, 1))      # only the year: no tighter floor


    def test_a_sentence_about_the_earlier_edition_is_not_this_reports_date(self):
        # Bank Mandiri, 2025 Sustainability Report (26 Oct 2026): dated March 2025 from its predecessor
        text = ("Reporting Period This report presents information on the Company's operations across Indonesia for the "
                "period from 1 January to 31 December 2025 and serves as a continuation of the 2024 Sustainability Report, "
                "which was published in March 2025.")
        self.assertIsNone(P.find_dates([(39, text)], D(2025, 12, 31), None, 2025))
        # the subject rule alone, even when the period end is unknown
        self.assertIsNone(P.find_dates([(39, "The 2024 Sustainability Report was published in June 2025.")], None, None, 2025))
        # this edition, named with its own year, still counts
        t = P.find_dates([(2, "The 2025 Sustainability Report was published on 12 March 2026.")], D(2025, 12, 31), None, 2025)
        self.assertEqual(t[:2], (2, D(2026, 3, 12)))
        # a report labelled with its publication year keeps its date (26 Oct 2026: removed by a 31 March floor)
        t = P.decide({"year": 2026}, [(132, "This report, confirmed by management, was approved by the Board of Directors "
                                            "on March 30, 2026 and released concurrently.")], None)
        self.assertEqual(t[:2], (3, D(2026, 3, 30)))


class Decide(unittest.TestCase):
    def test_the_period_the_report_states_is_a_floor(self):
        # no period_end in Airtable: the text's own period decides what cannot be the publication date
        pages = [(3, "This Sustainability Report for the period 1 April 2025 to 31 March 2026 provides enhanced disclosure."),
                 (40, "Our previous report was published in March 2026."), (41, "Published: June 2026")]
        pages += [(i, "Text") for i in range(36, 40)]
        self.assertEqual(P.stated_period_end(pages, 2026, None), D(2026, 3, 31))
        self.assertEqual(P.decide({"year": 2026}, [(3, pages[0][1]), (40, "This report was published in March 2026.")], None), None)
        self.assertEqual(P.decide({"year": 2026}, [(3, pages[0][1]), (41, "This report was published in June 2026.")], None)[:2],
                         (2, D(2026, 6, 1)))
        # targets and old comparatives do not move it
        self.assertIsNone(P.stated_period_end([(9, "reduce emissions over the period to 31 December 2030")], 2025, None))
        # plans, comparisons and forecasts are not the report's period (26 Oct 2026 dry run removed correct dates)
        for t in ("the programme runs until 31 December 2025", "net debt at year end compared to 31 December 2025",
                  "forecast for the fiscal year ending March 31, 2026", "valid from 1 January 2025 to 30 June 2025"):
            self.assertIsNone(P.stated_period_end([(1, t)], 2025, None), t)
        self.assertEqual(P.stated_period_end([(39, "This report presents information for the period from 1 January to "
                                                    "31 December 2025 and serves")], 2025, None), D(2025, 12, 31))
        # a bond's interest period is not the report's (Hua Xia Bank 2023 annual report)
        self.assertIsNone(P.stated_period_end([(94, "perpetual bonds. The interest accrual period is from 26 June 2023 to "
                                                    "25 June 2024 (interest rate at 4.85%)")], 2023, None))
        self.assertEqual(P.stated_period_end([(1, "for the year ended 31 December 2025 and the year ended 31 December 2024")],
                                             2025, None), D(2025, 12, 31))

    def test_regulator_filing_first(self):
        f = {"Reviewer notes": "Added from the SEC regulator database (filed 2025-02-21). ", "period_end": "2024-12"}
        self.assertEqual(P.decide(f, [(1, "Published on 1 March 2025")], None)[:3], (1, D(2025, 2, 21), "day"))

    def test_first_seen_only_after_the_initial_collection(self):
        watched = D(2026, 6, 10)
        self.assertEqual(P.decide({"period_end": "2026-03"}, [], D(2026, 8, 10), page_added=watched)[:2], (4, D(2026, 8, 10)))
        self.assertIsNone(P.decide({"period_end": "2025-12"}, [], D(2026, 3, 10), page_added=watched))      # initial collection
        self.assertIsNone(P.decide({"period_end": "2024-12"}, [], D(2026, 6, 20), page_added=watched))

    def test_first_seen_needs_a_page_watched_before_and_a_fresh_file(self):
        f = {"period_end": "2026-03"}
        self.assertIsNone(P.decide(f, [], D(2026, 8, 10)))                                   # no source page
        self.assertIsNone(P.decide(f, [], D(2026, 8, 10), page_added=D(2026, 8, 5)))         # page just added
        old_file = dict(f, file_created="2026-05-02", file_modified="2026-05-20")
        self.assertIsNone(P.decide(old_file, [], D(2026, 8, 10), page_added=D(2026, 6, 10)))  # file made in May
        fresh = dict(f, file_modified="2026-08-01")
        self.assertEqual(P.decide(fresh, [], D(2026, 8, 10), page_added=D(2026, 6, 10))[0], 4)


# Macy's 10-K for the year ended 31 January 2026 on its investor site (Q4 hosting, CIK in the address):
# first seen 18 July 2026; EDGAR: filed 27 March 2026, accession 0001628280-26-021721 (26 Oct 2026).
MACYS = {"period_end": "2026-01", "source_type_check": "10K Form", "year": 2026,
         "document_url": "https://d18rn0p25nwr6d.cloudfront.net/CIK-0000794367/1e901e63-ca0e-4c09-9a79-f2eba09e87e7.pdf",
         "doc_label": "2026 | Macy's | Macy's, Inc. Form 10-K Annual Report for the Fiscal Year Ended January 31, 2026"}
MACYS_FILINGS = [("10-Q", "0001628280-26-050001", "2026-08-01", "2026-09-05"),
                 ("8-K", "0001628280-26-021000", "2026-03-26", "2026-03-26"),
                 ("10-K", "0001628280-26-021721", "2026-01-31", "2026-03-27"),
                 ("10-K", "0000794367-25-000010", "2025-02-01", "2025-03-21")]


class SecFilings(unittest.TestCase):
    def test_cik_from_the_address_or_the_company(self):
        self.assertEqual(P.sec_cik(MACYS), "794367")
        self.assertEqual(P.sec_cik({"document_url": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/a.htm"}), "320193")
        self.assertEqual(P.sec_cik({"document_url": "https://example.com/ar.pdf"}, "1234"), "1234")
        self.assertIsNone(P.sec_cik({"document_url": "https://example.com/ar.pdf"}, "none"))

    def test_annual_filing_of_the_same_period_gives_the_date(self):
        t = P.decide(MACYS, [(1, "FORM 10-K")], D(2026, 7, 18), MACYS_FILINGS, D(2026, 6, 10))
        self.assertEqual(t[:3], (1, D(2026, 3, 27), "day"))
        self.assertIn("0001628280-26-021721", t[3])

    def test_accession_named_in_the_title_wins(self):
        f = dict(MACYS, period_end="2026", doc_label="0001628280-26-021721")
        t = P.decide(f, [], None, MACYS_FILINGS)
        self.assertEqual(t[:2], (1, D(2026, 3, 27)))

    def test_no_filing_of_that_period_is_no_date_from_edgar(self):
        f = dict(MACYS, period_end="2024-02")
        self.assertIsNone(P.sec_match(f, P.period_end_date("2024-02", 2024), MACYS_FILINGS, None))
        # a year-only period end cannot pick a filing by month
        self.assertIsNone(P.sec_match(dict(MACYS, period_end="2026"), P.period_end_date("2026", 2026), MACYS_FILINGS, None))

    def test_signature_date_when_edgar_has_nothing(self):
        pages = [(1, "UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. 20549 FORM 10-K"),
                 (104, "Pursuant to the requirements of Section 13 or 15(d) of the Securities Exchange Act of 1934, the "
                       "registrant has duly caused this report to be signed on its behalf by the undersigned, thereunto duly "
                       "authorized. MACY'S, INC. By: /s/ Tony Spring Chairman and Chief Executive Officer Date: March 27, 2026")]
        f = {"period_end": "2026-01", "source_type": "Annual Report", "document_url": "https://example.com/10k.pdf"}
        t = P.decide(f, pages, D(2026, 7, 18), [], D(2026, 6, 10))
        self.assertEqual(t[:3], (5, D(2026, 3, 27), "day"))
        f20 = {"period_end": "2025-12", "source_type_check": "10K Form"}
        t = P.decide(f20, [(210, "The registrant hereby certifies that it meets all of the requirements for filing on Form 20-F "
                                 "and that it has duly caused and authorized the undersigned to sign this annual report on its "
                                 "behalf. Date: 2 April 2026")], None)
        self.assertEqual(t[:2], (5, D(2026, 4, 2)))

    def test_archive_capture_is_an_upper_bound(self):
        f = {"period_end": "2014-12", "Reviewer notes": "Added from the Wayback Machine (captured 2015-09-05). Original address: http://x/a.pdf"}
        self.assertEqual(P.decide(f, [], D(2026, 10, 27))[:3], (4, D(2015, 9, 5), "day"))
        f = {"period_end": "2016-12", "Reviewer notes": "Added from the Wayback Machine (captured 2015-09-05). Original address: http://x/a.pdf"}
        self.assertIsNone(P.decide(f, [], D(2026, 10, 27)))                       # captured before the period ended
        f = {"period_end": "2014-12", "Reviewer notes": "Added from the Wayback Machine (captured 2015-09-05). Original address: http://x/a.pdf"}
        t = P.decide(f, [(3, "This report was published on 20 March 2015.")], D(2026, 10, 27))
        self.assertEqual(t[:2], (2, D(2015, 3, 20)))                               # a stated date still wins

    def test_signature_rule_is_only_for_sec_forms(self):
        f = {"period_end": "2025-12", "source_type": "Sustainability Report"}
        self.assertIsNone(P.decide(f, [(9, "the registrant has duly caused this report to be signed. Date: March 2, 2026")], None))


if __name__ == "__main__":
    unittest.main()
