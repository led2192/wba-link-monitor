"""Offline tests for doc_map.py: the document map, one line per page written once and searched by every attribute."""
import collections, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import doc_map as D  # noqa: E402
import extract_eval as X  # noqa: E402


class Lines(unittest.TestCase):
    def test_prompt_holds_each_page_cut_to_its_first_part(self):
        p = D.page_prompt([(3, "a" * 3000), (4, "Consolidated statement of cash flows 2024 2023")])
        self.assertIn("=== page 3 ===", p); self.assertIn("=== page 4 ===", p)
        self.assertIn("a" * D.PAGE_CHARS, p); self.assertNotIn("a" * (D.PAGE_CHARS + 1), p)
        self.assertIn("Consolidated statement", p)

    def test_parse_lines_keeps_only_the_pages_asked_and_never_invents(self):
        pages = [(3, "x"), (4, "y")]
        got = D.parse_lines('Here you go: {"3": "Consolidated statement of cash flows, capex and depreciation, 2024 2023", "4": "  Table of contents ", "9": "stray"}', pages)
        self.assertEqual(got, {3: "Consolidated statement of cash flows, capex and depreciation, 2024 2023", 4: "Table of contents"})
        self.assertEqual(D.parse_lines("not json", pages), {})
        self.assertEqual(D.parse_lines('{"page 3": "Cover"}', pages), {3: "Cover"})

    def test_summarise_uses_one_call_and_its_own_usage(self):
        seen = {}
        def call(messages, usage, sess=None, system=None, max_tokens=None):
            seen["n"] = seen.get("n", 0) + 1; seen["max"] = max_tokens
            usage["calls"] += 1
            return '{"1": "Cover", "2": "GHG emissions by scope, 2022-2024"}'
        lines, u = D.summarise([(1, "Annual report"), (2, "Scope 1 ... Scope 2 ...")], call=call)
        self.assertEqual(lines, {1: "Cover", 2: "GHG emissions by scope, 2022-2024"})
        self.assertEqual((seen["n"], u["calls"]), (1, 1))
        self.assertEqual(seen["max"], 60 * 2 + 100)

    def test_write_lines_is_dry_by_default(self):
        class Cur:
            def __init__(self): self.rows = []
            def executemany(self, q, rows): self.rows += rows
        cur = Cur()
        self.assertEqual(D.write_lines(cur, "d", {1: "Cover"}, "m", commit=False), 0); self.assertEqual(cur.rows, [])
        self.assertEqual(D.write_lines(cur, "d", {1: "Cover", 2: "x"}, "m", commit=True), 2)
        self.assertEqual(cur.rows[0], ("d", 1, "Cover", "m"))


class Table(unittest.TestCase):
    def test_ensure_table_creates_only_when_missing(self):
        class Cur:
            def __init__(self, exists): self.exists, self.q = exists, []
            def execute(self, q, params=None): self.q.append(q)
            def fetchone(self): return ("public.page_map" if self.exists else None,)
            class connection:
                @staticmethod
                def commit(): pass
        c = Cur(True); D.ensure_table(c); self.assertEqual(len(c.q), 1)
        c = Cur(False); D.ensure_table(c); self.assertEqual(len(c.q), 2); self.assertIn("create table if not exists public.page_map", c.q[1])
        self.assertNotIn("alter table", D.PAGE_MAP_SQL)


class Search(unittest.TestCase):
    def test_map_query_works_from_a_built_spec(self):
        q = D.map_query({"name": "Number of passengers per year", "definition": "Passengers carried in the year."})
        self.assertIn("passengers", q.lower()); self.assertIn("carried", q.lower())
        q2 = D.map_query({"name": "Revenue", "definition": "x" * 1000, "labels": ["total revenue", "net sales", "a", "b", "c", "d", "e"]})
        self.assertIn("net sales", q2); self.assertNotIn(" e", q2[-3:])

    def test_map_pages_fuses_words_and_meaning_and_returns_bodies(self):
        class Cur:
            def __init__(self): self.q = []
            def execute(self, q, params=None):
                self.q.append(q)
                if "page_map" in q:
                    self.rows = [("d", 5, 0.2, 0.9), ("d", 7, 0.4, 0.1), ("d", 9, 0.0, None)]
                else:
                    self.rows = [("d", 5, "body five"), ("d", 7, "body seven"), ("d", 9, "body nine")]
            def fetchall(self): return self.rows
        cur = Cur()
        got = D.map_pages(cur, ["d"], "passengers carried", 2, [0.1] * 384)
        self.assertEqual([(l, p) for l, p, _, _ in got], [("d", 5), ("d", 7)])     # 0.5*0.5+0.5*0.9 beats 0.5*1+0.5*0.1
        self.assertEqual(got[0][2], "body five")
        self.assertEqual(D.map_pages(cur, ["d"], "x", 2, None) and True, True)
        cur.rows = []
        class Empty(Cur):
            def execute(self, q, params=None): self.rows = []
        self.assertEqual(D.map_pages(Empty(), ["d"], "x", 2, None), [])

    def test_eval_keeps_todays_pages_when_the_documents_have_no_map(self):
        saved = X.SELECT
        try:
            X.SELECT = "map"
            class Cur:
                def execute(self, q, params=None): pass
                def fetchall(self): return []
            self.assertEqual(X.map_model_pages(Cur(), ["d"], 2024, {"name": "Revenue", "labels": ["revenue"]}, {"attributes": {}}, [], 16), [])
        finally:
            X.SELECT = saved


if __name__ == "__main__":
    unittest.main()
