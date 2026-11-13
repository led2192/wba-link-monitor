import unittest

import fiscal_months as F


class Cur:
    """A stub cursor: the list of companies to do, their documents' commonest period-end month, and what was written."""
    def __init__(self, todo, months):
        self.todo, self.months, self.written, self.rows = todo, months, [], []

    def execute(self, sql, params=None):
        if "with seen as" in sql:
            self.rows = [(w,) for w in self.todo[:params["limit"]]]
        elif "substr(period_end" in sql:
            m = self.months.get(params[0])
            if m == "boom":
                raise RuntimeError("database")
            self.rows = [(f"{m:02d}", 3)] if isinstance(m, int) else []
        elif sql.startswith("insert into public.company_fiscal"):
            self.written.append(params)

    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class Conn:
    def __init__(self, cur): self.cur, self.commits, self.rollbacks = cur, 0, 0
    def cursor(self): return self.cur
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


class FiscalMonths(unittest.TestCase):
    def test_the_period_end_first_then_the_pages_and_a_row_even_when_none(self):
        cur = Cur(["A", "B", "C"], {"A": 3})
        pages = lambda c, w: {"B": 5}.get(w)
        got = F.run(Conn(cur), pages, commit=True, out=lambda s: None)
        self.assertEqual(cur.written, [("A", 3, "period_end"), ("B", 5, "pages"), ("C", None, None)])     # C is remembered as looked for
        self.assertEqual((got["found"], got["period_end"], got["pages"], got["none"], got["written"]), (2, 1, 1, 1, 3))

    def test_dry_run_writes_nothing(self):
        cur = Cur(["A"], {"A": 12}); conn = Conn(cur)
        F.run(conn, lambda c, w: None, out=lambda s: None)
        self.assertEqual((cur.written, conn.commits), ([], 0))

    def test_one_company_failing_and_the_time_budget(self):
        cur = Cur(["A", "B", "C"], {"A": "boom", "B": 6, "C": 6}); conn = Conn(cur)
        ticks = iter([0, 0, 0, 5000, 5000])                                   # the budget runs out before C
        lines = []
        F.run(conn, lambda c, w: None, minutes=1, commit=True, out=lines.append, clock=lambda: next(ticks))
        self.assertEqual(cur.written, [("B", 6, "period_end")]); self.assertEqual(conn.rollbacks, 1)
        self.assertTrue(any("time budget" in l for l in lines))

    def test_a_month_out_of_range_is_no_month(self):
        self.assertEqual(F.month_of(Cur([], {}), "A", lambda c, w: 13), (None, None))


if __name__ == "__main__":
    unittest.main()
