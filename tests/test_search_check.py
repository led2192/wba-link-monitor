"""Offline test for search_check.py: a fake cursor, no database. The exact company count is asked only
when the summary was capped and the function is installed."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_QA_TOKEN", "x"); os.environ.setdefault("AIRTABLE_QA_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import search_check as S  # noqa: E402


class FakeCursor:
    def __init__(self, summary, rows, exact=1385):
        self.summary, self.rows, self.exact, self.calls = summary, rows, exact, []

    def execute(self, sql, args=None):
        self.calls.append(sql)
        self._last = sql

    def fetchone(self):
        if "search_summary" in self._last:
            return self.summary
        if "search_company_count" in self._last:
            return (self.exact,)
        if "to_regproc" in self._last:
            return (True,)
        return None

    def fetchall(self):
        return self.rows


class Run(unittest.TestCase):
    def test_exact_count_only_when_capped(self):
        cur = FakeCursor((363, 3768, 20000, True), [("Acme", 2024, 3)])
        s, rows, tc, tr, exact = S.run(cur, "pages", ("revenue",), exact_companies=True)
        self.assertEqual(s[0], 363)
        self.assertEqual(exact[0], 1385)
        self.assertTrue(any("search_company_count" in c for c in cur.calls))

        cur = FakeCursor((12, 40, 300, False), [("Acme", 2024, 3)])
        s, rows, tc, tr, exact = S.run(cur, "pages", ("TNFD",), exact_companies=True)
        self.assertIsNone(exact)
        self.assertFalse(any("search_company_count" in c for c in cur.calls))

    def test_without_the_function_nothing_is_asked(self):
        cur = FakeCursor((363, 3768, 20000, True), [])
        s, rows, tc, tr, exact = S.run(cur, "pages", ("revenue",), exact_companies=False)
        self.assertIsNone(exact)
        self.assertFalse(any("search_company_count" in c for c in cur.calls))

    def test_passages(self):
        cur = FakeCursor(None, [("Acme", 2024, 3)])
        s, rows, tc, tr, exact = S.run(cur, "passages", ("water", "nature"), exact_companies=True)
        self.assertIsNone(s); self.assertIsNone(exact); self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
