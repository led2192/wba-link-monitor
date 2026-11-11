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



class Unheld(unittest.TestCase):
    def test_only_documents_with_an_address_and_no_file(self):
        recs = [{"fields": {"library_id": "PT_1-a", "wba_id": "PT_1", "document_url": "https://x.org/ar-2024.pdf", "doc_label": "2024 | X | Annual Report", "effective_type": "Annual Report", "file_status": {"name": "blocked"}}},
                {"fields": {"library_id": "PT_1-b", "wba_id": "PT_1", "document_url": "https://x.org/b.pdf", "file": [{"url": "u"}]}},
                {"fields": {"library_id": "PT_1-c", "wba_id": "PT_1", "document_url": "upload: by hand"}},
                {"fields": {"library_id": "PT_1-d", "wba_id": "PT_1", "document_url": "https://x.org/d.pdf", "file_status": "dead"}}]
        self.assertEqual(L.unheld_rows(recs), [("PT_1-a", "PT_1", "https://x.org/ar-2024.pdf", "2024 | X | Annual Report", "Annual Report", "blocked"),
                                               ("PT_1-d", "PT_1", "https://x.org/d.pdf", None, None, "dead")])

    def test_the_table_is_the_picture_of_today(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "load_companies.py"), encoding="utf-8").read()
        self.assertIn('delete from public.unheld_documents where updated < %s', src)      # a document that got its file is no longer listed
        self.assertIn("NOT({discard})", L.UNHELD_FORMULA)


if __name__ == "__main__":
    unittest.main()
