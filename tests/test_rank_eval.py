import os
import random
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import rank_eval as R


class Text(unittest.TestCase):
    def test_terms_and_phrases(self):
        self.assertEqual(R.query_terms('"scope 1" emissions tCO2e'), ["scope", "1", "emissions", "tco2e"])
        self.assertEqual(R.query_terms("number of employees"), ["number", "employees"])
        self.assertEqual(R.query_phrases('"scope 1" "scope 2" emissions'), [["scope", "1"], ["scope", "2"]])
        self.assertTrue(R.has_phrase(["total", "scope", "1", "emissions"], ["scope", "1"]))
        self.assertFalse(R.has_phrase(["scope", "2", "1"], ["scope", "1"]))

    def test_line_labels(self):
        body = "Scope 1 emissions   12,300\nOur approach to emissions is described here\nRevenue 4,000"
        self.assertEqual(R.line_labels(body, ["scope", "emissions"]), 1)


class Scoring(unittest.TestCase):
    def test_idf_rare_words_count_more(self):
        self.assertGreater(R.idf(10, 10_000), R.idf(5_000, 10_000))
        self.assertGreater(R.idf(9_999, 10_000), 0)      # still positive on almost every page

    def test_bm25_saturates_and_normalises_length(self):
        idfs = {"scope": 1.0}
        one = R.bm25({"scope": 1}, 100, 100, ["scope"], idfs)
        ten = R.bm25({"scope": 10}, 100, 100, ["scope"], idfs)
        self.assertGreater(ten, one)
        self.assertLess(ten, 10 * one)                    # term frequency saturates
        long = R.bm25({"scope": 1}, 1000, 100, ["scope"], idfs)
        self.assertLess(long, one)                        # the same mention counts less on a long page

    def test_bm25_prefers_the_table_over_the_passing_mention(self):
        pages = {1: "Our emissions strategy. " * 30 + "scope",
                 2: "Scope 1 emissions tCO2e 12,300\nScope 2 emissions tCO2e 4,100\nScope 3 emissions 90,000"}
        idfs = {"scope": 3.0, "1": 1.0, "emissions": 1.5, "tco2e": 4.0}
        feats, bm, per_bm = R.page_features(pages, "GMC_00001.1", 2024, ['"scope 1" emissions tCO2e'], idfs, 60, {})
        self.assertEqual(len(per_bm), 1)
        self.assertEqual(R.order(bm)[0], 2)
        self.assertEqual(len(feats[2]), len(R.FEATURES))
        self.assertTrue(all(0 <= v <= 1 for v in feats[2]))

    def test_order_and_best_of(self):
        self.assertEqual(R.order({3: 1.0, 1: 1.0, 2: 5.0}), [2, 1, 3])
        self.assertEqual(R.best_of([{1: 2.0, 2: 1.0}, {2: 3.0}], {2}), 1)
        self.assertIsNone(R.best_of([{1: 2.0}], {2}))

    def test_fusion_rewards_agreement_between_queries(self):
        # page 2 is second for both queries; pages 1 and 3 are each first for one query and absent from the other
        fused = R.fuse([{1: 9.0, 2: 5.0}, {3: 9.0, 2: 5.0}])
        self.assertEqual(R.order(fused)[0], 2)
        self.assertNotIn(4, R.fuse([{4: 0.0, 1: 1.0}]))  # a page with no match is not ranked

    def test_zones(self):
        self.assertEqual(R.line_zone("Total revenue 4,120 3,980"), "table")
        self.assertEqual(R.line_zone("Board oversight of climate-related issues"), "heading")
        self.assertEqual(R.line_zone("Our revenue grew because customers bought more of our products in every region."), "prose")
        self.assertIsNone(R.line_zone("   "))

    def test_zones_lift_the_table_over_the_narrative(self):
        # the Revenue case: the word is on every page, the table row is what marks the right one
        pages = {1: "Revenue grew strongly this year as revenue from new markets rose and sales per customer improved.",
                 2: "Consolidated income statement\nRevenue 4,120 3,980\nCost of sales (2,100) (2,050)"}
        idfs = {"revenue": 0.5, "total": 0.4}
        plain, _ = {p: R.bm25(__import__("collections").Counter(R.tokens(b)), len(set(R.tokens(b))), 12, ["revenue"], idfs)
                    for p, b in pages.items()}, None
        self.assertEqual(R.order(plain)[0], 1)                       # plain BM25 prefers the page that repeats the word
        zs, per = R.zone_scores(pages, "GMC_00048.1", ["revenue"], idfs, 12)
        self.assertEqual(R.order(zs)[0], 2)                          # BM25F prefers the table row
        self.assertEqual(len(per), 1)

    def test_metrics(self):
        m = R.metrics([1, 2, 5, None])
        self.assertEqual(m["top 1"], 0.25)
        self.assertEqual(m["top 3"], 0.5)
        self.assertEqual(m["top 10"], 0.75)
        self.assertAlmostEqual(m["mrr"], (1 + 0.5 + 0.2) / 4)


class Learning(unittest.TestCase):
    def test_folds_are_by_company(self):
        self.assertEqual(R.fold_of("PT_00001"), R.fold_of("PT_00001"))
        self.assertTrue(len({R.fold_of(f"PT_{i:05d}") for i in range(50)}) == R.FOLDS)

    def test_learns_the_feature_that_marks_the_cited_page(self):
        rng = random.Random(1)
        cases = []
        for i in range(40):
            feats = {}
            for p in range(1, 31):
                x = [rng.random() for _ in R.FEATURES]
                x[R.FEATURES.index("line labels")] = 0.05 * rng.random()
                feats[p] = x
            cited = rng.randint(1, 30)
            feats[cited][R.FEATURES.index("line labels")] = 0.9      # the cited page has labels at line starts
            cases.append({"wba": f"PT_{i:05d}", "target": {cited}, "features": feats,
                          "bm25": {p: x[0] for p, x in feats.items()}, "ranks": {}})
        w = R.cross_validate(cases)
        self.assertGreater(w[R.FEATURES.index("line labels")], 1.0)
        top1 = sum(1 for c in cases if c["ranks"]["learned"] == 1) / len(cases)
        self.assertGreater(top1, 0.9)                     # every case scored by a model that never saw its company

    def test_training_rows_keep_every_cited_page(self):
        feats = {p: [0.0] * len(R.FEATURES) for p in range(1, 50)}
        case = {"target": {4, 5}, "features": feats, "bm25": {p: float(p) for p in feats}}
        rows = R.training_rows(case, random.Random(0))
        self.assertEqual(sum(y for _, y in rows), 2)
        self.assertEqual(len(rows), 2 + R.NEGATIVES)


if __name__ == "__main__":
    unittest.main()
