import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import base_room as B  # noqa: E402

TODAY = date(2026, 11, 9)
KEYS = {"Annual Report", "Sustainability Report", "Code of Conduct"}


def doc(**f):
    base = {"library_id": "L", "year": 2024, "source_type_check": "Annual Report", "first_seen": "2024-01-01"}
    base.update(f)
    return base


class BaseRoom(unittest.TestCase):
    def test_year_and_kind(self):
        self.assertEqual(B.year_of({"year": 2023}), 2023)
        self.assertEqual(B.year_of({"year": "2021"}), 2021)
        self.assertIsNone(B.year_of({"year": "#ERROR!"}))
        self.assertIsNone(B.year_of({}))
        self.assertEqual(B.year_group(2015), "2019 or earlier")
        self.assertEqual(B.year_group(2027), "2026")
        self.assertEqual(B.year_group(None), "no year")
        self.assertEqual(B.kind_of({"source_type_check": "Annual Report"}, KEYS), "key type")
        self.assertEqual(B.kind_of({"source_type_check": "Company Presentations"}, KEYS), "other type")
        self.assertEqual(B.kind_of({"source_type_check": "UNKNOWN"}, KEYS), "no type")
        self.assertEqual(B.kind_of({}, KEYS), "no type")

    def test_rules_never_touch_cited_verified_imported_or_key_reports(self):
        old = doc(year=2019, source_type_check="Company Presentations")
        self.assertEqual(B.rules_for(old, set(), KEYS, TODAY), "AB")
        self.assertEqual(B.rules_for(old, {"L"}, KEYS, TODAY), "")
        self.assertEqual(B.rules_for(dict(old, verified=True), set(), KEYS, TODAY), "")
        self.assertEqual(B.rules_for(dict(old, origin="Source Library 2024"), set(), KEYS, TODAY), "")
        self.assertEqual(B.rules_for(dict(old, key_role="financial"), set(), KEYS, TODAY), "")
        self.assertEqual(B.rules_for(dict(old, discard=True), set(), KEYS, TODAY), "")

    def test_rules_a_b_c_d(self):
        self.assertEqual(B.rules_for(doc(year=2021, source_type_check="Annual Report"), set(), KEYS, TODAY), "A")   # old key type: A, not B
        self.assertEqual(B.rules_for(doc(year=2022, source_type_check="Policy Documents"), set(), KEYS, TODAY), "")
        self.assertEqual(B.rules_for(doc(year=None, source_type_check="UNKNOWN", first_seen="2026-01-01"), set(), KEYS, TODAY), "C")
        self.assertEqual(B.rules_for(doc(year=None, source_type_check="UNKNOWN", first_seen="2026-10-01"), set(), KEYS, TODAY), "")  # not stale yet
        self.assertEqual(B.rules_for(doc(year=None, source_type_check="ESG Report", first_seen="2026-01-01"), set(), KEYS, TODAY), "")  # typed: kept
        self.assertEqual(B.rules_for(doc(file_status="blocked", first_seen="2026-01-01"), set(), KEYS, TODAY), "D")
        self.assertEqual(B.rules_for(doc(file_status="web_document", first_seen="2026-01-01"), set(), KEYS, TODAY), "")
        self.assertEqual(B.rules_for(doc(year=2018, file_status="gone", first_seen="2025-01-01"), set(), KEYS, TODAY), "AD")

    def test_analyse_counts_and_candidates(self):
        docs = [doc(library_id="a", year=2024), doc(library_id="b", year=2019, source_type_check="Policy Documents"),
                doc(library_id="c", discard=True, discarded_on="2026-09-01T00:00:00.000Z"), doc(library_id="d", year=2020, verified=True),
                doc(library_id="e", year=None, source_type_check="", first_seen="2025-12-01"), doc(library_id="f", year=2024, first_seen="2026-11-01")]
        c, by, nofile, cands = B.analyse(docs, {"a"}, KEYS, TODAY)
        self.assertEqual((c["rows"], c["live"], c["discarded"], c["purge_backlog"]), (6, 5, 1, 1))
        self.assertEqual(c["cited"], 1)
        self.assertEqual(c["new_30d"], 1)
        self.assertEqual(by["2024"]["total"], 2); self.assertEqual(by["2024"]["key type"], 2); self.assertEqual(by["2024"]["cited"], 1)
        self.assertEqual(by["2020"]["verified"], 1)
        self.assertEqual(sorted(l for l, *_ in cands), ["b", "e"])      # a cited, d verified, f recent; e is stale and untyped (rule C)
        self.assertEqual(c["candidates"], len(cands))

    def test_rule_c_needs_a_stale_row(self):
        c, _, _, cands = B.analyse([doc(library_id="e", year=None, source_type_check="", first_seen="2025-12-01")], set(), KEYS, TODAY)
        self.assertEqual(cands[0][1], "C")

    def test_report_mentions_every_rule_and_table(self):
        c, by, nofile, cands = B.analyse([doc(library_id="b", year=2019, source_type_check="Policy Documents")], set(), KEYS, TODAY)
        md = B.report_md({"report_library": 1, "attribute_values": 2}, c, by, nofile, cands, TODAY)
        for s in ("| report_library | 1 |", "A: reporting year 2021 or earlier | 1 |", "B: rule A and not a key type | 1 |",
                  "All rules together | 1 |", "2019 or earlier | 1 |", "no intake in 30 days"):
            self.assertIn(s, md, s)
        self.assertNotIn("—", md)


if __name__ == "__main__":
    unittest.main()
