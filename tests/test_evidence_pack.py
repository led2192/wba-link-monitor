"""Offline tests for evidence_pack.py and pack_eval --save: no network, no database (stubbed page search)."""
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import evidence_pack as EP  # noqa: E402
import pack_eval as P       # noqa: E402

MODEL = {"pack": 2, "per_page": 1, "avg": 20.0, "idf": {"board": 2.0, "oversight": 2.0, "committee": 1.5},
         "attributes": {"GMC_01930.1": {"all": {"terms": ["committee"]}, "2": {"terms": []}}}}
PAGES = [("D1", 3, "The board has oversight of climate matters. The board meets quarterly."),
         ("D1", 4, "Our sustainability committee reviews progress each year."),
         ("D2", 7, "Unrelated text about logistics and shipping routes.")]


class Terms(unittest.TestCase):
    def test_fold_and_all(self):
        self.assertEqual(EP.terms_for(MODEL, "GMC_01930.1"), ["committee"])
        self.assertEqual(EP.terms_for(MODEL, "GMC_01930.1", 2), [])
        self.assertEqual(EP.terms_for(MODEL, "GMC_01930.1", 4), [])       # no part for the fold: no words
        self.assertEqual(EP.terms_for(None, "GMC_01930.1"), [])


class Build(unittest.TestCase):
    def build(self, fold=None, model=MODEL):
        with mock.patch.object(P, "year_docs", return_value=[("D1", "Annual Report", 2024), ("D2", "Policy", 2023)]), \
             mock.patch.object(P, "candidate_pages", return_value=PAGES):
            return EP.build(None, "PT_00001", 2024, 12, "GMC_01930.1", ["board oversight"], model, fold)

    def test_the_pack_takes_the_best_passages_one_a_page(self):
        pas, chosen, docs = self.build()
        self.assertEqual(len(chosen), 2)
        self.assertEqual([pas[j][1] for j in chosen], [3, 4])            # the expansion word brings page 4
        self.assertEqual(docs["D2"], ("Policy", 2023))

    def test_without_the_learned_word_only_query_pages(self):
        pas, chosen, _ = self.build(fold=2)
        self.assertEqual([pas[j][1] for j in chosen], [3])

    def test_no_model_still_scores_the_query_words(self):
        pas, chosen, _ = self.build(model=None)
        self.assertTrue(chosen)

    def test_no_documents(self):
        with mock.patch.object(P, "year_docs", return_value=[]):
            self.assertEqual(EP.build(None, "PT_1", 2024, 12, "GMC_01930.1", ["x"], MODEL), ([], [], {}))


class Save(unittest.TestCase):
    def test_parts_for_every_fold_and_no_company(self):
        cases = []
        for f in range(P.R.FOLDS):
            for k in range(3):
                cases.append({"g": {"attribute": "GMC_01930.1", "kind": "narrative", "queries": ["board oversight"], "wba": f"PT_0{f}{k}"},
                              "fold": f, "target": {0}, "marks": {"committee": 1.0, "governance": 0.5}})
        m = P.save_terms(cases, {"board": 2.0, "committee": 1.5, "governance": 1.0}, 30.0)
        parts = m["attributes"]["GMC_01930.1"]
        self.assertEqual(set(parts), {"0", "1", "2", "3", "4", "all"})
        self.assertEqual(parts["2"]["answers"], 12)
        self.assertIn("committee", parts["all"]["terms"])
        self.assertIn("board", m["idf"])
        self.assertNotIn("PT_0", str(m))


if __name__ == "__main__":
    unittest.main()
