import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AIRTABLE_TOKEN", "t"); os.environ.setdefault("AIRTABLE_BASE", "appX")
import inbox as I


class NewCompany(unittest.TestCase):
    """11 Nov 2026: a row added by hand with a link and no id had its box emptied with "nothing could be added"."""
    def setUp(self):
        self.real = (I.get, I.write)
        self.written = []
        held = [{"id": "recU", "fields": {"wba_id": "PT_00001", "company_name": "Unilever PLC", "website": "https://www.unilever.com"}}]
        I.get = lambda table, params: held
        I.write = lambda table, method, records, typecast=True: self.written.append(records[0])

    def tearDown(self):
        I.get, I.write = self.real

    def test_a_new_company_gets_its_id_and_is_handled_now(self):
        rec = {"id": "recN", "fields": {"company_name": "Grupo Mutua", "website": "https://www.grupomutua.es/", "add_link": "https://www.grupomutua.es/informes"}}
        out = I.name_new_companies([rec])
        self.assertEqual(len(out), 1)
        self.assertRegex(out[0]["fields"]["wba_id"], r"^LN_[0-9a-f]{8}$")           # so handle_company no longer refuses it
        self.assertEqual(self.written[0]["fields"]["wba_id"], out[0]["fields"]["wba_id"])
        self.assertNotIn("add_link", self.written[0]["fields"])                      # the box is not touched here

    def test_a_company_we_hold_keeps_its_box_and_is_told_once(self):
        rec = {"id": "recD", "fields": {"company_name": "Unilever", "add_link": "https://x.example/report.pdf"}}
        self.assertEqual(I.name_new_companies([rec]), [])
        self.assertTrue(self.written[0]["fields"]["intake_note"].startswith("not added: already a company: PT_00001"))
        self.written.clear()
        rec["fields"]["intake_note"] = "not added: already a company"
        self.assertEqual(I.name_new_companies([rec]), []); self.assertEqual(self.written, [])   # not written again every few minutes

    def test_rows_with_an_id_pass_untouched(self):
        rec = {"id": "recU", "fields": {"wba_id": "PT_00001", "add_link": "x"}}
        self.assertEqual(I.name_new_companies([rec]), [rec]); self.assertEqual(self.written, [])


if __name__ == "__main__":
    unittest.main()
