import os
import unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")

import harvest_reports as H
import monitor_core as MC


class LinkTexts(unittest.TestCase):
    def test_field_round_trip(self):
        current = {"acme.com/ar24.pdf": ("https://www.acme.com/ar24.pdf", "Annual Report\t2024\n(PDF, 12 MB)"),
                   "acme.com/x.pdf": ("https://www.acme.com/x.pdf", "")}
        value = MC.link_texts_field(current)
        self.assertEqual(value, "https://www.acme.com/ar24.pdf\tAnnual Report 2024 (PDF, 12 MB)")
        self.assertEqual(MC.parse_link_texts(value), {MC.normalize("https://www.acme.com/ar24.pdf"): "Annual Report 2024 (PDF, 12 MB)"})

    def test_field_is_cut_at_a_line_end(self):
        current = {f"acme.com/{i}.pdf": (f"https://acme.com/{i}.pdf", "Report " * 5) for i in range(100)}
        value = MC.link_texts_field(current, limit=500)
        self.assertLessEqual(len(value), 500)
        self.assertTrue(all("\t" in line for line in value.split("\n")))

    def test_monitor_writes_link_texts(self):
        upd = {}
        current = {"acme.com/ar24.pdf": ("https://acme.com/ar24.pdf", "Annual Report 2024")}
        MC.detection_fields({}, upd, current, False, __import__("datetime").date(2026, 11, 4))
        self.assertEqual(upd[MC.F_LINK_TEXTS], "https://acme.com/ar24.pdf\tAnnual Report 2024")

    def test_harvest_copies_the_text_to_the_library_row(self):
        page = {H.F_WBA: "PT_1", H.F_NAME: "Acme", H.F_TYPE: "reports_hub", H.F_URL: "https://acme.com/reports",
                H.F_SEEN: "https://acme.com/ar24.pdf\nhttps://acme.com/sr23.pdf",
                H.F_LINK_TEXTS: "https://acme.com/ar24.pdf\tAnnual Report 2024"}
        rows, _, _ = H.build([page], {"reports_hub"}, False)
        by_url = {r["document_url"]: r for r in rows}
        self.assertEqual(by_url["https://acme.com/ar24.pdf"]["link_text"], "Annual Report 2024")
        self.assertNotIn("link_text", by_url["https://acme.com/sr23.pdf"])

    def test_existing_rows_get_only_the_link_text(self):
        row = {"library_id": "PT_1-a", "wba_id": "PT_1", "document_url": "https://acme.com/ar24.pdf",
               "found_on": "https://acme.com/reports", "link_text": "Annual Report 2024"}
        existing = {"PT_1-a": {"url": "https://acme.com/ar24.pdf", "first_seen": "2026-06-01", "id": "recA",
                               "wba": "PT_1", "link_text": ""}}
        writes = []
        with mock.patch.object(H, "write_records", side_effect=lambda b, t, tbl, recs, m: writes.append((m, recs)) or []), \
             mock.patch("purge_discarded.load_tombstones", return_value=set()):
            H.upsert([row], {}, "appX", "tok", "report_library", existing=existing)
        patches = [recs for m, recs in writes if m == "PATCH" and recs]
        self.assertEqual(patches, [[{"id": "recA", "fields": {"link_text": "Annual Report 2024"}}]])


if __name__ == "__main__":
    unittest.main()
