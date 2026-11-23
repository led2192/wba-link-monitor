import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import base_health as B


class DayWindow(unittest.TestCase):
    def test_previous_calendar_day(self):
        # the 26 Oct 2026 row ran at 00:50 UTC: it counts 25 Oct, not 00:50 on 25 Oct to 00:50 on 26 Oct
        start, end = B.day_window(datetime(2026, 10, 26, 0, 50, tzinfo=timezone.utc))
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-10-25T00:00:00+00:00", "2026-10-26T00:00:00+00:00"))
        # a second run the same day gives the same window, so nothing is counted twice
        self.assertEqual(B.day_window(datetime(2026, 10, 26, 16, 41, tzinfo=timezone.utc)), (start, end))
        self.assertTrue(start <= B.parse_ts("2026-10-25T08:38:21.000Z") < end)


if __name__ == "__main__":
    unittest.main()


class Timed(unittest.TestCase):
    def test_a_part_is_run_once_and_its_time_printed(self):
        import io, contextlib
        import base_health as B
        buf = io.StringIO(); calls = []
        with contextlib.redirect_stdout(buf):
            out = B.timed("companies", lambda: calls.append(1) or {"avg": 0.5})
        self.assertEqual((out, len(calls)), ({"avg": 0.5}, 1))
        self.assertRegex(buf.getvalue(), r"^    companies: \d+ s\n$")
