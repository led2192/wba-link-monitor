import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import evidence_health as E  # noqa: E402
import qa  # noqa: E402

DOCS = {"total": 100, "by_status": {"ok": 90, "no_text": 10}, "with_pages": 90, "no_type": 5, "ok_without_pages": 0,
        "by_year": {"2023": 40, "2024": 50, "no year": 10}}
PMAP = {"lines": 1000, "embedded": 1000, "documents": 20, "last_24h": 100, "by_year": {2023: 400, 2024: 500, 2025: 100}}
VALS = {"by_method": {"model reader": 50}, "last_24h": 5, "last_3d": 20, "orphans": 0, "reads_24h": 7, "reads_found_24h": 5}


class EvidenceHealth(unittest.TestCase):
    def test_every_check_passes_on_a_healthy_database(self):
        self.assertEqual(E.checks(DOCS, PMAP, VALS, 0), [])
        self.assertIn("Every check passed", E.report_md({"documents": 100, "pages": 5000}, DOCS, PMAP, VALS, 0, [], date(2026, 11, 9)))

    def test_the_map_year_bug_and_missing_embeddings_are_caught(self):
        bad = dict(PMAP, embedded=900, by_year={2023: 100, 2024: 200, 2025: 400, 2026: 300})
        names = [n for n, _ in E.checks(DOCS, bad, VALS, 0)]
        self.assertEqual(names, ["map_embeddings", "map_years"])

    def test_documents_pages_versions_and_orphans(self):
        failed = E.checks(dict(DOCS, ok_without_pages=3), PMAP, dict(VALS, last_3d=0, orphans=2), 12000)
        self.assertEqual([n for n, _ in failed], ["docs_without_pages", "pages_unindexed", "stale_versions", "orphan_versions"])
        self.assertIn("10,000 or more", dict(failed)["pages_unindexed"])
        self.assertEqual([n for n, _ in E.checks(DOCS, PMAP, dict(VALS, last_3d=0), 0, nightly_values=False)], [])

    def test_data_check_is_a_quality_code(self):
        self.assertIn("DATA_CHECK", qa.CODES)
        self.assertEqual(qa.DEFAULT_SEVERITY["DATA_CHECK"], "High")
        md = E.report_md({"documents": 100}, DOCS, PMAP, VALS, 0, [("map_years", "x")], date(2026, 11, 9))
        self.assertIn("1 check(s) failed", md); self.assertIn("**map_years**", md); self.assertNotIn("—", md)


if __name__ == "__main__":
    unittest.main()
