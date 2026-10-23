import unittest

import field_docs


DL = {"tables": [{"name": "report_library", "description": "Documents.", "fields": [
    {"name": "source_type_check", "type": "singleSelect", "description": "The type | AI.",
     "config": {"choices": [{"name": "Annual Report"}, {"name": "Code of Conduct"}]}},
    {"name": "year", "type": "formula", "description": "THE year."},
    {"name": "page_count", "type": "number"},
    {"name": "source_title", "type": "aiText"},
]}]}
QA = {"tables": [{"name": "labels", "fields": [
    {"name": "true_year", "type": "number", "description": "Yours."},
    {"name": "reviewed", "type": "checkbox"},
    {"name": "stratum", "type": "singleSelect", "options": {"choices": [{"name": "non_english"}]}},
]}]}


class FieldDocs(unittest.TestCase):
    def setUp(self):
        self.text = field_docs.render(DL, QA, "2026-10-23")

    def test_who_writes(self):
        self.assertIn("| `source_type_check` | pick one | you |", self.text)
        self.assertIn("| `year` | formula | formula |", self.text)
        self.assertIn("| `source_title` | Airtable AI | Airtable AI |", self.text)
        self.assertIn("| `true_year` | number | you |", self.text)
        self.assertIn("| `reviewed` | checkbox | robot |", self.text)

    def test_choices_from_both_shapes_and_pipes_escaped(self):
        self.assertIn("Values: Annual Report, Code of Conduct.", self.text)
        self.assertIn("Values: non_english.", self.text)
        self.assertIn("The type \\| AI.", self.text)

    def test_extra_note_for_undescribed_field(self):
        self.assertIn("Pages in the PDF", self.text)

    def test_both_bases_and_date(self):
        self.assertIn("## Disclosure Library", self.text)
        self.assertIn("## Pipeline QA", self.text)
        self.assertIn("2026-10-23", self.text)


if __name__ == "__main__":
    unittest.main()
