import json, unittest

import library_mirror as M


class Year(unittest.TestCase):
    def y(self, **f):
        return M.year_of(f, this_year=2026)

    def test_the_seven_rules_in_airtables_order(self):
        base = dict(doc_year="2022", doc_year_from="rules", source_title={"state": "generated", "value": "Annual Report 2021"})
        self.assertEqual(self.y(**base, curated_doc_title="Sustainability Report 2019", fiscal_year=2023, year_score=95), (2019, "1 your title"))
        self.assertEqual(self.y(doc_year="2024-03-31", doc_year_from="filing", fiscal_year=2023, year_score=95), (2024, "2 regulator filing"))
        self.assertEqual(self.y(**base, fiscal_year=2023, year_score=80), (2023, "3 document text (high)"))
        self.assertEqual(self.y(doc_year="2020", doc_year_from="source library", fiscal_year=2023, year_score=70), (2020, "4 analyst (Source Library)"))
        self.assertEqual(self.y(**base, fiscal_year=2023, year_score=60), (2023, "5 document text (medium)"))
        self.assertEqual(self.y(**base, fiscal_year=2023, year_score=40, ai_fiscal_year={"state": "generated", "value": " 2018 (cover)"}, ai_year_verified=True),
                         (2018, "6 AI (quote verified)"))
        self.assertEqual(self.y(**base, ai_fiscal_year={"value": "2018"}), (2021, "7 rules (title / address)"))      # the AI year not verified: the rules
        self.assertEqual(self.y(), (None, "none"))

    def test_the_rules_on_the_title(self):
        t = lambda s: {"state": "generated", "value": s}
        self.assertEqual(M.year_rules(t("Annual Report 2023"), "2024", 2026), 2023)
        self.assertEqual(M.year_rules(t("4.5% Notes due 2031 | Prospectus 2024"), "2024", 2026), 2024)        # a bond's maturity is not the year
        self.assertEqual(M.year_rules(t("Strategy to 2029"), "2024", 2026), 2024)                              # later than the year after doc_year: doc_year
        self.assertEqual(M.year_rules(t("Report 2027"), "", 2026), 2027)                                       # no doc_year: up to next year
        self.assertEqual(M.year_rules(t("Report 2028"), "", 2026), None)
        self.assertEqual(M.year_rules(t("Code of conduct"), "2020", 2026), 2020)
        self.assertEqual(M.year_rules({"state": "error", "value": "x 2019"}, "", 2026), None)                  # an AI field in error reads as empty

    def test_the_type(self):
        self.assertEqual(M.type_of({"source_type_check": "Annual Report", "source_type": "CSR Report"}), "Annual Report")
        self.assertEqual(M.type_of({"source_type": "CSR Report"}), "CSR Report")
        self.assertEqual(M.type_of({}), "")


class Rows(unittest.TestCase):
    def test_a_row_and_the_comparison(self):
        rec = {"id": "rec1", "fields": {"library_id": " L1 ", "wba_id": "PT_1", "discard": True, "doc_year": "2022", "doc_year_from": "rules",
                                        "year": 2022, "effective_type": "ESG Report", "source_type": "ESG Report", "cover_text": "six pages",
                                        "file": [{"url": "https://expiring"}], "company": ["recC"]}}
        r = M.row_of(rec, this_year=2026)
        self.assertEqual((r[0], r[1], r[2], r[3], r[5], r[6], r[8], r[9]), ("L1", "rec1", "PT_1", True, 2022, 2022, "ESG Report", "ESG Report"))
        kept = json.loads(r[4])
        for left_out in ("cover_text", "file", "company", "year", "effective_type"):
            self.assertNotIn(left_out, kept)                                    # not copied: heavy, expiring, a link, or Airtable's own result
        self.assertIsNone(M.row_of({"id": "rec2", "fields": {"wba_id": "PT_1"}}))            # no id: not a document
        other = M.row_of({"id": "rec3", "fields": {"library_id": "L3", "doc_year": "2020", "year": 2021, "source_type": "GRI", "effective_type": "GRI"}}, 2026)
        c, differ = M.compare([r, other])
        self.assertEqual((c["documents"], c["year: the same"], c["year: differs"], c["type: the same"]), (2, 1, 1, 2))
        self.assertEqual(c["year differs, our rule 7 rules (title / address)"], 1)
        self.assertIn("L3 ours 2020 Airtable 2021", differ["year"][0])

    def test_the_fields_copied_are_the_approved_ones(self):
        for dropped in ("classify_input", "scope_input", "is_urd", "year_rules", "ai_year_test", "ai_year_input", "ai_year", "delete_now", "file", "cover_text"):
            self.assertNotIn(dropped, M.FIELDS)
        self.assertEqual(len(M.FIELDS), len(set(M.FIELDS)))


import os
URL = os.environ.get("PG_SEARCH_TEST_URL", "").strip()


@unittest.skipUnless(URL, "PG_SEARCH_TEST_URL not set")
class OnPostgres(unittest.TestCase):
    def test_written_once_changed_rows_only_and_a_missing_document_is_marked_not_deleted(self):
        import psycopg
        mk = lambda lid, year: M.row_of({"id": "rec" + lid, "fields": {"library_id": lid, "wba_id": "PT_1", "doc_year": str(year), "year": year, "source_type": "GRI", "effective_type": "GRI"}}, 2026)
        with psycopg.connect(URL) as conn:
            conn.execute("drop table if exists public.library")
            self.assertEqual(M.write(conn, [mk("A", 2020), mk("B", 2021)], complete=True), (2, 0))
            self.assertEqual(M.write(conn, [mk("A", 2020), mk("B", 2021)], complete=True), (0, 0))             # nothing changed: nothing written
            self.assertEqual(M.write(conn, [mk("A", 2022)], complete=False), (1, 0))                           # a partial sweep marks nothing gone
            self.assertEqual(M.write(conn, [mk("A", 2022)], complete=True), (0, 1))                            # B is gone from Airtable: marked
            got = conn.execute("select library_id, year_ours, gone is not null from public.library order by 1").fetchall()
            self.assertEqual(got, [("A", 2022, False), ("B", 2021, True)])                                     # and kept
            self.assertEqual(M.write(conn, [mk("A", 2022), mk("B", 2021)], complete=True), (1, 0))             # back again: no longer gone
            conn.execute("drop table public.library"); conn.commit()


if __name__ == "__main__":
    unittest.main()
