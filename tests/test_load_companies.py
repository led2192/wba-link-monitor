import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import load_companies as L  # noqa: E402


class Rows(unittest.TestCase):
    def test_rows_come_from_the_fields_and_need_a_wba_id(self):
        recs = [{"fields": {"wba_id": "PT_00001", "company_name": "Acme", "wba_industry": "Oil & Gas", "hq_country": "USA", "hq_region": "North America", "website": "https://acme.example", "lantern_id": "LN_0a0a0a0a"}},
                {"fields": {"company_name": "No id"}},
                {"fields": {"wba_id": " PT_00002 ", "company_name": "", "wba_industry": "", "lei": " 529900t8bm49aursdo55 "}},
                {"fields": {"wba_id": "LN_deadbeef", "company_name": "Not one", "intake_note": "rejected in lanterN (2026-11-15): a duplicate"}}]
        rows = L.company_rows(recs)
        self.assertEqual(rows[0], ("PT_00001", "Acme", "Oil & Gas", "USA", "North America", "https://acme.example", "LN_0a0a0a0a", None, None))
        self.assertEqual(rows[1], ("PT_00002", None, None, None, None, None, None, "529900T8BM49AURSDO55", None))      # a rejected company is not copied back
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
        L.upsert(Conn(), [("PT_1", "A", None, None, None, None, None), ("PT_2", "B", "Banks", "JPN", "East Asia & Pacific", None, "LN_0b0b0b0b")])
        self.assertIn("create table", calls[0]); self.assertIn("many 2", calls); self.assertEqual(calls[-1], "commit")



class Pages(unittest.TestCase):
    def test_the_pages_watched_of_each_company(self):
        recs = [{"fields": {"wba_id": "PT_1", "urls_total": 6}}, {"fields": {"wba_id": "LN_a"}}, {"fields": {"wba_id": "LN_b", "urls_total": "x"}},
                {"fields": {"urls_total": 3}}, {"fields": {"wba_id": "LN_c", "urls_total": 2, "intake_note": "rejected 25 Nov"}}]
        self.assertEqual(L.pages_rows(recs), [("PT_1", 6), ("LN_a", 0), ("LN_b", 0)])          # no count is none watched; no id and a rejected company are left out


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


    def test_the_fiscal_month_airtable_holds(self):
        recs = [{"fields": {"wba_id": "PT_1", "fy_end_month": 3}}, {"fields": {"wba_id": "PT_2", "fy_end_month": "12"}},
                {"fields": {"wba_id": "PT_3"}}, {"fields": {"wba_id": "PT_4", "fy_end_month": 13}}, {"fields": {"fy_end_month": 6}}]
        self.assertEqual(L.fiscal_rows(recs), [("PT_1", 3), ("PT_2", 12)])                # no month, a month out of range, no company: left out


if __name__ == "__main__":
    unittest.main()
