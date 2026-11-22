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
        for word in ("insert into public", "drop table", "alter table public", "truncate public", "create index if not exists chunks_embedding on public", "delete from public"):
            self.assertNotIn(word, low)
        self.assertEqual(low.count("update public"), 1)                                  # one row, ever: the option switched off when the copy is not current
        self.assertIn("update public.search_options set is_on = false, changed = now(), note = note || ' [off by the refresh]' where name = 'meaning_remote' and is_on", SCRIPT)
        self.assertEqual(low.count("delete from"), 1)                                    # the server's own copy of a document, before it is copied again
        self.assertIn('d.execute(f"delete from {schema}.chunks where library_id = any(%s)"', SCRIPT)
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


    def test_what_differs_between_the_two_copies(self):
        theirs, ours = {"A": 10, "B": 5, "C": 7}, {"A": 10, "B": 4, "D": 2, "E": 9}
        self.assertEqual(M.drift(theirs, ours, dead=["E"]), (["C"], ["B"], ["D"]))             # new, changed, gone (one already marked is not gone again)
        self.assertTrue(M.SAFE_ID.match("recAbC_12.x:y-z")); self.assertFalse(M.SAFE_ID.match("a,b")); self.assertFalse(M.SAFE_ID.match("a}'"))
        dry = "\n".join(M.refresh_report(dict(new=3, changed=1, gone=2, copied=0, skipped=0, left=4, switched_off=False), "meaning", False))
        self.assertIn("dry run, nothing copied", dry); self.assertNotIn("NOT current", dry)
        bad = "\n".join(M.refresh_report(dict(new=3, changed=1, gone=2, copied=3, skipped=1, left=1, switched_off=True), "meaning", True))
        self.assertIn("Documents that still differ: 1.", bad)
        self.assertIn("was switched off: Supabase's own search carries on.", bad)

    def test_the_switch_report_judges_both_against_the_truth(self):
        res = {v: dict(off=[], on=[], truth=[], ms_off=[], ms_on=[]) for v in M.VARIANTS[:3]}
        res["no filter"].update(truth=[["a", "b", "c", "d"]] * 10, off=[["a", "b", "c", "x"]] * 10, on=[["a", "b", "c", "d"]] * 10, ms_off=[400.0] * 10, ms_on=[90.0] * 10)
        res["two years"].update(truth=[["a", "b", "c", "d"]] * 10, off=[["a", "b", "c", "d"]] * 10, on=[["a", "b"]] * 10, ms_off=[20.0] * 10, ms_on=[95.0] * 10)
        lines, worse = M.switch_report(res)
        text = "\n".join(lines)
        self.assertEqual(worse, ["two years"])
        self.assertIn("| no filter | 10 | 75.0% | 100.0% | 0 | 4.0 / 4.0 | 400 / 400 | 90 / 90 |", text)
        self.assertIn("LOWER with the option on by more than 3 points: two years.", text)
        self.assertIn("| a type | 0 |", text)

    def test_the_clock_starts_the_refresh_every_night(self):
        import json
        clock = json.load(open(os.path.join(HERE, "server", "clock.json")))
        mine = [c for c in clock if c["workflow"] == "server_meaning.yml"]
        self.assertEqual((len(mine), mine[0]["inputs"]), (1, {"as_scheduled": "true"}))
        self.assertIn("(inputs.refresh || inputs.as_scheduled) && '--refresh'", FLOW)
        self.assertIn("(inputs.commit || inputs.as_scheduled) && '--commit'", FLOW)


if __name__ == "__main__":
    unittest.main()
