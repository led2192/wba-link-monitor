"""Google Drive file links (Decathlon, 18 Nov 2026).

sustainability.decathlon.com/annual-sustainable-development-reports links every report as
drive.google.com/file/d/<id>/view: a viewer page on another domain, with no .pdf. The link was
dropped before harvest saw it, and a link added by hand was filed as a page to watch.
Offline: the network is a stub.
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doc_probe as DP
import harvest_reports as HR
import monitor_core as MC
from ids import normalize

NFRD_2024 = "https://drive.google.com/file/d/1gZXdnYcWWweIqV5z1mAgppubfazTILxg/view"
NFRD_2022 = "https://drive.google.com/file/d/1ZzGAlw2W1FK5GrOJzzjGvoXsyzAyZkQa/view?usp=sharing"
ESG_2025 = "https://drive.google.com/file/d/1HJKEylzOx3caZgVWQFkUtPrOsnS3oALf/view"
PAGE = "https://sustainability.decathlon.com/annual-sustainable-development-reports"
DIRECT_2024 = ("https://drive.usercontent.google.com/download?id=1gZXdnYcWWweIqV5z1mAgppubfazTILxg"
               "&export=download&confirm=t")
OLD_2024 = "https://drive.google.com/uc?export=download&id=1gZXdnYcWWweIqV5z1mAgppubfazTILxg"


class Resp:
    def __init__(self, body, ctype="text/html; charset=utf-8", code=200, url=""):
        self.body, self.status_code, self.url = body, code, url
        self.headers = {"Content-Type": ctype}
        self.text = body.decode("utf-8", "ignore")
    def iter_content(self, n):
        yield self.body[:n]
    def close(self):
        pass


class Sess:
    """Answers a PDF for the addresses in `pdfs`, an HTML page for every other one."""
    def __init__(self, pdfs=(), code=200):
        self.pdfs, self.code, self.asked = set(pdfs), code, []
    def get(self, url, **k):
        self.asked.append(url)
        if url in self.pdfs:
            return Resp(b"%PDF-1.7\n...", "application/octet-stream", url=url)
        return Resp(b"<html><title>FINAL_Decathlon_NFRD2024.pdf - Google Drive</title></html>",
                    code=self.code, url=url)


class DriveFileLinks(unittest.TestCase):
    def test_the_id_is_read_from_a_file_link_and_keeps_its_case(self):
        self.assertEqual(MC.drive_file_id(NFRD_2024), "1gZXdnYcWWweIqV5z1mAgppubfazTILxg")
        self.assertEqual(MC.drive_file_id(NFRD_2022), "1ZzGAlw2W1FK5GrOJzzjGvoXsyzAyZkQa")
        self.assertEqual(MC.drive_file_id(OLD_2024), "1gZXdnYcWWweIqV5z1mAgppubfazTILxg")

    def test_folders_documents_and_other_hosts_are_not_files(self):
        for u in ("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
                  "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit",
                  "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit",
                  "https://example.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view",
                  "https://drive.google.com/", "", None):
            self.assertEqual(MC.drive_file_id(u), "", u)
            self.assertEqual(MC.drive_downloads(u), [], u)

    def test_download_addresses_carry_the_id(self):
        self.assertEqual(MC.drive_downloads(NFRD_2024), [DIRECT_2024, OLD_2024])


class PageMonitor(unittest.TestCase):
    HTML = f"""<html><body>
      <a href="{ESG_2025}">Download our 2025 ESG Report</a>
      <a href="{NFRD_2024}">Download Declaration of extra financial performance 2024</a>
      <a href="{NFRD_2022}">Download Declaration of extra financial performance 2022</a>
      <a href="https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345">All files</a>
      <a href="https://www.instagram.com/decathlon/">Instagram</a>
    </body></html>"""

    def test_drive_files_linked_from_the_company_page_are_kept(self):
        links = MC.doc_links(self.HTML, PAGE)
        kept = {u for u, _ in links.values()}
        self.assertEqual(kept, {ESG_2025, NFRD_2024, NFRD_2022})
        self.assertEqual(links[normalize(NFRD_2024)][1],
                         "Download Declaration of extra financial performance 2024")


class Probe(unittest.TestCase):
    def test_a_drive_file_link_is_probed_first(self):
        self.assertEqual(DP.priority(NFRD_2024), 0)
        self.assertEqual(DP.priority(OLD_2024), 0)

    def test_the_viewer_is_not_a_pdf_and_the_download_address_is(self):
        s = Sess(pdfs=[DIRECT_2024])
        self.assertEqual(DP.check(NFRD_2024, s), ("pdf", DIRECT_2024))

    def test_the_older_download_address_is_the_fallback(self):
        s = Sess(pdfs=[OLD_2024])
        self.assertEqual(DP.check(NFRD_2024, s), ("pdf", OLD_2024))

    def test_a_file_that_is_not_a_pdf_or_not_shared_stays_not(self):
        self.assertEqual(DP.check(NFRD_2024, Sess()), ("not", ""))

    def test_a_refused_viewer_still_tries_the_download_address(self):
        class Refusing(Sess):
            def get(self, url, **k):
                if url == NFRD_2024:
                    return Resp(b"", code=403, url=url)
                return super().get(url, **k)
        self.assertEqual(DP.check(NFRD_2024, Refusing(pdfs=[DIRECT_2024])), ("pdf", DIRECT_2024))

    def test_other_links_are_never_sent_to_drive(self):
        s = Sess()
        self.assertEqual(DP.check("https://example.com/download/18265", s), ("not", ""))
        self.assertEqual(s.asked, ["https://example.com/download/18265"])


class Harvest(unittest.TestCase):
    def tearDown(self):
        HR.CONFIRMED_DOCS.clear()

    def test_a_confirmed_drive_link_is_harvested_at_its_download_address(self):
        row = {HR.F_TYPE: "reports_hub", HR.F_URL: PAGE, HR.F_SEEN: NFRD_2024 + "\n" + ESG_2025}
        self.assertEqual(list(HR.pdfs_from_row(row, {"reports_hub"})), [])     # not probed yet
        HR.CONFIRMED_DOCS[normalize(NFRD_2024)] = DIRECT_2024
        self.assertEqual(list(HR.pdfs_from_row(row, {"reports_hub"})), [(DIRECT_2024, PAGE)])


class AddedByHand(unittest.TestCase):
    def test_a_drive_link_added_by_hand_is_a_document_not_a_page(self):
        import inbox
        self.assertEqual(inbox.what_is(NFRD_2024, Sess(pdfs=[DIRECT_2024])), ("pdf", DIRECT_2024))

    def test_a_drive_link_that_is_no_pdf_stays_a_page(self):
        import inbox
        kind, title = inbox.what_is(NFRD_2024, Sess())
        self.assertEqual(kind, "page")
        self.assertIn("Google Drive", title)


if __name__ == "__main__":
    unittest.main()
