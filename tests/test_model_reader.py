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
         ("lib1", 5, "Consolidated income statement (in USD million) 2024 2023 Revenues 102,502 106,848")]


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
