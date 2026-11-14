import os, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test"); os.environ.setdefault("AIRTABLE_BASE", "appTEST")
import build_cover_text as B


class Reply:
    def __init__(self, j): self._j = j
    def json(self): return self._j


class HashBackfill(unittest.TestCase):
    def setUp(self):
        self.keep = (B.airtable_request, B.download_pdf, B.HASH_MINUTES)
        self.events = []

    def tearDown(self):
        B.airtable_request, B.download_pdf, B.HASH_MINUTES = self.keep

    def stub(self, pages, kinds):
        def request(method, url, headers, payload=None, params=None):
            if method == "GET":
                offset = dict(params or []).get("offset")
                self.events.append("list")
                at = int(offset or 0)
                rows = pages[at]
                return Reply({"records": rows, **({"offset": str(at + 1)} if at + 1 < len(pages) else {})})
            self.events.append(("write", [r["id"] for r in payload["records"]]))
            return Reply({})
        def download(url):
            self.events.append("file")
            kind = kinds.get(url, "ok")
            return kind, (b"%PDF-1.7 body" if kind == "ok" else b"")
        B.airtable_request, B.download_pdf = request, download

    def test_the_queue_is_listed_before_any_file_is_fetched_and_failures_are_named(self):
        # 14 Nov 2026: files that failed slowly kept one listing open for 62 minutes and Airtable dropped it
        row = lambda i: {"id": f"rec{i}", "fields": {B.FILE_F: [{"url": f"u{i}"}]}}
        self.stub([[row(1), row(2)], [row(3)]], {"u2": "truncated", "u3": "download_fail"})
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            B.run_hash_backfill(0, True)
        lists = [i for i, e in enumerate(self.events) if e == "list"]
        files = [i for i, e in enumerate(self.events) if e == "file"]
        self.assertEqual(len(lists), 2); self.assertLess(max(lists), min(files))          # both pages listed first
        self.assertIn(("write", ["rec1"]), self.events)                                    # the one good file gets its hash
        self.assertIn("'truncated': 1", out.getvalue()); self.assertIn("'download_fail': 1", out.getvalue())
        self.assertIn("3 of 3 rows", out.getvalue())

    def test_a_run_stops_at_its_time_and_says_what_is_left(self):
        row = lambda i: {"id": f"rec{i}", "fields": {B.FILE_F: [{"url": f"u{i}"}]}}
        self.stub([[row(i) for i in range(150)]], {})
        B.HASH_MINUTES = -1                                                                # the time is already up
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            B.run_hash_backfill(0, True)
        self.assertNotIn("file", self.events)
        self.assertIn("150 rows left for the next run", out.getvalue())

    def test_a_row_with_no_file_address_is_a_named_failure(self):
        self.assertEqual(B.hash_one({"id": "r", "fields": {}}), ("r", None, "no attachment address"))


if __name__ == "__main__":
    unittest.main()
