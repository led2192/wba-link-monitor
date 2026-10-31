"""Offline tests for model_reader.py: the Messages API is stubbed. The value is computed from the printed
number and the scale, never taken from the model, and a number the named page does not print is dropped."""
import json, os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
os.environ["ANTHROPIC_API_KEY"] = "test-key"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import extract_eval as X  # noqa: E402
import model_reader as M  # noqa: E402

SPEC = json.load(open(X.SPEC, encoding="utf-8"))
REV = SPEC["attributes"]["GMC_00048.1"]
G = {"wba": "PT_09998", "year": 2024, "value": 102502e6, "unit": "USD"}
PAGES = [("lib1", 3, "Our revenue grew strongly in 2024, to more than 100 billion."),
         ("lib1", 5, "Consolidated income statement (in USD million) 2024 2023 Revenues 102,502 106,848"),
         ("lib1", 9, "Five-year summary (USD million) 2024 2023 2022 2021 2020 Revenues 102,502 106,848 98,120 90,003 85,555")]
YEARS = ('{"values": [{"page": "P2", "printed": "102,502", "scale": "millions", "unit": "USD", "year": 2024, "column": "2024"}, '
         '{"page": "P2", "printed": "106,848", "scale": "millions", "unit": "USD", "year": 2023, "column": "2023"}, '
         '{"page": "P3", "printed": "98,120", "scale": "millions", "unit": "USD", "year": 2022, "column": "2022"}, '
         '{"page": "P3", "printed": "90,003", "scale": "millions", "unit": "USD", "year": 2021, "column": "2021"}, '
         '{"page": "P3", "printed": "85,555", "scale": "millions", "unit": "USD", "year": 2020, "column": "2020"}]}')


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._p


class Sess:
    """Answers each post with the next reply; keeps what was sent."""
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.sent.append(json)
        status, text = self.replies.pop(0)
        return Resp(status, {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 2000, "output_tokens": 40}})


class ModelReader(unittest.TestCase):
    def setUp(self):
        M.time.sleep = lambda s: None

    def test_value_is_printed_number_times_scale(self):
        usage = X.collections.Counter()
        s = Sess((200, 'Here: {"page": "P2", "printed": "102,502", "scale": "millions", "unit": "USD", "year": 2024}'))
        got = M.read_with_model(G, REV, PAGES, usage, s)
        self.assertEqual((got["value"], got["library_id"], got["page"]), (102502e6, "lib1", 5))
        self.assertTrue(X.right(got["value"], G["value"]))
        self.assertEqual((usage["calls"], usage["input_tokens"], usage["output_tokens"]), (1, 2000, 40))
        prompt = s.sent[0]["messages"][0]["content"]
        self.assertIn("Revenue", prompt); self.assertIn('<page id="P2">', prompt); self.assertIn("2024", prompt)
        self.assertIn('"column"', prompt)
        self.assertNotIn("temperature", s.sent[0])                 # refused by Haiku 5.5 (HTTP 400, 29 Oct 2026)

    def test_a_number_not_on_the_named_page_is_dropped(self):
        usage = X.collections.Counter()
        s = Sess((200, '{"page": "P1", "printed": "102,502", "scale": "millions"}'))     # P1 does not print it
        self.assertIsNone(M.read_with_model(G, REV, PAGES, usage, s))
        self.assertEqual(usage["quote_not_on_page"], 1)

    def test_no_figure_bad_json_and_bad_page(self):
        for text in ('{"page": null}', "I cannot find it.", '{"page": "P9", "printed": "1"}', '{"page": "P2", "printed": "n/a"}'):
            self.assertIsNone(M.read_with_model(G, REV, PAGES, X.collections.Counter(), Sess((200, text))), text)

    def test_retries_overload_then_stops_on_a_refused_key(self):
        usage = X.collections.Counter()
        s = Sess((529, ""), (200, '{"page": "P2", "printed": "102,502", "scale": "millions"}'))
        self.assertTrue(M.read_with_model(G, REV, PAGES, usage, s))
        for code in (400, 401, 404):
            with self.assertRaises(M.ModelRefused):
                M.read_with_model(G, REV, PAGES, usage, Sess((code, "")))

    def test_the_reason_for_no_figure_is_kept(self):
        note = {}
        self.assertIsNone(M.read_with_model(G, REV, PAGES, X.collections.Counter(),
                                            Sess((200, '{"page": null, "why": "only segment revenue is shown"}')), note=note))
        self.assertEqual(note["why"], "only segment revenue is shown")
        self.assertIn("none of the pages prints it", M.prompt(G, REV, PAGES))

    def test_a_reply_without_json_is_named(self):
        note = {}
        M.read_with_model(G, REV, PAGES, X.collections.Counter(), Sess((200, "Looking at page P2, the revenue line shows")), note=note)
        self.assertEqual(note["why"], "(no JSON in the reply)")

    def test_a_point_on_a_page_of_comma_thousands_is_a_decimal_point(self):
        # CNOOC, 30 Oct 2026: "14.408" million tonnes on a page that prints 1,701 and 293,316 was read as 14,408
        page = [("lib2", 126, "Electricity Million KWh 1,701 2,280 2,646 Annual energy savings 293,316 276,800 "
                              "Direct GHG emissions Million tonnes of CO2 equivalent 13.954 14.408 15.234")]
        s = Sess((200, '{"page": "P1", "printed": "14.408", "scale": "millions", "unit": "tCO2e", "year": 2024}'))
        got = M.read_with_model(G, REV, page, X.collections.Counter(), s)
        self.assertAlmostEqual(got["value"], 14_408_000)
        euro = [("lib3", 4, "Umsatz 1.701 2.280 293.316 Mio. EUR 14.408")]           # points group thousands
        s = Sess((200, '{"page": "P1", "printed": "14.408", "scale": "millions"}'))
        self.assertAlmostEqual(M.read_with_model(G, REV, euro, X.collections.Counter(), s)["value"], 14_408e6)
        self.assertEqual(M.printed_number("102,502", "Revenues 102,502 106,848", X.to_number), 102502)

    def test_ten_thousands(self):
        # Master Kong, 30 Oct 2026: 16.05 万吨 is 160,500 tonnes; with no such scale it was written as 16.05
        page = [("lib4", 100, "直接溫室氣體排放 萬噸 16.05 15.20")]
        s = Sess((200, '{"page": "P1", "printed": "16.05", "scale": "ten thousands", "unit": "tCO2e"}'))
        self.assertAlmostEqual(M.read_with_model(G, REV, page, X.collections.Counter(), s)["value"], 160_500)
        self.assertIn("万 is ten thousands", M.prompt(G, REV, page))

    def test_every_year_in_one_call(self):
        usage = X.collections.Counter()
        got = M.read_with_model(G, REV, PAGES, usage, Sess((200, YEARS)))
        self.assertEqual((got["value"], got["year"], got["page"]), (102502e6, 2024, 5))
        self.assertEqual([(e["year"], e["value"], e["page"]) for e in got["others"]],
                         [(2023, 106848e6, 5), (2022, 98120e6, 9), (2021, 90003e6, 9), (2020, 85555e6, 9)])
        self.assertEqual((usage["calls"], usage["years_returned"]), (1, 5))
        self.assertIn("each other reporting year the same row prints", M.prompt(G, REV, PAGES))

    def test_the_year_asked_comes_first_whatever_the_order(self):
        text = ('{"values": [{"page": "P2", "printed": "106,848", "scale": "millions", "year": 2023, "column": "2023"}, '
                '{"page": "P2", "printed": "102,502", "scale": "millions", "year": 2024, "column": "2024"}]}')
        got = M.read_years(G, REV, PAGES, X.collections.Counter(), Sess((200, text)))
        self.assertEqual([e["year"] for e in got], [2024, 2023])

    def test_a_year_the_heading_does_not_allow_is_dropped(self):
        # the model mixed its columns: "2023" headed, year 2024; FY25 may be 2025 or 2024, never 2023
        text = ('{"values": [{"page": "P2", "printed": "106,848", "scale": "millions", "year": 2024, "column": "2023"}, '
                '{"page": "P2", "printed": "102,502", "scale": "millions", "year": 2024, "column": "FY25"}, '
                '{"page": "P2", "printed": "102,502", "scale": "millions", "year": 2024, "column": "2024"}, '
                '{"page": "P2", "printed": "106,848", "scale": "millions", "year": 1999, "column": ""}]}')
        got = M.read_years(G, REV, PAGES, X.collections.Counter(), Sess((200, text)))
        self.assertEqual([(e["year"], e["value"]) for e in got], [(2024, 102502e6)])      # FY25 -> 2024 kept, once
        self.assertEqual(M.column_year("Year ended 31 March 2025"), 2025)
        self.assertEqual(M.column_year("FY'24"), 2024); self.assertIsNone(M.column_year("Group"))

    def test_only_other_years_is_no_proposal_for_the_year_asked(self):
        note = {}
        text = '{"values": [{"page": "P2", "printed": "106,848", "scale": "millions", "year": 2023, "column": "2023"}]}'
        self.assertIsNone(M.read_with_model(G, REV, PAGES, X.collections.Counter(), Sess((200, text)), note=note))
        self.assertIn("only other years: 2023", note["why"])
        note = {}
        self.assertIsNone(M.read_with_model(G, REV, PAGES, X.collections.Counter(), Sess((200, '{"values": [], "why": "no revenue table"}')), note=note))
        self.assertEqual(note["why"], "no revenue table")

    def test_a_reply_cut_at_the_limit_keeps_its_complete_entries(self):
        # 31 Oct 2026: 1 in 10 replies hit the token limit and read as "no JSON", losing the year asked
        cut = YEARS[:YEARS.index('{"page": "P3", "printed": "90,003"')] + '{"page": "P3", "printed": "90,0'
        got = M.read_with_model(G, REV, PAGES, X.collections.Counter(), Sess((200, cut)))
        self.assertEqual((got["value"], got["year"]), (102502e6, 2024))
        self.assertEqual([e["year"] for e in got["others"]], [2023, 2022])
        self.assertIsNone(M.parse_reply('{"values": [{"page": "P1", "printed": "1'))

    def test_cost(self):
        u = X.collections.Counter(input_tokens=1_000_000, output_tokens=100_000)
        self.assertAlmostEqual(M.cost(u), 0.10 + 0.05)

    def test_pages_are_cut(self):
        p = M.prompt(G, REV, [("l", 1, "x" * 50000)])
        self.assertLess(len(p), M.PAGE_CHARS + 3000)


@unittest.skipUnless(os.environ.get("PG_TEST_URL"), "set PG_TEST_URL to a throwaway Postgres (after tests/test_load_supabase.py)")
class AgainstPostgres(unittest.TestCase):
    def test_evaluate_with_the_model_reader(self):
        import psycopg
        real = M.call
        M.call = lambda messages, usage, sess=None, model=None: '{"page": "P1", "printed": "102,502", "scale": "millions"}'
        try:
            with psycopg.connect(os.environ["PG_TEST_URL"]) as c:
                if not c.execute("select to_regclass('public.pages')").fetchone()[0]:
                    self.skipTest("run tests/test_load_supabase.py first")
                c.execute("delete from public.pages where library_id like 'xmod%'"); c.execute("delete from public.documents where library_id like 'xmod%'")
                c.execute("insert into public.documents (library_id, wba_id, company, year, doc_type) values ('xmod1', 'PT_09997', 'Testco', 2024, 'Annual Report')")
                c.execute("insert into public.pages (library_id, page, body) values "
                          "('xmod1', 5, 'Consolidated income statement (in USD million) 2024 2023 Revenues 102,502 106,848'), "
                          "('xmod1', 9, 'Revenue growth was driven by volume')")
                r = X.evaluate(c.cursor(), dict(G, wba="PT_09997"), REV, SPEC, "model", X.collections.Counter())
                self.assertTrue(r["proposal"] and r["right"] and r["model_pages_have_number"])
                self.assertEqual(r["got"][1:], ("xmod1", 5))
                c.execute("delete from public.pages where library_id like 'xmod%'"); c.execute("delete from public.documents where library_id like 'xmod%'")
        finally:
            M.call = real


if __name__ == "__main__":
    unittest.main()
