import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import add_companies as A


def rec(wba, name, website="", lei=""):
    return {"id": "rec" + wba, "fields": {"wba_id": wba, "company_name": name, "website": website, "lei": lei}}


class Names(unittest.TestCase):
    def test_a_name_is_compared_without_accents_case_and_legal_endings(self):
        self.assertEqual(A.norm_name("Nestlé S.A."), A.norm_name("NESTLE SA"))
        self.assertEqual(A.norm_name("Target Corporation"), "target")
        self.assertEqual(A.norm_name("The Coca-Cola Company"), "coca cola")
        self.assertEqual(A.norm_name("Holdings Ltd"), "holdings ltd")            # only legal words: kept, not emptied

    def test_lei_and_host(self):
        self.assertEqual(A.norm_lei(" 549300 mkfyeko zzzzz 12 ".replace(" ", " ")), "549300MKFYEKOZZZZZ12")
        self.assertEqual(A.norm_lei("not an lei"), "")
        self.assertEqual(A.host_of("https://www.Example.com/investors"), "example.com")
        self.assertEqual(A.host_of("example.org"), "example.org")
        self.assertEqual(A.host_of(""), "")

    def test_the_id_is_the_same_every_time(self):
        a = A.company_id("Acme Robotics Inc.", "")
        self.assertRegex(a, r"^LN_[0-9a-f]{8}$")
        self.assertEqual(a, A.company_id("ACME ROBOTICS", ""))                    # the same company, the same id
        self.assertNotEqual(a, A.company_id("Acme Robotics", "549300MKFYEKOZZZZZ12"))   # with an LEI, the LEI decides
        self.assertEqual(A.company_id("One name", "549300MKFYEKOZZZZZ12"), A.company_id("Another name", "549300mkfyekozzzzz12"))


class Decide(unittest.TestCase):
    def setUp(self):
        self.ex = A.index_existing([rec("PT_00001", "Unilever PLC", "https://www.unilever.com", "549300MKFYEKOZZZZZ12"), rec("", "No id yet")])

    def test_a_new_company_is_added_once(self):
        what, cid, note = A.decide("Acme Robotics", "https://acme.example", "", self.ex)
        self.assertEqual(what, "add"); self.assertIn(cid, note)
        self.assertEqual(A.decide("ACME Robotics Inc", "", "", self.ex)[0], "skip")      # twice in one batch: one company

    def test_what_is_already_a_company_is_not_added(self):
        self.assertIn("the same name", A.decide("Unilever", "", "", self.ex)[2])
        self.assertIn("the same website", A.decide("Unilever Foods", "unilever.com/brands", "", self.ex)[2])
        self.assertIn("the same LEI", A.decide("UL", "", "549300MKFYEKOZZZZZ12", self.ex)[2])

    def test_a_wrong_lei_or_no_name_is_said_so(self):
        self.assertIn("LEI", A.decide("Beta Mining", "", "12345", self.ex)[2])
        self.assertEqual(A.decide("  ", "x.com", "", self.ex)[:2], ("skip", None))

    def test_no_website_is_added_with_a_warning(self):
        what, cid, note = A.decide("Gamma Shipping", "", "", self.ex)
        self.assertEqual(what, "add"); self.assertIn("no website", note)

    def test_empty_fields_are_not_sent(self):
        self.assertEqual(A.clean({"a": "x", "b": None, "c": ""}), {"a": "x"})


if __name__ == "__main__":
    unittest.main()


class Usable(unittest.TestCase):
    def test_an_approved_company_is_told_to_the_search_at_once(self):
        import os
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "add_companies.py"), encoding="utf-8").read()
        self.assertIn("insert into public.companies (wba_id, name, website) values (%s, %s, %s) on conflict (wba_id) do nothing", src)

