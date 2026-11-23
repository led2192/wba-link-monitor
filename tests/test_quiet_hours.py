"""Heavy jobs do not start in the hours people search (quiet_hours.py, 23 Nov 2026)."""
import datetime as dt, os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quiet_hours as Q


class QuietHours(unittest.TestCase):
    def at(self, hour):
        return dt.datetime(2026, 11, 23, hour, 30, tzinfo=dt.timezone.utc)

    def test_a_writing_run_waits_for_the_night(self):
        os.environ.pop("HEAVY_BY_DAY", None)
        self.assertIn("Not started", Q.held(True, now=self.at(14)))            # the afternoon a guest's searches timed out
        self.assertIn("14:30 UTC", Q.held(True, now=self.at(14)))
        for hour in (21, 23, 0, 4):
            self.assertIsNone(Q.held(True, now=self.at(hour)))
        self.assertIsNotNone(Q.held(True, now=self.at(5))); self.assertIsNotNone(Q.held(True, now=self.at(20)))

    def test_a_dry_run_or_a_person_who_says_so_is_not_held(self):
        os.environ.pop("HEAVY_BY_DAY", None)
        self.assertIsNone(Q.held(False, now=self.at(14))); self.assertIsNone(Q.held(True, allowed=True, now=self.at(14)))
        os.environ["HEAVY_BY_DAY"] = "yes"
        try:
            self.assertIsNone(Q.held(True, now=self.at(14)))
        finally:
            os.environ.pop("HEAVY_BY_DAY", None)

    def test_the_heavy_jobs_ask_the_clock(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        for name in ("reference_use.py", "reference_examples.py", "reference_candidates.py", "company_short_names.py", "company_profile.py"):
            self.assertIn("quiet_hours.held(", open(os.path.join(root, name), encoding="utf-8").read(), name)
        for name in ("reference_use.yml", "reference_examples.yml", "reference_candidates.yml", "company_short_names.yml"):
            self.assertIn("group: heavy-database", open(os.path.join(root, ".github", "workflows", name), encoding="utf-8").read(), name)


if __name__ == "__main__":
    unittest.main()
