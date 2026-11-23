"""label_docs.py: what counts as readable, what may be written on a row, and the queue. No model, no network."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import label_docs as L


class Guards(unittest.TestCase):
    def test_noise_is_not_read_and_gets_no_title(self):
        self.assertTrue(L.readable("Annual Report 2024 of the company, with enough words to be a page of text. " * 2))
        self.assertTrue(L.readable("平成26年3月期 第2四半期決算短信〔日本基準〕(連結) 当社グループの業績について説明いたします。" * 3))
        self.assertFalse(L.readable("12 34 56 78 90 " * 30))                                  # figures alone
        self.assertFalse(L.readable("\u0888\u08bf\u04e1\u0f3b\u0495\u04bb\u0571 \u0377\u03a7\u03e5\u03dc " * 20))   # letters of unrelated scripts: a wrong decoding
        self.assertFalse(L.readable("short"))
        self.assertEqual(L.clean_title("  Annual   Report 2024 "), "Annual Report 2024")
        self.assertEqual(L.clean_title("\u0888\u08bf\u04e1\u0f3b\u0495\u04bb\u0571"), "")
        self.assertEqual(L.clean_title("12 / 2024"), ""); self.assertEqual(L.clean_title("AR"), "")
        self.assertEqual(L.clean_title("\u80a1\u7968\u639b\u5931\u7533\u8acb\u66f8"), "\u80a1\u7968\u639b\u5931\u7533\u8acb\u66f8")

    def test_only_a_language_the_field_knows_is_written(self):
        self.assertEqual(L.language_name("chinese"), "Chinese (Simplified)"); self.assertEqual(L.language_name(" japanese "), "Japanese")
        self.assertEqual(L.language_name("Klingon"), ""); self.assertEqual(L.language_name(None), "")


class Row(unittest.TestCase):
    def test_an_empty_row_gets_all_three(self):
        self.assertEqual(L.row_updates({}, "Annual Report", "English", "Annual Report 2024"),
                         {"source_type_check": "Annual Report", "doc_language": "English", "auto_title": "Annual Report 2024"})

    def test_nothing_a_person_or_the_ai_field_wrote_is_touched(self):
        held = {"source_type_check": {"name": "ESG Report"}, "doc_language": {"name": "German"}, "auto_title": "Geschaeftsbericht 2024"}
        self.assertEqual(L.row_updates(held, "Annual Report", "English", "Another title"), {})
        self.assertEqual(L.row_updates({"source_type": "Annual Report"}, "ESG Report", "", ""), {})                       # typed by the address rules
        self.assertEqual(L.row_updates({"source_title": {"state": "generated", "value": "AI title"}}, "", "", "Ours"), {})     # the AI field gave a title
        self.assertEqual(L.row_updates({"curated_doc_title": "A person's title"}, "", "", "Ours"), {})

    def test_a_poor_robot_title_is_replaced(self):
        for poor in ("", "Untitled", "Document 2026", "x" * 91):
            self.assertEqual(L.row_updates({"auto_title": poor}, None, None, "Sustainability Report 2026"), {"auto_title": "Sustainability Report 2026"}, poor)
        self.assertEqual(L.row_updates({"auto_title": "Kia Sustainability Report 2025"}, None, None, "Other"), {})

    def test_the_queue_is_documents_with_text_and_no_type_not_looked_at_newest_first(self):
        q = L.QUEUE_SQL
        for part in ("d.text_status = 'ok'", "coalesce(d.doc_type, '') = ''", "not exists (select 1 from public.doc_labels", "order by d.found desc nulls last"):
            self.assertIn(part, q)

    def test_the_script_never_deletes_and_writes_no_link_field(self):
        src = open(L.__file__, encoding="utf-8").read()
        self.assertNotIn('"DELETE"', src); self.assertIn('"typecast": True})     # no link field among them', src)


if __name__ == "__main__":
    unittest.main()
