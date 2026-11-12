"""lantern_ids.py: the plan, offline."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lantern_ids as L
from add_companies import company_id


def rec(i, wba, name, lei="", have=""):
    return {"id": f"rec{i}", "fields": {"wba_id": wba, "company_name": name, "lei": lei, "lantern_id": have}}


class Plan(unittest.TestCase):
    def test_every_company_gets_one_and_a_lantern_company_keeps_its_own(self):
        todo, clashes, c = L.plan([rec(1, "PT_00001", "Acme Holdings plc", "529900T8BM49AURSDO55"), rec(2, "LN_0a1b2c3d", "Beta SA"),
                                   rec(3, "", "Not yet a company"), rec(4, "PT_00004", "Gamma AG", have="LN_11111111")])
        self.assertEqual(todo, [("rec1", "PT_00001", company_id("Acme Holdings plc", "529900T8BM49AURSDO55")), ("rec2", "LN_0a1b2c3d", "LN_0a1b2c3d")])
        self.assertEqual(clashes, [])
        self.assertEqual((c["already has one"], c["no wba_id yet"], c["to write"]), (1, 1, 2))
        self.assertRegex(todo[0][2], r"^LN_[0-9a-f]{8}$")

    def test_an_id_written_is_never_changed(self):
        todo, _, _ = L.plan([rec(1, "PT_00001", "A new name after a merger", have="LN_aaaaaaaa")])
        self.assertEqual(todo, [])

    def test_two_companies_never_share_an_id_and_none_is_made_up(self):
        same = company_id("Acme", "")
        todo, clashes, c = L.plan([rec(1, "PT_00001", "Acme"), rec(2, "PT_00002", "ACME Ltd."), rec(3, "PT_00003", "Other", have=company_id("Zed", "")),
                                   rec(4, "PT_00004", "Zed")])
        self.assertEqual(todo, [])                                             # the pair, and the one whose id another row already holds
        self.assertEqual([i for i, _ in clashes], sorted([same, company_id("Zed", "")]))
        self.assertEqual(c["left empty: the id is shared"], 3)

    def test_the_writes_are_plain_text_in_tens(self):
        bodies = list(L.payloads([(f"rec{i}", f"PT_{i}", f"LN_{i:08x}") for i in range(23)]))
        self.assertEqual([len(b["records"]) for b in bodies], [10, 10, 3])
        self.assertNotIn("typecast", bodies[0])
        self.assertEqual(bodies[0]["records"][0], {"id": "rec0", "fields": {"lantern_id": "LN_00000000"}})


if __name__ == "__main__":
    unittest.main()
