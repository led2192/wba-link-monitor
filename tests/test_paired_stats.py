import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import paired_stats as P  # noqa: E402
import compare_runs as C  # noqa: E402


class McNemar(unittest.TestCase):
    def test_exact_values(self):
        self.assertAlmostEqual(P.mcnemar_p(10, 0), 2 / 1024)
        self.assertEqual(P.mcnemar_p(5, 5), 1.0)
        self.assertEqual(P.mcnemar_p(0, 0), 1.0)
        self.assertAlmostEqual(P.mcnemar_p(3, 1), 0.625)     # 2 * (1 + 4) / 16

    def test_symmetric(self):
        self.assertEqual(P.mcnemar_p(7, 2), P.mcnemar_p(2, 7))


class Bootstrap(unittest.TestCase):
    def test_no_difference_interval_holds_zero(self):
        rows = [(f"c{i % 20}", i % 3 == 0, i % 3 == 0) for i in range(200)]
        d, lo, hi = P.cluster_bootstrap(rows)
        self.assertEqual((d, lo, hi), (0.0, 0.0, 0.0))

    def test_a_clear_gain_is_better(self):
        rows = [(f"c{i % 40}", False, True) for i in range(60)] + [(f"c{i % 40}", True, True) for i in range(140)]
        s = P.compare(rows)
        self.assertEqual((s["fixed"], s["broken"]), (60, 0))
        self.assertGreater(s["low"], 0)
        self.assertEqual(P.verdict(s), "better")

    def test_one_company_carries_the_gain_so_it_is_not_shown(self):
        # 20 fixes, all in a single company: answers look independent, companies do not
        rows = [("big", False, True)] * 20 + [(f"c{i}", True, True) for i in range(30)] + [(f"c{i}", False, False) for i in range(30, 60)]
        s = P.compare(rows)
        self.assertLess(s["p"], 0.05)                    # McNemar alone would call it
        self.assertLessEqual(s["low"], 0.0)              # resampling companies does not
        self.assertEqual(P.verdict(s), "not distinguishable from chance")

    def test_deterministic(self):
        rows = [(f"c{i % 9}", i % 2 == 0, i % 5 == 0) for i in range(90)]
        self.assertEqual(P.cluster_bootstrap(rows), P.cluster_bootstrap(rows))


class CompareRuns(unittest.TestCase):
    def test_reads_the_block_from_a_gh_log(self):
        log = ("evaluate\tEvaluate\t2026-11-07T01:00:00Z >>> OUTCOMES_JSON_BEGIN\n"
               'evaluate\tEvaluate\t2026-11-07T01:00:00Z {"select":"base","rows":[["k1","c1","GMC_1",1,1,1,1]]}\n'
               "evaluate\tEvaluate\t2026-11-07T01:00:00Z >>> OUTCOMES_JSON_END\n")
        self.assertEqual(C.outcomes_from_log(log)["rows"][0][0], "k1")
        self.assertIsNone(C.outcomes_from_log("nothing here"))

    def test_only_shared_answers_are_compared(self):
        a = {"reader": "model", "rows": [["k1", "c1", "G", 1, 1, 1, 1], ["k2", "c2", "G", 0, 1, 1, 1], ["k3", "c3", "G", 1, 1, 1, 1]]}
        b = {"reader": "model", "rows": [["k1", "c1", "G", 1, 1, 1, 1], ["k2", "c2", "G", 1, 1, 1, 1], ["k9", "c9", "G", 0, 1, 1, 1]]}
        md = C.report(a, b)
        self.assertIn("Answers scored by both runs: 2; only by A: 1; only by B: 1", md)
        self.assertIn("| G | 2 | 2 | 50% | 100% | 1 | 0 |", md)


if __name__ == "__main__":
    unittest.main()
