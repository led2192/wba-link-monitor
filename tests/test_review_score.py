"""Offline tests for review_score.py: the reviewers' decisions counted per method and attribute, the notes
grouped into patterns, the corrected values and the pace."""
import datetime as dt, os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import review_score as R  # noqa: E402


def row(**f):
    base = {"wba_id": "PT_1", "attribute": "GMC_00048.1", "attribute_name": "Revenue", "year": 2024,
            "method": {"name": "SEC XBRL"}, "status": {"name": "rejected"}, "matches_validated": {"name": "differs"},
            "reviewer_note": "", "value": 1.0, "validated_value": 2.0, "unit": "USD", "reviewed_on": "2026-10-30T11:00:00.000Z"}
    base.update(f)
    return {"id": "rec", "fields": base}


class Score(unittest.TestCase):
    def test_counts_notes_corrections_and_pace(self):
        rows = [row(reviewer_note="Validated value is correct. We prefer sales than net sales when both figures presented."),
                row(status={"name": "accepted"}, matches_validated={"name": "match"}, reviewed_on="2026-10-20T10:00:00.000Z"),
                row(reviewer_note="Validated value is wrong, it's 2023FY.", status={"name": "accepted"}),
                row(method={"name": "model reader"}, attribute="GMC_00001.1", attribute_name="Scope 1 emissions", unit="tCO2e",
                    reviewer_note="x1000 parsing error; this report gives 14,408,000 for 2024", corrected_value=14408000),
                row(status={"name": "proposed"})]                                   # not reviewed: ignored
        s = R.score(rows, today=dt.date(2026, 10, 31))
        rev = s["by"][("SEC XBRL", "GMC_00048.1 Revenue")]
        self.assertEqual((rev["reviewed"], rev["accepted"], rev["rejected"]), (3, 2, 1))
        self.assertEqual((rev["differs accepted"], rev["match accepted"], rev["differs rejected"]), (1, 1, 1))
        self.assertEqual(s["by"][("model reader", "GMC_00001.1 Scope 1 emissions")]["rejected"], 1)
        self.assertEqual(set(s["notes"]), {"another line of the statement (total, net sales, gross)", "a scale or unit"})
        self.assertEqual(len(s["corrected"]), 1); self.assertIn("corrected to 14408000", s["corrected"][0])
        self.assertEqual((s["weeks"][0], s["weeks"][1]), (3, 1))
        md = R.report(s)
        self.assertIn("| SEC XBRL | GMC_00048.1 Revenue | 3 | 2 | 1 | 67% | 0 of 1 | 1 of 2 |", md)
        self.assertIn("**another line of the statement", md)
        self.assertNotIn("reviewed_by", md)

    def test_patterns(self):
        self.assertEqual(R.pattern_of("Validated value is wrong, it's 2023FY."), "the validated answer is wrong")
        self.assertEqual(R.pattern_of("Unable to find value in the report."), "the figure was not found")
        self.assertEqual(R.pattern_of("took the Europe segment"), "a segment or subsidiary, not the group")
        self.assertEqual(R.pattern_of("meh"), "other")


if __name__ == "__main__":
    unittest.main()
