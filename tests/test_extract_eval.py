"""Offline tests for extract_eval.py, the generic reader: real table rows and sentences from the 2025
analysts' quotes (company names left out), read with attributes.json, and one end-to-end answer against a
local Postgres."""
import json, os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_eval as X  # noqa: E402

SPEC = json.load(open(X.SPEC, encoding="utf-8"))
A = SPEC["attributes"]
CASES = [
    ("Consolidated statement of income Full year (in USD million) Note 2024 2023 2022 Revenues 7 102,502 106,848 149,004 Net income", 2024, "GMC_00048.1", 102502e6),
    ("in millions of euros Full Year 2024 Revenue 38,153.", 2024, "GMC_00048.1", 38153e6),
    ("Consolidated statement of profit or loss For the year ended 31 December 2024 $M Revenue 30,142.0.", 2024, "GMC_00048.1", 30142e6),
    ("2024 USD’000 Revenue 20,023,110.", 2024, "GMC_00048.1", 20023110e3),
    ("Millions of yen 3/2025 Net sales ￥ 2,561,249.", 2024, "GMC_00048.1", 2561249e6),
    ("In TEUR 2024 Consolidated Income Statement Revenues 4,512,665.", 2024, "GMC_00048.1", 4512665e3),
    ("Revenue (In millions of Argentine Pesos) 12.31.2023 513,727", 2023, "GMC_00048.1", 513727e6),
    ("Revenue 2024 2023 5,000 4,000", 2023, "GMC_00048.1", 4000),
    ("GHG emissions breakdown (in metric tons CO2e Scope 1 6,266,000 Scope 2 location-based 2,569,000 REC sales", 2024, "GMC_00001.1", 6266000),
    ("GHG emissions breakdown (in metric tons CO2e Scope 1 6,266,000 Scope 2 location-based 2,569,000 REC sales", 2024, "GMC_00002.1", 2569000),
    ("Emissions FY21 FY22 FY23 FY24 FY25 Scope 1 3,59,387 3,61,621 4,47,207 4,14,356 7,50,616 Scope 2", 2024, "GMC_00001.1", 414356),
    ("Total Scope 1 emissions Metric tonnes of CO2 equivalent 7,50,616", 2024, "GMC_00001.1", 750616),
    ("Total Emissions - Indirect (Scope 2) Market- Based(7) 858.", 2024, "GMC_00003.1", 858),
]


class Reader(unittest.TestCase):
    def test_real_rows(self):
        for text, year, code, gold in CASES:
            got = X.propose(text, year, A[code], SPEC)
            self.assertTrue(got and X.right(got[0], gold), f"{code} {year}: {got} for {text[:60]}")

    def test_the_table_row_beats_the_prose(self):
        page = ("Our climate strategy covers scope 1, 2 and 3. We aim to reduce Scope 1 and 2 emissions by 42% by 2030 "
                "and to reach 100% renewable electricity. GHG emissions (tCO2e) 2024 2023 Scope 1 24,442 25,100 "
                "Scope 2 (market-based) 53,335 61,020")
        got = X.propose(page, 2024, A["GMC_00001.1"], SPEC)
        self.assertEqual(got[1], 24442)
        got = X.propose(page, 2023, A["GMC_00003.1"], SPEC)
        self.assertEqual(got[1], 61020)

    def test_excluded_neighbours(self):
        self.assertIsNone(X.propose("Scope 1 emissions intensity 0.45 tCO2e per tonne", 2024, A["GMC_00001.1"], SPEC))
        self.assertIsNone(X.propose("Revenue per share 3.10", 2024, A["GMC_00048.1"], SPEC))

    def test_numbers(self):
        self.assertEqual(X.to_number("1.234,5"), 1234.5)
        self.assertEqual(X.to_number("7,50,616"), 750616)
        self.assertEqual(X.to_number("65 534"), 65534)
        self.assertEqual(X.year_header("FY2024 FY2023 Revenue", 2023), 1)
        self.assertIsNone(X.year_header("Revenue 1,234", 2024))

    def test_scales(self):
        self.assertEqual(X.page_scale("(in thousands of euros)", "currency", SPEC), 1e3)
        self.assertEqual(X.page_scale("€m 2024", "currency", SPEC), 1e6)
        self.assertEqual(X.page_scale("MSEK 2024", "currency", SPEC), 1e6)
        self.assertEqual(X.page_scale("Scope 1 (ktCO2e)", "tco2e", SPEC), 1e3)
        self.assertEqual(X.page_scale("Revenue 1,234", "currency", SPEC), 1)


@unittest.skipUnless(os.environ.get("PG_TEST_URL"), "set PG_TEST_URL to a throwaway Postgres (after tests/test_load_supabase.py)")
class AgainstPostgres(unittest.TestCase):
    def test_one_answer_end_to_end(self):
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xeval%'"); c.execute("delete from public.documents where library_id like 'xeval%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values "
                      "('xeval1', 'PT_09998', 'Testco', 2024, 'Annual Report'), ('xeval2', 'PT_09998', 'Testco', 2024, 'Code of Conduct')")
            c.execute("insert into public.pages (library_id, page, body) values "
                      "('xeval1', 5, 'Consolidated income statement (in USD million) 2024 2023 Revenues 102,502 106,848'), "
                      "('xeval2', 1, 'We never talk about revenue figures here')")
            r = X.evaluate(c.cursor(), {"wba": "PT_09998", "year": 2024, "value": 102502e6, "unit": "USD"}, A["GMC_00048.1"], SPEC)
            self.assertTrue(r["docs"] and r["in_docs"] and r["in_pages"] and r["proposal"] and r["right"])
            c.execute("delete from public.pages where library_id like 'xeval%'"); c.execute("delete from public.documents where library_id like 'xeval%'")


if __name__ == "__main__":
    unittest.main()
