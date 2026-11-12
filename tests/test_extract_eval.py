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


class Ranking(unittest.TestCase):
    def test_a_table_page_with_the_year_beats_prose(self):
        labels, ex = A["GMC_00001.1"]["labels"], A["GMC_00001.1"]["exclude"]
        table = "GHG emissions (tCO2e) 2024 2023 Scope 1 24,442 25,100 Scope 2 53,335 61,020 Scope 3 1,204,000 1,250,000"
        prose = "We report our scope 1 emissions every year and work to reduce scope 1 emissions across our sites."
        t = X.page_score(0.3, X.propose(table, 2024, A["GMC_00001.1"], SPEC) and 6.0, table, 2024, labels, ex, 0.8)
        p = X.page_score(0.5, None, prose, 2024, labels, ex, 0.8)
        self.assertGreater(t, p)

    def test_keep_pages_caps_one_document(self):
        rows = [(10 - i, "big", i, "", None) for i in range(20)] + [(5.5, "small", 1, "", None)]
        kept = X.keep_pages(rows, n=12, per_doc=10)
        self.assertEqual(sum(1 for r in kept if r[1] == "big"), 10)
        self.assertIn("small", [r[1] for r in kept])


class Employees(unittest.TestCase):
    def test_reads_headcount_rows(self):
        a = A["GMC_02526.1"]
        got = X.propose("Our people 2024 2023 Total number of employees 16,682 15,903 of which women 41% 40%", 2024, a, SPEC)
        self.assertTrue(got and X.right(got[0], 16682), got)
        got = X.propose("Employees (in thousands) 2024: Headcount 140.2", 2024, a, SPEC)
        self.assertTrue(got and X.right(got[0], 140200), got)


class Hyphens(unittest.TestCase):
    def test_labels_match_with_or_without_hyphen(self):
        self.assertIn("location based", X.label_variants(["location-based"]))
        self.assertIn("taxonomy-aligned", X.label_variants(["taxonomy aligned"]))
        self.assertIn('"location based"', X.label_query(["location-based"]))
        self.assertTrue(X.label_positions("Scope 2 location based emissions 12,345", ["location-based"], []))
        self.assertTrue(X.label_positions("Scope 2 location-based 12,345", ["location based"], []))


class RetrievalMiss(unittest.TestCase):
    def test_kinds(self):
        from value_eval import printed_forms, EMISSION_SCALES
        forms = printed_forms(24442, EMISSION_SCALES)
        holding = [("d1", 7, "Gross direct emissions tCO2e 24,442 25,100")]
        rows = [(3.0, "d1", 2, "", None), (2.0, "d1", 7, "", None)]
        self.assertEqual(X.retrieval_miss(holding, rows, forms)["kind"], "ranked too low")
        self.assertEqual(X.retrieval_miss(holding, rows, forms)["rank"], 2)
        self.assertEqual(X.retrieval_miss(holding, [(1.0, "d1", 6, "", None)], forms)["kind"], "label on the next or previous page")
        m = X.retrieval_miss(holding, [(1.0, "d9", 1, "", None)], forms)
        self.assertEqual(m["kind"], "no label on the page")
        self.assertIn("gross direct emissions", m["phrases"])


class PagesForModel(unittest.TestCase):
    def test_candidates_first_then_rank(self):
        scored = [(-99.0, None, "a", 1, ""), (0.5 - 0.05, ("c",), "a", 2, ""), (-99.1, None, "a", 3, ""),
                  (0.9 - 0.15, ("c",), "a", 4, ""), (-99.2, None, "a", 5, ""), (-99.25, None, "a", 6, "")]
        got = [x[3] for x in X.pages_for_model(scored, 4)]
        self.assertEqual(got, [4, 2, 1, 3])                 # best candidates, then rank order, no repeats
        self.assertEqual([x[3] for x in X.pages_for_model(scored, 10)], [4, 2, 1, 3, 5, 6])
        self.assertEqual(X.pages_for_model([], 6), [])


class QueryCap(unittest.TestCase):
    def test_a_long_label_list_is_capped(self):
        # 31 Oct 2026: 46 phrases for capex timed out the word index on 31 of 40 answers
        labels = [f"phrase number {i}" for i in range(60)]
        self.assertEqual(X.label_query(labels).count(" or ") + 1, X.MAX_QUERY_LABELS)
        self.assertIn('"phrase number 0"', X.label_query(labels))


class Neighbours(unittest.TestCase):
    """1 Nov 2026: a page next to a well-scored page scores higher, less so two pages away."""
    def test_scores_spread_to_the_next_pages_only(self):
        rows = [(4.0, "L", 10, "a", None), (0.5, "L", 11, "b", None), (0.5, "L", 12, "c", None), (0.5, "L", 15, "d", None),
                (0.5, "M", 11, "e", None)]
        out = {(lid, p): sc for sc, lid, p, *_ in X.with_neighbours(rows)}
        self.assertAlmostEqual(out[("L", 11)], 0.5 + X.NEIGHBOUR_WEIGHT * 1.0)            # next page: the whole share
        self.assertAlmostEqual(out[("L", 12)], 0.5 + X.NEIGHBOUR_WEIGHT * 0.4)            # two pages away: less
        self.assertAlmostEqual(out[("L", 15)], 0.5 + X.NEIGHBOUR_WEIGHT * 0.0)            # five pages away: nothing
        self.assertAlmostEqual(out[("M", 11)], 0.5)                                      # another document: nothing
        self.assertAlmostEqual(out[("L", 10)], 4.0 + X.NEIGHBOUR_WEIGHT * 0.125)          # its own neighbour's share, from the scores before
        self.assertEqual(X.with_neighbours([]), [])

    def test_the_model_is_told_which_pages_follow_each_other(self):
        import model_reader as M
        notes = M.adjacent_notes([("L", 40, "x"), ("M", 3, "y"), ("L", 41, "z"), ("L", 43, "w")])
        self.assertEqual(notes, ["P3 is the page right after P1 in the same document: a table may run from one to the other."])
        self.assertEqual(M.adjacent_notes([("L", 1, "x")]), [])


@unittest.skipUnless(os.environ.get("PG_TEST_URL"), "set PG_TEST_URL to a throwaway Postgres (after tests/test_load_supabase.py)")
class AgainstPostgres(unittest.TestCase):
    def test_a_sibling_attributes_page_brings_in_an_unlabelled_figure(self):
        # 2 Nov 2026: Scope 1 and Scope 2 are printed on the same page 9 times in 10. Here Scope 2 sits on the page
        # after the Scope 1 table under a heading that names neither "Scope 2" nor "location": only the sibling's
        # page brings it into the pool
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xsib%'"); c.execute("delete from public.documents where library_id like 'xsib%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type, period_end) values "
                      "('xsib1', 'PT_09991', 'Sibco', 2024, 'Sustainability Report', '2024-12')")
            rows = [(p, f"Sibco Sustainability Report 2024\nNarrative on page {p}.") for p in range(1, 60) if p not in (40, 41)]
            rows.append((40, "Greenhouse gas emissions (tCO2e) 2024 2023\nScope 1 emissions 4,074 4,500\nof which stationary 3,000 3,300"))
            rows.append((41, "Purchased electricity, grid average factors (tCO2e) 2024 2023\nTotal 12,345 13,000\nRenewable share 40% 35%"))
            c.cursor().executemany("insert into public.pages (library_id, page, body) values ('xsib1', %s, %s)", rows)
            X.SIBLING_CACHE.clear(); X.FY_CACHE.clear() if hasattr(X, "FY_CACHE") else None
            cur = c.cursor()
            sib = X.sibling_pages(cur, "PT_09991", 2024, A["GMC_00002.1"], SPEC)
            self.assertEqual(sib.get(("xsib1", 40)), 1.0); self.assertEqual(sib.get(("xsib1", 41)), 0.6)
            r = X.evaluate(cur, {"wba": "PT_09991", "year": 2024, "value": 12345.0, "unit": "tCO2e"}, A["GMC_00002.1"], SPEC)
            self.assertTrue(r["in_docs"]); self.assertTrue(r["sibling_hit"]); self.assertTrue(r["in_pages"])
            self.assertEqual(X.sibling_pages(cur, "PT_09991", 2024, A["GMC_00001.1"], SPEC), {})     # Scope 1 has no sibling
            X.SIBLING_CACHE.clear()
            c.execute("delete from public.pages where library_id like 'xsib%'"); c.execute("delete from public.documents where library_id like 'xsib%'")

    def test_a_march_year_end_reads_the_document_dated_the_year_after(self):
        # 2 Nov 2026: ORIX. Reporting year 2024 is April 2024 to March 2025: the document dated 2025 (the year its
        # period ends, the library's convention), not the one dated 2024, which is reporting year 2023's
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xfy%'"); c.execute("delete from public.documents where library_id like 'xfy%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type, period_end) values "
                      "('xfy24', 'PT_09994', 'Fyco', 2024, 'Annual Report', '2024-03'), ('xfy25', 'PT_09994', 'Fyco', 2025, 'Annual Report', '2025-03'), "
                      "('xfy26', 'PT_09994', 'Fyco', 2026, 'Annual Report', '2026-03'), ('xcal', 'PT_09993', 'Calco', 2024, 'Annual Report', '2024-12')")
            X.FY_CACHE.clear()
            cur = c.cursor()
            self.assertEqual(X.fy_end_month(cur, "PT_09994"), 3)
            self.assertEqual(X.fy_end_month(cur, "PT_09993"), 12)
            self.assertIsNone(X.fy_end_month(cur, "PT_09992"))
            self.assertEqual(X.document_year(2024, 3), 2025); self.assertEqual(X.document_year(2024, 12), 2024); self.assertEqual(X.document_year(2024, None), 2024)
            docs, weight = X.year_documents(cur, "PT_09994", 2024, A["GMC_00048.1"])
            self.assertEqual([d[0] for d in docs], ["xfy25", "xfy26"])              # 2025 first, 2026 as the comparison column
            self.assertGreater(weight["xfy25"], weight["xfy26"])
            docs, _ = X.year_documents(cur, "PT_09993", 2024, A["GMC_00048.1"])
            self.assertEqual([d[0] for d in docs], ["xcal"])
            # without period_end the month is read from the pages
            c.execute("update public.documents set period_end = null where wba_id = 'PT_09994'")
            c.cursor().executemany("insert into public.pages (library_id, page, body) values ('xfy25', %s, %s)",
                                   [(1, "Fyco Annual Report 2025"), (2, "Consolidated statement of income for the year ended March 31, 2025"),
                                    (3, "This report covers the fiscal year from April 1, 2024 to March 31, 2025.")])
            X.FY_CACHE.clear()
            self.assertEqual(X.fy_end_month(cur, "PT_09994"), 3)
            X.FY_CACHE.clear()
            c.execute("delete from public.pages where library_id like 'xfy%'"); c.execute("delete from public.documents where library_id like 'xfy%'")

    def test_a_table_that_runs_on_to_the_next_page_is_found(self):
        # 1 Nov 2026: the label and the year columns are on page 50, the Scope 1 row on page 51, which carries
        # no label: only its neighbour brings it in
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xnb%'"); c.execute("delete from public.documents where library_id like 'xnb%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values ('xnb1', 'PT_09995', 'Nbco', 2024, 'Sustainability Report')")
            rows = [(p, f"Nbco Sustainability Report 2024\nNarrative on page {p}.") for p in range(1, 80) if p not in (50, 51)]
            rows.append((50, "Environmental performance\nScope 1 emissions by source (tCO2e) 2024 2023\nEnergy consumption (MWh) 12,000 11,000"))
            rows.append((51, "Total 4,074 4,500\nStationary combustion 3,000 3,300\nMobile combustion 1,074 1,200\nWater (m3) 1,200 1,100"))
            c.cursor().executemany("insert into public.pages (library_id, page, body) values ('xnb1', %s, %s)", rows)
            r = X.evaluate(c.cursor(), {"wba": "PT_09995", "year": 2024, "value": 4074.0, "unit": "tCO2e"}, A["GMC_00001.1"], SPEC)
            self.assertTrue(r["in_docs"])
            self.assertTrue(r["neighbour_hit"], "page 51 came in as the neighbour of page 50")
            self.assertTrue(r["in_pages"] and r["neighbour_kept"])
            c.execute("delete from public.pages where library_id like 'xnb%'"); c.execute("delete from public.documents where library_id like 'xnb%'")

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

    def test_the_content_index_brings_in_a_page_with_no_label(self):
        # 31 Oct 2026: the figure sits in a table that names no attribute; the GRI index says "305-1 ... 84"; printed
        # page 84 is pdf page 86 (the pages print their number two behind)
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xidx%'"); c.execute("delete from public.documents where library_id like 'xidx%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values ('xidx1', 'PT_09996', 'Idxco', 2024, 'Sustainability Report')")
            rows = [(p, f"{p - 2} Idxco Sustainability Report 2024\nSome narrative about our strategy on page {p - 2}.") for p in range(3, 120) if p not in (86, 118)]
            rows.append((86, "84 Idxco Sustainability Report 2024\nEnvironmental performance (tCO2e) 2024 2023\nTotal 4,074 4,500\nEnergy (MWh) 12,000 11,000"))
            rows.append((118, "116 GRI content index\nGRI 2: General Disclosures 2021\n2-1 Organizational details 4\n2-7 Employees 45\n"
                              "GRI 305: Emissions 2016\n305-1 Direct (Scope 1) GHG emissions 84\n305-2 Energy indirect (Scope 2) GHG emissions 84\n"
                              "305-3 Other indirect (Scope 3) GHG emissions 87\n305-4 GHG emissions intensity 88\n405-1 Diversity 46"))
            c.cursor().executemany("insert into public.pages (library_id, page, body) values ('xidx1', %s, %s)", rows)
            r = X.evaluate(c.cursor(), {"wba": "PT_09996", "year": 2024, "value": 4074.0, "unit": "tCO2e"}, A["GMC_00001.1"], SPEC)
            self.assertTrue(r["in_docs"]); self.assertTrue(r["index_cited"]); self.assertTrue(r["index_hit"])
            self.assertTrue(r["in_pages"])                                        # no label on page 86: only the index brings it in
            c.execute("delete from public.pages where library_id like 'xidx%'"); c.execute("delete from public.documents where library_id like 'xidx%'")

    def test_more_terms_bring_pages_and_a_slow_query_loses_only_its_own(self):
        # 31 Oct 2026: narrative capex wording in the label query timed out the whole search; now it is a second query
        import psycopg
        with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
            if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                self.skipTest("run tests/test_load_supabase.py first")
            c.execute("delete from public.pages where library_id like 'xmore%'"); c.execute("delete from public.documents where library_id like 'xmore%'")
            c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values ('xmore1', 'PT_09995', 'Moreco', 2024, 'Sustainability Report')")
            c.execute("insert into public.pages (library_id, page, body) values "
                      "('xmore1', 4, 'In 2024 we made investments in energy efficiency of EUR 493.4 million across our plants.'), "
                      "('xmore1', 9, 'Our taxonomy-aligned capex was 12.7 EUR m in 2024.')")
            cur = c.cursor(); cur.execute("set statement_timeout = '30s'")
            attr = dict(A["GMC_00064.1"])
            info = {}
            rows = X.ranked_pages(cur, ["xmore1"], {"xmore1": 1.0}, 2024, attr, SPEC, info)
            self.assertEqual(sorted(p for _, _, p, _, _ in rows), [4, 9]); self.assertEqual(info["more_terms"], "ok")
            real = X.MORE_TERMS_TIMEOUT
            X.MORE_TERMS_TIMEOUT = "1ms"
            try:
                cur.execute("select pg_sleep(0)")                          # the connection still works after a timeout
                info = {}
                rows = X.ranked_pages(cur, ["xmore1"], {"xmore1": 1.0}, 2024, attr, SPEC, info)
            finally:
                X.MORE_TERMS_TIMEOUT = real
            self.assertIn(9, [p for _, _, p, _, _ in rows])                # the label query's pages are kept
            cur.execute("show statement_timeout"); self.assertEqual(cur.fetchone()[0], "30s")
            cur.execute("select 1"); self.assertEqual(cur.fetchone()[0], 1)
            c.execute("delete from public.pages where library_id like 'xmore%'"); c.execute("delete from public.documents where library_id like 'xmore%'")



class Oracle(unittest.TestCase):
    def test_pages_printing_the_figure_come_first_in_ranked_order(self):
        rows = [(9, "d1", 3, "a", None), (8, "d1", 7, "b", None), (7, "d2", 1, "c", None)]
        holding = [("d2", 1, "c"), ("d9", 4, "z"), ("d1", 7, "b")]        # d9 p4 was never ranked: it goes last
        best = [(1, None, "d1", 3, "a"), (1, None, "d1", 7, "b"), (1, None, "d3", 2, "e")]
        got = X.oracle_pages(holding, rows, best, n=4)
        self.assertEqual([(x[2], x[3]) for x in got], [("d1", 7), ("d2", 1), ("d9", 4), ("d1", 3)])

    def test_never_more_than_the_model_pages(self):
        holding = [("d", i, "x") for i in range(20)]
        self.assertEqual(len(X.oracle_pages(holding, [], [], n=8)), 8)

    def test_oracle_follows_the_model_pages_setting(self):
        holding = [("d", i, "x") for i in range(20)]
        saved = X.MODEL_PAGES
        try:
            X.MODEL_PAGES = 16
            self.assertEqual(len(X.oracle_pages(holding, [], [])), 16)
        finally:
            X.MODEL_PAGES = saved

    def test_pages_for_model_takes_n(self):
        scored = [(1.0, "c" if i < 3 else None, "d", i, "x") for i in range(25)]
        self.assertEqual(len(X.pages_for_model(scored, 16)), 16)
        self.assertEqual(len(X.pages_for_model(scored, 8)), 8)

    def test_consensus_counts_pages_and_documents_printing_the_figure(self):
        class Cur:
            def execute(self, q, params=None):
                self.rows = [("d1", 7, "Revenue 5,147 4,892"), ("d1", 9, "Revenue of 5,147 million"), ("d2", 3, "Group revenue 5,147"), ("d2", 4, "5,1470 nothing")]
            def fetchall(self): return self.rows
        self.assertEqual(X.consensus(Cur(), ["d1", "d2"], 5147000000.0, X.scales_for("currency")), (3, 2))
        self.assertEqual(X.consensus(Cur(), [], 5147000000.0, X.scales_for("currency")), (0, 0))
        self.assertEqual(X.consensus(Cur(), ["d1"], None, X.scales_for("currency")), (0, 0))

    def test_escalation_choice(self):
        a, b, c = {"value": 100.0}, {"value": 200.0}, {"value": 201.0}
        self.assertIs(X.escalation_choice(None, None, b), b)              # empty first level: the second level's figure
        self.assertIs(X.escalation_choice(a, None, None), a)
        self.assertIs(X.escalation_choice(a, [100.0, 200.0], c), c)      # the second level sides with the second read
        self.assertIs(X.escalation_choice(a, [100.0, 200.0], {"value": 100.4}), a)
        self.assertIs(X.escalation_choice(a, [100.0, 200.0], {"value": 999.0}), a)   # agrees with nobody: the first stays

    def test_quorum_helpers(self):
        self.assertTrue(X.same_value(1000.0, 1003.0)); self.assertTrue(X.same_value(None, 0))
        self.assertFalse(X.same_value(1000.0, None)); self.assertFalse(X.same_value(1000.0, 1200.0))
        pages = [("d", i, f"p{i}") for i in range(1, 5)]
        self.assertEqual([p[1] for p in X.read_order(pages, 1)], [4, 3, 2, 1])
        self.assertEqual([p[1] for p in X.read_order(pages, 2)], [3, 4, 1, 2])
        self.assertEqual(X.read_order([], 2), [])

    def test_figure_frequency_census_and_boost(self):
        self.assertEqual([X.num_key(s) for s in ("5,147.3", "5.147", "5 147", "12,345.50", "12345")], ["5147", "5147", "5147", "12345", "12345"])
        class Cur:
            def __init__(self):
                self.calls = []
            def execute(self, q, params=None):
                self.calls.append(q)
                if q.startswith("show"):
                    self.rows = [("30s",)]
                elif "regexp_matches" in q:
                    self.rows = [("5,147", 4), ("5.147", 2), ("4,892", 3), ("123", 5)]
            def fetchone(self):
                return self.rows[0]
            def fetchall(self):
                return self.rows
            class connection:
                @staticmethod
                def transaction():
                    import contextlib
                    return contextlib.nullcontext()
        cur = Cur()
        rec = X.recurring_numbers(cur, ["d"])
        self.assertEqual(rec, {"5147": 4, "4892": 3})                    # keys merge printed forms; short numbers left out
        self.assertTrue(any("set statement_timeout = '30s'" in q for q in cur.calls))   # the timeout restored
        self.assertEqual(X.frequency_boost("Revenue 5,147 4,892\nCosts 1,234 999", rec), 0.4)
        self.assertEqual(X.frequency_boost("Revenue 5,147", {}), 0.0)
        body = "Key figures 2024 2023\nTurnover 5,147 4,892"
        plain = X.page_score(0.0, None, body, 2024, ["revenue"], [], 0.0)
        self.assertAlmostEqual(X.page_score(0.0, None, body, 2024, ["revenue"], [], 0.0, freq_boost=0.4) - plain, X.FREQ_WEIGHT * 0.4)

    def test_keys_are_stable_and_say_nothing(self):
        self.assertEqual(X.short_hash("PT_00001|2024|GMC"), X.short_hash("PT_00001|2024|GMC"))
        self.assertEqual(len(X.short_hash("x")), 12)
        self.assertNotIn("PT", X.short_hash("PT_00001"))

if __name__ == "__main__":
    unittest.main()


class BM25Pool(unittest.TestCase):
    """--bm25-pool (7 Nov 2026): pages found by BM25 over any label word join the pool; never replaces it."""

    class Cur:
        def __init__(self, rows):
            self.rows, self.last = rows, None
            import contextlib
            self.connection = type("C", (), {"transaction": lambda _s: contextlib.nullcontext()})()

        def execute(self, sql, args=None):
            self.last = sql

        def fetchone(self):
            return ("30s",)

        def fetchall(self):
            return self.rows

    def setUp(self):
        from page_ranker import RANKER
        self.R, self.saved = RANKER, RANKER._ensure
        RANKER._ensure = lambda cur, terms: None
        RANKER.avg_len, RANKER.idfs = 50, {"employees": 2.0, "headcount": 3.0, "total": 0.5, "number": 0.5}

    def tearDown(self):
        self.R._ensure = self.saved

    def test_adds_new_pages_best_first_scaled_to_the_pool(self):
        cur = self.Cur([("d", 1, "employees employees headcount 12,000"), ("d", 2, "nothing relevant here"),
                        ("d", 3, "employees 900"), ("d", 4, "already in the pool employees")])
        attr = {"labels": ["total number of employees"], "more_terms": ["headcount"]}
        got = X.bm25_pool_pages(cur, ["d"], attr, {("d", 4)}, 2, 0.4)
        self.assertEqual([(l, p) for l, p, _, _ in got], [("d", 1), ("d", 3)])
        self.assertAlmostEqual(got[0][3], 0.4)          # the best gets the pool's best ts rank
        self.assertLess(got[1][3], 0.4)

    def test_off_and_failures_add_nothing(self):
        attr = {"labels": ["employees"]}
        self.assertEqual(X.bm25_pool_pages(self.Cur([]), ["d"], attr, set(), 0, 0.4), [])

        class Broken(self.Cur):
            def fetchall(self):
                raise RuntimeError("timeout")
        self.assertEqual(X.bm25_pool_pages(Broken([]), ["d"], attr, set(), 5, 0.4), [])


class WithKnown(unittest.TestCase):
    def test_known_other_year_keeps_companies_with_two_validated_years(self):
        import extract_eval as X
        g = lambda w, c, y, v=1.0: {"wba": w, "attribute": c, "year": y, "value": v}
        rows = [g("A", "X", 2023), g("A", "X", 2024), g("B", "X", 2024), g("C", "X", 2023), g("C", "X", 2024, None), g("A", "Y", 2024)]
        got = X.known_other_year(rows)
        self.assertEqual([(r["wba"], r["year"]) for r in got], [("A", 2023), ("A", 2024)])   # B one year; C's other year has no value; Y one year


class WholeDocument(unittest.TestCase):
    """--select whole (10 Nov 2026): every page of the year's documents in one call, up to a character budget."""

    class Cur:
        def __init__(self, pages):
            self.pages, self.lid = pages, None

        def execute(self, sql, args=None):
            self.lid = args[0]

        def fetchall(self):
            return [(p, b) for l, p, b in self.pages if l == self.lid]

    def test_the_document_holding_the_chosen_pages_is_read_whole_first_in_page_order(self):
        import extract_eval as X
        pages = [("A", 1, "a" * 10), ("A", 2, "b" * 10), ("A", 3, "c" * 10), ("B", 1, "d" * 10), ("B", 2, "e" * 10)]
        docs = [("A", "Annual Report", 2024), ("B", "Sustainability Report", 2024)]
        best = [(1.0, None, "B", 2, "e" * 10), (0.9, None, "B", 1, "d" * 10), (0.8, None, "A", 3, "c" * 10)]
        got = X.whole_document_pages(self.Cur(pages), docs, best, budget=1000)
        self.assertEqual([(l, p) for _, _, l, p, _ in got], [("B", 1), ("B", 2), ("A", 1), ("A", 2), ("A", 3)])
        self.assertTrue(all(s == 0.0 and c is None for s, c, *_ in got))

    def test_the_budget_cuts_the_second_document_and_counts_a_page_up_to_page_chars(self):
        import extract_eval as X, model_reader as M
        pages = [("A", 1, "a" * (M.PAGE_CHARS + 5000)), ("A", 2, "b" * 100), ("B", 1, "c" * 100), ("B", 2, "d" * 100)]
        docs = [("A", "Annual Report", 2024), ("B", "Sustainability Report", 2024)]
        got = X.whole_document_pages(self.Cur(pages), docs, [], budget=M.PAGE_CHARS + 250)
        self.assertEqual([(l, p) for _, _, l, p, _ in got], [("A", 1), ("A", 2), ("B", 1)])   # the long page counts PAGE_CHARS; B2 does not fit
        self.assertEqual(X.whole_document_pages(self.Cur(pages), docs, [], budget=10), [])    # no page fits: the caller keeps today's pages

    def test_the_budget_follows_the_model_unless_given(self):
        import extract_eval as X
        old = os.environ.get("READER_MODEL")
        try:
            os.environ["READER_MODEL"] = "gemini-3.8-flash"
            self.assertEqual(X.whole_chars(), 1_200_000)
            os.environ["READER_MODEL"] = "claude-haiku-5-5"
            self.assertEqual(X.whole_chars(), 600_000)
            X.WHOLE_CHARS = 250_000
            self.assertEqual(X.whole_chars(), 250_000)
        finally:
            X.WHOLE_CHARS = 0
            if old is None:
                os.environ.pop("READER_MODEL", None)
            else:
                os.environ["READER_MODEL"] = old

    def test_a_long_call_gets_one_adjacency_line_per_run(self):
        import model_reader as M
        pages = [("A", p, "x") for p in range(1, 21)] + [("B", 7, "y")] + [("B", 9, "y"), ("B", 10, "y")]
        notes = M.adjacent_notes(pages)
        self.assertEqual(notes, ["P1 to P20 are consecutive pages of one document, in order: a table may run from one to the next.",
                                 "P22 to P23 are consecutive pages of one document, in order: a table may run from one to the next."])
        short = [("A", p, "x") for p in range(1, 4)]                 # 16 pages or fewer: today's lines, unchanged
        self.assertEqual(len(M.adjacent_notes(short)), 2)
        self.assertTrue(M.adjacent_notes(short)[0].startswith("P2 is the page right after P1"))


class DocumentChunks(unittest.TestCase):
    """--escalate whole (10 Nov 2026): the whole document in consecutive chunks that fit the model's window."""

    def test_pages_are_cut_into_consecutive_chunks_by_characters(self):
        import extract_eval as X, model_reader as M
        pages = [(0.0, None, "A", p, "x" * 100) for p in range(1, 8)]
        chunks = X.document_chunks(pages, 250)
        self.assertEqual([[t[3] for t in c] for c in chunks], [[1, 2], [3, 4], [5, 6], [7]])
        long = [(0.0, None, "A", 1, "y" * (M.PAGE_CHARS + 1000)), (0.0, None, "A", 2, "z" * 100)]
        self.assertEqual(len(X.document_chunks(long, M.PAGE_CHARS + 50)), 2)      # a page counts PAGE_CHARS at most
        self.assertEqual(X.document_chunks([], 1000), [])

    def test_the_escalation_measure_reads_the_page_body(self):
        import extract_eval as X
        more = [(1.0, "A", 3, "Scope 1 emissions 12,345 tCO2e", {"value": 1})]
        forms = X.printed_forms(12345.0, X.EMISSION_SCALES)
        hits = [X.found_on(b, v) for _, _, _, b, _ in more for v, *_ in forms]
        self.assertTrue(any(hits))


class SemanticPool(unittest.TestCase):
    """--semantic-pool: pages nearest in meaning, added to the pool. Offline: the embedding and the database are stubs."""

    class Cur:
        def __init__(self, rows):
            self.rows, self.sql, self.args = rows, [], []
            self.connection = self

        def transaction(self):
            import contextlib
            return contextlib.nullcontext()

        def execute(self, q, args=None):
            self.sql.append(" ".join(q.split())); self.args.append(args)

        def fetchone(self):
            return ("120s",)

        def fetchall(self):
            return list(self.rows)

    def test_what_is_asked(self):
        self.assertEqual(X.semantic_query({"name": "Revenue", "labels": ["total revenue", "net sales", "turnover"]}), "Revenue: total revenue, net sales, turnover")
        self.assertEqual(X.semantic_query({"name": "Revenue", "labels": ["a"], "semantic_query": "Total revenue for the fiscal year."}), "Total revenue for the fiscal year.")
        self.assertEqual(X.semantic_query({"name": "Employees"}), "Employees")
        self.assertEqual(X.semantic_query({}), "")

    def test_adds_the_nearest_new_pages_scaled_to_the_pool(self):
        cur = self.Cur([("d", 7, "our workforce totalled 45,000", 0.10), ("d", 4, "already in the pool", 0.12), ("d", 9, "plantilla media 44.000", 0.28),
                        ("e", 2, "something else", 0.55)])
        got = X.semantic_pool_pages(cur, ["d", "e"], {"name": "Employees", "labels": ["number of employees"]}, {("d", 4)}, 2, 0.4, embed=lambda t: [0.1, 0.2])
        self.assertEqual([(l, p) for l, p, _, _ in got], [("d", 7), ("d", 9)])
        self.assertAlmostEqual(got[0][3], 0.4); self.assertAlmostEqual(got[1][3], 0.4 * 0.72 / 0.90)
        q = next(x for x in cur.sql if "public.chunks" in x)
        self.assertIn("c.library_id = any(%s)", q); self.assertIn("group by 1, 2", q)       # exact, inside these documents, by page
        a = cur.args[cur.sql.index(q)]
        self.assertEqual((a[0], a[1], a[2]), ("[0.100000,0.200000]", ["d", "e"], 3))             # k plus the pages the pool holds
        self.assertIn("set statement_timeout = '120s'", cur.sql[-1])                             # the caller's timeout is put back

    def test_off_no_passages_and_failures_add_nothing(self):
        attr = {"name": "Employees", "labels": ["employees"]}
        self.assertEqual(X.semantic_pool_pages(self.Cur([]), ["d"], attr, set(), 0, 0.4, embed=lambda t: [1.0]), [])
        self.assertEqual(X.semantic_pool_pages(self.Cur([]), ["d"], attr, set(), 5, 0.4, embed=lambda t: [1.0]), [])      # documents with no passages
        self.assertEqual(X.semantic_pool_pages(self.Cur([]), [], attr, set(), 5, 0.4, embed=lambda t: [1.0]), [])

        def no_model(t):
            raise ImportError("sentence_transformers")
        self.assertEqual(X.semantic_pool_pages(self.Cur([("d", 1, "x", 0.1)]), ["d"], attr, set(), 5, 0.4, embed=no_model), [])

        class Broken(self.Cur):
            def fetchall(self):
                raise RuntimeError("timeout")
        self.assertEqual(X.semantic_pool_pages(Broken([]), ["d"], attr, set(), 5, 0.4, embed=lambda t: [1.0]), [])

    def test_off_by_default(self):
        self.assertEqual(X.SEMANTIC_POOL, 0)

