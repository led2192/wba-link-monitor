"""intent_model_trial.py: the counting. The model is asked, in real use, only where the rules fall back to a plain
search with a company named; what it changes is counted as made right or made wrong."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import intent_model_trial as T

G = lambda intent, company="X", **kw: dict({"intent": intent, "company": company, "typo": None, "from": None, "to": None, "type": None, "q": "words"}, **kw)


class Counting(unittest.TestCase):
    def test_the_reply_is_one_of_the_five_ways_or_nothing(self):
        self.assertEqual(T.parse_reply('{"way": "overview"}'), "overview")
        self.assertEqual(T.parse_reply('Sure: {"way":"fact"} because'), "fact")
        self.assertEqual(T.parse_reply("overview"), "overview"); self.assertEqual(T.parse_reply("**Fact**."), "fact")
        self.assertIsNone(T.parse_reply("It could be a fact or a question")); self.assertIsNone(T.parse_reply("I think this long reply is about a search for pages of it"))
        self.assertIsNone(T.parse_reply('{"way": "browse"}')); self.assertIsNone(T.parse_reply("")); self.assertIsNone(T.parse_reply(None))

    def test_the_model_is_told_what_was_recognised_and_never_the_answer(self):
        said = T.describe({"typed": "Gerdau capex 2024", "intent": "fact"}, G("fact", **{"from": 2024, "to": 2024}))
        self.assertEqual(said, "Typed: Gerdau capex 2024\nRecognised: a company is named; years 2024.")
        self.assertIn("no company is named", T.describe({"typed": "living wage"}, G("search", company=None)))
        self.assertIn("years 2021 to 2023", T.describe({"typed": "x"}, G("search", **{"from": 2021, "to": 2023})))
        self.assertIn("a kind of report is named (Annual Report)", T.describe({"typed": "x"}, G("search", type="Annual Report")))
        for c in T.E.load()["cases"]:
            self.assertNotIn(c["typed"], T.SYSTEM)                              # no example taken from the exam

    def test_the_model_is_asked_only_where_the_rules_do_not_recognise_the_text(self):
        self.assertTrue(T.unrecognised(G("search")))
        self.assertTrue(T.unrecognised(G("search", company=None, typo="Nike")))
        self.assertFalse(T.unrecognised(G("search", company=None)))             # no company: pages are what is wanted
        self.assertFalse(T.unrecognised(G("search", type="Annual Report")))
        self.assertFalse(T.unrecognised(G("search", q='"science based targets"')))
        self.assertFalse(T.unrecognised(G("fact")))

    def test_what_the_model_changes_is_counted(self):
        cases = [{"typed": "a", "intent": "overview"}, {"typed": "b", "intent": "search"}, {"typed": "c", "intent": "fact"}, {"typed": "d", "intent": "overview"}, {"typed": "e", "intent": "question"}]
        got = [G("search"), G("search"), G("fact"), G("search"), G("search")]
        replies = ["overview", "overview", "search", "fact", None]
        r = T.score(cases, got, replies)
        self.assertEqual((r["alone"], r["rules"], r["after"], r["asked"], r["unanswered"]), ([1, 5], 2, 2, 4, 1))
        self.assertEqual(r["changed"], {"made right": 1, "made wrong": 1, "wrong either way": 1})
        self.assertEqual(r["by_way"]["overview"], [1, 1, 2])
        self.assertEqual(r["by_way"]["fact"], [0, 1, 1])                        # the rules knew it: the model is not asked, its wrong reply costs nothing

    def test_a_model_that_fails_on_a_text_is_counted_not_fatal_and_the_cap_stops_it(self):
        sets = [("two", [{"typed": "a", "intent": "search"}, {"typed": "b", "intent": "overview"}], [G("search"), G("search")])]
        calls = []
        def ask(text):
            calls.append(text)
            if len(calls) == 1: raise OSError("down")
            return '{"way": "overview"}'
        scored, secs = T.run_model(ask, sets)
        self.assertEqual((scored[0][1]["unanswered"], scored[0][1]["alone"][0], len(secs)), (1, 1, 2))
        scored, _ = T.run_model(ask, sets, stop=lambda: True)
        self.assertEqual((scored[0][1]["unanswered"], len(calls)), (2, 2))       # the cap reached: nothing more is asked


if __name__ == "__main__":
    unittest.main()
