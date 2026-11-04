import os
import unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import load_guidance as L

REC = {"dimension": "Social", "area": "Planning", "indicator": "Commitment on human rights", "attribute": "Commitment language",
       "type_normalised": "boolean", "type": "Boolean", "options": "Yes/No", "null codes": "Skipped (SK)",
       "description": "The policy uses sufficient commitment language.", "evaluation guidance": "respect, uphold"}


class Load(unittest.TestCase):
    def test_fields(self):
        f = L.fields_of(REC, "ITA Social draft", "draft")
        self.assertEqual(f["guidance_id"], "Social | Commitment on human rights | Commitment language")
        self.assertEqual((f["type"], f["area"], f["status"]), ("boolean", "Planning", "draft"))
        self.assertNotIn("unit", f)                                   # empty fields are not sent
        self.assertEqual(L.fields_of(dict(REC, type_normalised="Multi"), "x", "draft")["type"], "?")

    def test_reload_updates_adds_and_leaves_edited_rows(self):
        other = dict(REC, attribute="Scope of commitment")
        existing = {L.guidance_id(REC): {"id": "rec1", "edited": True},
                    "Social | Old indicator | Old attribute": {"id": "rec2", "edited": False}}
        creates, updates, kept, gone = L.plan([REC, other, other], existing, "src", "draft")
        self.assertEqual(([c["attribute"] for c in creates], updates, kept), (["Scope of commitment"], [], 1))
        self.assertEqual(gone, ["Social | Old indicator | Old attribute"])   # reported, never deleted


if __name__ == "__main__":
    unittest.main()
