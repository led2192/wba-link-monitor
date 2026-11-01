"""Offline tests for load_company_master.py: the plan from the CSV against the companies rows."""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_company_master as L  # noqa: E402

CSV = ("﻿WBA ID,WBA Company Name,Website,Corporate Name,Ownership,ISIN,Location HQ ISO Code,Location HQ Region,Location Country,ISIC Rev. 4,WBA Industry\n"
       "PT_00001,3M,https://www.3m.com,3M Company,Public,US88579Y1010,USA,North America,United States,2011,Chemicals\n"
       "PT_00010,ADIA,https://www.adia.ae,Abu Dhabi Investment Authority,Government,,ARE,Middle East & North Africa,United Arab Emirates,8411,Sovereign Wealth Funds\n"
       "PT_09999,New Co,https://new.example,New Co,Private,,FRA,Europe & Central Asia,France,4771,Apparel & Footwear\n")


class Plan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        self.tmp.write(CSV); self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_reads_the_file(self):
        m = L.read_master(self.tmp.name)
        self.assertEqual(set(m), {"PT_00001", "PT_00010", "PT_09999"})
        self.assertEqual(m["PT_00001"]["wba_industry"], "Chemicals")
        self.assertEqual(m["PT_00001"]["isic_code"], "2011")
        self.assertNotIn("isin", m["PT_00010"])                                  # blanks dropped

    def test_plan_writes_the_file_fields_and_fills_the_rest(self):
        m = L.read_master(self.tmp.name)
        rows = [{"id": "r1", "fields": {"wba_id": "PT_00001", "hq_country": "USA", "ownership": "Public", "isin": "US88579Y1010",
                                        "wba_industry": "Chemicals", "isic_code": "2011", "hq_region": "North America"}},
                {"id": "r2", "fields": {"wba_id": "PT_00010", "hq_country": "", "ownership": "State-owned", "wba_industry": "Banks"}},
                {"id": "r3", "fields": {"wba_id": "PT_00500"}}]
        updates, rep = L.plan(m, rows)
        self.assertEqual([u["id"] for u in updates], ["r2"])                       # r1 unchanged, r3 not in the file
        self.assertEqual(updates[0]["fields"], {"wba_industry": "Sovereign Wealth Funds", "isic_code": "8411",
                                                "hq_region": "Middle East & North Africa", "hq_country": "ARE"})
        self.assertEqual(rep["differs"], [("PT_00010", "ownership", "State-owned", "Government")])   # left alone
        self.assertEqual(rep["missing_in_file"], ["PT_00500"])
        self.assertEqual(rep["missing_in_base"], ["PT_09999"])
        c = rep["counters"]
        self.assertEqual((c["companies matched"], c["rows to update"], c["ownership differs, left alone"]), (2, 1, 1))

    def test_rerun_changes_nothing(self):
        m = L.read_master(self.tmp.name)
        rows = [{"id": "r1", "fields": {"wba_id": "PT_00001"}}]
        updates, _ = L.plan(m, rows)
        rows[0]["fields"].update(updates[0]["fields"])
        self.assertEqual(L.plan(m, rows)[0], [])


if __name__ == "__main__":
    unittest.main()
