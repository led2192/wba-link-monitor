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


def test_added_values_are_signed_and_one_per_person():
    add = SQL[SQL.index("create or replace function public.add_value("):SQL.index("-- The signed-in person's own reviews")]
    assert "public.email_may_search(who)" in add and "42501" in add
    assert "on conflict on constraint value_submissions_one_per_person do update" in add
    assert "to authenticated;" in add and "from public, anon;" in add
    assert "status      text not null default 'new'" in SQL


def test_the_documents_list_shows_a_document_held_twice_once():
    assert "select distinct on (d0.wba_id, d0.year, lower(coalesce(nullif(btrim(d0.label), ''), d0.library_id)))" in FIND


def test_the_review_game_is_signed_and_names_no_one():
    lucky = SQL[SQL.index("create or replace function public.review_score("):SQL.index("-- The signed-in person's own reviews")]
    assert "public.email_may_search(who)" in lucky and "grant execute on function public.lucky_value() to authenticated;" in lucky
    assert "revoke all on function public.review_score(text) from public, anon, authenticated;" in lucky
    assert "r.seconds < 4 then 0.25" in lucky
    score = lucky[:lucky.index("create or replace function public.lucky_value")]
    assert "'reviewer'" not in score and "email" not in score.split("as $$")[1]


def test_the_company_card_has_the_website():
    assert "'website', c.website" in SQL


def test_added_values_are_shown_without_names():
    sub = SQL[SQL.index("create or replace function public.find_submissions("):SQL.index("-- \"I'm feeling lucky\"")]
    assert "public.search_key_name(key) is null" in sub and "'mine'" in sub
    assert "'reviewer'" not in sub and "to anon, authenticated;" in sub


def test_my_reviews_carries_the_score_for_the_level():
    mine = SQL[SQL.index("create or replace function public.my_reviews("):]
    assert "'score', public.review_score(who)" in mine[:1500]


def test_scores_for_one_company_with_places_counted():
    sc = SQL[SQL.index("create or replace function public.find_scores("):SQL.index("-- The values people have added")]
    assert "public.search_key_name(key) is null" in sc and "array_length(ids, 1), 0) <> 1" in sc
    assert "'industry_rank'" in sc and "'areas'" in sc and "to_regclass('public.company_scores') is null" in sc
