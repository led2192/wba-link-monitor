"""evidence_health.unindexed_pages: a count that runs out of time must not spoil the queries after it (20 Nov 2026)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import evidence_health


class Conn:
    def __init__(self): self.rolled = 0
    def rollback(self): self.rolled += 1


class Cur:
    def __init__(self): self.connection = Conn(); self.ran = []
    def execute(self, sql, params=None):
        self.ran.append(sql)
        if "words is null" in sql: raise RuntimeError("canceling statement due to statement timeout")
    def fetchone(self): return (0,)


class UnindexedPages(unittest.TestCase):
    def test_a_timeout_puts_the_connection_back(self):
        cur = Cur()
        self.assertIsNone(evidence_health.unindexed_pages(cur))
        self.assertEqual(cur.connection.rolled, 1)
        self.assertEqual(cur.ran[-1], "set statement_timeout = '120s'")


if __name__ == "__main__":
    unittest.main()
