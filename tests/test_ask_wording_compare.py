"""ask_wording_compare.py: the sums and the rule that decides, offline; and what the option changes, on the text."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ask_wording_compare as W

SQL = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supabase", "search.sql"), encoding="utf-8").read()


def arm(answered=True, facts=2, values=("10", "20"), **flags):
    return {"answered": answered, "facts": facts, "values": list(values), "cites": 2, "length": 100, **flags}


class Sums(unittest.TestCase):
    def test_counts_and_the_rule(self):
        trials = [{"off": arm(bare=True, colleague=True), "again": arm(bare=True), "on": arm(), "known": True, "tokens_in": 10, "tokens_out": 1},
                  {"off": arm(wba=True), "again": arm(values=("10",)), "on": arm(), "fiscal": True}, {"skipped": "x"}]
        s = W.summarise(trials)
        self.assertEqual((s["cases"], s["skipped"], s["with_known"], s["with_fiscal"]), (2, 1, 1, 1))
        self.assertEqual((s["arms"]["off"]["bare"], s["arms"]["on"]["bare"], s["arms"]["off"]["colleague"], s["arms"]["off"]["wba"]), (1, 0, 1, 1))
        self.assertEqual((s["arms"]["again"]["values_kept"], s["arms"]["on"]["values_kept"]), (3, 4))
        self.assertEqual(W.verdict(s), (True, []))
        worse = W.summarise([{"off": arm(), "again": arm(), "on": arm(answered=False, facts=0, values=())}])
        ok, why = W.verdict(worse); self.assertFalse(ok); self.assertEqual(len(why), 3)
        self.assertIn("No attribute worse by more than 3 points: yes.", W.report(s))
        self.assertEqual(W.verdict(W.summarise([])), (False, ["no case ran"]))

    def test_the_option_is_off_and_the_trial_keeps_nothing(self):
        self.assertIn("insert into public.search_options (name, is_on, note) values ('reader_plain_words', false,", SQL)
        t = SQL.split("create or replace function public.ask_wording_trial(the_ask bigint)")[1].split("\n$$;")[0]
        self.assertIn("array['off', 'again', 'on']", t); self.assertNotIn("insert into", t); self.assertNotIn("ask_keep", t)
        self.assertIn("perform set_config('lantern.reader_plain_words', case when arm = 'on' then 'on' else 'off' end, true);", t)
        self.assertIn("revoke all on function public.ask_wording_trial(bigint) from public, anon, authenticated;", SQL)
        wf = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows", "ask_wording_compare.yml"), encoding="utf-8").read()
        self.assertIn("workflow_dispatch", wf); self.assertNotIn("schedule", wf); self.assertNotIn("pull_request", wf)      # by hand only: it costs


if __name__ == "__main__":
    unittest.main()
