"""job_state.py: the runs of the workflows are read from GitHub page by page and written by run id; a dry run writes
nothing; a refusal of GitHub writes nothing and fails."""
import json, os, sys, unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import job_state as J


def run_of(i, **kw):
    r = {"id": i, "path": ".github/workflows/load_supabase.yml", "name": "Evidence database load", "event": "workflow_dispatch", "status": "completed",
         "conclusion": "success", "created_at": "2026-11-19T01:41:00Z", "run_started_at": "2026-11-19T01:41:05Z"}
    r.update(kw); return r


class Reading(unittest.TestCase):
    def test_a_run_becomes_a_row_and_a_run_without_a_file_is_left_out(self):
        self.assertEqual(J.as_row(run_of(7)), (7, "load_supabase.yml", "Evidence database load", "workflow_dispatch", "completed", "success", "2026-11-19T01:41:05Z"))
        self.assertEqual(J.as_row(run_of(8, path=".github/workflows/tests.yml@refs/heads/x", conclusion=None, status="in_progress"))[1:6], ("tests.yml", "Evidence database load", "workflow_dispatch", "in_progress", None))
        self.assertIsNone(J.as_row(run_of(9, path="dynamic/pages")))
        self.assertIsNone(J.as_row(run_of(None)))

    def test_the_pages_are_read_until_a_short_one(self):
        asked = []
        def run(cmd, **kw):
            asked.append(cmd[2]); page = int(cmd[2].split("&page=")[1].split("&")[0])
            return mock.Mock(returncode=0, stdout=json.dumps({"workflow_runs": [run_of(page * 1000 + i) for i in range(100 if page < 3 else 40)]}))
        got = J.gh_runs("o/r", datetime(2026, 11, 16, tzinfo=timezone.utc), run=run)
        self.assertEqual((len(got), len(asked)), (240, 3))
        self.assertIn("created=>=2026-11-16", asked[0])
        with self.assertRaises(RuntimeError):
            J.gh_runs("o/r", datetime(2026, 11, 16, tzinfo=timezone.utc), run=lambda cmd, **kw: mock.Mock(returncode=1, stdout=""))

    def _main(self, argv, runs=None, fail=False, db=True):
        cur = mock.MagicMock(); cur.fetchone.return_value = (2,)
        conn = mock.MagicMock(); conn.__enter__.return_value = conn; conn.cursor.return_value = cur
        env = {"GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t"}
        if db: env["SUPABASE_DB_URL"] = "postgresql://x"
        gh = mock.Mock(side_effect=RuntimeError("no")) if fail else mock.Mock(return_value=runs or [])
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["job_state.py"] + argv), mock.patch.object(J, "gh_runs", gh), \
             mock.patch.dict(sys.modules, {"evidence_db": mock.MagicMock(connect=lambda: conn)}), mock.patch.object(J.qa, "Run") as log, mock.patch("builtins.print") as out:
            code = J.main()
        return code, cur, log, " ".join(str(c.args[0]) for c in out.call_args_list)

    def test_a_dry_run_counts_and_writes_nothing(self):
        code, cur, _, said = self._main([], [run_of(1), run_of(2, conclusion="failure")])
        self.assertEqual(code, 0); cur.executemany.assert_not_called()
        self.assertIn("2 runs of 1 workflows", said); self.assertIn("1 failed", said)
        self.assertNotIn("Evidence database load", said)                       # counts only

    def test_with_commit_the_runs_are_written_by_run_id(self):
        code, cur, log, _ = self._main(["--commit"], [run_of(1), run_of(2, conclusion="failure"), run_of(3, path="x")])
        self.assertEqual(code, 0)
        sql, rows = cur.executemany.call_args.args
        self.assertIn("on conflict (run_id) do update", sql); self.assertEqual([r[0] for r in rows], [1, 2])
        log.return_value.finish.assert_called_once()

    def test_when_github_refuses_nothing_is_written(self):
        code, cur, log, _ = self._main(["--commit"], fail=True)
        self.assertEqual(code, 1); cur.executemany.assert_not_called()
        log.return_value.failure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
