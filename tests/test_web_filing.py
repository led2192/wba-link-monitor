"""Offline tests for web_filing.py: which addresses are filings, and how a filing's HTML becomes pages."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_filing as W  # noqa: E402

TENK = """<html><body>
<div style="display:none"><ix:header><ix:references>dei:EntityCentralIndexKey 0000050863</ix:references></ix:header></div>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION. Form 10-K. Intel Corporation, annual report for the fiscal year ended December 28, 2024.</p>
<hr style="page-break-after:always"/>
<p>Item 7. Management's discussion and analysis of financial condition and results of operations.</p>
<table><tr><td>Net revenue</td><td>53,101</td><td>54,228</td></tr></table>
<div style="break-before: page"><p>Item 8. Financial statements and supplementary data. Consolidated statements of income.</p></div>
<p>3</p>
</body></html>"""


class Filing(unittest.TestCase):
    def test_filing_urls(self):
        self.assertTrue(W.is_filing_url("https://www.sec.gov/Archives/edgar/data/50863/000005086325000009/intc-20241228.htm"))
        self.assertTrue(W.is_filing_url("https://filings.xbrl.org/549300B8P6MUJ1YWTS08/2024-12-31/ESEF/FR/0/x/reports/x.xhtml"))
        self.assertFalse(W.is_filing_url("https://www.sec.gov/ix?doc=/Archives/edgar/data/1/2/x.htm"))   # the viewer
        self.assertFalse(W.is_filing_url("https://www.merckgroup.com/report.html"))
        self.assertFalse(W.is_filing_url(None))

    def test_pages_follow_the_printed_page_breaks(self):
        pages = W.html_pages(TENK)
        self.assertEqual([n for n, _ in pages], [1, 2, 3])
        self.assertIn("fiscal year ended December 28, 2024", pages[0][1])
        self.assertIn("Net revenue 53,101 54,228", pages[1][1])                  # table cells keep apart
        self.assertTrue(pages[2][1].startswith("Item 8."))
        self.assertTrue(pages[2][1].rstrip().endswith("3"))                       # a lone page number joins a page
        self.assertFalse(any("EntityCentralIndexKey" in t for _, t in pages))     # the hidden XBRL header is dropped

    def test_no_page_breaks_gives_blocks(self):
        html = "<html><body>" + "".join(f"<p>{'Revenue grew in the year. ' * 40}</p>" for _ in range(10)) + "</body></html>"
        pages = W.html_pages(html, chunk=3000)
        self.assertGreater(len(pages), 2)
        self.assertTrue(all(len(t) < 6000 for _, t in pages))

    def test_empty(self):
        self.assertEqual(W.html_pages(""), [])


if __name__ == "__main__":
    unittest.main()
