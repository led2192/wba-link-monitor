import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import db_locks as L  # noqa: E402


class Waiting(unittest.TestCase):
    def test_only_schema_statements_waiting_for_a_lock(self):
        rows = [(1, "u", "", "active", "Lock", "relation", 900, "alter table public.pages add column if not exists words tsvector"),
                (2, "u", "", "active", "Lock", "relation", 10, "select library_id from public.documents where wba_id = $1"),
                (3, "u", "", "active", None, None, 5, "create table if not exists public.page_map (x int)"),
                (4, "u", "", "active", "Lock", "relation", 30, "  CREATE INDEX if not exists page_map_words on public.page_map using gin (words)")]
        self.assertEqual([r[0] for r in L.waiting_schema(rows)], [1, 4])


if __name__ == "__main__":
    unittest.main()
