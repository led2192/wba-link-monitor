import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "appX")
import scan_now as S


class R:
    returncode = 0


class SideJobs(unittest.TestCase):
    def test_nothing_without_the_database(self):
        with mock.patch.dict(os.environ, {"SUPABASE_DB_URL": ""}):
            self.assertEqual(S.side_jobs(run=lambda *a, **k: self.fail("ran")), [])

    def test_both_jobs_run_and_one_failing_does_not_stop_the_other(self):
        calls = []
        def run(cmd, timeout=None):
            calls.append(cmd[1])
            if cmd[1] == "add_companies.py":
                raise RuntimeError("database busy")
            return R()
        with mock.patch.dict(os.environ, {"SUPABASE_DB_URL": "postgresql://x"}):
            done = S.side_jobs(run=run)
        self.assertEqual(calls, ["add_companies.py", "browser_uploads.py"])
        self.assertEqual(done, [("add_companies", -1), ("browser_uploads", 0)])

    def test_new_companies_have_their_pages_searched_for(self):
        calls = []
        def run(cmd, timeout=None):
            calls.append(cmd[1:])
            if cmd[1] == "add_companies.py":
                open(cmd[cmd.index("--ids-file") + 1], "w").write("LN_0a1b2c3d")
            return R()
        with mock.patch.dict(os.environ, {"SUPABASE_DB_URL": "postgresql://x"}):
            S.side_jobs(run=run)
        self.assertEqual(calls[-1], ["discover_pages.py", "--commit", "--max", "200", "--wba", "LN_0a1b2c3d"])
        self.assertFalse(os.path.exists("new_company_ids.txt"))

    def test_the_watch_calls_them(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scan_now.py"), encoding="utf-8").read()
        self.assertIn("if checks % SIDE_EVERY == 1:\n            side_jobs()", src)
        wf = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows", "scan_now.yml"), encoding="utf-8").read()
        self.assertIn("SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}", wf)


if __name__ == "__main__":
    unittest.main()
