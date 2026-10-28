"""Offline test for base_health.evidence_backlog: pages files of the store not in load_log are counted,
with their size and the day of the last load; without credentials it returns None."""
import datetime as dt, os, sys, types, unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import base_health as B  # noqa: E402


class Backlog(unittest.TestCase):
    def test_without_credentials(self):
        with mock.patch.dict(os.environ, {"SUPABASE_DB_URL": "", "TEXT_STORE_TOKEN": ""}):
            self.assertIsNone(B.evidence_backlog())

    def test_counts_files_waiting(self):
        import load_supabase as L, text_core as tc
        listing = {"pages-a.jsonl.gz": {"size": 5_000_000}, "pages-b.jsonl.gz": {"size": 7_000_000}}
        cur = mock.MagicMock(); cur.fetchone.return_value = (dt.date(2026, 10, 27),)
        conn = mock.MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        store = types.SimpleNamespace(assets=lambda: listing)
        with mock.patch.dict(os.environ, {"SUPABASE_DB_URL": "x", "TEXT_STORE_TOKEN": "x"}), \
             mock.patch.object(tc, "open_store", lambda: store), mock.patch.object(L, "connect", lambda url: conn), \
             mock.patch.object(L, "loaded_names", lambda c: {"pages-a.jsonl.gz"}), \
             mock.patch.object(L, "plan", lambda lst, loaded: ([n for n in lst if n not in loaded], [])):
            self.assertEqual(B.evidence_backlog(), (1, 7.0, dt.date(2026, 10, 27)))


if __name__ == "__main__":
    unittest.main()
