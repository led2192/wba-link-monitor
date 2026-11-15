"""ask_names_compare.py: the sums over the trials, the rule that says whether the option made anything worse, and the
report. No network: the trials are given as the database returns them."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ask_names_compare as C


def arm(answered=True, facts=4, with_metric=1, as_listed=0, values=("5", "7"), names=("a", "b"), cites=3, length=400):
    return {"answered": answered, "facts": facts, "with_metric": with_metric, "as_listed": as_listed, "values": list(values), "names": list(names), "cites": cites, "length": length}


def trial(off=None, again=None, on=None):
    return {"off": off or arm(), "again": again or arm(), "on": on or arm(), "names_given": 40, "tokens_in": 9000, "tokens_out": 600}


class Sums(unittest.TestCase):
    def test_skipped_trials_are_counted_apart(self):
        s = C.summarise([trial(), {"skipped": "no name to give"}, trial()])
        self.assertEqual((s["cases"], s["skipped"], s["tokens_in"]), (2, 1, 18000))
        self.assertEqual(s["arms"]["off"]["facts"], 8)

    def test_what_the_first_answer_gave_is_looked_for_in_the_others(self):
        s = C.summarise([trial(off=arm(values=("5", "7", "9"), names=("scope 1", "revenue")), again=arm(values=("5", "7")), on=arm(values=("5", "9", "11"), names=("revenue", "x")))])
        self.assertEqual((s["arms"]["off"]["values_kept"], s["arms"]["again"]["values_kept"], s["arms"]["on"]["values_kept"]), (3, 2, 2))
        self.assertEqual((s["arms"]["again"]["names_kept"], s["arms"]["on"]["names_kept"]), (0, 1))


class Rule(unittest.TestCase):
    def test_names_that_line_up_and_nothing_lost_passes(self):
        s = C.summarise([trial(on=arm(with_metric=4, as_listed=4)) for _ in range(10)])
        self.assertEqual(C.verdict(s), (True, []))
        text = C.report(s)
        self.assertIn("| facts with a metric | 10 (25%) | 10 (25%) | 40 (100%) |", text)
        self.assertIn("No attribute worse by more than 3 points: yes.", text)

    def test_fewer_answers_fails(self):
        trials = [trial() for _ in range(9)] + [trial(on=arm(answered=False, facts=0, values=(), names=()))]
        ok, why = C.verdict(C.summarise(trials))
        self.assertFalse(ok); self.assertIn("fewer answers", why)

    def test_losing_figures_a_second_plain_answer_keeps_fails(self):
        trials = [trial(on=arm(values=("5",))) for _ in range(10)]
        ok, why = C.verdict(C.summarise(trials))
        self.assertFalse(ok); self.assertEqual(why, ["keeps fewer of the figures than a second plain answer does"])

    def test_figures_the_plain_answers_lose_too_are_noise_not_a_loss(self):
        trials = [trial(again=arm(values=("5",)), on=arm(values=("5",))) for _ in range(10)]
        self.assertEqual(C.verdict(C.summarise(trials)), (True, []))

    def test_no_case_is_not_a_pass(self):
        self.assertEqual(C.verdict(C.summarise([{"skipped": "x"}]))[0], False)

    def test_the_report_prints_no_question_name_or_value(self):
        text = C.report(C.summarise([trial(off=arm(names=("secret metric",), values=("123456",)))]))
        self.assertNotIn("secret metric", text); self.assertNotIn("123456", text)


if __name__ == "__main__":
    unittest.main()
