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


class Anchor(unittest.TestCase):
    """--anchor (9 Nov 2026): another year's known value searched in this year's pages."""
    def test_anchor_for_picks_the_nearest_other_year(self):
        self.assertEqual(M.anchor_for({2023: 4.0, 2024: 5.0, 2025: 6.0}, 2024), (2025, 6.0))   # later year first on a tie
        self.assertEqual(M.anchor_for({2022: 4.0, 2025: 0}, 2024), (2022, 4.0))               # an empty value is not known
        self.assertIsNone(M.anchor_for({2024: 5.0}, 2024)); self.assertIsNone(M.anchor_for(None, 2024))

    def test_anchor_pages_keeps_only_pages_printing_the_value(self):
        class Cur:
            def execute(self, q, params):
                self.q = q
                self.rows = [("d", 3, "Revenue 5,147 4,892\nCosts 100 90"), ("d", 7, "In 2023 we had 4,892 employees"), ("d", 9, "Nothing 4,8921")]
            def fetchall(self):
                return self.rows
        cur = Cur()
        got = M.anchor_pages(cur, ["d"], 4892000000.0, {"unit_family": "currency"})
        self.assertEqual(sorted(got), [("d", 3), ("d", 7)])                 # found_on checked, 4,8921 is not the number
        self.assertIn("to_tsquery", cur.q)
        self.assertEqual(M.anchor_pages(cur, [], 4892000000.0, {"unit_family": "currency"}), {})

    def test_anchor_hint_names_the_other_year_and_forbids_its_value(self):
        h = M.anchor_hint(2024, 2023, 4892000000.0, {"unit_family": "currency"})
        self.assertIn("2023 figure", h); self.assertIn("4,892", h); self.assertIn("Never give", h); self.assertIn("2024 column", h)

    def test_anchor_pages_join_the_pool_and_score_higher(self):
        body = "Group figures 2024 2023\nTurnover 5,147 4,892"
        with_anchor = X.page_score(0.0, None, body, 2024, ["revenue"], [], 0.0, anchor_boost=1.0)
        self.assertAlmostEqual(with_anchor - X.page_score(0.0, None, body, 2024, ["revenue"], [], 0.0), M.ANCHOR_WEIGHT)
        self.assertEqual(X.hints(None, None, 2024, {}), "")
        X.ANCHOR = "note"
        try:
            h = X.hints(None, (2023, 4892000000.0, {("d", 3): body}), 2024, {"unit_family": "currency"})
            self.assertIn("Anchor:", h)
            X.ANCHOR = "pages"
            self.assertEqual(X.hints(None, (2023, 4892000000.0, {("d", 3): body}), 2024, {"unit_family": "currency"}), "")
        finally:
            X.ANCHOR = "off"


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
