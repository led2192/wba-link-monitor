import argparse, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import propose_values as P  # noqa: E402
import extract_eval  # noqa: E402


class SecondLevel(unittest.TestCase):
    def tearDown(self):
        extract_eval.ESCALATE = "none"
        extract_eval.SEMANTIC_POOL = 8

    def test_the_whole_document_on_doubtful_answers_is_the_default(self):
        self.assertEqual(P.set_reader_options(argparse.Namespace()), "whole"); self.assertEqual(extract_eval.ESCALATE, "whole")

    def test_it_can_be_turned_off(self):
        self.assertEqual(P.set_reader_options(argparse.Namespace(escalate="none")), "none"); self.assertEqual(extract_eval.ESCALATE, "none")

    def test_the_semantic_pool_is_on_and_can_be_turned_off(self):
        P.set_reader_options(argparse.Namespace()); self.assertEqual(extract_eval.SEMANTIC_POOL, 8)
        P.set_reader_options(argparse.Namespace(semantic_pool=0)); self.assertEqual(extract_eval.SEMANTIC_POOL, 0)

    def test_a_pool_that_never_had_a_vector_is_a_failure(self):
        import collections

        class Run:
            def __init__(self): self.failures = []
            def failure(self, code, detail, key=None, **f): self.failures.append((code, key))
        keep = collections.Counter(extract_eval.SEMANTIC_STATS)
        try:
            extract_eval.SEMANTIC_STATS.clear(); extract_eval.SEMANTIC_STATS.update({"asked": 4, "no vector": 4})
            run, c = Run(), collections.Counter()
            self.assertFalse(P.semantic_counters(c, run))
            self.assertEqual(run.failures, [("DATA_CHECK", "semantic_pool_no_vector")]); self.assertEqual(c["model reader: semantic pool, asked"], 4)
            extract_eval.SEMANTIC_STATS.clear(); extract_eval.SEMANTIC_STATS.update({"asked": 4, "vector from the database": 2, "pages added": 30})
            run = Run()
            self.assertTrue(P.semantic_counters(collections.Counter(), run)); self.assertEqual(run.failures, [])
            extract_eval.SEMANTIC_STATS.clear()
            self.assertTrue(P.semantic_counters(collections.Counter(), Run()))                    # the model reader did not run
        finally:
            extract_eval.SEMANTIC_STATS.clear(); extract_eval.SEMANTIC_STATS.update(keep)

    def test_the_night_run_has_the_cap_the_owner_set(self):
        y = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "propose_values.yml")).read()
        self.assertIn("--max-usd ${{ vars.READER_MAX_USD || '5' }} --fresh-days 30 --commit", y); self.assertIn("--max-usd 0.3 --escalate none", y)


if __name__ == "__main__":
    unittest.main()
