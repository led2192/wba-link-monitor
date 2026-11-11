import argparse, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import propose_values as P  # noqa: E402
import extract_eval  # noqa: E402


class SecondLevel(unittest.TestCase):
    def tearDown(self):
        extract_eval.ESCALATE = "none"

    def test_the_whole_document_on_doubtful_answers_is_the_default(self):
        self.assertEqual(P.set_reader_options(argparse.Namespace()), "whole"); self.assertEqual(extract_eval.ESCALATE, "whole")

    def test_it_can_be_turned_off(self):
        self.assertEqual(P.set_reader_options(argparse.Namespace(escalate="none")), "none"); self.assertEqual(extract_eval.ESCALATE, "none")

    def test_the_night_run_has_the_cap_the_owner_set(self):
        y = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "propose_values.yml")).read()
        self.assertIn("--max-usd ${{ vars.READER_MAX_USD || '5' }} --fresh-days 30 --commit", y); self.assertIn("--max-usd 0.3 --escalate none", y)


if __name__ == "__main__":
    unittest.main()
