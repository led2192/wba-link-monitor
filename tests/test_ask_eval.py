import unittest

import ask_eval as A


class Cur:
    """Stub: a vector for some questions; the pages ask_pages returns, with and without the vector."""
    def __init__(self, vectors, with_vec, words_only, boom=()):
        self.vectors, self.with_vec, self.words_only, self.boom, self.rows, self.connection = vectors, with_vec, words_only, set(boom), [], self
        self.rollbacks = 0

    def rollback(self): self.rollbacks += 1

    def execute(self, sql, params=None):
        if "query_vector_made" in sql:
            if params[0] in self.boom:
                raise RuntimeError("database")
            self.rows = [({"vec": [0.1]} if params[0] in self.vectors else {"error": "x"},)]
        else:
            q, vec = params[0], params[1]
            self.rows = [(p,) for p in (self.with_vec if vec else self.words_only).get(q, [])]

    def fetchone(self): return self.rows[0]
    def fetchall(self): return self.rows


class AskEval(unittest.TestCase):
    def test_place_of_the_first_confirmed_page(self):
        self.assertEqual(A.place(["a|1", "b|2", "c|3"], {"c|3", "b|2"}), 2)
        self.assertEqual(A.place(["a|1"], {"z|9"}), 0)

    def test_counts_and_a_summary_that_names_nothing(self):
        cases = [(1, "kw one", "question one about Acme?", "W1", ["L1|5"]),          # found second, only with the meaning
                 (2, "kw two", "question two?", "W2", ["L2|7"]),                       # no vector: words alone, found fifth
                 (3, "kw three", "question three?", "W3", ["L3|9"]),                   # not found
                 (4, "kw four", "question four?", "W4", ["L4|1"])]                     # the database fails on it
        cur = Cur(vectors={"question one about Acme?", "question three?"},
                  with_vec={"kw one": ["X|1", "L1|5"], "kw three": ["X|1"]},
                  words_only={"kw one": ["X|1"], "kw two": ["a|1", "b|1", "c|1", "d|1", "L2|7"], "kw three": []},
                  boom={"question four?"})
        lines = []
        c = A.evaluate(cur, cases, out=lines.append)
        self.assertEqual((c["cases"], c["found"], c["first_three"], c["found_words"], c["no_vector"], c["failed"]), (3, 2, 1, 1, 1, 1))
        text = A.summary(c) + " ".join(lines)
        self.assertIn("2 of 3 (67%)", text)
        for secret in ("Acme", "question one", "question two", "kw one", "L1", "W1"):
            self.assertNotIn(secret, text)                                              # the log is public


if __name__ == "__main__":
    unittest.main()
