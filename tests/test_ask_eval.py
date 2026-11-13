import unittest

import ask_eval as A


class Cur:
    """Stub: a vector for some questions; the pages ask_pages returns, with and without the vector."""
    def __init__(self, vectors, with_vec, words_only, boom=()):
        self.vectors, self.with_vec, self.words_only, self.boom, self.rows, self.connection = vectors, with_vec, words_only, set(boom), [], self
        self.rollbacks = 0
        self.asked = []

    def rollback(self): self.rollbacks += 1

    def execute(self, sql, params=None):
        if "query_vector_made" in sql:
            if params[0] in self.boom:
                raise RuntimeError("database")
            self.rows = [({"vec": [0.1]} if params[0] in self.vectors else {"error": "x"},)]
        else:
            q, vec = params[0], params[1]
            self.asked.append((("search" if "find_blend" in sql else "pages"), q, params[2], params[3], params[4], params[5]))
            self.rows = [(p,) for p in (self.with_vec if vec else self.words_only).get(q, [])]

    def fetchone(self): return self.rows[0]
    def fetchall(self): return self.rows


class AskEval(unittest.TestCase):
    def test_place_of_the_first_confirmed_page(self):
        self.assertEqual(A.place(["a|1", "b|2", "c|3"], {"c|3", "b|2"}), 2)
        self.assertEqual(A.place(["a|1"], {"z|9"}), 0)

    def test_counts_and_a_summary_that_names_nothing(self):
        cases = [(1, "kw one", None, "question one about Acme?", "W1", ["L1|5"]),     # found second, only with the meaning
                 (2, "kw two", None, "question two?", "W2", ["L2|7"]),                  # no vector: words alone, found fifth
                 (3, "kw three", None, "question three?", "W3", ["L3|9"]),              # not found
                 (4, "kw four", None, "question four?", "W4", ["L4|1"])]                # the database fails on it
        cur = Cur(vectors={"question one about Acme?", "question three?"},
                  with_vec={"kw one": ["X|1", "L1|5"], "kw three": ["X|1"]},
                  words_only={"kw one": ["X|1"], "kw two": ["a|1", "b|1", "c|1", "d|1", "L2|7"], "kw three": []},
                  boom={"question four?"})
        lines = []
        c = A.evaluate(cur, cases, out=lines.append)
        self.assertEqual((c["cases"], c["found"], c["first_three"], c["found_words"], c["no_vector"], c["failed"]), (3, 2, 1, 1, 1, 1))
        self.assertEqual((c["in_search"], c["exact"], c["exact_found"]), (2, 0, 0))
        self.assertIn(("pages", "kw one", "W1", None, None, None), cur.asked)                # an old answer: within the fact's company
        text = A.summary(c) + " ".join(lines)
        self.assertIn("2 of 3 (67%)", text)
        for secret in ("Acme", "question one", "question two", "kw one", "L1", "W1"):
            self.assertNotIn(secret, text)                                              # the log is public

    def test_an_answer_that_kept_its_search_is_asked_again_exactly(self):
        kept = {"q": "scope 1", "company": "W9;W8", "from": 2023, "to": 2024, "type": "Annual Report", "vector": True}
        self.assertEqual(A.replay("emissions scope one", kept, "what is the scope 1 of Acme in 2023?", "W9"),
                         (["emissions scope one", "scope 1"], "W9;W8", 2023, 2024, "Annual Report", True))
        self.assertEqual(A.replay(None, None, "typed?", "W1"), (["typed?"], "W1", None, None, None, False))
        # the model's words find nothing: the words the box sent are tried, as Ask does
        cur = Cur(vectors={"what is the scope 1 of Acme in 2023?"}, with_vec={"scope 1": ["L9|3"]}, words_only={"scope 1": ["L9|3"]})
        c = A.evaluate(cur, [(7, "emissions scope one", kept, "what is the scope 1 of Acme in 2023?", "W9", ["L9|3"])], out=lambda s: None)
        self.assertEqual((c["cases"], c["found"], c["exact"], c["exact_found"], c["in_search"]), (1, 1, 1, 1, 1))
        self.assertIn(("pages", "scope 1", "W9;W8", 2023, 2024, "Annual Report"), cur.asked)
        self.assertIn(("search", "scope 1", "W9;W8", 2023, 2024, "Annual Report"), cur.asked)

    def test_ask_keeps_what_it_searched_with(self):
        import pathlib
        sql = pathlib.Path(__file__).resolve().parent.parent.joinpath("supabase", "search.sql").read_text()
        self.assertIn("alter table public.ask_log add column if not exists search jsonb;", sql)
        self.assertIn("jsonb_build_object('q', search_q, 'company', company_filter, 'from', year_from, 'to', year_to, 'type', type_filter, 'vector', qvec is not null))", sql)


if __name__ == "__main__":
    unittest.main()
