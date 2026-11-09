"""Offline tests for rerank_train.py: pairs from validated answers, the model never trained on evaluated answers."""
import collections, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rerank_train as RT  # noqa: E402
import extract_eval as X  # noqa: E402


class Pairs(unittest.TestCase):
    def test_attribute_text_has_no_labels(self):
        q = RT.attribute_text({"name": "Revenue", "definition": "Total revenue of the group.", "labels": ["net sales", "turnover"]})
        self.assertEqual(q, "Revenue. Total revenue of the group."); self.assertNotIn("turnover", q)

    def test_pairs_for_answer_positives_and_hard_negatives(self):
        class FakeX:
            SPEC = X.SPEC
            @staticmethod
            def year_documents(cur, wba, year, attr): return [("d1", "Annual Report", 2024)], {"d1": 1.0}
            @staticmethod
            def ranked_pages(cur, ids, weight, year, attr, spec):
                return [(3.0, "d1", 7, "Revenue 5,147 4,892", None), (2.0, "d1", 9, "Costs 1,000", None), (1.0, "d1", 11, "People 12,000", None)]
        class Cur:
            def execute(self, q, params=None): self.rows = [("d1", 7, "Revenue 5,147 4,892"), ("d1", 8, "Revenue 5,147 again")]
            def fetchall(self): return self.rows
        g = {"id": "PT_1-GMC_00048.1-2024", "wba": "PT_1", "year": 2024, "value": 5147000000.0}
        attr = {"name": "Revenue", "definition": "x", "unit_family": "currency", "labels": ["revenue"]}
        pairs = RT.pairs_for_answer(Cur(), g, attr, {"attributes": {}}, FakeX)
        self.assertEqual([y for _, _, y in pairs], [1, 1, 0, 0])            # two holding pages, then the pool's non-holding pages
        self.assertTrue(all(q == "Revenue. x" for q, _, _ in pairs))
        self.assertEqual(RT.pairs_for_answer(Cur(), dict(g, value=None), attr, {"attributes": {}}, FakeX), [])

    def test_build_pairs_skips_the_evaluated_answers(self):
        from unittest import mock
        calls = []
        def gold_values(token, base, codes): return [{"id": f"{c}-{i}", "wba": f"PT_{i}", "year": 2024, "value": 1.0 * i, "attribute": c} for c in codes for i in range(1, 8)]
        with mock.patch("value_eval.gold_values", gold_values), mock.patch.object(RT, "pairs_for_answer", lambda cur, g, attr, spec, X: calls.append(g["id"]) or [("q", "p", 1)]):
            pairs, counts = RT.build_pairs(None, "t", {"attributes": {"GMC_00048.1": {"type": "number"}}}, ["b"], skip=5, per_attribute=10, X=X)
        self.assertEqual(calls, ["GMC_00048.1-6", "GMC_00048.1-7"])        # the first five are the evaluations'
        self.assertEqual((len(pairs), counts["GMC_00048.1"]), (2, 2))

    def test_ensure_model_reuses_an_existing_directory(self):
        import tempfile
        d = tempfile.mkdtemp(); open(os.path.join(d, "config.json"), "w").write("{}")
        self.assertEqual(RT.ensure_model(None, "t", {"attributes": {}}, [], d, X), d)


if __name__ == "__main__":
    unittest.main()
