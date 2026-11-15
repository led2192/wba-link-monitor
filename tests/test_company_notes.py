import os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()
PAGE = open(os.path.join(ROOT, "site", "search.html"), encoding="utf-8").read()


class CompanyNotes(unittest.TestCase):
    def test_saving_is_signed_and_closed(self):
        src = SQL[SQL.index("create or replace function public.company_note_set("):SQL.index("-- A company with a gap among its main reports")]
        self.assertIn("public.email_may_search(who)", src)
        self.assertIn("if not t <@ public.company_traits() then", src)                      # only the listed traits
        self.assertIn("> 400", src)                                                         # a short note
        self.assertIn("delete from public.company_notes n where n.wba_id = wba_id;", src)   # nothing ticked, nothing written: the row goes
        self.assertIn("to authenticated;", src); self.assertIn("from public, anon;", src)
        self.assertIn("alter table public.company_notes enable row level security;", SQL)

    def test_the_gap_card_reads_the_traits_after_a_cells_own_note(self):
        card = SQL[SQL.index("create or replace function public.gap_card("):SQL.index("revoke all on function public.gap_card(")]
        self.assertEqual(card.count("public.company_implied(drawn.wba_id, "), 2)
        self.assertRegex(card, r"coalesce\(\(select g\.reason from public\.gap_notes g[^\n]*\n\s*public\.company_implied\(drawn\.wba_id, 'annual'\)\)")
        self.assertIn("'profile', (select jsonb_build_object('traits', to_jsonb(n.traits), 'note', n.note)", card)

    def test_the_page_offers_exactly_the_traits_the_database_takes(self):
        listed = set(re.findall(r"'([a-z_]+)'", SQL[SQL.index("create or replace function public.company_traits()"):SQL.index("-- What the company's traits say")]))
        block = PAGE[PAGE.index("const COMPANY_TRAITS = {"):PAGE.index("async function gapCard()")]
        offered = set(re.findall(r"\b([a-z_]+): \"", block))
        self.assertEqual(offered, listed)
        self.assertIn('authed("company_note_set", { key, wba_id: g.wba_id, traits: Object.keys(boxes).filter(k => boxes[k].checked), note: ta.value })', PAGE)
        self.assertIn("ta.maxLength = 400", PAGE)


if __name__ == "__main__":
    unittest.main()
