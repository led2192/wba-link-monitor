"""Offline tests for narrative_eval.py (3 Nov 2026: the first judgement attributes, governance of sustainability)."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collections, narrative_eval as N  # noqa: E402

BOOL = {"type": "boolean", "question": "Is the board responsible?", "name": "x"}
LIST = {"type": "multiple select", "question": "Which?", "options": ["Sustainability", "Climate mitigation", "Transition Plan"], "name": "y"}


class Truth(unittest.TestCase):
    def test_no_evidence_is_a_no_and_unclear_rows_are_left_out(self):
        self.assertIs(N.truth_of("true", "", "boolean"), True)
        self.assertIs(N.truth_of("", "No Evidence", "boolean"), False)
        self.assertEqual(N.truth_of("", "No Evidence", "multiple select"), frozenset())
        self.assertIsNone(N.truth_of("", "Non-English Evidence", "boolean"))
        self.assertIsNone(N.truth_of("", "Insufficient Evidence", "multiple select"))
        self.assertEqual(N.truth_of("Sustainability, Climate mitigation", "", "multiple select"), {"Sustainability", "Climate mitigation"})

    def test_ids(self):
        self.assertEqual(N.gold_id("PT_02130-GMC_01930.1-2024"), ("PT_02130", "GMC_01930.1", 2024))


class Replies(unittest.TestCase):
    def test_yes_no_and_options_are_read_case_blind_and_unknown_options_dropped(self):
        self.assertEqual(N.read_reply({"answer": "Yes", "page": 12, "quote": "The Board oversees"}, BOOL), (True, 12, "The Board oversees"))
        self.assertEqual(N.read_reply({"answer": "no"}, BOOL)[0], False)
        a, _, _ = N.read_reply({"answer": ["sustainability", "Transition plan", "Biodiversity"]}, LIST)
        self.assertEqual(a, {"Sustainability", "Transition Plan"})
        self.assertEqual(N.read_reply(None, BOOL), (None, None, ""))

    def test_a_cut_reply_keeps_its_answer(self):
        cut = '{"answer": "yes", "page": 12, "quote": "The Board of Directors oversees the sustainability strategy and'
        self.assertEqual(N.salvage(cut), {"answer": "yes"})
        self.assertEqual(N.salvage('{"answer": ["Sustainability", "Climate mitigation"], "page": 4, "quote": "The ESG'),
                         {"answer": ["Sustainability", "Climate mitigation"]})
        self.assertIsNone(N.salvage("I cannot tell from these pages."))
        self.assertEqual(N.read_reply({"values": [], "cut": True}, BOOL), (None, None, ""))

    def test_scores(self):
        s = collections.Counter()
        self.assertTrue(N.score(s, BOOL, True, True)); self.assertFalse(N.score(s, BOOL, False, True))
        self.assertEqual((s["answers"], s["right"], s["truth yes"], s["said yes"], s["yes and right"]), (2, 1, 1, 2, 1))
        t = collections.Counter()
        N.score(t, LIST, frozenset({"Sustainability"}), frozenset({"Sustainability", "Climate mitigation"}))
        self.assertEqual((t["right"], t["option Sustainability: right"], t["option Climate mitigation: right"]), (0, 1, 0))

    def test_query_and_prompt(self):
        self.assertEqual(N.or_query(["board oversight of climate", "TCFD governance"]), "(board & oversight & climate) | (tcfd & governance)")
        p = N.prompt(LIST, [("L1", "Sustainability Report", 7, "The ESG committee leads...")], "PT_1")
        self.assertIn("Options: Sustainability; Climate mitigation; Transition Plan", p)
        self.assertIn('<page document="Sustainability Report" number="7">', p)


if __name__ == "__main__":
    unittest.main()
