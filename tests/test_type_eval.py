"""type_eval.py: the scoring and the report, offline (no network, no model)."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import type_eval as E
from qa_sync import checks_of


def doc(truth, stored, rule, ours, english=True, **k):
    return dict({"library_id": "PT_1-x", "truth": truth, "stored": stored, "rule": rule, "ours": ours, "english": english}, **k)


class Values(unittest.TestCase):
    def test_plain_pick_one_and_ai_fields(self):
        self.assertEqual(E.value(" Annual Report "), "Annual Report")
        self.assertEqual(E.value({"name": "ESG Report"}), "ESG Report")
        self.assertEqual(E.value({"state": "generated", "value": "GRI"}), "GRI")
        self.assertEqual(E.value({"state": "error", "value": None}), "")
        self.assertEqual(E.value(None), "")

    def test_the_same_documents_every_run_and_not_the_oldest(self):
        truth = {f"rec{i:03d}": "Annual Report" for i in range(200)}
        a = E.pick(truth, 20)
        self.assertEqual(a, E.pick(dict(reversed(list(truth.items()))), 20))
        self.assertNotEqual(a, sorted(truth)[:20])
        self.assertEqual(len(E.pick(truth, 0)), 200)

    def test_titles(self):
        self.assertTrue(E.same_title("Annual Report 2024", "Acme plc - Annual report 2024"))
        self.assertFalse(E.same_title("Annual Report 2024", "Sustainability Report 2024"))
        self.assertFalse(E.same_title("", "Annual Report"))


class Scores(unittest.TestCase):
    def setUp(self):
        self.docs = [doc("Annual Report", "Annual Report", "Annual Report", "Annual Report"),
                     doc("Annual Report", "10K Form", "", "Annual Report"),                   # check level right for the stored type
                     doc("Code of Conduct", "Code of Conduct", "", "Policy Documents"),
                     doc("ESG Report", "", "", "", english=False)]                            # nobody typed it

    def test_counts(self):
        c = E.score(self.docs, checks_of)
        self.assertEqual((c["English", "n"], c["Other", "n"], c["All", "n"]), (3, 1, 4))
        self.assertEqual((c["English", "stored", "exact"], c["English", "stored", "check"]), (2, 3))
        self.assertEqual((c["English", "ours", "exact"], c["English", "rule", "typed"]), (2, 1))
        self.assertEqual((c["English", "both"], c["English", "only_stored"], c["English", "only_ours"], c["English", "neither"]), (1, 1, 1, 0))
        self.assertEqual((c["Other", "ours", "typed"], c["Other", "ours", "check"], c["Other", "neither"]), (0, 0, 1))   # no type is never right

    def test_the_report_names_no_text_of_a_document(self):
        for d in self.docs:
            d.update(our_title="Annual Report 2024", ai_title="Acme Annual Report 2024")
        md = E.report(self.docs, E.score(self.docs, checks_of), {"documents with no first pages read": 2}, "4 calls.")
        self.assertIn("| English (3) | our reader | 100.0% | 66.7% | 66.7% |", md)
        self.assertIn("Code of Conduct > Policy Documents 1", md)
        self.assertIn("100% are the same title", md)
        self.assertIn("Left out: documents with no first pages read 2.", md)
        self.assertNotIn("Acme", md)


if __name__ == "__main__":
    unittest.main()
