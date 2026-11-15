import gzip, json, os, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fast_lane as F
import build_fulltext as B
import load_supabase as L


class Cur:
    def __init__(self, rows): self.rows, self.sql = rows, []
    def execute(self, sql, args=None): self.sql.append(" ".join(sql.split()))
    def fetchone(self): return (True,)
    def fetchall(self): return self.rows


class Lane(unittest.TestCase):
    def test_what_waits_is_what_was_sent_is_in_the_library_and_has_no_text_status(self):
        cur = Cur([("LIB-1", None), ("LIB-2", None)])
        self.assertEqual([r[0] for r in F.waiting(cur)], ["LIB-1", "LIB-2"])
        sql = cur.sql[-1]
        self.assertIn("b.status = 'taken'", sql); self.assertIn("b.library_id is not null", sql)
        self.assertIn("d.text_status is not null", sql); self.assertIn("interval '3 days'", sql)

    def test_three_steps_in_order_for_those_documents_only(self):
        got = F.steps(["A", "B"], 77, py="py")
        self.assertEqual([n for n, _ in got], ["full text", "load", "meaning"])
        self.assertEqual(got[0][1][:5], ["py", "build_fulltext.py", "--ids", "A,B", "--commit"])
        self.assertEqual(got[1][1], ["py", "load_supabase.py", "--fast-run", "77", "--commit"])
        self.assertEqual(got[2][1][:5], ["py", "semantic_index.py", "--ids", "A,B", "--commit"])

    def test_a_step_that_fails_stops_the_lane(self):
        class R:
            def __init__(self, code): self.returncode = code
        calls = []
        def run(cmd, timeout=None):
            calls.append(cmd[1]); return R(1 if cmd[1] == "load_supabase.py" else 0)
        done = F.run_steps(["A"], 1, run=run, say=lambda *a, **k: None)
        self.assertEqual(calls, ["build_fulltext.py", "load_supabase.py"])
        self.assertEqual([(n, c) for n, _, c in done], [("full text", 0), ("load", 1)])

    def test_the_workflow_runs_on_the_server_one_at_a_time(self):
        wf = open(os.path.join(ROOT, ".github", "workflows", "fast_lane.yml"), encoding="utf-8").read()
        self.assertIn("runs-on: [self-hosted, lantern]", wf); self.assertIn("group: fast-lane", wf)
        self.assertNotIn("schedule:", wf)                                   # the watch starts it; no clock of GitHub's
        src = open(os.path.join(ROOT, "scan_now.py"), encoding="utf-8").read()
        self.assertIn('("fast_lane", [sys.executable, "fast_lane.py", "--dispatch"])', src)


class Indexes(unittest.TestCase):
    def test_a_run_that_adds_documents_never_drops_the_index_of_meanings(self):
        """15 Nov 2026: the fast lane's first real run dropped the index of 4 million embeddings."""
        src = open(os.path.join(ROOT, "semantic_index.py"), encoding="utf-8").read()
        self.assertIn("if a.commit and a.drop_indexes:\n            drop_vector_index(cur)", src)
        self.assertEqual(src.count("drop_vector_index(cur)"), 2)                         # its definition and this one call
        for _, cmd in F.steps(["A"], 1):
            self.assertNotIn("--drop-indexes", cmd)
        for wf in os.listdir(os.path.join(ROOT, ".github", "workflows")):
            self.assertNotIn("--drop-indexes", open(os.path.join(ROOT, ".github", "workflows", wf), encoding="utf-8").read(), wf)


class Store(unittest.TestCase):
    def test_the_files_of_one_run(self):
        listing = {n: {"size": 1} for n in (
            "docs-20261115T120000-r77a1-00-001.jsonl.gz", "pages-20261115T120000-r77a1-00-001.jsonl.gz",
            "docs-20261115T110000-r7a1-00-001.jsonl.gz", "pages-20261115T110000-r7a1-00-001.jsonl.gz",
            "pages-20261115T120500-r77a1-00-002.jsonl.gz",                                   # no docs file yet: not complete
            "fastmeta-20261115T120100-r77.jsonl.gz", "fastmeta-20261115T110100-r7.jsonl.gz", "meta-20261114T213700.jsonl.gz")}
        docs, pages, meta = L.fast_files(listing, set(), "77")
        self.assertEqual(docs, ["docs-20261115T120000-r77a1-00-001.jsonl.gz"])
        self.assertEqual(pages, ["pages-20261115T120000-r77a1-00-001.jsonl.gz"])
        self.assertEqual(meta, ["fastmeta-20261115T120100-r77.jsonl.gz"])
        self.assertEqual(L.fast_files(listing, {"pages-20261115T120000-r77a1-00-001.jsonl.gz"}, "77")[1], [])      # loaded once

    def test_the_snapshot_wins_and_the_fast_lane_fills_what_it_lacks(self):
        d = tempfile.mkdtemp()
        with gzip.open(os.path.join(d, "fastmeta-x.jsonl.gz"), "wt", encoding="utf-8") as f:
            f.write(json.dumps({"id": "A", "wba": "LN_1", "co": "New Co", "type": "Annual Report", "year": 2025}) + "\n")
            f.write(json.dumps({"id": "B", "wba": "PT_1", "co": "Stale"}) + "\n")
        meta = {"B": {"id": "B", "wba": "PT_1", "co": "Snapshot"}}
        self.assertEqual(L.add_fastmeta(meta, d, ["fastmeta-x.jsonl.gz"]), 1)
        self.assertEqual((meta["A"]["co"], meta["B"]["co"]), ("New Co", "Snapshot"))
        row = L.document_row({"id": "A", "pages": 80, "status": "ok"}, meta["A"])
        self.assertEqual(row[:6], ("A", None, "LN_1", "New Co", "Annual Report", 2025))

    def test_fast_metadata_is_kept_a_week(self):
        now = time.mktime(time.strptime("20261115T120000", "%Y%m%dT%H%M%S"))
        listing = {"fastmeta-20261101T000000-r1.jsonl.gz": {}, "fastmeta-20261114T000000-r2.jsonl.gz": {}, "meta-20261101T000000.jsonl.gz": {}}
        self.assertEqual(B.old_fastmeta(listing, now=now), ["fastmeta-20261101T000000-r1.jsonl.gz"])
        self.assertTrue(B.fastmeta_name(77).endswith("-r77.jsonl.gz"))

    def test_only_the_documents_asked_for_are_read_from_the_table(self):
        asked = []
        class Resp:
            def json(self): return {"records": [
                {"id": "rec1", "fields": {"library_id": "A", "wba_id": "LN_1", "file": [{"id": "att1", "url": "u", "size": 9, "type": "application/pdf", "filename": "a.pdf"}]}},
                {"id": "rec2", "fields": {"library_id": "B", "discard": True, "file": [{"id": "att2", "url": "u", "size": 9, "type": "application/pdf", "filename": "b.pdf"}]}},
                {"id": "rec3", "fields": {"library_id": "C"}}]}
        def request(method, url, headers, params=None):
            asked.append(dict(params)["filterByFormula"]); return Resp()
        old = B._airtable; B._airtable = lambda: (request, "url", {})
        try:
            docs = B.rows_by_id(["A", "B", "C", "A", "it's"])
        finally:
            B._airtable = old
        self.assertEqual(asked, ["OR({library_id}='A',{library_id}='B',{library_id}='C')"])      # one request; a quote never reaches the formula
        self.assertEqual([d["id"] for d in docs], ["A"])                                        # discarded, or no file yet: not read


if __name__ == "__main__":
    unittest.main()


class Scope(unittest.TestCase):
    def test_the_semantic_scope_is_every_report_that_reads_like_an_annual_or_sustainability_report(self):
        import semantic_index as S, evidence_health as H
        for t in ("Annual Report", "Sustainability Report", "Impact Report", "Climate Report", "Environmental Reports", "Social Reports", "10K Form", "Registration Document", "CDP Report", "GRI"):
            self.assertIn(t, S.MAIN_TYPES)
        for t in ("Policy Documents", "Interim Reports", "Company Presentations", "SASB Index"):
            self.assertNotIn(t, S.MAIN_TYPES)
        self.assertEqual(S.MAIN_TYPES, H.MAIN_TYPES)                    # the health check counts the same scope
