"""Offline test for mention_search.TextSource.run: the store is downloaded and scanned in batches of
about BATCH_MB, each batch deleted after its scan, and the hits of all batches merged (28 Oct 2026: the
whole store no longer fits on a runner's disk)."""
import os, sys, tempfile, unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mention_search as M  # noqa: E402


class Batches(unittest.TestCase):
    def test_batches_are_scanned_merged_and_deleted(self):
        work = tempfile.mkdtemp()
        src = M.TextSource.__new__(M.TextSource)
        names = [f"pages-x-0{i}.jsonl.gz" for i in range(5)]
        src.work, src.workers, src.store = work, 1, object()
        src.page_files = names
        src.listing = {n: {"size": 1_000_000_000} for n in names}          # 1,000 MB each
        src.meta, src.owner, src.index = {}, {}, {}
        downloaded, present_at_scan = [], []

        def sync_down(store, batch, folder):
            for n in batch:
                open(os.path.join(folder, n), "w").close()
            downloaded.append(list(batch))
            return len(batch)

        def scan_store(folder, batch, queries, meta, owner, workers):
            present_at_scan.append(sorted(os.listdir(folder)))
            return {0: {f"doc-{n}": {"id": f"doc-{n}"} for n in batch}}

        with mock.patch.object(M, "BATCH_MB", 2500), \
             mock.patch.object(M.tc, "sync_down", sync_down), mock.patch.object(M.tc, "scan_store", scan_store):
            out = src.run([mock.Mock(in_scope=lambda m: True)])
        self.assertEqual([len(b) for b in downloaded], [2, 2, 1])            # 2,500 MB at a time
        self.assertEqual([len(p) for p in present_at_scan], [2, 2, 1])       # earlier batches already deleted
        self.assertEqual(os.listdir(work), [])
        self.assertEqual(len(out[0]["hits"]), 5)


if __name__ == "__main__":
    unittest.main()


class OpenLink(unittest.TestCase):
    """3 Nov 2026: 28 of the 207 hits of the first climate search had no link, because their documents came in
    from the Source Library as files with no address. Those open the library row, where the PDF is attached."""
    def test_a_document_with_an_address_opens_on_its_page(self):
        self.assertEqual(M.open_url({"url": "https://x.com/r.pdf", "rec": "recA"}, 39), "https://x.com/r.pdf#page=39")

    def test_a_file_without_an_address_opens_its_library_row(self):
        with mock.patch.dict(os.environ, {"AIRTABLE_BASE": "appTEST"}):
            self.assertEqual(M.open_url({"url": "", "rec": "recA"}, 39), f"https://airtable.com/appTEST/{M.LIB_TABLE_ID}/recA")
        self.assertEqual(M.open_url({"url": "", "rec": ""}, 39), "")
