import unittest

import search_exam as X


class Cur:
    """Stub of the evidence database: documents by company, pages by key, what the search returns."""
    def __init__(self, docs, pages, eight, wide, vector=False, boom=()):
        self.docs, self.pages, self.eight, self.wide, self.vector, self.boom = docs, pages, eight, wide, vector, set(boom)
        self.rows, self.connection, self.rollbacks, self.settings, self.asked = [], self, 0, [], []

    def rollback(self): self.rollbacks += 1

    def execute(self, sql, params=None):
        if "set_config" in sql:
            self.settings.append(tuple(params))
        elif "from public.documents" in sql:
            if params[0] in self.boom:
                raise RuntimeError("database")
            self.rows = [(d,) for d in self.docs.get(params[0], [])]
        elif "to_tsquery" in sql:
            self.rows = [(b,) for k, b in self.pages.items() if k.rsplit("|", 1)[0] in params[0]]
        elif "query_vector_made" in sql:
            self.rows = [({"vec": [0.1]} if self.vector else {"error": "none"},)]
        elif "ask_pages" in sql:
            self.asked.append(tuple(params))
            self.rows = [(k,) for k in self.eight.get(params[2], [])]
        elif "find_blend" in sql:
            self.rows = [(k,) for k in self.wide.get(params[2], [])]
        elif "from public.pages p" in sql:
            self.rows = [(self.pages.get(k, ""),) for k in params[1]]

    def fetchone(self): return self.rows[0]
    def fetchall(self): return self.rows


ATTR = {"A1": {"name": "Scope 1 emissions (total)", "type": "number", "unit_family": "tco2e"}}


class SearchExam(unittest.TestCase):
    def test_the_words_are_the_attribute_name_and_small_numbers_are_not_looked_for(self):
        self.assertEqual(X.words_of(ATTR["A1"]), "Scope 1 emissions total")
        self.assertTrue(X.forms_of(513727.0, ATTR["A1"]))
        self.assertEqual(X.forms_of(40.0, ATTR["A1"]), [])                          # two digits: on any page by chance

    def test_counts_by_stage_and_a_report_that_names_no_company(self):
        g = lambda wba, v=513727.0: {"wba": wba, "attribute": "A1", "year": 2023, "value": v}
        cur = Cur(docs={"W1": ["L1"], "W2": ["L2"], "W3": ["L3"], "W5": ["L5"]},
                  pages={"L1|4": "Scope 1 513,727 tCO2e", "L1|9": "nothing", "L2|7": "total 513,727", "L2|1": "cover", "L3|2": "no figure here", "L5|3": "513,727"},
                  eight={"W1": ["L1|9", "L1|4"], "W2": ["L2|1"], "W3": ["L3|2"], "W5": ["L5|3"]},
                  wide={"W1": ["L1|4"], "W2": ["L2|1", "L2|7"], "W3": ["L3|2"], "W5": ["L5|3"]}, boom={"W5"})
        lines = []
        stats = X.run_exam(cur, [g("W1"), g("W2"), g("W3"), g("W4"), g("W5"), g("W1", 40.0)], ATTR, out=lines.append)
        s = stats["A1"]
        # W1: read; W2: in the 24 only; W3: not in the library; W4: no documents; W5: the database fails; the last has no form
        self.assertEqual((s["answers"], s["no_documents"], s["in_library"], s["in_search"], s["in_eight"], s["failed"]), (4, 1, 2, 2, 1, 1))
        self.assertEqual(cur.asked[0][2:5], ("W1", 2023, 2024))                     # the answer's year and the next
        text = "\n".join(X.table(stats, ATTR, "t") + lines)
        self.assertIn("1 of 2 (50%) on the eight pages", text)
        self.assertIn("2 of 2 (100%) on the 24 pages", text)
        for secret in ("W1", "L1", "513"):
            self.assertNotIn(secret, text)                                          # the log is public

    def test_an_option_is_set_before_every_answer(self):
        cur = Cur(docs={"W1": ["L1"]}, pages={"L1|4": "513,727"}, eight={"W1": ["L1|4"]}, wide={})
        g = {"wba": "W1", "attribute": "A1", "year": "2023", "value": 513727.0}
        X.run_exam(cur, [g, g], ATTR, option=("ask_later", "on"), out=lambda s: None)
        self.assertEqual(cur.settings, [("lantern.ask_later", "on")] * 2)
        self.assertEqual(cur.rollbacks, 2)                                          # the exam's searches are not kept

    def test_the_option_name_is_checked(self):
        with self.assertRaises(SystemExit):
            X.main(["--option", "x; drop"])


if __name__ == "__main__":
    unittest.main()
