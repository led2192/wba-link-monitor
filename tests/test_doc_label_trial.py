"""doc_label_trial.py: what the model is told, how its reply is read, and the numbers printed. No model, no network."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import doc_label_trial as T


class Told(unittest.TestCase):
    def test_the_question_lists_the_types_asks_the_language_and_cuts_the_cover(self):
        text = T.told("x" * 5000)
        self.assertIn("- Annual Report:", text); self.assertIn('"language"', text); self.assertIn('"title"', text)
        self.assertIn("x" * T.CHARS, text); self.assertNotIn("x" * (T.CHARS + 1), text)
        self.assertNotIn('"confidence"', text)

    def test_the_reply_may_only_name_a_listed_type(self):
        self.assertEqual(T.schema()["properties"]["type"]["enum"][-1], "UNKNOWN")
        self.assertEqual(T.parse_reply('{"type": "Annual Report", "language": "Japanese", "title": " Annual   Report 2024 "}'), ("Annual Report", "Japanese", "Annual Report 2024"))
        self.assertEqual(T.parse_reply('{"type": "Yearbook", "language": "x", "title": ""}'), (None, None, None))
        self.assertEqual(T.parse_reply("I think it is an annual report"), (None, None, None))

    def test_the_numbers(self):
        rows = [("Annual Report", "English", "Annual Report", "english", "AR 2024", 10.0), ("ESG Report", "English", "Sustainability Report", "English", "", 20.0),
                ("", "", "UNKNOWN", "German", "", 30.0), ("", "", None, None, None, 40.0)]
        s = T.summarise(rows)
        self.assertEqual((s["documents"], s["answered"], s["with_reference"], s["same_type"], s["typed"], s["titled"], s["lang_reference"], s["same_language"]), (4, 3, 2, 1, 2, 1, 2, 2))
        self.assertEqual((s["seconds"], s["slowest"]), (25.0, 40.0))
        self.assertIn("1 of 2 (50%)", T.line("m", "english", s))

    def test_a_reply_with_words_around_the_json_is_still_read(self):
        self.assertEqual(T.parse_reply('Here it is:\n```json\n{"type": "CSR Report", "language": "English", "title": "CSR 2016"}\n```'), ("CSR Report", "English", "CSR 2016"))

    def test_the_three_groups_are_asked_for_apart_and_always_the_same_documents(self):
        self.assertEqual(set(T.WHERE), set(T.GROUPS)); self.assertIn("order by md5(d.library_id)", T.SAMPLE_SQL)


if __name__ == "__main__":
    unittest.main()
