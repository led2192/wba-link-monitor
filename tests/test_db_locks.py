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
        schema = (5, "u", "", "active", "Lock", "relation", 1394, "-- Schema of the evidence database (Supabase project wba-evidence).\n-- Applied by load_supabase.py on every committed run.\ncreate extension if not exists vector")
        self.assertEqual([r[0] for r in L.waiting_schema([schema])], [5])
        self.assertEqual(L.statement_text("-- a\n-- b\n  ALTER TABLE x"), "alter table x")


if __name__ == "__main__":
    unittest.main()


class Orphans(unittest.TestCase):
    def test_only_statements_older_than_any_job(self):
        import db_locks as D
        rows = [(1, "postgres", "x", "active", "IO", "DataFileRead", 25042, "insert into public.documents"),
                (2, "postgres", "x", "active", None, None, 3600, "select 1"),
                (3, "postgres", "x", "idle in transaction", None, None, 99999, "select 1")]
        self.assertEqual([r[0] for r in D.orphans(rows)], [1])
        self.assertGreater(D.ORPHAN_SECONDS, 360 * 60)                 # longer than GitHub lets a job live
        self.assertEqual([r[0] for r in D.orphans(rows, 300 * 60)], [1])                      # a known-dead job's statement, five hours old
        self.assertEqual([r[0] for r in D.orphans(rows, 60)], [1])                             # never under an hour, whatever is asked

