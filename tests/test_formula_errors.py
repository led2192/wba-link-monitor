import unittest
from unittest import mock

import monitor_core as M


class FakeResp:
    status_code = 200
    text = ""

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class FormulaErrors(unittest.TestCase):
    def test_errors_become_none_ai_fields_stay(self):
        data = {"records": [{"id": "r1", "fields": {"doc_label": {"error": "#ERROR!"}, "year": {"specialValue": "NaN"},
                                                    "source_title": {"state": "generated", "value": "X"}, "name": "ok"}}]}
        with mock.patch.object(M.requests, "request", return_value=FakeResp(data)):
            j = M.airtable_request("GET", "https://api.airtable.com/v0/app/t", {}).json()
        f = j["records"][0]["fields"]
        self.assertIsNone(f["doc_label"])
        self.assertIsNone(f["year"])
        self.assertEqual(f["source_title"]["value"], "X")
        self.assertEqual(f["name"], "ok")

    def test_latest_doc_text_survives_an_error_label(self):
        import os
        os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
        import company_coverage as CC
        CC.latest_doc_text({"doc_label": {"error": "#ERROR!"}}, "Annual Report")


if __name__ == "__main__":
    unittest.main()
