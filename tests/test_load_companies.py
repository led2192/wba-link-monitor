import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import load_companies as L  # noqa: E402


class Rows(unittest.TestCase):
    def test_rows_come_from_the_fields_and_need_a_wba_id(self):
        recs = [{"fields": {"wba_id": "PT_00001", "company_name": "Acme", "wba_industry": "Oil & Gas", "hq_country": "USA", "hq_region": "North America", "website": "https://acme.example"}},
                {"fields": {"company_name": "No id"}},
                {"fields": {"wba_id": " PT_00002 ", "company_name": "", "wba_industry": ""}}]
        rows = L.company_rows(recs)
        self.assertEqual(rows[0], ("PT_00001", "Acme", "Oil & Gas", "USA", "North America", "https://acme.example"))
        self.assertEqual(rows[1], ("PT_00002", None, None, None, None, None))
        self.assertEqual(len(rows), 2)

    def test_upsert_creates_the_table_and_writes_every_row(self):
        calls = []

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def execute(self, sql, args=None): calls.append(sql.split()[0] + " " + sql.split()[1] if len(sql.split()) > 1 else sql)
            def executemany(self, sql, rows): calls.append(f"many {len(rows)}")

        class Conn:
            def cursor(self): return Cur()
            def commit(self): calls.append("commit")
        L.upsert(Conn(), [("PT_1", "A", None, None, None, None), ("PT_2", "B", "Banks", "JPN", "East Asia & Pacific", None)])
        self.assertIn("create table", calls[0]); self.assertIn("many 2", calls); self.assertEqual(calls[-1], "commit")


if __name__ == "__main__":
    unittest.main()
