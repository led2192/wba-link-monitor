"""Passages kept on the server only (server_own_index.py): what is left to do, what is written, and that nothing
is searched until switched on. No network, no database, no model."""
import os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server_own_index as O

SRC = open(os.path.join(ROOT, "server_own_index.py"), encoding="utf-8").read()
MEANING = open(os.path.join(ROOT, "server_meaning.py"), encoding="utf-8").read()
FLOW = open(os.path.join(ROOT, ".github", "workflows", "server_own_index.yml"), encoding="utf-8").read()
SQL = open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()


class Cursor:
    """Records what is executed; answers from a queue."""
    def __init__(self, answers=()):
        self.sql, self.answers, self.rowcount, self.copied = [], list(answers), 0, []
    def execute(self, sql, args=None):
        self.sql.append((" ".join(sql.split()), args))
    def fetchone(self):
        return self.answers.pop(0) if self.answers else None
    def copy(self, sql):
        outer = self
        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def write_row(s, row): outer.copied.append(row)
        self.sql.append((" ".join(sql.split()), None))
        return C()


class OwnIndex(unittest.TestCase):
    def test_what_is_left_keeps_the_scopes_order(self):
        scope = [("a", "W1", 2025, "Policy Documents"), ("b", "W1", 2024, "Code of Conduct"), ("c", "W2", None, "Human Rights Policy")]
        self.assertEqual([r[0] for r in O.to_do(scope, ["b"])], ["a", "c"])
        self.assertEqual(O.to_do(scope, ["a", "b", "c", "other"]), [])

    def test_a_document_is_written_whole_and_not_live(self):
        d = Cursor()
        O.write_document(d, ("a", "W1", 2025, "Policy Documents"), [(1, 0, [0.5, -0.25]), (2, 1, [1, 0])], live=False)
        self.assertIn("delete from meaning.chunks where library_id = %s", d.sql[0][0])          # a stopped run's leftovers are replaced
        self.assertEqual(d.copied, [("a", 1, 0, "[0.50000,-0.25000]"), ("a", 2, 1, "[1.00000,0.00000]")])
        self.assertIn("values (%s, %s, %s, %s, %s, true)", d.sql[-1][0]); self.assertEqual(d.sql[-1][1], ("a", "W1", 2025, "Policy Documents", False))
        self.assertNotIn("live = excluded.live", d.sql[-1][0])                                   # writing again never switches a document on
        empty = Cursor(); O.write_document(empty, ("z", None, None, "Code of Conduct"), [], live=False)
        self.assertEqual(empty.copied, []); self.assertEqual(len(empty.sql), 2)                  # no passage: the row alone marks it done

    def test_the_switch_is_one_setting_and_the_own_rows(self):
        d = Cursor(); d.rowcount = 7
        self.assertEqual(O.set_live(d, True), 7)
        self.assertIn("update meaning.settings set is_on = %s", d.sql[0][0])
        self.assertIn("where own and live is distinct from %s", d.sql[1][0])

    def test_supabase_is_only_read_and_a_dry_run_embeds_nothing(self):
        self.assertIn("set default_transaction_read_only = on", SRC)
        for word in ("insert into public.", "update public.", "delete from public."):
            self.assertNotIn(word, SRC)
        self.assertLess(SRC.index("if a.remaining or not a.commit:"), SRC.index("emb = Embedder("))
        self.assertEqual(O.OWN_TYPES, ("Policy Documents", "Code of Conduct", "Human Rights Policy"))

    def test_the_report(self):
        m = dict(scope=100, held=10, todo=90, done=30, passages=1200, empty=1, minutes=60.0, rate=19.5, live=False)
        text = O.report(m, True)
        self.assertIn("| Left before this run | 90 |", text); self.assertIn("| Searched (live) | no |", text)
        self.assertIn("the 60 documents left take about 2.0 hours", text)
        self.assertIn("dry run", O.report(dict(m, done=0), False))

    def test_the_nightly_refresh_leaves_them_alone(self):
        self.assertIn("where not exists (select 1 from {schema}.chunk_filters f where f.library_id = c.library_id and f.own)", MEANING)   # not compared with Supabase
        self.assertIn("where f.live and not f.own and not exists", MEANING)                                                               # not marked gone
        own = MEANING[MEANING.index("def refresh_own("):MEANING.index("def refresh_report(")]
        self.assertIn("name = 'own_live'", own); self.assertIn("set live = false where f.own and f.live and not exists", own)

    def test_the_workflow_is_manual_on_the_server_and_the_search_shows_the_page(self):
        self.assertIn("workflow_dispatch", FLOW); self.assertNotIn("schedule:", FLOW); self.assertNotIn("pull_request", FLOW)
        self.assertIn("as_scheduled:", FLOW); self.assertIn("OMP_NUM_THREADS", FLOW)
        import json
        slices = [e["inputs"]["partition"] for e in json.load(open(os.path.join(ROOT, "server", "clock.json"))) if e["workflow"] == "server_own_index.yml"]
        self.assertEqual(slices, ["0/3", "1/3", "2/3"])                           # the three slices cover every document once
        self.assertIn("runs-on: [self-hosted, lantern]", FLOW); self.assertIn("alter role lantern_own nologin password null", FLOW)
        self.assertIn("grant lantern_load to lantern_own", FLOW)
        pages = SQL[SQL.index("create or replace function public.meaning_remote_pages("):SQL.index("revoke all on function public.meaning_remote_pages(")]
        self.assertIn("left join public.chunks c", pages); self.assertIn("select left(p.body, 600) from public.pages p", pages)


if __name__ == "__main__":
    unittest.main()
