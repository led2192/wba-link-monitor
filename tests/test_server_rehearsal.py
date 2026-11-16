import os, unittest

import server_rehearsal as R

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = open(os.path.join(HERE, "server_rehearsal.py"), encoding="utf-8").read()
FLOW = open(os.path.join(HERE, ".github", "workflows", "server_rehearsal.yml"), encoding="utf-8").read()


class Rehearsal(unittest.TestCase):
    def test_the_arithmetic(self):
        self.assertEqual(R.sample_percent(1_000_000, 4_100_000), 26.829)
        self.assertEqual(R.sample_percent(1_000_000, 500_000), 100.0)
        self.assertEqual(R.sample_percent(10, 0), 100.0)
        self.assertEqual(R.recall([["a", "b"], ["c", "d"]], [["a", "x"], ["c", "d"]]), 0.75)
        self.assertEqual(R.recall([], []), 0.0)
        self.assertEqual(R.percentile(list(range(1, 101)), 95), 95)
        self.assertEqual(R.percentile([], 95), 0)
        self.assertEqual(R.extrapolate(10, 1_000_000, 21_000_000), 210)

    def test_the_report_extrapolates_and_names_nothing(self):
        text = "\n".join(R.report(1_000_000, 300, 1024 ** 3, 600, 2 * 1024 ** 3, 0.987, [5.0, 7.0, 30.0], "8GB", 6))
        self.assertIn("1,000,000 in 5.0 min (3,333 a second)", text)
        self.assertIn("| Index build | 10.0 min with 8GB and 6 workers |", text)
        self.assertIn("98.7% over 3 searches", text)
        self.assertIn("| Index | 8.2 GB | 42.0 GB |", text)
        self.assertIn("| Index build, at least | 41 min | 3.5 h |", text)

    def test_supabase_is_only_read_and_only_the_rehearsal_schema_is_dropped(self):
        low = SCRIPT.lower()
        for word in ("insert into public", "update public", "delete from", "truncate", "drop index", "drop table public", "alter table public"):
            self.assertNotIn(word, low)
        drops = [l.strip() for l in SCRIPT.splitlines() if "drop " in l.lower() and "execute" in l]
        self.assertEqual(len(drops), 2)
        self.assertTrue(all("{SCHEMA}" in l for l in drops))                            # its own table, its own schema
        self.assertEqual(R.SCHEMA, "rehearsal")

    def test_dry_by_default_and_arguments_checked(self):
        os.environ.pop("LOCAL_DB_URL", None)
        with self.assertRaises(SystemExit):
            R.main(["--memory", "8GB; drop"])
        with self.assertRaises(SystemExit):
            R.main(["--rows", "50000000"])
        self.assertIn('ap.add_argument("--commit", action="store_true"', SCRIPT)

    def test_the_workflow_is_manual_and_the_password_is_for_one_run(self):
        self.assertNotIn("schedule:", FLOW)
        self.assertNotIn("pull_request", FLOW)
        self.assertIn("openssl rand", FLOW)
        self.assertIn("::add-mask::", FLOW)
        self.assertIn("if: always()", FLOW)
        self.assertIn("nologin password null", FLOW)
        self.assertIn("default: false", FLOW)


if __name__ == "__main__":
    unittest.main()
