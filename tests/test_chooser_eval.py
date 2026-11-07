"""Offline tests for chooser_eval.py: candidate features on a page, the conditional logit learning a choice
rule from synthetic answers, and the fold rule. No network, no database."""
import math
import os
import random
import sys
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chooser_eval as C  # noqa: E402

SPEC = {"unit_families": {"currency": {"scales": {"million": 1e6, "millions": 1e6, "thousand": 1e3}}}}
ATTR = {"unit_family": "tco2e", "labels": ["scope 2 location-based", "scope 2"], "exclude": []}
PAGE = ("GHG emissions (tCO2e)   2024    2023\n"
        "Scope 2 location-based  12,300  11,900\n"
        "Scope 2 market-based  8,100  9,000\n")


class Candidates(unittest.TestCase):
    def test_every_column_is_a_candidate_with_its_year(self):
        cs = C.all_candidates(PAGE, 2024, ATTR, SPEC, 0, "Sustainability Report", 0)
        vals = [v for _, v, _ in cs]
        self.assertIn(12300.0, vals); self.assertIn(11900.0, vals); self.assertIn(8100.0, vals)
        f = next(f for f, v, _ in cs if v == 12300.0)
        self.assertEqual(f["column is the year"], 1.0)
        self.assertEqual(f["column=0"], 1.0)
        self.assertIn("word=location-based", f)
        g = next(f for f, v, _ in cs if v == 11900.0)
        self.assertEqual(g["column is the year"], 0.0)


class Learning(unittest.TestCase):
    def synthetic(self, n, seed):
        """Answers where the right figure is the year's column on a row with the word 'location': the learner
        must find that; position alone is uninformative (shuffled)."""
        rnd = random.Random(seed)
        cases = []
        for k in range(n):
            cands = []
            right_at = rnd.randrange(4)
            for i in range(4):
                good = i == right_at
                f = {"bias": 1.0, "column is the year": 1.0 if (good or rnd.random() < 0.3) else 0.0,
                     ("word=location" if good or rnd.random() < 0.2 else "word=market"): 1.0, f"column={rnd.randrange(3)}": 1.0}
                cands.append((f, float(i), -math.inf, good))
            cases.append({"wba": f"PT_{k:05d}", "fold": k % 5, "cands": cands})
        return cases

    def test_the_logit_learns_the_rule_and_generalises(self):
        train, test = self.synthetic(300, 1), self.synthetic(200, 2)
        w, vocab = C.fit(train, l2=0.001, steps=200)
        right = sum(c["cands"][C.choose(w, vocab, c)][3] for c in test)
        self.assertGreater(right / len(test), 0.8)          # chance is 25%
        self.assertGreater(w[vocab.ix["word=location"]], w[vocab.ix["word=market"]])

    def test_rare_features_are_left_out(self):
        cases = self.synthetic(50, 3)
        cases[0]["cands"][0][0]["word=acmecorp"] = 1.0       # one company's name, in one answer
        w, vocab = C.fit(cases, steps=5)
        self.assertNotIn("word=acmecorp", vocab.ix)

    def test_no_candidates(self):
        w, vocab = C.fit(self.synthetic(20, 4), steps=5)
        self.assertIsNone(C.choose(w, vocab, {"cands": []}))



class Memory(unittest.TestCase):
    def test_size_row_and_direction(self):
        f = C.memory_features(300e6, [300e6, 280e6], {2023: 280e6}, 2024, "revenue increased to 300 million")
        self.assertEqual(f["memory: same size (within 25%)"], 1.0)
        self.assertEqual(f["memory: the row prints a known year's figure"], 1.0)
        self.assertEqual(f["memory: change agrees with the words"], 1.0)
        g = C.memory_features(275e9, [275e9], {2023: 280e6}, 2024, "revenue increased")
        self.assertEqual(g["memory: 10 times off or more"], 1.0)
        self.assertEqual(g["memory: change agrees with the words"], 1.0)     # bigger, as the words say, but 1,000 times off
        h = C.memory_features(250e6, [250e6], {2023: 280e6}, 2024, "revenue increased by 7%")
        self.assertEqual(h["memory: change contradicts the words"], 1.0)
        self.assertEqual(C.memory_features(1.0, [1.0], {}, 2024, ""), {})


class Fancy(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib").util.find_spec("pymupdf"), "pymupdf not installed")
    def test_headings_bold_and_figures_from_a_pdf(self):
        import pymupdf, tempfile
        doc = pymupdf.open(); p = doc.new_page()
        p.insert_text((72, 60), "Scope 2 emissions", fontsize=20, fontname="hebo")
        p.insert_text((72, 300), "Scope 2 location-based 12,300 11,900", fontsize=10)
        p.insert_text((72, 320), "Total 45,600", fontsize=10, fontname="hebo")
        for k in range(10):
            p.insert_text((72, 400 + 12 * k), "ordinary body text line with words", fontsize=10)
        path = os.path.join(tempfile.mkdtemp(), "s.pdf"); doc.save(path)
        st = C.page_style(path, 1)
        self.assertIn("scope 2 emissions", st["heading"])
        self.assertIn("scope 2 emissions", st["top"])
        self.assertIn("45600", st["bold_figures"])
        f = C.fancy_features(st, "Scope 2", "12,300")
        self.assertEqual(f["fancy: label in a heading"], 1.0)
        self.assertEqual(f["fancy: figure in bold"], 0.0)
        self.assertEqual(C.fancy_features(st, "Total", "45,600")["fancy: figure in bold"], 1.0)
        self.assertEqual(C.fancy_features(None, "x", "1"), {})
        self.assertIsNone(C.page_style("/no/such.pdf", 1))

if __name__ == "__main__":
    unittest.main()
