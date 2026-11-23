"""company_profile.py: what is asked, what is kept of a reply, and its workflow. Invented company and pages; no
network, no database, no model."""
import json, os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import company_profile as C

SRC = open(os.path.join(ROOT, "company_profile.py"), encoding="utf-8").read()
FLOW = open(os.path.join(ROOT, ".github", "workflows", "company_profile.yml"), encoding="utf-8").read()
SQL = open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()
PAGES = [("L1", 5, "Acme is headquartered in Lyon, France. Founded in 1921."), ("L2", 9, "Subsidiaries: Acme GmbH, Acme Inc.")]


class Reply(unittest.TestCase):
    def test_a_field_is_kept_only_with_a_value_and_an_excerpt_that_was_shown(self):
        reply = json.dumps({"headquarters_city": "Lyon ", "headquarters_country": "France", "country_of_incorporation": None, "year_founded": 1921,
                            "listing": "Not stated", "ownership": "unknown", "parent": "N/A", "subsidiaries": ["Acme GmbH", "Acme Inc.", "acme gmbh", ""],
                            "countries": [], "countries_count": 0,
                            "from": {"headquarters_city": 1, "headquarters_country": 1, "year_founded": 1, "subsidiaries": 2, "listing": 1, "parent": 2}})
        self.assertEqual(C.rows_of("identity", reply, PAGES, 2026),
                         [("headquarters_city", "Lyon", "L1", 5), ("headquarters_country", "France", "L1", 5), ("year_founded", 1921, "L1", 5),
                          ("subsidiaries", ["Acme GmbH", "Acme Inc"], "L2", 9)])
        no_page = json.dumps({"headquarters_city": "Lyon", "year_founded": 1921, "from": {"headquarters_city": 7, "year_founded": True}})
        self.assertEqual([C.excerpt_number(x) for x in (2, "2", "[3]", [1, 2], True, "two", None, [])], [2, 2, 3, 1, None, None, None, None])     # as the paid model writes them
        self.assertEqual((C.why_nothing("no"), C.why_nothing('{"a": 1}'), C.why_nothing('{"from": {"a": 9}}')), ("not JSON", "no from", "no field with its excerpt"))
        self.assertEqual(C.rows_of("identity", no_page, PAGES, 2026), [])                      # an excerpt that was not shown: the value cannot be checked
        for bad in ("not json", "[]", None, ""):
            self.assertEqual(C.rows_of("identity", bad, PAGES, 2026), [])

    def test_values_are_cleaned_by_kind(self):
        self.assertEqual(C.clean("year", 2031, 2026), None); self.assertEqual(C.clean("year", 1886, 2026), 1886); self.assertEqual(C.clean("year", "1886", 2026), None)
        self.assertEqual(C.clean("count", 0, 2026), None); self.assertEqual(C.clean("count", 62, 2026), 62)
        self.assertEqual(C.clean(["listed", "private", "unknown"], "Listed", 2026), "listed"); self.assertEqual(C.clean(["listed", "unknown"], "unknown", 2026), None)
        self.assertEqual(C.clean("text", "  Analog   chips. ", 2026), "Analog chips"); self.assertEqual(C.clean("text", "Not disclosed", 2026), None)
        self.assertEqual(C.clean("list", "France", 2026), None)

    def test_the_question_names_the_company_numbers_the_excerpts_and_forbids_memory(self):
        text = C.told("Acme", "activity", PAGES)
        self.assertIn("the company Acme", text); self.assertIn("never from memory", text); self.assertIn("[1] Acme is headquartered", text); self.assertIn("[2] Subsidiaries", text)
        self.assertIn("subsector", text); self.assertEqual(set(C.schema("activity")["required"]), set(C.QUESTIONS["activity"]["fields"]) | {"from"})
        self.assertEqual(C.tsquery("Head-office; DROP table x1 of"), "head | office | drop | table")       # letters only reach the words index

    def test_slices_cover_every_company_once(self):
        ids = [f"PT_{n:05d}" for n in range(300)]
        self.assertEqual(sorted(i for k in range(3) for i in ids if C.in_partition(i, f"{k}/3")), ids)


class Rules(unittest.TestCase):
    def test_nothing_is_deleted_a_persons_row_is_kept_and_the_log_names_no_company(self):
        self.assertNotIn("delete from", SRC.lower()); self.assertIn("where public.company_profile.status = 'read'", SRC)
        self.assertNotRegex(SRC, r"print\([^\n]*\{(name|value|reply|got|rows)\b")
        self.assertNotIn("find_pages", SRC)                                                # its own query: nothing is logged as a search
        self.assertIn("create table if not exists public.company_profile (", SQL); self.assertIn("alter table public.company_profile enable row level security;", SQL)
        self.assertIn("alter table public.company_profile_runs enable row level security;", SQL)

    def test_the_workflow_runs_on_the_server_dry_by_hand_and_no_pull_request_starts_it(self):
        self.assertIn("runs-on: [self-hosted, lantern]", FLOW); self.assertNotIn("pull_request", FLOW); self.assertIn("/srv/lantern/venv-llm/bin/python company_profile.py", FLOW)
        self.assertIn("default: false", FLOW)
        paid = FLOW.split("\n  paid:")[1]
        self.assertIn("inputs.model == 'paid' && inputs.as_scheduled != 'true'", paid); self.assertIn("--max-usd", paid)     # the paid model by hand only, with a cap
        self.assertNotIn("ANTHROPIC", FLOW.split("\n  paid:")[0])

    def test_a_reply_with_words_around_its_json_is_still_read_and_the_paid_model_has_a_cap(self):
        self.assertEqual(C.json_of('Here it is:\n{"sector": "Technology", "from": {"sector": 1}}\nDone.'), '{"sector": "Technology", "from": {"sector": 1}}')
        self.assertEqual(C.json_of("no json"), "no json"); self.assertEqual(C.json_of(None), "")
        self.assertIn("if spent() >= a.max_usd:", SRC)


if __name__ == "__main__":
    unittest.main()
