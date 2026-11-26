"""own_migrate.py: what leaves Airtable, what changes there and what may be deleted, offline."""
import gzip, json, unittest

import own_migrate as M


def rec(i, **f):
    return {"id": f"rec{i}", "createdTime": "2026-11-20T00:00:00.000Z", "fields": f}


class Move(unittest.TestCase):
    def setUp(self):
        self.companies = [rec(1, wba_id="LN_aa", intake_note="added as LN_aa"), rec(2, wba_id="LN_bb", intake_note="added; moved to lantrNa's own lane (2026-11-26)")]
        self.pages = [rec(3, wba_id="LN_aa", url="https://a.example/r", monitor=True), rec(4, wba_id="LN_aa", url="https://a.example/old")]
        self.docs = [rec(5, wba_id="LN_aa", library_id="LN_aa-1", file=[{"url": "x"}], stored_file="archive/LN_aa/LN_aa-1.pdf (9 bytes)"),
                     rec(6, wba_id="LN_aa", library_id="LN_aa-2", file=[{"url": "y"}]), rec(7, wba_id="LN_bb", library_id="LN_bb-1", discard=True)]

    def test_only_ids_that_begin_pt_are_sdg2000(self):
        self.assertTrue(M.is_sdg("PT_00012")); self.assertTrue(M.is_sdg(" PT_1"))
        for w in ("LN_aa", "REQ_4", "", None, "pt_1"):
            self.assertFalse(M.is_sdg(w))

    def test_counts(self):
        c = M.inventory(self.companies, self.pages, self.docs)
        self.assertEqual((c["companies"], c["pages"], c["pages monitored"], c["documents"], c["documents discarded"], c["documents with a file"], c["documents with a file and no copy"]), (2, 2, 1, 3, 1, 2, 1))

    def test_what_changes_in_airtable_is_reversible_and_said_once(self):
        p = M.airtable_patches(self.companies, self.pages, self.docs, "2026-11-26")
        self.assertEqual(p["monitored_links"], [("rec3", {"monitor": False})])
        self.assertEqual(p["report_library"], [("rec5", {"discard": True, "discard_reason": M.MOVED}), ("rec6", {"discard": True, "discard_reason": M.MOVED})])
        self.assertEqual(p["companies"], [("rec1", {"intake_note": "added as LN_aa; moved to lantrNa's own lane (2026-11-26)"})])          # the second already says it

    def test_a_document_whose_file_has_no_copy_is_never_deleted(self):
        may, keep = M.deletable(self.docs)
        self.assertEqual([r["id"] for r in may], ["rec5", "rec7"]); self.assertEqual([r["id"] for r in keep], ["rec6"])

    def test_the_archive_holds_every_row_with_every_field(self):
        data = M.archive_bytes({"companies": self.companies[:1], "report_library": self.docs[:1]})
        lines = [json.loads(x) for x in gzip.decompress(data).decode("utf-8").splitlines()]
        self.assertEqual([(x["table"], x["id"]) for x in lines], [("companies", "rec1"), ("report_library", "rec5")]); self.assertEqual(lines[1]["fields"]["stored_file"], "archive/LN_aa/LN_aa-1.pdf (9 bytes)")
        self.assertEqual(data, M.archive_bytes({"companies": self.companies[:1], "report_library": self.docs[:1]}))          # the same rows, the same bytes


if __name__ == "__main__":
    unittest.main()
