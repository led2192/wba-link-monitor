import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import archive_documents as A  # noqa: E402

TODAY = date(2026, 11, 9)
KEYS = {"Annual Report", "Sustainability Report"}


def doc(rid, **f):
    base = {"library_id": rid, "wba_id": "PT_1", "year": 2019, "source_type_check": "Company Presentations", "created_time": "2024-01-01T00:00:00.000Z"}
    base.update(f)
    return rid, base


class ArchiveDocuments(unittest.TestCase):
    def test_rule_b_leaves_key_types_cited_verified_imported_and_key_reports(self):
        docs = [doc("a"), doc("b", source_type_check="Annual Report"), doc("c", year=2022), doc("d", verified=True),
                doc("e", origin="Source Library 2025"), doc("f", key_role="financial"), doc("g", discard=True), doc("h")]
        b = A.candidates(docs, {"h"}, KEYS, TODAY, "B")
        self.assertEqual([rid for rid, _ in b], ["a"])                  # b key type, c too recent, d e f g h out
        a = A.candidates(docs, {"h"}, KEYS, TODAY, "A")
        self.assertEqual([rid for rid, _ in a], ["a", "b"])             # rule A also takes the old annual report

    def test_only_periodic_documents_with_a_type_are_candidates(self):
        docs = [doc("a"), doc("p", source_type_check="Policy Documents"), doc("c", source_type_check="Code of Conduct"),
                doc("s", source_type_check="Statutory Documents"), doc("z", source_type_check="Certifications"),
                doc("u", source_type_check=""), doc("k", source_type_check="UNKNOWN"), doc("i", source_type_check="Interim Reports")]
        self.assertEqual([rid for rid, _ in A.candidates(docs, set(), KEYS, TODAY, "B")], ["a", "i"])
        self.assertEqual([rid for rid, _ in A.candidates(docs, set(), KEYS, TODAY, "A")], ["a", "i"])   # a 2019 policy may be the one in force

    def test_the_text_must_be_in_the_evidence_database(self):
        class Cur:
            def __init__(self): self.asked = []
            def execute(self, sql, args): self.asked.append(args[0]); self.rows = [(i,) for i in args[0] if i != "gone"]
            def fetchall(self): return self.rows
        cur = Cur()
        self.assertEqual(A.with_text(cur, ["a", "gone", "b", None, "a"], batch=2), {"a", "b"})
        self.assertEqual(cur.asked, [["a", "b"], ["gone"]])                                        # unique, in batches

    def test_the_size_of_the_files_is_reported(self):
        self.assertAlmostEqual(A.size_gb([doc("a", file_size_mb=512), doc("b", file_size_mb=1536), doc("c")]), 2.0)

    def test_payloads_tick_discard_with_the_reason_in_batches_of_ten(self):
        cands = [doc(f"r{i}") for i in range(23)]
        bodies = list(A.payloads(cands))
        self.assertEqual([len(b["records"]) for b in bodies], [10, 10, 3])
        self.assertEqual(bodies[0]["records"][0], {"id": "r0", "fields": {"discard": True, "discard_reason": A.ARCHIVE_REASON}})
        self.assertTrue(all(b["typecast"] for b in bodies))
        self.assertTrue(all(set(r["fields"]) == {"discard", "discard_reason"} for b in bodies for r in b["records"]))   # no link field

    def test_report_counts_and_marks_the_dry_run(self):
        cands = [doc("a"), doc("b", year=2018, source_type_check="Policy Documents", wba_id="PT_2")]
        md = A.report_md(cands, "B", TODAY)
        self.assertIn("2 live documents meet rule B", md)
        self.assertIn("| Company Presentations | 1 |", md); self.assertIn("| 2018 | 1 |", md)
        self.assertIn("Dry run: nothing written", md)
        self.assertIn("Written: discard ticked on 2 rows", A.report_md(cands, "B", TODAY, committed=2))
        self.assertNotIn("—", md)


if __name__ == "__main__":
    unittest.main()
