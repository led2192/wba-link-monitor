"""
reporting_year.py: WBA's reporting year of a fiscal year, in one place.

The SDG2000 data collection guidance (FY2024 update, August 2025) defines it by where at least six months of
the activity fall: fiscal year 2024 is a year ending from 30 June 2024 to 29 June 2025. So a year ending
30 June counts as the year it ends in (Telstra, Diageo, Coty, FirstRand: their validated answers did), and a
year ending January to May, or before 30 June, counts as the year before (31 March 2025 is 2024).

Until 4 Nov 2026 the pipeline counted every June year end as the year before, one year off for every
company with a 30 June year end.
"""
from datetime import date

LAST_SHIFTED_MONTH = 5          # with only the month known: January to May end in the following reporting year


def from_end_date(end):
    """Reporting year of a fiscal year ending on `end` (a date or 'YYYY-MM-DD')."""
    d = date.fromisoformat(end) if isinstance(end, str) else end
    return d.year - 1 if (d.month, d.day) < (6, 30) else d.year


def from_end_month(year, month):
    """Reporting year of a fiscal year ending in `month` of `year` when the day is unknown. June counts as
    its own year: nearly every June year end is the 30th."""
    return year - 1 if month and 1 <= int(month) <= LAST_SHIFTED_MONTH else year


def shifted(month):
    """True when a fiscal year ending in this month belongs to the reporting year before."""
    return bool(month) and 1 <= int(month) <= LAST_SHIFTED_MONTH
