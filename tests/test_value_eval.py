"""Offline tests for value_eval.py: how printed numbers are generated, indexed and read back. The cases
are real analyst quotes from the 2025 gold answers (company names left out)."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import value_eval as V  # noqa: E402


class Reading(unittest.TestCase):
    def test_numbers_as_reports_print_them(self):
        cases = [("Revenue (In millions) 12.31.2023 513,727", 513727),
                 ("Net operating revenue 40,181,552.", 40181552),
                 ("Revenue from operations 799,366.15.", 799366.15),
                 ("Notes 2024 Rm [.]Total net income1 65 534.", 65534),
                 ("Revenues 7 102,502 106,848 149,004", 102502),           # a table row: numbers side by side
                 ("Total Operating Revenue 27,554,827,451.25 63,238", 27554827451.25),
                 ("$9.83 Billion Total Sales.", 9.83),
                 ("$M Revenue 30,142.0.", 30142.0),
                 ("Revenue 2.561.249", 2561249),
                 ("Total Scope 1 emissions Metric tonnes of CO2 equivalent 7,50,616", 750616),   # Indian grouping
                 ("Scopes 1) tCO2e 248,248.44 245,133.97", 248248.44)]
        for text, r in cases:
            self.assertTrue(V.found_on(text, r), text)

    def test_no_match_inside_a_bigger_number(self):
        self.assertFalse(V.found_on("total 1,513,727", 513727))
        self.assertFalse(V.found_on("total 513,7271", 513727))

    def test_value_and_placeholders(self):
        self.assertEqual(V.parse_value("513727000000 ARS"), (513727000000.0, "ARS"))
        self.assertEqual(V.parse_value("1 ZAR"), (None, "ZAR"))
        self.assertEqual(V.parse_value(" "), (None, ""))
        self.assertEqual(V.parse_value("858000 tCO2e"), (858000.0, "tCO2e"))


class Forms(unittest.TestCase):
    def test_scales_and_significant_digits(self):
        forms = V.printed_forms(513727000000, V.CURRENCY_SCALES)
        self.assertIn((513727.0, 6, 0), forms)                         # in millions
        self.assertIn((9.83, 3, 2), V.printed_forms(9830000000, V.CURRENCY_SCALES))   # in billions
        self.assertEqual(V.printed_forms(858000, V.EMISSION_SCALES), [(858000.0, 3, 0), (858.0, 3, 0)])
        forms = V.printed_forms(248248.44, V.EMISSION_SCALES)                # the decimals are kept as a form
        self.assertIn((248248.44, 8, 2), forms)
        self.assertIn((248248.0, 6, 0), forms)

    def test_index_query(self):
        self.assertEqual(V.tsquery_for(513727, 0),
                         "(513 <-> 727) | 513727 | (513 <-> 727.0) | 513727.0 | (513 <-> 727.00) | 513727.00 | 513.727 | (5 <-> 13 <-> 727)")
        self.assertEqual(V.tsquery_for(799366.15, 2), "(799 <-> 366.15) | 799366.15 | (7 <-> 99 <-> 366.15)")
        self.assertIn("(7 <-> 50 <-> 616)", V.tsquery_for(750616, 0))

    def test_attribute_words(self):
        cfg = {"attributes": {"R": {"name": "Revenue"}, "S": {"name": "Scope 1 emissions"}}}
        self.assertTrue(V.has_words("Net sales 1,234", V.attr_words("R", cfg)))
        self.assertTrue(V.has_words("Scope 1 (tCO2e)", V.attr_words("S", cfg)))
        self.assertFalse(V.has_words("Scope 12", V.attr_words("S", cfg)))


@unittest.skipUnless(os.environ.get("PG_TEST_URL"), "set PG_TEST_URL to a throwaway Postgres (after tests/test_load_supabase.py)")
class AgainstPostgres(unittest.TestCase):
    def test_found_in_another_document_of_the_company(self):
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'veval%'"); c.execute("delete from public.documents where library_id like 'veval%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values "
                      "('veval1', 'PT_09999', 'Testco', 2024, 'Annual Report'), ('veval2', 'PT_09999', 'Testco', 2021, 'Annual Report')")
            c.execute("insert into public.pages (library_id, page, body) values "
                      "('veval1', 7, 'Consolidated statement (in millions) Revenue 513,727 498,100'), "
                      "('veval2', 3, 'Total 1,513,727 old figure')")
            cur = c.cursor()
            g = {"wba": "PT_09999", "year": 2024, "value": 513727000000.0, "unit": "USD"}
            r = V.evaluate(cur, g, ["revenue", "sales"], V.CURRENCY_SCALES)
            self.assertTrue(r["found_any"] and r["found_year"] and r["found_words"])
            self.assertFalse(r["short"])
            g2 = dict(g, year=2021)
            self.assertFalse(V.evaluate(cur, g2, ["revenue"], V.CURRENCY_SCALES)["found_year"])   # 1,513,727 is not it
            c.execute("delete from public.pages where library_id like 'veval%'"); c.execute("delete from public.documents where library_id like 'veval%'")



class GoldDuplicates(unittest.TestCase):
    def test_an_answer_in_two_bases_counts_once(self):
        import value_eval as VE
        rows = {"a": [{"fields": {"Metadata ID": "PT_00001-GMC_00048.1-2024", "Selected Value": "100 USD"}}],
                "b": [{"fields": {"Metadata ID": "PT_00001-GMC_00048.1-2024", "Selected Value": "100 USD"}},
                      {"fields": {"Metadata ID": "PT_00001-GMC_02526.1-2024", "Selected Value": "16682 "}}]}
        real = VE.get_all
        VE.get_all = lambda token, base, table, fields, formula=None: rows[base]
        VE._SEEN_IDS.clear()
        try:
            got = VE.gold_values("t", "a", ["GMC_00048.1", "GMC_02526.1"]) + VE.gold_values("t", "b", ["GMC_00048.1", "GMC_02526.1"])
        finally:
            VE.get_all = real; VE._SEEN_IDS.clear()
        self.assertEqual([g["attribute"] for g in got], ["GMC_00048.1", "GMC_02526.1"])
        self.assertEqual(got[1]["value"], 16682)


if __name__ == "__main__":
    unittest.main()
