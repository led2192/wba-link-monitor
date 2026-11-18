import unittest

import survey_eval as S


class Cur:
    """Stub of the evidence database: what a reading returns for each company."""
    def __init__(self, said, boom=()):
        self.said, self.boom, self.connection, self.rollbacks, self.asked, self.rows, self.settings = said, set(boom), self, 0, [], [], []

    def rollback(self): self.rollbacks += 1

    def execute(self, sql, params=None):
        if "set_config" in sql:
            self.settings.append(tuple(params)); return
        assert "public.survey_read('eval'" in sql
        self.asked.append(tuple(params))
        if params[3] in self.boom:
            raise RuntimeError("database")
        self.rows = [(self.said.get(params[3]),)]

    def fetchone(self): return self.rows[0]


G = lambda wba, truth, i=0: {"id": f"{wba}-{i}", "wba": wba, "company": "c", "attribute": "A1", "year": 2024, "truth": truth}
ATTRS = {"A1": {"question": "Does it?", "words": "does it", "vec": None}}


class SurveyEval(unittest.TestCase):
    def test_yes_and_no_in_turn_and_the_same_every_run(self):
        answers = [G("W3", True), G("W1", True), G("W2", False), G("W4", True), G("W5", True)]
        self.assertEqual([g["wba"] for g in S.pick(answers, 4)], ["W1", "W2", "W3", "W4"])
        self.assertEqual(len(S.pick(answers, 0)), 5)

    def test_counts_keep_what_the_survey_claims_apart_from_what_it_leaves_unanswered(self):
        cur = Cur({"W1": {"status": "yes", "page": 4, "printed": True, "in": 4000, "out": 200}, "W2": {"status": "not_stated", "in": 4000, "out": 100},
                   "W3": {"status": "yes", "page": None, "printed": None, "in": 4000, "out": 200}, "W4": {"status": "no_page"}, "W5": None, "W6": {"status": "no", "in": 10, "out": 10}}, boom={"W7"})
        todo = [G("W1", True), G("W2", True), G("W3", False), G("W4", False), G("W5", True), G("W6", False), G("W7", True)]
        lines = []
        s = S.run_eval(cur, todo, ATTRS, 6, 1.0, out=lines.append)["A1"]
        self.assertEqual((s["yes_yes"], s["yes_not_stated"], s["yes_failed"], s["no_yes"], s["no_no_page"], s["no_no"]), (1, 1, 2, 1, 1, 1))
        self.assertEqual((s["named"], s["printed"], s["in"], s["out"]), (1, 1, 12010, 510))
        self.assertEqual(cur.asked[0], ("Does it?", "does it", None, "W1", "c", 2024, 2025, 6))            # the answer's year and the next
        self.assertEqual(cur.rollbacks, 7)                                                              # nothing of a reading is kept
        text = "\n".join(S.table({"A1": s}, "t") + lines)
        self.assertIn("the survey says yes for 1 of 4 (25%)", text)
        self.assertIn("the analysts also say yes for 1 of 2 (50%)", text)
        self.assertIn("a page is named for 1 of 2 (50%)", text)
        self.assertIn("whose sentence stands on its page: the analysts also say yes for 1 of 1 (100%)", text)
        self.assertEqual(cur.settings[:2], [("lantern.survey_strict", "off"), ("lantern.survey_again", "off")])   # every reading says which options it runs with
        self.assertNotIn("Does it?", text)                                                              # the log is public: codes and counts only

    def test_an_option_is_set_before_every_reading(self):
        cur = Cur({"W1": {"status": "yes"}, "W2": {"status": "no"}})
        S.run_eval(cur, [G("W1", True), G("W2", True)], ATTRS, 8, 1.0, options=("survey_again",))
        self.assertEqual(cur.settings, [("lantern.survey_strict", "off"), ("lantern.survey_again", "on"), ("lantern.reader_whole_page", "off")] * 2)   # a rollback undoes a setting
        self.assertEqual(cur.asked[0][-1], 8)

    def test_reading_stops_at_the_ceiling_of_spending(self):
        cur = Cur({"W1": {"status": "yes", "in": 5_000_000, "out": 0}, "W2": {"status": "yes"}})
        s = S.run_eval(cur, [G("W1", True), G("W2", True)], ATTRS, 6, 0.4)["A1"]
        self.assertEqual((s["yes_yes"], s["unread"], len(cur.asked)), (1, 1, 1))
        self.assertIn("1 left unread at the ceiling", "\n".join(S.table({"A1": s}, "t")))


if __name__ == "__main__":
    unittest.main()
