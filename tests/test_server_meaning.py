import os, unittest

import server_meaning as M

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(HERE, "server_meaning.py"), encoding="utf-8").read()
FLOW = open(os.path.join(HERE, ".github", "workflows", "server_meaning.yml"), encoding="utf-8").read()


class ServerMeaning(unittest.TestCase):
    def test_the_arithmetic(self):
        self.assertEqual(M.overlap([["a", "b"], ["c", "d"]], [["b", "a"], ["c", "x"]]), 0.75)
        self.assertEqual(M.overlap([], []), 1.0)
        self.assertEqual(M.percentile(list(range(1, 101)), 95), 95)

    def test_the_servers_search_is_find_meaning_written_for_its_tables(self):
        plain, company, inside = M.server_sql("meaning", False), M.server_sql("meaning", True), M.server_sql("meaning", False, inside=True)
        for sql in (plain, company, inside):
            self.assertIn("limit 400", sql)                                             # the nearest 400 passages, as find_meaning
            self.assertIn("x.in_doc <= 3", sql)                                         # three pages a document
            self.assertIn("select distinct on (n.library_id, n.page)", sql)
            self.assertNotIn("{", sql)
        self.assertIn("order by c.embedding <=> %(q)s::halfvec(384) limit 400), pages", plain)       # the index first, the year and type after
        self.assertIn("join meaning.chunk_filters f on f.library_id = p.library_id where", plain)
        self.assertIn("+ 0 as dist", company)                                           # exact inside the company's documents, never the index
        self.assertIn("f.wba_id = any(%(ids)s)", company)
        self.assertIn("join meaning.chunk_filters f on f.library_id = c.library_id where", inside)   # filtered while the index is walked

    def test_supabase_is_only_read(self):
        low = SCRIPT.lower()
        for word in ("insert into public", "update public", "delete from", "drop table", "alter table public", "truncate public", "create index if not exists chunks_embedding on public"):
            self.assertNotIn(word, low)
        self.assertEqual(SCRIPT.count("truncate {schema}.chunks, {schema}.chunk_filters"), 1)        # the server's copy, and only with --reload
        self.assertIn("if held and not reload:", SCRIPT)

    def test_the_reports(self):
        m = dict(rows=4_100_000, docs=16_000, filters=16_000, orphans=0, differ=0, copy_s=720, build_s=600, table_bytes=3 * 1024 ** 3, index_bytes=4 * 1024 ** 3)
        text = "\n".join(M.load_report(m, "meaning", "8GB", 6))
        self.assertIn("4,100,000 of 16,000 documents in 12.0 min", text)
        self.assertIn("differs between Supabase and the server | 0 |", text)
        res = {v: dict(theirs=[], ours=[], ms_server=[], ms_supabase=[], short=0, filled=0) for v in M.VARIANTS}
        res["no filter"].update(theirs=[["a|1", "b|2"]] * 50, ours=[["a|1", "b|2"]] * 50, ms_server=[12.0] * 50, ms_supabase=[40.0] * 50)
        res["two years"].update(theirs=[["a|1", "b|2"]] * 50, ours=[["a|1", "x|9"]] * 50, ms_server=[20.0] * 50, ms_supabase=[45.0] * 50, short=7, filled=6)
        lines, ov, med, p95 = M.compare_report(res, "meaning")
        text = "\n".join(lines)
        self.assertEqual((ov, p95), (0.75, 20.0))
        self.assertIn("first ten 75.0% (mark 98%): FAIL", text)
        self.assertIn("95th percentile 20 ms (mark under 800): PASS", text)
        self.assertIn("fewer than ten pages (it filters after taking the nearest 400 passages): 7; of those, the server returns ten when it filters while it searches: 6.", text)
        self.assertIn("| a company | 0 |", text)
        res["no filter"]["exact"] = [(["a|1", "b|2"], ["a|1", "c|3"], ["a|1", "c|3"])] * 10
        text = "\n".join(M.compare_report(res, "meaning")[0])
        self.assertIn("Supabase's index returns 50.0% of the true first ten, the server's index 100.0%.", text)
        self.assertIn("+ 0 as dist", M.exact_sql("meaning"))                               # every passage read, never the index
        self.assertNotIn("chunk_filters", M.exact_sql("meaning"))

    def test_dry_by_default_arguments_checked_and_the_workflow_manual(self):
        os.environ.pop("LOCAL_DB_URL", None)
        with self.assertRaises(SystemExit):
            M.main(["--memory", "8GB; x"])
        with self.assertRaises(SystemExit):
            M.main(["--load"])                                                          # no databases named: stops before anything
        self.assertNotIn("schedule:", FLOW)
        self.assertNotIn("pull_request", FLOW)
        self.assertIn("nologin password null", FLOW)
        self.assertIn("::add-mask::", FLOW)


if __name__ == "__main__":
    unittest.main()
