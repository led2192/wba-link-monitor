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
        elif "set_config" in sql:
            self.settings = getattr(self, "settings", []) + [tuple(params)]
        elif "from public.documents" in sql:
            self.rows = [r for r in getattr(self, "docs", []) if r[0] in params[0]]
        elif "find_company_ids" in sql:
            self.rows = [(params[0].split(";"),)]
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


class SearchTimingLevels(unittest.TestCase):
    def test_the_two_levels_are_built_as_find_pages_builds_them(self):
        import search_timing as T

        class Cur:
            def execute(self, sql, args=None): self.args = args
            def fetchone(self): return ["a:*", "b", "c:* | x <-> y"]
        every, but_one = T.level_queries(Cur(), ["a", "b", "c"])
        self.assertEqual(every, "(a:*) & (b) & (c:* | x <-> y)")
        self.assertEqual(but_one, "((b) & (c:* | x <-> y)) | ((a:*) & (c:* | x <-> y)) | ((a:*) & (b))")
        for _, question, _ in T.LONG:
            self.assertNotIn("ANA", question)                       # our own sentences only: the log is public


    def test_an_option_is_set_before_every_case_and_the_two_runs_are_compared(self):
        cases = [(1, "kw", None, "q one?", "W1", ["L1|5"]), (2, "kw2", None, "q two?", "W2", ["L2|7"])]
        cur = Cur(vectors=set(), with_vec={}, words_only={"kw": ["L1|5"], "kw2": ["x|1"]})
        places = {}
        A.evaluate(cur, cases, out=lambda s: None, option=("ask_later", "on"), places=places)
        self.assertEqual(cur.settings, [("lantern.ask_later", "on")] * 2)             # a rollback would have undone one set once
        self.assertEqual(places, {0: (1, True, ('L1|5',)), 1: (0, False, ('x|1',))})
        line = A.compare({1: (1, True), 2: (0, False), 3: (2, True)}, {1: (0, True), 2: (4, True), 3: (5, True)})
        self.assertIn("of 3 questions", line)
        self.assertIn("1 gained and 1 lost", line)
        self.assertIn("(1 kept it at another place)", line)
        self.assertIn("1 gained and 0 lost.", line)
        self.assertNotIn("did not act", A.compare({1: (1, True, ("a|1",))}, {1: (1, True, ("b|1",))}))
        self.assertIn("did not act", A.compare({1: (1, True, ("a|1",))}, {1: (1, True, ("a|1",))}))
        # one question about two companies is two cases: both are kept
        two = {}
        A.evaluate(cur, [(5, "kw", None, "q one?", "W1", ["L1|5"]), (5, "kw", None, "q one?", "W2", ["L1|5"])], out=lambda s: None, places=two)
        self.assertEqual(len(two), 2)

    def test_why_counts_causes_and_names_nothing(self):
        def case(i, right, wide, y0=None, y1=None, kind=None, company="W1"):
            return (i, [f"kw{i}"], None, company, y0, y1, kind, set(right), wide)
        cur = Cur(vectors=set(), with_vec={}, words_only={"kw4": ["x|1"] * 30 + ["L4|9"], "kw5": ["L5|2"], "kw6": ["x|1"], "kw7": ["x|1"]})
        cur.docs = [("L2", 2022, "Annual Report", "W1", True), ("L3", 2024, "Policy", "W1", True), ("L3b", 2024, "Annual Report", "W9", True),
                    ("L4", 2024, "Annual Report", "W1", True), ("L5", 2024, "Annual Report", "W1", True),
                    ("L6", 2024, "Annual Report", "W1", True), ("L7", None, "Annual Report", "W1", False)]
        missed = [case(1, ["L1|5"], ["L1|6", "x|1"]),                                   # the document is there, another page
                  case(2, ["L2|5"], ["x|1"], y0=2023, y1=2024),                         # the years leave it out
                  case(3, ["L3|5"], ["x|1"], kind="Annual Report"),                     # the type
                  case(30, ["L3b|5"], ["x|1"]),                                         # the company
                  case(4, ["L4|9"], ["x|1"]),                                           # deeper
                  case(5, ["L5|9"], ["x|1"]),                                           # the document deeper, another page
                  case(6, ["L6|9"], ["x|1"]), case(7, ["L7|9"], ["x|1"]),               # not found: indexed, not indexed
                  case(8, ["L8|9"], ["x|1"])]                                           # gone
        self.assertEqual([A.cause(cur, m) for m in missed],
                         [A.CAUSES[0], A.CAUSES[2], A.CAUSES[3], A.CAUSES[4], A.CAUSES[5], A.CAUSES[6], A.CAUSES[7], A.CAUSES[8], A.CAUSES[1]])
        lines = A.why(cur, missed, out=lambda s: None)
        self.assertIn("9 questions", lines[0])
        self.assertEqual(len(lines), 11)
        self.assertIn("the next or the one before in 1,", lines[-1])
        self.assertEqual(A.apart({"L1|5", "L1|40"}, ["L1|43", "x|5"]), 3)
        self.assertIsNone(A.apart({"L1|5"}, ["x|5"]))
        for secret in ("L1", "L4", "kw4", "W1", "Policy"):
            self.assertNotIn(secret, " ".join(lines))

    def test_the_option_name_is_checked_and_writes_nothing(self):
        with self.assertRaises(SystemExit):
            A.main(["--option", "x; drop"])
        with self.assertRaises(SystemExit):
            A.main(["--option", "ask_later", "--commit"])


if __name__ == "__main__":
    unittest.main()
