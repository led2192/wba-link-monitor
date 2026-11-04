import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import anchor_capture_eval as A
from monitor_core import normalize

HUB = """<html><body>
<a href="/en/node/4411">Annual Report 2024</a>
<a href="/en/annual-report-2023">Annual Report 2023</a>
<a href="/files/held.pdf">Sustainability Report 2024 (PDF)</a>
<a href="/en/node/9">Contact us</a>
<a href="/en/node/10">Download</a>
<a href="https://other-co.com/x">Annual report of a supplier</a>
<a href="https://acme.com/held-page">Code of Conduct</a>
</body></html>"""
MICROSITE = """<html><body><a href="/docs/ar-2023-full.pdf">Full report (PDF)</a>
<a href="/docs/old.pdf">Old</a></body></html>"""


class Resp:
    def __init__(self, code, ctype, body, url):
        self.status_code, self.headers, self._body, self.url, self.encoding = code, {"Content-Type": ctype}, body, url, "utf-8"
    def iter_content(self, n):
        yield self._body
    def close(self):
        pass


class Session:
    def get(self, url, **kw):
        if url.endswith("/reports"):
            return Resp(200, "text/html", HUB.encode(), url)
        if url.endswith("/en/node/4411"):
            return Resp(200, "application/octet-stream", b"%PDF-1.7 ...", url)        # direct: address says nothing
        if url.endswith("/en/annual-report-2023"):
            return Resp(200, "text/html; charset=utf-8", MICROSITE.encode(), url)    # one hop
        if url.endswith("/held-page"):
            return Resp(404, "text/html", b"", url)
        return Resp(200, "text/html", b"<html></html>", url)


class Capture(unittest.TestCase):
    def test_named(self):
        self.assertTrue(A.named("Annual Report 2024"))
        self.assertTrue(A.named("Relatório de Sustentabilidade"))
        self.assertFalse(A.named("Download"))
        self.assertFalse(A.named("Contact us"))

    def test_candidates_skip_held_pdfs_generic_and_other_domains(self):
        held = {normalize("https://acme.com/files/held.pdf")}
        got = A.candidates(HUB, "https://acme.com/reports", held, 12)
        urls = [u for u, _, _ in got]
        self.assertIn("https://acme.com/en/node/4411", urls)
        self.assertIn("https://acme.com/en/annual-report-2023", urls)
        self.assertNotIn("https://acme.com/files/held.pdf", urls)          # .pdf addresses: the harvester takes them
        self.assertFalse(any("other-co.com" in u for u in urls))             # another site, not document-shaped
        shaped = {u: s for u, _, s in got}
        self.assertFalse(shaped["https://acme.com/en/node/4411"])
        self.assertTrue(shaped["https://acme.com/en/annual-report-2023"])  # DOCISH path: today's rules take it

    def test_direct_and_one_hop_finds(self):
        held = {normalize("https://acme.com/docs/old.pdf")}
        pages = [{"url": "https://acme.com/reports", "type": "reports_hub", "company": "Acme"}]
        rows, c = A.company_run("PT_1", pages, held, 12, deadline=9e12, session_factory=Session, pause=0)
        finds = {r["find"]: r for r in rows if r["find"]}
        self.assertEqual(finds["direct"]["found_pdf"], "https://acme.com/en/node/4411")
        self.assertEqual(finds["one hop"]["found_pdf"], "https://acme.com/docs/ar-2023-full.pdf")
        self.assertEqual(finds["direct"]["label_year"], "2024")                # the document's label, not its reporting year
        self.assertEqual(len([r for r in rows if r["find"] == "one hop"]), 1)  # the held old.pdf is not a find
        self.assertEqual(c["pages read"], 1)

    def test_spread_is_repeatable(self):
        ids = [f"PT_{i:05d}" for i in range(100)]
        self.assertEqual(A.spread(ids, 10), A.spread(list(reversed(ids)), 10))
        self.assertEqual(len(A.spread(ids, 0)), 100)

    def test_summary(self):
        rows = [{"wba_id": "PT_1", "company": "Acme", "find": "direct", "type_named": "Annual Report",
                 "link_text": "Annual Report 2024", "found_pdf": "https://acme.com/en/node/4411"}]
        md = "\n".join(A.summary(rows, {"pages read": 1, "page not read": 0, "named links opened": 3}, 1, False))
        self.assertIn("| direct | 1 | 1 |", md)


if __name__ == "__main__":
    unittest.main()
