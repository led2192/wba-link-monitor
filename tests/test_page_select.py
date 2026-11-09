"""Offline tests for page_select.py (the learned page choice) and extract_eval --select learned, with a stub
trained model and no embedder: no network, no database."""
import json
import os
import sys
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extract_eval as E   # noqa: E402
import page_select as P    # noqa: E402
import select_eval as SE   # noqa: E402

ATTR = {"name": "Revenue", "queries": ["revenue", "net sales"]}


def pool(n, cand=()):
    """n ranked pages of documents A (even pages) and B (odd pages); pages in cand carry a rule candidate."""
    return [(10 - i, "A" if i % 2 == 0 else "B", i + 1, f"page {i + 1} revenue 1,234 text",
             (5.0, 1, 1, 1) if i in cand else None) for i in range(n)]


TYPES = {"A": "sustainability report", "B": "annual report"}


def model(type_weight=5.0, lift=None, fold_key="all"):
    """A trained model that only looks at the document-type prior (feature 3)."""
    w = [0.0] * len(P.FEATURE_NAMES) + [0.0]
    w[3] = type_weight
    part = {"w": w, "lift": lift or {"annual report": 4.0, "sustainability report": 0.5}, "terms": [], "rows": 10}
    return {"avg_len": 100, "idf": {}, "attributes": {"GMC_00048.1": {fold_key: part}}}


class Order(unittest.TestCase):
    def test_the_learned_prior_puts_the_annual_report_first(self):
        o = P.order(model(), "GMC_00048.1", ATTR, pool(10), TYPES)
        self.assertEqual(sorted(o), list(range(10)))
        self.assertTrue(all(i % 2 == 1 for i in o[:5]), o)        # document B (annual report) pages first

    def test_nothing_trained_keeps_todays_order(self):
        self.assertIsNone(P.order(model(), "GMC_00001.1", ATTR, pool(10), TYPES))
        self.assertIsNone(P.order(None, "GMC_00048.1", ATTR, pool(10), TYPES))
        self.assertIsNone(P.order(model(), "GMC_00048.1", ATTR, [], TYPES))

    def test_a_validated_answer_uses_the_part_trained_without_its_fold(self):
        m = model(fold_key="3")
        self.assertIsNotNone(P.order(m, "GMC_00048.1", ATTR, pool(6), TYPES, fold=3))
        self.assertIsNone(P.order(m, "GMC_00048.1", ATTR, pool(6), TYPES, fold=2))      # never another fold's part
        self.assertIsNone(P.order(m, "GMC_00048.1", ATTR, pool(6), TYPES, fold=None))   # "all" not trained here

    def test_select_eval_scores_the_same_features(self):
        p = pool(5)
        case = {"pool": p, "types": TYPES}
        exp = [0.0] * 5
        self.assertEqual(SE.features(case, 2, {"annual report": 2.0}, exp, None),
                         P.features(p, TYPES, 2, {"annual report": 2.0}, exp, None))


class ModelPages(unittest.TestCase):
    def tearDown(self):
        E.SELECT, E.SELECT_MODEL, E.EMBEDDER = "base", None, None

    def scored(self, p):
        return [((top[0] if top else -99) - 0.05 * i, top, lid, page, body) for i, (_, lid, page, body, top) in enumerate(p)]

    def test_base_is_pages_for_model(self):
        p = pool(25, cand={20})
        sc = self.scored(p)
        self.assertEqual(E.model_pages(sc, "GMC_00048.1", ATTR, p, [("A", "sustainability report"), ("B", "annual report")], 1),
                         E.pages_for_model(sc, E.MODEL_PAGES))

    def test_learned_changes_only_the_rank_half(self):
        p = pool(25, cand={20})
        sc = self.scored(p)
        E.SELECT, E.SELECT_MODEL = "learned", model()
        got = E.model_pages(sc, "GMC_00048.1", ATTR, p, [("A", "sustainability report"), ("B", "annual report")])
        pages = [(lid, page) for _, _, lid, page, _ in got]
        self.assertEqual(len(pages), E.MODEL_PAGES)
        self.assertIn(("A", 21), pages)                              # the rule candidate stays
        rank_half = [x for x in pages if x != ("A", 21)]
        b_pages = sum(1 for _, _, lid, _, _ in p if lid == "B")
        self.assertTrue(all(lid == "B" for lid, _ in rank_half[:b_pages]), rank_half)   # B first, A only once B is used up

    def test_learned_without_a_trained_attribute_is_today(self):
        p = pool(25, cand={20})
        sc = self.scored(p)
        E.SELECT, E.SELECT_MODEL = "learned", model()
        self.assertEqual(E.model_pages(sc, "GMC_00001.1", ATTR, p, []), E.pages_for_model(sc, E.MODEL_PAGES))


class Save(unittest.TestCase):
    def test_the_saved_model_has_every_fold_and_no_company(self):
        SE.SPEC_ATTRS["GMC_00048.1"] = ATTR
        cases, rows = [], {}
        for f in range(SE.R.FOLDS):
            p = pool(6)
            c = {"wba": f"WBA{f}", "attribute": "GMC_00048.1", "fold": f, "pool": p, "types": TYPES,
                 "target_idx": {1}, "target": {1}, "marks": {"revenue": 0.5}}
            cases.append(c)
            rows[("GMC_00048.1", f)] = [([0.1] * len(P.FEATURE_NAMES), 1), ([0.0] * len(P.FEATURE_NAMES), 0)]
        m = SE.save_model(cases, ["GMC_00048.1"], rows, {"revenue": 1.5}, 50.0)
        parts = m["attributes"]["GMC_00048.1"]
        self.assertEqual(set(parts), {"0", "1", "2", "3", "4", "all"})
        self.assertEqual(parts["2"]["rows"], 8)                      # four other folds, two rows each
        self.assertEqual(parts["all"]["rows"], 10)
        self.assertNotIn("WBA", json.dumps(m))
        self.assertEqual(len(parts["all"]["w"]), len(P.FEATURE_NAMES) + 1)
        o = P.order(m, "GMC_00048.1", ATTR, pool(6), TYPES, fold=2)
        self.assertEqual(sorted(o), list(range(6)))


if __name__ == "__main__":
    unittest.main()
