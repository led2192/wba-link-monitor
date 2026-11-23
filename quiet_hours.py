"""Jobs that search or rewrite a large part of the database do not start in the hours people search.

23 Nov 2026: three such jobs started by hand in the working day (a count over every page, the examples of the
reference list, the paid company profiles) made searches time out for the owner twice and then for a guest. The
database is one machine and has no notion of who is more important. So a heavy job that would write (--commit)
checks the clock first: between DAY_FROM and DAY_TO (UTC) it does not start, says so, and ends well (exit 0, so no
alarm); at night it runs. A person who wants it by day says so (--by-day, or HEAVY_BY_DAY=yes).

A dry run is not held back: it is short by design (a limit of a few entries).
"""
import datetime as dt
import os

DAY_FROM, DAY_TO = 5, 21          # UTC: 06:00 to 22:00 in Madrid in winter, 07:00 to 23:00 in summer


def by_day(now=None):
    """True in the hours people search."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return DAY_FROM <= now.hour < DAY_TO


def held(commit, allowed=False, now=None):
    """The line to print when a writing run must wait for the night, or None when it may start."""
    if not commit or allowed or os.environ.get("HEAVY_BY_DAY", "").lower() in ("yes", "true", "1") or not by_day(now):
        return None
    return (f"Not started: this job loads the database and it is {(now or dt.datetime.now(dt.timezone.utc)).strftime('%H:%M')} UTC, inside the hours people search "
            f"({DAY_FROM:02d}:00 to {DAY_TO:02d}:00 UTC). It runs at night; to run it now, start it with by_day (--by-day).")
