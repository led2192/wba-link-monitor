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

    def test_the_option_went_on_after_its_comparison_and_the_trial_keeps_nothing(self):
        self.assertIn("insert into public.search_options (name, is_on, note) values ('reader_plain_words', false,", SQL)
        self.assertIn("note || ' (on since 20 Nov 2026, run 44827372584)' where name = 'reader_plain_words' and not is_on and note not like '%on since%';", SQL)
        t = SQL.split("create or replace function public.ask_wording_trial(the_ask bigint)")[1].split("\n$$;")[0]
        self.assertIn("array['off', 'again', 'on']", t); self.assertNotIn("insert into", t); self.assertNotIn("ask_keep", t)
        self.assertIn("perform set_config('lantern.reader_plain_words', case when arm = 'on' then 'on' else 'off' end, true);", t)
        self.assertIn("revoke all on function public.ask_wording_trial(bigint) from public, anon, authenticated;", SQL)
        wf = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows", "ask_wording_compare.yml"), encoding="utf-8").read()
        self.assertIn("workflow_dispatch", wf); self.assertNotIn("schedule", wf); self.assertNotIn("pull_request", wf)      # by hand only: it costs

    def test_any_option_of_the_reader_is_compared_the_same_way(self):
        """22 Nov 2026: option reader_asked_form (Yes or No only to a question; a part named only when it is missing)."""
        self.assertIn("values ('reader_asked_form', false,", SQL)
        t = SQL.split("create or replace function public.ask_option_trial(the_ask bigint, option text)")[1].split("\n$$;")[0]
        self.assertIn("array['off', 'again', 'on']", t); self.assertNotIn("insert into", t); self.assertNotIn("ask_keep", t)
        self.assertIn("o.name like 'reader\\_%'", t)                                                    # only an option of the reader can be named
        self.assertIn("perform set_config('lantern.' || option, case when arm = 'on' then 'on' else 'off' end, true);", t)
        self.assertIn("revoke all on function public.ask_option_trial(bigint, text) from public, anon, authenticated;", SQL)
        note = SQL.split("create or replace function public.ask_overview_note(topic jsonb)")[1].split("$fn$;")[0]
        self.assertIn("when they cover all of them, say nothing about the parts", note); self.assertIn("Then say in one line which of these parts the excerpts do not cover. ", note)
        self.assertEqual(SQL.count("Then say in one line which of these parts the excerpts do not cover."), 1)       # ask and the trial read it from one place
        arm = lambda **k: {"answered": True, "facts": 2, "cites": 2, "length": 100, "values": [1.0], "starts_yes": False, "verdict_unasked": False, "parts_talk": False, **k}
        s = W.summarise([{"off": arm(starts_yes=True, verdict_unasked=True), "again": arm(parts_talk=True), "on": arm(), "overview": True, "question": False}])
        self.assertEqual((s["overviews"], s["not_questions"], s["arms"]["off"]["verdict_unasked"], s["arms"]["again"]["parts_talk"], s["arms"]["on"]["starts_yes"]), (1, 1, 1, 1, 0))
        text = W.report(s, "reader_asked_form")
        self.assertIn("option reader_asked_form", text); self.assertIn("| Yes or No to words that were not a question | 1 | 0 | 0 |", text)
        self.assertNotIn("Yes or No to words", W.report(s))                                                # the first option's report is as it was
        wf = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".github", "workflows", "ask_wording_compare.yml"), encoding="utf-8").read()
        self.assertIn("--option \"${{ inputs.option || 'reader_plain_words' }}\"", wf)


if __name__ == "__main__":
    unittest.main()
