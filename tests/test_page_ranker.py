import unittest

import page_ranker as P


class Cur:
    """Stub cursor: the collection sample first, then the candidate pages."""
    def __init__(self, pages):
        self.pages, self.calls, self.rows = pages, [], []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))
        if "tablesample" in sql:
            terms = args
            self.rows = [(10000, 300.0) + tuple(50 if t == "board" else 5 for t in terms)]
        else:
            self.rows = self.pages

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class Ranker(unittest.TestCase):
    def test_any_word_finds_pages_and_rare_words_count_more(self):
        pages = [("D1", 3, "The board meets four times a year."),
                 ("D1", 9, "The board oversees climate risks and the transition plan."),
                 ("D1", 12, "Products and services.")]
        cur = Cur(pages)
        r = P.Ranker().rank(cur, ["D1"], ['board "climate strategy"', "transition plan oversight"], top=2)
        self.assertEqual([p for _, p, _, _ in r], [9, 3])                 # 12 matches no word: not kept
        sql, args = cur.calls[-1]
        self.assertIn("|", args[1])                                        # any word, not all words of one wording

    def test_idf_read_once_per_word(self):
        cur = Cur([("D1", 1, "board climate")])
        rk = P.Ranker()
        rk.rank(cur, ["D1"], ["board climate"], top=1)
        rk.rank(cur, ["D1"], ["board climate"], top=1)
        self.assertEqual(sum("tablesample" in c[0] for c in cur.calls), 1)

    def test_nothing_to_rank(self):
        self.assertEqual(P.Ranker().rank(Cur([]), [], ["board"], top=3), [])


class NumericReader(unittest.TestCase):
    def test_bm25_text_rank_rescales_the_pool(self):
        import extract_eval as X
        found = [("D1", 4, "Revenue grew and revenue rose", 0.9), ("D1", 7, "Total revenue 4,120 3,980", 0.1),
                 ("D1", 8, "Board of directors", 0.0)]
        out = X.bm25_text_rank(Cur([]), found, {"labels": ["total revenue"]})
        self.assertEqual([p for _, p, _, _ in out], [4, 7, 8])                 # same pool, same order
        self.assertEqual(max(r[3] for r in out), 1.0)
        self.assertEqual(dict((p, r) for _, p, _, r in out)[8], 0.0)
        self.assertGreater(dict((p, r) for _, p, _, r in out)[7], 0.0)


if __name__ == "__main__":
    unittest.main()
