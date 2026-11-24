import json, os, unittest
from datetime import datetime, timedelta, timezone

import clock as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = timezone.utc
E1 = {"workflow": "a.yml", "name": "A", "at": [(1, 41), (19, 41)], "inputs": {"as_scheduled": "true"}}
E2 = {"workflow": "b.yml", "name": "B", "at": [(3, 30)], "inputs": {}}


def at(h, m, day=15):
    return datetime(2026, 11, day, h, m, tzinfo=UTC)


class Due(unittest.TestCase):
    def test_only_what_came_due_since_the_last_look(self):
        self.assertEqual(C.due([E1, E2], at(1, 40), at(1, 41)), [(at(1, 41), E1)])
        self.assertEqual(C.due([E1, E2], at(1, 41), at(1, 42)), [])                      # started once, not at every look
        self.assertEqual([(m, e["name"]) for m, e in C.due([E1, E2], at(1, 0), at(4, 0))], [(at(1, 41), "A"), (at(3, 30), "B")])

    def test_across_midnight(self):
        self.assertEqual(C.due([E1], at(23, 50, 14), at(2, 0, 15)), [(at(1, 41, 15), E1)])

    def test_a_stop_of_some_hours_is_caught_up_and_a_long_one_is_not_replayed(self):
        now = at(8, 0)
        self.assertEqual(C.start_from(at(3, 0), now), at(3, 0))                          # five hours: what came due is started
        self.assertEqual(C.start_from(at(1, 0), now), now)                               # seven hours: from now
        self.assertEqual(C.start_from(None, now), now)
        self.assertEqual(C.start_from(at(9, 0), now), now)                               # a time in the future is not believed


class Watch(unittest.TestCase):
    def run_watch(self, starter, minutes=3, saved=None):
        t = [at(1, 39)]
        states = []
        failed = []
        out = C.watch([E1, E2], minutes, "o/r", now=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + timedelta(seconds=s)),
                      starter=starter, load=lambda: saved, save=states.append, failed=lambda e, m: failed.append((e["name"], m)))
        return out, states, failed

    def test_starts_each_thing_once_at_its_time(self):
        started = []
        out, states, failed = self.run_watch(lambda e, repo: started.append((e["workflow"], repo)) or True)
        self.assertEqual(started, [("a.yml", "o/r")])
        self.assertEqual(out, (1, 0)); self.assertEqual(failed, [])
        self.assertEqual(states[-1], at(1, 42))                                          # the time of the last look is kept

    def test_a_refused_start_is_tried_three_times_then_recorded(self):
        calls = []
        out, _, failed = self.run_watch(lambda e, repo: calls.append(1) and False)
        self.assertEqual((len(calls), out), (3, (0, 1)))
        self.assertEqual(failed, [("A", at(1, 41))])

    def test_a_weekly_entry_comes_due_on_its_day_only(self):
        """24 Nov 2026: the two nightly reference jobs had a schedule of GitHub's and it never fired. They and the three
        weekly jobs are on the clock; a weekly entry names its day."""
        from datetime import datetime, timezone
        e = [{"workflow": "w.yml", "name": "w", "at": [(2, 43)], "inputs": {}, "days": [0]}]
        mon, tue = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc), datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
        self.assertEqual([m.day for m, _ in C.due(e, mon, datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc))], [23, 30])
        self.assertEqual(C.due(e, tue, datetime(2026, 11, 29, 23, 0, tzinfo=timezone.utc)), [])
        weekly = {x["workflow"]: x["days"] for x in C.table() if x["days"]}
        self.assertEqual(weekly, {"keywords_by_year.yml": [0], "reference_candidates.yml": [0], "company_short_names.yml": [0], "standards_watch.yml": [0], "gleif_register.yml": [1]})
        self.assertIn("reference_use.yml", {x["workflow"] for x in C.table()}); self.assertIn("reference_examples.yml", {x["workflow"] for x in C.table()})

    def test_what_came_due_while_the_clock_was_away_is_started(self):
        started = []
        self.run_watch(lambda e, repo: started.append(e["name"]) or True, minutes=1, saved=at(19, 40, 14))
        self.assertEqual(started, ["A"])                                                 # the 19:41 of the evening before


class Table(unittest.TestCase):
    def test_the_table_reads_and_names_workflows_that_exist_and_take_as_scheduled(self):
        entries = C.table()
        self.assertTrue(entries)
        for e in entries:
            path = os.path.join(ROOT, ".github", "workflows", e["workflow"])
            self.assertTrue(os.path.exists(path), e["workflow"])
            src = open(path, encoding="utf-8").read()
            self.assertIn("as_scheduled:", src, e["workflow"])                           # it can be started as its scheduled run
            self.assertNotIn("schedule:", src, e["workflow"] + " still has a schedule of its own: it would run twice")

    def test_a_job_that_follows_a_job_of_the_clock_is_on_the_clock_too(self):
        """23 Nov 2026: GitHub starts no follow-up run after a run started with the repository's own token, which is how
        the clock starts jobs. Four jobs did not run for two nights. A workflow that follows (workflow_run) one the clock
        starts must be started by the clock as well (Scores is switched off on purpose)."""
        import glob, re
        flows = os.path.join(ROOT, ".github", "workflows")
        on_clock = {e["workflow"] for e in C.table()}
        names = {re.search(r"^name:\s*(.+)$", open(os.path.join(flows, w), encoding="utf-8").read(), re.M).group(1).strip().strip('"'): w for w in on_clock}
        for path in glob.glob(os.path.join(flows, "*.yml")):
            src = open(path, encoding="utf-8").read()
            m = re.search(r"workflow_run:\s*\n\s*workflows:\s*\[([^\]]*)\]", src)
            if not m or os.path.basename(path) == "load_scores.yml":
                continue
            parents = [p.strip().strip('"') for p in m.group(1).split(",")]
            if any(p in names for p in parents):
                self.assertIn(os.path.basename(path), on_clock, os.path.basename(path) + " follows a job the clock starts and is not on the clock")

    def test_the_command(self):
        self.assertEqual(C.command(E1, "o/r"), ["gh", "workflow", "run", "a.yml", "--repo", "o/r", "--ref", "main", "-f", "as_scheduled=true"])

    def test_a_bad_table_is_refused(self):
        import tempfile
        for bad in ([{"workflow": "../x.yml", "at": ["01:00"]}], [{"workflow": "x.yml", "at": ["25:00"]}], [{"workflow": "x.sh", "at": ["01:00"]}]):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(bad, fh)
            with self.assertRaises(ValueError):
                C.table(fh.name)
            os.remove(fh.name)


if __name__ == "__main__":
    unittest.main()
