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



class ProviderTier(unittest.TestCase):
    def test_tiers_and_auto_attributes(self):
        from unittest import mock
        import narrative_eval as N
        calls = []
        def fake_get_all(token, base, table, fields, formula=None):
            calls.append((base, table, formula))
            if table == "Metadata" and "GMC Type" in fields:
                return [{"fields": {"Metadata ID": f"PT_0000{i}-GMC_00566.1-2024", "GMC Type": ["Boolean"]}} for i in range(5)] + \
                       [{"fields": {"Metadata ID": "PT_00001-GMC_00863.1-2024", "GMC Type": ["Multiple Select"]}}]
            if table == "Metadata Codes":
                return [{"fields": {"Global Metadata Code (GMC)": "GMC_00566.1", "Description": "Does the company require FPIC",
                                    "Data Type": "Boolean"}},
                        {"fields": {"Global Metadata Code (GMC)": "GMC_00863.1", "Description": "Reporting coverage",
                                    "Data Type": "Multiple Select", "Select Options": "Own operations; Value chain"}}]
            return [{"fields": {"Metadata ID": "PT_00001-GMC_00566.1-2024", "Selected Value": "true"}},
                    {"fields": {"Metadata ID": "PT_00001-GMC_00566.1-2024", "Selected Value": "true"}}]   # the same answer in two bases
        with mock.patch.object(N, "get_all", side_effect=fake_get_all):
            cfg = N.auto_attributes("t", ["appA"], "provider", 2, set())
            got = N.gold_answers("t", ["GMC_00566.1"], ["appA", "appB"], "provider")
        self.assertEqual(list(cfg), ["GMC_00566.1", "GMC_00863.1"])
        self.assertEqual(cfg["GMC_00863.1"]["options"], ["Own operations", "Value chain"])
        self.assertEqual(cfg["GMC_00566.1"]["type"], "boolean")
        self.assertEqual(len(got), 1)                                     # deduplicated across bases
        self.assertIn("QC Complete", calls[-1][2]); self.assertIn("NOT({Data Reviewed})", calls[-1][2])

    def test_a_refused_base_is_skipped_and_codes_come_from_the_next(self):
        from unittest import mock
        import requests
        import narrative_eval as N
        N.SKIPPED.clear()
        def fake_get_all(token, base, table, fields, formula=None):
            if base == "appOld":
                r = requests.Response(); r.status_code = 403
                raise requests.HTTPError("403 Client Error: Forbidden", response=r)
            if table == "Metadata Codes":
                return [{"fields": {"Global Metadata Code (GMC)": "GMC_00566.1", "Description": "Does the company require FPIC",
                                    "Data Type": "Boolean"}}]
            if "GMC Type" in fields:
                return [{"fields": {"Metadata ID": "PT_00001-GMC_00566.1-2024", "GMC Type": ["Boolean"]}}]
            return [{"fields": {"Metadata ID": "PT_00001-GMC_00566.1-2024", "Selected Value": "true"}}]
        with mock.patch.object(N, "get_all", side_effect=fake_get_all):
            cfg = N.auto_attributes("t", ["appOld", "appA"], "provider", 1, set())
            got = N.gold_answers("t", ["GMC_00566.1"], ["appOld", "appA"], "provider")
        self.assertEqual(list(cfg), ["GMC_00566.1"])
        self.assertEqual(len(got), 1)
        self.assertIn(("appOld", "Metadata", "HTTP 403"), N.SKIPPED)
        self.assertNotIn("app6oIEJuBV9fkwux", N.MONTHLY_BASES.values())

    def test_other_errors_still_stop_the_run(self):
        from unittest import mock
        import requests
        import narrative_eval as N
        r = requests.Response(); r.status_code = 500
        with mock.patch.object(N, "get_all", side_effect=requests.HTTPError("500", response=r)):
            with self.assertRaises(requests.HTTPError):
                N.read_base("t", "appA", "Metadata", [])


if __name__ == "__main__":
    unittest.main()
