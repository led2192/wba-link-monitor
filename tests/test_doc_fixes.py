"""doc_fixes.py: what a correction from the admin list writes to the library. No network."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import doc_fixes as F


class Fields(unittest.TestCase):
    def test_a_discard_is_a_tick_and_a_reason_never_a_deletion(self):
        self.assertEqual(F.fields_for("discard", reason="Dead link"), ({"discard": True, "discard_reason": "admin: dead link"}, None))
        self.assertEqual(F.fields_for("discard", reason="because I say so")[0]["discard_reason"], "admin: other")
        self.assertEqual(F.fields_for("discard")[0]["discard_reason"], "admin: other")

    def test_a_relabel_writes_the_type_and_the_year_given_and_only_those(self):
        self.assertEqual(F.fields_for("relabel", "CSR Report", 2016), ({"source_type_check": "CSR Report", "doc_year": "2016"}, None))
        self.assertEqual(F.fields_for("relabel", None, 2016), ({"doc_year": "2016"}, None))
        self.assertEqual(F.fields_for("relabel", "CSR Report"), ({"source_type_check": "CSR Report"}, None))

    def test_what_is_not_a_type_a_year_or_an_action_is_refused(self):
        self.assertEqual(F.fields_for("relabel", "Yearbook", 2016), (None, "not one of the library's types"))
        self.assertEqual(F.fields_for("relabel", "CSR Report", 1816), (None, "not a year"))
        self.assertEqual(F.fields_for("relabel"), (None, "nothing to change"))
        self.assertEqual(F.fields_for("delete"), (None, "not an action"))

    def test_no_link_field_and_no_deletion_in_the_script(self):
        src = open(F.__file__, encoding="utf-8").read()
        self.assertNotIn('"DELETE"', src); self.assertIn("applied_at is null", F.WAITING_SQL)


if __name__ == "__main__":
    unittest.main()
