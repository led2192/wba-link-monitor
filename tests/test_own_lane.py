"""own_lane.py: the pure parts of the lane for the companies that are not SDG2000, offline."""
import io, os, unittest

import own_lane as L


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self._b, self.headers = status, body, headers or {}
    def iter_content(self, n):
        yield self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class Sess:
    def __init__(self, resp):
        self.resp, self.asked = resp, []
    def get(self, url, **kw):
        self.asked.append((url, kw.get("headers", {}).get("User-Agent")))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


PDF = b"%PDF-1.7 " + b"x" * 200 + b"\n%%EOF\n"


class Lane(unittest.TestCase):
    def test_where_to_start(self):
        self.assertEqual(L.home_of("acme.example"), "https://acme.example"); self.assertEqual(L.home_of(" http://a.example/x "), "http://a.example/x")
        self.assertEqual(L.home_of(""), ""); self.assertEqual(L.home_of("acme"), "")

    def test_the_documents_of_a_page_are_registered_once_under_the_librarys_id(self):
        from ids import doc_id
        links = {"k1": ("https://acme.example/files/annual-report-2025.pdf", "Annual report 2025"), "k2": ("https://acme.example/investors/results", "Results"),
                 "k3": ("https://cdn.example.net/acme/sustainability-2025.PDF?v=2", " Sustainability  ")}
        got = L.new_docs("LN_ab12cd34", links, set())
        self.assertEqual([g[1] for g in got], ["https://acme.example/files/annual-report-2025.pdf", "https://cdn.example.net/acme/sustainability-2025.PDF?v=2"])      # a page that is not a PDF is not a document yet
        self.assertEqual(got[0][0], doc_id("LN_ab12cd34", links["k1"][0])); self.assertEqual(got[1][2], "Sustainability")
        self.assertEqual(L.new_docs("LN_ab12cd34", links, {got[0][0], got[1][0]}), [])          # known: nothing twice

    def test_type_year_and_label(self):
        typ, year, label = L.describe("https://acme.example/files/annual-report-2024.pdf", "Annual Report 2024", "Acme Group\nAnnual Report 2024")
        self.assertEqual((typ, year, label), ("Annual Report", 2024, "Annual Report 2024"))
        typ, year, label = L.describe("https://acme.example/dl/7781.pdf", "", "")
        self.assertEqual((typ, year, label), (None, None, "7781.pdf"))          # nothing says what it is: no type and no year are invented
        row = L.document_row("LN_x-1", "LN_x", "Acme", "https://a.example/a.pdf", "Annual Report", 2024, "AR", "ok", 120, 5000, "2026-11-26T20:00:00+00:00")
        self.assertEqual(len(row), 16); self.assertEqual((row[0], row[2], row[4], row[5], row[11], row[12], row[15]), ("LN_x-1", "LN_x", "Annual Report", 2024, "ok", "own", "2026-11-26"))

    def test_fetching_a_document(self):
        s = Sess(Resp(200, PDF, {"Content-Length": str(len(PDF))}))
        self.assertEqual(L.fetch_doc(s, "https://a.example/a.pdf", "bot/1.0"), ("ok", PDF)); self.assertEqual(s.asked, [("https://a.example/a.pdf", "bot/1.0")])      # as the robot, by its name
        self.assertEqual(L.fetch_doc(Sess(Resp(403)), "u", "b")[0], "refused"); self.assertEqual(L.fetch_doc(Sess(Resp(404)), "u", "b")[0], "download_fail")
        self.assertEqual(L.fetch_doc(Sess(Resp(200, b"<html>no</html>")), "u", "b")[0], "not_pdf")
        self.assertEqual(L.fetch_doc(Sess(Resp(200, PDF[:-8])), "u", "b")[0], "truncated")
        self.assertEqual(L.fetch_doc(Sess(Resp(200, PDF, {"Content-Length": "999999999"})), "u", "b")[0], "too_big")
        self.assertEqual(L.fetch_doc(Sess(OSError("x")), "u", "b")[0], "download_fail")

    def test_the_file_is_kept_in_the_storage_and_on_the_disk(self):
        import tempfile

        class Arch:
            def __init__(self, answer): self.answer, self.got = answer, []
            def upload(self, path, data):
                self.got.append((path, len(data)))
                if isinstance(self.answer, Exception): raise self.answer
                return self.answer
        with tempfile.TemporaryDirectory() as d:
            a = Arch("ok")
            self.assertEqual(L.keep_file(PDF, "LN_ab12cd34", "LN_ab12cd34|a.example/x.pdf", a, d), f"archive/LN_ab12cd34/LN_ab12cd34_a.example_x.pdf.pdf ({len(PDF)} bytes) + disk")
            self.assertEqual(a.got, [("LN_ab12cd34/LN_ab12cd34_a.example_x.pdf.pdf", len(PDF))])
            self.assertEqual(open(os.path.join(d, "LN_ab12cd34", "LN_ab12cd34_a.example_x.pdf.pdf"), "rb").read(), PDF)
            self.assertEqual(L.keep_file(PDF, "LN_x", "LN_x|b", Arch("too large"), d), "disk")          # refused by the storage: on the disk only
            self.assertEqual(L.keep_file(PDF, "LN_x", "LN_x|c", Arch(RuntimeError("down")), None), None)          # kept nowhere: said, and the reading goes on
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "own_lane.py"), encoding="utf-8").read()
        self.assertLess(src.index('os.environ.setdefault("AIRTABLE_TOKEN", "none'), src.index("import requests, active_search as AS"))          # 26 Nov: the first run stopped at this import

    def test_nothing_of_the_lane_is_written_to_airtable(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "own_lane.py"), encoding="utf-8").read()
        self.assertNotIn("airtable_request", src); self.assertNotIn('environ["AIRTABLE', src); self.assertNotIn("secrets.AIRTABLE_TOKEN", src)
        self.assertIn("q.wba_id not like 'PT\\\\_%%'", src)          # an SDG2000 company never enters the lane


if __name__ == "__main__":
    unittest.main()
