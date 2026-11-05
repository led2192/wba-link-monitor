import os
import unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import extract_eval as E


class Outage(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.cwd, self.tmp = os.getcwd(), tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def test_ten_database_failures_in_a_row_stop_the_run_without_a_score(self):
        answers = [{"wba": f"PT_{i:05d}", "attribute": "GMC_00048.1", "year": 2024, "value": "100", "unit": "USD",
                    "id": f"PT_{i:05d}-GMC_00048.1-2024"} for i in range(40)]
        class Cur:
            def execute(self, *a, **k): pass
        class Conn:
            def cursor(self): return Cur()
            def rollback(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        calls = []
        def boom(*a, **k):
            calls.append(1)
            raise RuntimeError("canceling statement due to statement timeout")
        evals = []
        with mock.patch.dict(os.environ, {"GOLD_TOKEN": "t", "QA_COMMIT": "false"}), \
             mock.patch("value_eval.gold_values", return_value=answers), \
             mock.patch("evidence_db.connect", return_value=Conn()), \
             mock.patch.object(E, "evaluate", side_effect=boom), \
             mock.patch.object(E.qa.Run, "evaluation", side_effect=lambda *a, **k: evals.append(a)), \
             mock.patch("sys.argv", ["extract_eval.py", "--limit", "0", "--attributes", "GMC_00048.1"]):
            code = E.main()
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), E.DB_FAILS_IN_A_ROW)
        self.assertEqual(evals, [])
        with open("extract_eval.md") as fh:
            self.assertIn("STOPPED: the evidence database did not answer 10 times in a row", fh.read())


if __name__ == "__main__":
    unittest.main()
