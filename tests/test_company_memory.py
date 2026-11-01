"""Offline tests for company_memory.py: the line a company printed a right answer on another year, found in
that year's documents and used to find this year's page (1 Nov 2026)."""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import company_memory as M  # noqa: E402
import extract_eval as X  # noqa: E402

SPEC = json.load(open(X.SPEC, encoding="utf-8"))
A = SPEC["attributes"]


class Lines(unittest.TestCase):
    def test_label_of_a_table_line(self):
        body = "GHG emissions 2024 2023\nDirect GHG emissions (Scope 1) (tCO2e) 14,408 15,230\nScope 2 9,100 9,800"
        self.assertEqual(M.label_of(body, 14408.0), "direct ghg emissions scope tco2e")
        # a table read column by column: the number alone on its line, the words above
        self.assertEqual(M.label_of("Energy and emissions\nDirect GHG emissions (Scope 1)\n14,408\n15,230", 14408.0),
                         "direct ghg emissions scope")
        self.assertEqual(M.label_of("Nothing here 12", 14408.0), "")

    def test_boost_and_hint(self):
        mem = {"year": 2024, "doc_type": "Sustainability Report", "label": "direct ghg emissions scope tco2e", "scale": 1}
        self.assertEqual(M.boost("Direct GHG emissions (Scope 1), tCO2e: 13,950", mem), 1.0)
        self.assertEqual(M.boost("Scope 1 emissions were lower", mem), 0.0)
        self.assertEqual(M.boost("anything", None), 0.0)
        h = M.hint(mem)
        self.assertIn("its 2024 Sustainability Report", h); self.assertIn("\"direct ghg emissions scope tco2e\"", h)
        self.assertNotIn("14", h)                                     # never the value
        self.assertEqual(M.hint(None), "")


class AgainstPostgres(unittest.TestCase):
    def test_memory_from_another_year_brings_this_years_page(self):
        url = os.environ.get("PG_TEST_URL")
        if not url:
            self.skipTest("PG_TEST_URL not set")
        import psycopg
        with psycopg.connect(url) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xmem%'"); c.execute("delete from public.documents where library_id like 'xmem%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values "
                      "('xmem24', 'PT_09994', 'Memco', 2024, 'Sustainability Report'), "
                      "('xmem25', 'PT_09994', 'Memco', 2025, 'Sustainability Report')")
            c.execute("insert into public.pages (library_id, page, body) values "
                      "('xmem24', 31, 'Environmental data 2024 2023\nOperational carbon footprint own plants (tCO2e) 14,408 15,230'), "
                      "('xmem25', 12, 'Our climate strategy and targets for 2030.'), "
                      "('xmem25', 33, 'Environmental data 2025 2024\nOperational carbon footprint own plants (tCO2e) 13,950 14,408')")
            cur = c.cursor(); cur.execute("set statement_timeout = '30s'")
            attr = A["GMC_00001.1"]
            mem = M.memory_for(cur, "PT_09994", attr, 2025, {2024: 14408.0, 2025: 13950.0}, X.year_documents)
            self.assertEqual((mem["year"], mem["library_id"], mem["page"], mem["scale"]), (2024, "xmem24", 31, 1))
            self.assertEqual(mem["label"], "operational carbon footprint own plants tco2e")
            self.assertIsNone(M.memory_for(cur, "PT_09994", attr, 2025, {2025: 13950.0}, X.year_documents))   # never the year read
            self.assertIsNone(M.memory_for(cur, "PT_09994", dict(attr, memory=False), 2025, {2024: 14408.0}, X.year_documents))
            self.assertIs(A["GMC_00064.1"].get("memory"), False)                 # capex: 0 of 10 right with a memory (1 Nov 2026)
            docs, weight = X.year_documents(cur, "PT_09994", 2025, attr)
            ids = [d[0] for d in docs]
            plain = X.ranked_pages(cur, ids, weight, 2025, attr, SPEC)
            self.assertNotIn(33, [p for _, lid, p, _, _ in plain if lid == "xmem25"])     # no Scope 1 label on the page
            rows = X.ranked_pages(cur, ids, weight, 2025, attr, SPEC, memory=mem)
            self.assertIn(("xmem25", 33), [(lid, p) for _, lid, p, _, _ in rows])         # the remembered line brings it in
            c.execute("delete from public.pages where library_id like 'xmem%'"); c.execute("delete from public.documents where library_id like 'xmem%'")


if __name__ == "__main__":
    unittest.main()
