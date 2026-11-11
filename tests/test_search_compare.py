"""search_compare.py: the small pieces that read a search's rows (no database)."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import search_compare as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()
PAGE = open(os.path.join(ROOT, "site", "search.html"), encoding="utf-8").read()
FIND = SQL[SQL.index("create or replace function public.find_pages("):SQL.index("-- A click on a result")]


def test_together_reads_the_marks():
    assert C.together("we pay a ⟦living⟧ ⟦wage⟧ to all")              # side by side
    assert C.together("carried out ⟦human rights due diligence⟧ yearly")   # one mark of several words
    assert not C.together("a ⟦wage⟧ here and, far away, ⟦living⟧ costs")
    assert not C.together("") and not C.together(None)


def test_rank_and_click_stats():
    rows = [("a", 1, ""), ("b", 2, ""), ("c", 3, "")]
    assert C.rank_of(rows, "b", 2) == 2 and C.rank_of(rows, "z", 9) == 0
    found, mrr = C.click_stats([1, 2, 0, 20])
    assert found == 2 and abs(mrr - (1 + 0.5 + 0 + 0.05) / 4) < 1e-9
    assert C.click_stats([]) == (0, 0.0)


def test_pairs_are_an_option_off_by_default():
    assert "values ('pairs', false," in SQL                                             # shipped off: measured before it is the default
    assert "current_setting('lantern.pairs', true)" in FIND and "o.name = 'pairs' and o.is_on" in FIND
    assert "use_pairs := ids is null and to_regclass('public.page_pairs') is not null" in FIND   # a company filter keeps the word index
    assert "quote_literal(toks[j] || '_' || toks[j + 1])::tsquery" in FIND             # a cast: to_tsquery would cut the lexeme at '_'
    assert "(phrase_q is null or p.words @@ phrase_q)" in FIND                         # a phrase is still checked on the page
    assert "exit levels when via_pairs >= need;" in FIND


def test_a_phrase_is_marked_as_one_piece_and_typos_are_offered():
    assert "public.phrase_excerpt(left(p.body, 200000), phrases[1])" in FIND
    dym = SQL[SQL.index("create or replace function public.did_you_mean("):SQL.index("-- \"Ask\" (10 Nov 2026")]
    assert "public.search_key_name(key) is null" in dym and "levenshtein_less_equal" in dym and "limit 20) x) >= 20" in dym
    assert 'rpc("did_you_mean"' in PAGE and "Did you mean " in PAGE


def test_only_the_scheduled_load_starts_a_paid_pass():
    wf = open(os.path.join(ROOT, ".github", "workflows", "propose_values.yml"), encoding="utf-8").read()
    assert '[ "${{ github.event.workflow_run.event }}" != "schedule" ]' in wf
