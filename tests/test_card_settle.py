import unittest

from qa_sample import settle

TODAY, CUT = "2026-10-26", "2026-10-23"
CLAUDE = {"suggested_value": "Interim Reports", "suggested_year": 2025, "claude_confidence": {"name": "high"},
          "batch_date": "2026-10-26", "in_review": True}


class Settle(unittest.TestCase):
    def test_agree_takes_claudes_answer(self):
        u = settle(dict(CLAUDE, your_call="Agree", suggested_title="Half-year report 2025"), TODAY, CUT)
        self.assertEqual((u["true_type"], u["true_year"], u["labelled_by"]), ("Interim Reports", 2025, "Edi"))
        self.assertTrue(u["title_wrong"] and u["reviewed"])
        self.assertFalse(u["in_review"])
        self.assertNotIn("sure", u)

    def test_disagree_keeps_the_correction_and_fills_the_rest(self):
        u = settle(dict(CLAUDE, your_call={"name": "Disagree"}, true_type="Annual Report"), TODAY, CUT)
        self.assertNotIn("true_type", u)          # the person's value is never overwritten
        self.assertEqual(u["true_year"], 2025)
        self.assertEqual(u["labelled_by"], "Edi")

    def test_cant_tell_is_not_sure(self):
        u = settle(dict(CLAUDE, your_call="Can't tell"), TODAY, CUT)
        self.assertIs(u["sure"], False)

    def test_old_way_still_counts_as_yours(self):
        u = settle({"true_type": "Annual Report", "true_year": 2024, "in_review": True, "batch_date": TODAY},
                   TODAY, CUT)
        self.assertEqual(u["labelled_by"], "Edi")
        self.assertNotIn("true_type", u)

    def test_left_out_by_the_pre_read_becomes_claudes_label(self):
        u = settle(dict(CLAUDE, in_review=False), TODAY, CUT)
        self.assertEqual((u["labelled_by"], u["true_type"], u["sure"]), ("Claude", "Interim Reports", True))

    def test_unanswered_for_three_days_becomes_claudes_label_sure_only_if_high(self):
        u = settle(dict(CLAUDE, batch_date="2026-10-21", claude_confidence="medium"), TODAY, CUT)
        self.assertEqual(u["labelled_by"], "Claude")
        self.assertIs(u["sure"], False)

    def test_waiting_for_you_stays(self):
        self.assertEqual(settle(dict(CLAUDE), TODAY, CUT), {})

    def test_not_pre_read_yet_stays_then_leaves_after_a_week(self):
        self.assertEqual(settle({"in_review": True, "batch_date": "2026-10-24"}, TODAY, CUT), {})
        self.assertEqual(settle({"in_review": True, "batch_date": "2026-10-10"}, TODAY, CUT), {"in_review": False})

    def test_reviewed_is_left_alone(self):
        self.assertEqual(settle({"reviewed": True, "true_type": "GRI"}, TODAY, CUT), {})


if __name__ == "__main__":
    unittest.main()
