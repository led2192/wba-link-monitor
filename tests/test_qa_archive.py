"""Offline tests for qa_archive.py."""
import gzip, json, os, sys, tempfile, unittest

os.environ["QA_STRICT"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qa_archive as A  # noqa: E402


class ArchiveTests(unittest.TestCase):
    def test_formula_only_closed_and_old(self):
        f = A.formula("2026-07-24")
        for s in ("fixed", "resolved_silently", "wontfix"):
            self.assertIn(f"{{status}}='{s}'", f)
        self.assertNotIn("'open'", f); self.assertNotIn("'expected'", f)
        self.assertIn("IS_BEFORE({last_seen}, '2026-07-24')", f)

    def test_archive_round_trip(self):
        rows = [{"id": "rec1", "createdTime": "2026-01-01T00:00:00.000Z", "fields": {"failure_key": "k", "message": "ñ é"}}]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.jsonl.gz")
            self.assertGreater(A.write_archive(rows, p), 0)
            back = [json.loads(l) for l in gzip.open(p, "rt", encoding="utf-8")]
        self.assertEqual(back, rows)


if __name__ == "__main__":
    unittest.main()
