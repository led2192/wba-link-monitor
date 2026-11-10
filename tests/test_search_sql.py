"""The shape of find_pages (supabase/search.sql) that the timing of 10 Nov 2026 taught, kept from coming back,
and search_timing.py's helper. Offline: the SQL is read as text."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SQL = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supabase", "search.sql"), encoding="utf-8").read()
FIND = SQL[SQL.index("create or replace function public.find_pages("):SQL.index("-- A click on a result")]


def test_no_statement_per_year():
    # one statement per year cost 0.8 s each over 31 years
    assert "select distinct coalesce(d.year, 0)" not in FIND
    assert "p.year = y\n" not in FIND


def test_nothing_is_cut_by_the_clock():
    # the same search must give the same pages whatever the load
    assert not re.search(r"clock_timestamp\(\) - t0 > interval", FIND)


def test_a_span_of_years_is_not_asked_of_the_word_index():
    assert "p.year >= year_from" not in FIND and "p.year <= year_to" not in FIND
    assert "p.year + 0 >= year_from" in FIND and "p.year = yeq" in FIND


def test_company_names_come_from_the_companies_table():
    assert "public.find_company_ids(company_filter)" in FIND
    assert "public.companies_matching(company_filter)" not in FIND


def test_a_phrase_is_checked_on_a_bounded_sample():
    assert "p.words @@ loose_q" in FIND and "limit 3000) x" in FIND


def test_title_and_copies():
    assert "public.type_words(c.doc_type)" in FIND and "in_title" in FIND
    assert "partition by w.co, w.year, w.lexemes, w.rel" in FIND


def test_timed_returns_the_first_row_and_seconds():
    import search_timing

    class Cur:
        description = [("n",)]
        def execute(self, sql, args=None): self.sql = sql
        def fetchone(self): return (7,)
    row, s = search_timing.timed(Cur(), "select 7")
    assert row == (7,) and s >= 0
