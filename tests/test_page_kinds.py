"""page_kinds.py: the documents are read in steps until none is left or the minutes are over; a dry run writes nothing."""
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import page_kinds as P


class Cursor:
    def __init__(self, steps):
        self.steps = list(steps); self.asked = []
    def execute(self, sql, params=None):
        self.asked.append((sql, params))
    def fetchone(self):
        return (self.steps.pop(0),)


class ReadingInSteps(unittest.TestCase):
    def test_it_stops_when_no_document_is_left(self):
        cur = Cursor([{"documents": 200, "pages": 9000, "marked": 40}, {"documents": 50, "pages": 2000, "marked": 5}, {"documents": 0, "pages": 0, "marked": 0}])
        commits = []
        got = P.run(cur, 40, 200, out=lambda s: None, clock=lambda: 0, commit=lambda: commits.append(1))
        self.assertEqual(got, (250, 11000, 45, True))
        self.assertEqual(len(commits), 3)                                   # each step is kept before the next
        self.assertTrue(all(p == (200,) for _, p in cur.asked))

    def test_it_stops_when_the_minutes_are_over_and_says_so(self):
        cur = Cursor([{"documents": 200, "pages": 9000, "marked": 40}] * 5)
        ticks = iter([0, 30, 61, 999])
        got = P.run(cur, 1, 200, out=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(got, (400, 18000, 80, False))

    def test_the_state_is_said_in_counts(self):
        line = P.state_line({"documents": 284928, "read": 1200, "pages": 50000, "kinds": {"index": 150, "contents": 100}})
        self.assertIn("284,928", line); self.assertIn("contents 100, index 150", line)
        self.assertIn("none", P.state_line({}))

    def test_a_dry_run_asks_only_the_state(self):
        cur = Cursor([{"documents": 10, "read": 0, "pages": 0, "kinds": {}}])
        conn = mock.MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value = cur
        with mock.patch.dict(os.environ, {"SUPABASE_DB_URL": "postgresql://x"}), mock.patch.object(sys, "argv", ["page_kinds.py"]), \
             mock.patch.dict(sys.modules, {"evidence_db": mock.MagicMock(connect=lambda: conn)}), mock.patch.object(P.qa, "Run"), mock.patch("builtins.print"):
            self.assertEqual(P.main(), 0)
        self.assertFalse(any("page_kinds_step" in sql for sql, _ in cur.asked))


if __name__ == "__main__":
    unittest.main()
