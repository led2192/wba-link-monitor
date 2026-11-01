"""Offline tests for propose_values.py: deterministic ids, the comparison with the validated answer, and
rows a person decided left alone."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import propose_values as P  # noqa: E402

FACT = (0, "us-gaap:Revenues", 2024, "2024-12-31", 102502e6, "USD", "10-K", "2025-02-20")
NOW = "2026-10-29T12:00:00.000Z"


class Propose(unittest.TestCase):
    def test_row_and_validated_answer(self):
        gold = {("PT_1", "GMC_00048.1", 2024): {"value": 102500e6, "unit": "USD"}}
        f = P.row("PT_1", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", gold, "recA", NOW)
        self.assertEqual(f["value_id"], "PT_1|GMC_00048.1|2024|SEC XBRL")
        self.assertEqual((f["company"], f["value"], f["unit"], f["matches_validated"]), (["recA"], 102502e6, "USD", "match"))
        gold[("PT_1", "GMC_00048.1", 2024)] = {"value": 102502e3, "unit": "USD"}       # a factor of 1,000
        self.assertEqual(P.row("PT_1", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", gold, "recA", NOW)["matches_validated"], "differs")
        f = P.row("PT_1", "GMC_00048.1", "Revenue", 2023, FACT, "SEC XBRL", "u", gold, None, NOW)
        self.assertEqual(f["matches_validated"], "no validated answer"); self.assertNotIn("company", f)

    def test_where_the_analyst_took_the_value_from(self):
        g = {"value": 10_886_000.0, "unit": "tCO2e", "quote": "Direct GHG emissions: 10.886 million tonnes", "page": "84",
             "editor_url": "https://airtable.com/appX/pagY/recZ", "sources": [{"name": "PT_1-2024-ESG", "url": "https://x.com/esg2024.pdf", "year": "2024"}]}
        f = P.row("PT_1", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {("PT_1", "GMC_00048.1", 2024): g}, None, NOW)
        self.assertEqual((f["validated_source"], f["validated_source_name"], f["validated_page"], f["validated_record"]),
                         ("https://x.com/esg2024.pdf", "PT_1-2024-ESG", "84", "https://airtable.com/appX/pagY/recZ"))
        self.assertIn("10.886", f["validated_quote"])
        bare = P.validated_fields({"value": 1.0, "editor_url": "#RECORD_ID#"})                       # nothing cited
        self.assertTrue(all(v is None for v in bare.values()))

    def test_plan_never_touches_a_decision(self):
        rows = [P.row(w, "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {}, None, NOW) for w in ("A", "B", "C")]
        existing = {"B|GMC_00048.1|2024|SEC XBRL": {"id": "recB", "status": "accepted"},
                    "C|GMC_00048.1|2024|SEC XBRL": {"id": "recC", "status": "proposed"}}
        creates, updates, kept = P.plan(rows, existing, NOW)
        self.assertEqual([f["value_id"][0] for f in creates], ["A"])
        self.assertEqual((creates[0]["status"], creates[0]["first_proposed"]), ("proposed", NOW))
        self.assertEqual([u[0] for u in updates], ["recC"]); self.assertNotIn("status", updates[0][1])
        self.assertEqual(kept, 1)

    def test_rows_no_longer_proposed_are_superseded(self):
        rows = [P.row("A", "GMC_00048.1", "Revenue", 2024, FACT, "SEC XBRL", "u", {}, None, NOW)]
        existing = {"B|GMC_00048.1|2024|SEC XBRL": {"id": "recB", "status": "proposed"},
                    "C|GMC_00048.1|2024|SEC XBRL": {"id": "recC", "status": "accepted"},
                    "D|GMC_00048.1|2019|SEC XBRL": {"id": "recD", "status": "proposed"}}
        in_scope = lambda vid: vid.endswith("|2024|SEC XBRL")
        _, updates, _ = P.plan(rows, existing, NOW, in_scope)
        self.assertEqual(updates, [("recB", {"status": "superseded", "last_run": NOW})])

    def test_model_row_names_the_column(self):
        # Eutelsat, 30 Oct 2026: the FY25 column (July 2024 to June 2025) is reporting year 2024, which a reviewer doubted
        got = {"value": 630.52, "printed": 630.52, "scale": 1, "library_id": "PT_2-x", "page": 1, "unit": "tCO2e", "column": "FY25"}
        d = P.model_row("PT_2", "GMC_00001.1", {"name": "Scope 1"}, 2024, got, {}, {}, None, NOW)["detail"]
        self.assertIn('in the column "FY25" on page 1', d); self.assertIn("so this is 2024", d)
        got["column"] = "2024"
        self.assertNotIn("fiscal year", P.model_row("PT_2", "GMC_00001.1", {"name": "Scope 1"}, 2024, got, {}, {}, None, NOW)["detail"])
        del got["column"]
        self.assertIn("(units) on page 1", P.model_row("PT_2", "GMC_00001.1", {"name": "Scope 1"}, 2024, got, {}, {}, None, NOW)["detail"])

    def test_model_row(self):
        got = {"value": 2804e6, "printed": 2804.0, "scale": 1e6, "library_id": "PT_1-abc", "page": 57, "unit": "EUR"}
        doc = {"id": "recDoc", "url": "https://example.com/ar.pdf", "label": "2024 | Testco | Annual Report"}
        gold = {("PT_1", "GMC_00048.1", 2024): {"value": 2804e6, "unit": "EUR"}}
        f = P.model_row("PT_1", "GMC_00048.1", {"name": "Revenue"}, 2024, got, doc, gold, "recCo", NOW)
        self.assertEqual(f["value_id"], "PT_1|GMC_00048.1|2024|model reader")
        self.assertEqual((f["method"], f["page"], f["document"], f["company"]), ("model reader", 57, ["recDoc"], ["recCo"]))
        self.assertEqual(f["source_url"], "https://example.com/ar.pdf#page=57")
        self.assertIn("Printed as 2,804 (millions) on page 57 of 2024 | Testco | Annual Report", f["detail"])
        self.assertEqual(f["matches_validated"], "match")
        f = P.model_row("PT_2", "GMC_00001.1", {"name": "Scope 1", "unit_family": "tco2e"}, 2024, dict(got, unit=""), {}, {}, None, NOW)
        self.assertEqual((f["unit"], f["source_url"], f["matches_validated"]), ("tCO2e", None, "no validated answer"))
        self.assertNotIn("document", f)

    def test_printed_text_keeps_every_digit(self):
        self.assertEqual(P.printed_text(41709.13), "41,709.13")
        self.assertEqual(P.printed_text(2804.0), "2,804")
        self.assertEqual(P.printed_text(5206474.45), "5,206,474.45")

    def test_the_model_reader_stops_at_the_time_budget(self):
        # Propose values #43 (30 Oct 2026) read for two hours, hit the job limit and wrote nothing
        import types, collections, psycopg, extract_eval
        class Cur:
            def execute(self, *a): pass
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cur()
        reads = []
        real = (psycopg.connect, extract_eval.read_values, P.T0)
        psycopg.connect = lambda *a, **k: Conn()
        extract_eval.read_values = lambda *a, **k: reads.append(a) or []
        os.environ["ANTHROPIC_API_KEY"] = "k"; os.environ["SUPABASE_DB_URL"] = "postgres://x"
        a = types.SimpleNamespace(attributes="GMC_00001.1", model_years="2024", companies="PT_1,PT_2", limit=0, max_usd=1.0, budget=1)
        spec = {"GMC_00001.1": {"name": "Scope 1", "type": "number"}}
        try:
            P.T0 = P.time.time() - 120                                              # two minutes in, budget one
            c = collections.Counter()
            self.assertEqual(P.model_proposals(a, spec, [], {}, {}, {}, {}, "b", NOW, c), [])
            self.assertEqual((reads, c["model reader: stopped at the time budget"]), ([], 1))
            P.T0 = P.time.time()
            P.model_proposals(a, spec, [], {}, {}, {}, {}, "b", NOW, collections.Counter())
            self.assertEqual(len(reads), 2)
        finally:
            psycopg.connect, extract_eval.read_values, P.T0 = real

    def test_a_year_asked_is_read_and_its_own_figure_wins(self):
        # 31 Oct 2026: a 2025 read returns 2025, 2024 and 2023. 1 Nov 2026: comparison figures matched the validated
        # answer less often (66% against 76%), so 2024 is still read and its own figure replaces the comparison one
        import types, collections, psycopg, extract_eval
        class Cur:
            def execute(self, *a): pass
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cur()
        asked = []
        def fake(cur, wba, year, attr, spec, usage, known=None):
            asked.append(year)
            return [dict(value=v, printed=v, scale=1, library_id="L", page=1, unit="tCO2e", year=y, column=str(y), asked=year)
                    for y, v in ((year, 100.0 + year), (year - 1, 90.0), (year - 2, 80.0))]
        real = (psycopg.connect, extract_eval.read_values, P.library_docs)
        psycopg.connect = lambda *a, **k: Conn(); extract_eval.read_values = fake; P.library_docs = lambda *a: {}
        os.environ["ANTHROPIC_API_KEY"] = "k"; os.environ["SUPABASE_DB_URL"] = "postgres://x"
        a = types.SimpleNamespace(attributes="GMC_00001.1", model_years="2024,2025", companies="PT_1", limit=0, max_usd=1.0, budget=300)
        spec = {"GMC_00001.1": {"name": "Scope 1", "type": "number", "unit_family": "tco2e"}}
        try:
            c = collections.Counter()
            rows = P.model_proposals(a, spec, [], {}, {}, {}, {}, "b", NOW, c)
        finally:
            psycopg.connect, extract_eval.read_values, P.library_docs = real
        self.assertEqual(asked, [2025, 2024])                                       # newest first, 2024 still asked
        self.assertEqual(sorted(r["year"] for r in rows), [2022, 2023, 2024, 2025])
        self.assertEqual(next(r for r in rows if r["year"] == 2024)["value"], 2124.0)   # its own read, not the comparison 90
        self.assertEqual(c["model reader: comparison figure replaced by the year's own read"], 1)
        self.assertEqual((c["model reader: values for the year asked"], c["model reader: values for other years"]), (2, 2))
        d = next(r for r in rows if r["year"] == 2023)["detail"]
        got = dict(value=1.0, printed=1.0, scale=1, library_id="L", page=1, unit="tCO2e", basis_note="The report gives one Scope 2 figure without saying location- or market-based; it is filed under location-based.")
        self.assertIn("filed under location-based", P.model_row("PT_2", "GMC_00002.1", {"name": "S2"}, 2024, got, {}, {}, None, NOW)["detail"])
        self.assertIn("pages of the 2025 report, where it is a comparison figure for 2023", d)
        self.assertNotIn("comparison", next(r for r in rows if r["year"] == 2025)["detail"])

    def test_a_run_goes_on_where_the_last_one_stopped(self):
        # 1 Nov 2026: all 8 shards stopped at their 5-hour limit; a second run must not read the same companies again
        import types, collections, psycopg, extract_eval
        sql = []
        class Cur:
            def execute(self, q, p=None): sql.append(q)
            def fetchone(self): return ("public.model_reads",)
            def fetchall(self): return [("PT_1", "GMC_00001.1", 2025)]
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cur()
        asked = []
        def fake(cur, wba, year, attr, spec, usage, known=None):
            asked.append((wba, year)); return []
        real = (psycopg.connect, extract_eval.read_values, P.library_docs)
        psycopg.connect = lambda *a, **k: Conn(); extract_eval.read_values = fake; P.library_docs = lambda *a: {}
        os.environ["ANTHROPIC_API_KEY"] = "k"; os.environ["SUPABASE_DB_URL"] = "postgres://x"
        spec = {"GMC_00001.1": {"name": "Scope 1", "type": "number", "unit_family": "tco2e"}}
        try:
            a = types.SimpleNamespace(attributes="GMC_00001.1", model_years="2024,2025", companies="PT_1,PT_2", limit=0,
                                      max_usd=1.0, budget=300, fresh_days=30)
            c = collections.Counter()
            P.model_proposals(a, spec, [], {}, {}, {}, {}, "b", NOW, c)
            self.assertEqual(asked, [("PT_1", 2024), ("PT_2", 2025), ("PT_2", 2024)])
            self.assertEqual(c["model reader: read in the last days, skipped"], 1)
            asked.clear()
            a.fresh_days = 0                                   # 0: read everything again, the table is not asked
            n = len(sql)
            P.model_proposals(a, spec, [], {}, {}, {}, {}, "b", NOW, collections.Counter())
            self.assertEqual(len(asked), 4)
            self.assertFalse(any("model_reads" in q for q in sql[n:]))
        finally:
            psycopg.connect, extract_eval.read_values, P.library_docs = real

    def test_a_superseded_row_proposed_again_is_proposed(self):
        # 30 Oct 2026: filings.xbrl.org did not answer for 56 companies and their 270 ESEF rows were superseded
        rows = [P.row("A", "GMC_00048.1", "Revenue", 2024, FACT, "ESEF XBRL", "u", {}, None, NOW)]
        existing = {"A|GMC_00048.1|2024|ESEF XBRL": {"id": "recA", "status": "superseded"}}
        _, updates, _ = P.plan(rows, existing, NOW)
        self.assertEqual(updates[0][1]["status"], "proposed")

    def test_a_filing_json_is_fetched_three_times(self):
        class R:
            def __init__(self, ok, code=500): self.ok, self.status_code = ok, code
            def json(self): return {"facts": {}}
        class Sess:
            def __init__(self, answers): self.answers, self.n = list(answers), 0
            def get(self, url, timeout=None):
                self.n += 1
                a = self.answers.pop(0)
                if a == "boom":
                    raise ConnectionError("x")
                return a
        P.time.sleep = lambda s: None
        s = Sess(["boom", R(False), R(True)])
        self.assertEqual(P.fetch_json(s, "u"), {"facts": {}}); self.assertEqual(s.n, 3)
        self.assertIsNone(P.fetch_json(Sess([R(False), "boom", R(False)]), "u"))
        s = Sess([R(False, 404)])
        self.assertIsNone(P.fetch_json(s, "u")); self.assertEqual(s.n, 1)                 # gone: no retry

    def test_years(self):
        from datetime import datetime
        t = datetime(2026, 10, 29)
        self.assertEqual(P.parse_years("2018-", t), list(range(2018, 2026)))
        self.assertEqual(P.parse_years("2024", t), [2024])
        self.assertEqual(P.parse_years("2022,2020-2021", t), [2020, 2021, 2022])

    def test_sec_accession_and_url(self):
        cf = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"end": "2024-12-31", "filed": "2025-02-20", "val": 102502e6, "accn": "0000123456-25-000010"}]}}}}}
        self.assertEqual(P.sec_accession(cf, FACT), "0000123456-25-000010")
        self.assertEqual(P.sec_url("123456", "0000123456-25-000010"), "https://www.sec.gov/Archives/edgar/data/123456/000012345625000010/")
        self.assertIn("type=10-K", P.sec_url("123456", ""))
        self.assertEqual(P.sec_url("4962", "0000004962-24-000013", "axp-20231231.htm"),
                         "https://www.sec.gov/ix?doc=/Archives/edgar/data/4962/000000496224000013/axp-20231231.htm")

    def test_how_to_find(self):
        self.assertEqual(P.how_to_find(60515e6), "Search the report for 60,515 (in millions) or 60,515,000 (in thousands), in the income statement.")
        self.assertIn("341 (in millions)", P.how_to_find(341e6))
        self.assertIn("7.9 (in millions)", P.how_to_find(7.9e6))


    def test_the_memory_gets_the_other_years_answers_only(self):
        # 1 Nov 2026: the validated answer of another year is the company's memory; the year read never is
        import types, collections, psycopg, extract_eval
        class Cur:
            def execute(self, *a): pass
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cur()
        seen = []
        def fake(cur, wba, year, attr, spec, usage, known=None):
            seen.append((year, dict(known or {}))); return []
        real = (psycopg.connect, extract_eval.read_values, P.library_docs)
        psycopg.connect = lambda *a, **k: Conn(); extract_eval.read_values = fake; P.library_docs = lambda *a: {}
        os.environ["ANTHROPIC_API_KEY"] = "k"; os.environ["SUPABASE_DB_URL"] = "postgres://x"
        a = types.SimpleNamespace(attributes="GMC_00001.1", model_years="2024,2025", companies="PT_1", limit=0, max_usd=1.0, budget=300)
        spec = {"GMC_00001.1": {"name": "Scope 1", "type": "number", "unit_family": "tco2e"}}
        gold = {("PT_1", "GMC_00001.1", 2024): {"value": 14408.0}, ("PT_1", "GMC_00001.1", 2025): {"value": 13950.0},
                ("PT_2", "GMC_00001.1", 2023): {"value": 1.0}}
        try:
            P.model_proposals(a, spec, [], {}, {}, gold, {}, "b", NOW, collections.Counter())
        finally:
            psycopg.connect, extract_eval.read_values, P.library_docs = real
        self.assertEqual(seen, [(2025, {2024: 14408.0}), (2024, {2025: 13950.0})])

    def test_every_version_is_kept_and_the_others_are_summarised(self):
        # 1 Nov 2026: a 2023 figure printed in three reports is three versions; the row shown names the other two
        chosen = dict(value=100.0, library_id="L23", page=40, year=2023, asked=2023)
        versions = [chosen, dict(value=100.2, library_id="L24", page=50, year=2023, asked=2024),
                    dict(value=96.0, library_id="L25", page=60, year=2023, asked=2025)]
        docs = {"L24": {"label": "2024 | Co | Sustainability Report 2024"}, "L25": {"label": "2025 | Co | Annual Report 2025"}}
        o = P.other_reports(chosen, versions, docs)
        self.assertTrue(o["restated"])
        self.assertEqual(o["other_reports"].split("\n"), ["2025 | Co | Annual Report 2025, page 60: 96 (-4.0%)",
                                                         "2024 | Co | Sustainability Report 2024, page 50: 100.2 (same)"])
        self.assertEqual(P.other_reports(chosen, [chosen], docs), {"other_reports": None, "restated": False})
        url = os.environ.get("PG_TEST_URL")
        if not url:
            self.skipTest("PG_TEST_URL not set")
        import psycopg
        with psycopg.connect(url) as c:
            c.execute("drop table if exists public.value_versions"); c.commit()
            vs = [("PT_1", "GMC_00001.1", dict(v, unit="tCO2e", printed=str(v["value"]), scale=1, column="2023")) for v in versions]
            self.assertEqual(P.write_versions(c, vs, "model reader", NOW), 3)
            vs[2][2]["value"] = 97.0
            self.assertEqual(P.write_versions(c, vs, "model reader", NOW), 3)             # read again: updated, not duplicated
            rows = c.execute("select library_id, value from public.value_versions order by library_id").fetchall()
            self.assertEqual(rows, [("L23", 100.0), ("L24", 100.2), ("L25", 97.0)])
            c.execute("drop table public.value_versions"); c.commit()

if __name__ == "__main__":
    unittest.main()
