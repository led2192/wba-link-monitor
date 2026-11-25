"""catalogue_build.py: a candidate is checked against the companies held, offline."""
import unittest

import catalogue_build as C

A, B, D = "529900ABCDEFGHIJKL12", "549300ZZZZZZZZZZZZ34", "213800YYYYYYYYYYYY56"
HELD = {"by_lei": {A: "W1"}, "group": {B: "W2"}, "by_host": {"zebra.com": "W3"}, "by_name": {"okapi motors": "W4"}}


def cand(lei, site=None, *names):
    return {"lei": lei, "website": site, "names": list(names), "wikidata": None}


class Check(unittest.TestCase):
    def test_the_order_of_the_checks(self):
        self.assertEqual(C.check(cand(A, "https://zebra.com", "Okapi Motors AG"), HELD), ("held: the same LEI", "W1"))
        self.assertEqual(C.check(cand(B, "https://zebra.com"), HELD), ("in the group of a held company", "W2"))
        self.assertEqual(C.check(cand(D, "https://www.investors.zebra.com/x", "Other"), HELD), ("held: the same website", "W3"))
        self.assertEqual(C.check(cand(D, "https://okapi.example", "Something", "OKAPI Motors AG"), HELD), ("held: the same name", "W4"))          # the legal form is not part of the name
        self.assertEqual(C.check(cand(D, "https://new.example", "Gnu Rail plc"), HELD), (None, None))
        self.assertEqual(C.check(cand(D, None, "AG"), HELD), (None, None))          # a name of two letters is nobody's name

    def test_name_keys(self):
        self.assertEqual(C.name_keys("Zébra Motors AG", None, ""), {"zebra motors ag", "zebra motors"})

    def test_two_candidates_alike(self):
        cs = [dict(cand(D, "https://gnu.example"), wikidata="Q1"), dict(cand(B, "https://www.gnu.example/en"), wikidata="Q2"), dict(cand("969500XXXXXXXXXXXX78", "https://emu.example"), wikidata="Q1"),
              dict(cand(A, "https://gnu.example"), wikidata="Q9", reason="held: the same LEI")]
        for c in cs:
            c.setdefault("reason", None)
        C.among(cs)
        got = {c["lei"]: (c["reason"], c.get("like")) for c in cs}
        self.assertEqual(got[D], (None, None))          # the first by LEI stands: 2138... before 5493... and 9695...
        self.assertEqual(got[B], ("another candidate: the same website", D)); self.assertEqual(got["969500XXXXXXXXXXXX78"], ("another candidate: the same entry", D))
        self.assertEqual(got[A][0], "held: the same LEI")          # one already explained is left as it is

    def test_an_exact_match_stands_before_one_by_name(self):
        cs = [dict(cand(A, "https://one.example"), wikidata="Q1", match="name", reason=None), dict(cand(D, "https://two.example"), wikidata="Q1", match="lei", reason=None)]
        C.among(cs)
        self.assertEqual([(c["lei"], c["reason"]) for c in cs], [(A, "another candidate: the same entry"), (D, None)])          # though its LEI sorts first

    def test_labels(self):
        v = lambda x: {"value": x}
        self.assertEqual(C.labels({"results": {"bindings": [{"c": v("http://www.wikidata.org/entity/Q5"), "label_": v("Zebra"), "aliases": v("Z|Zeb")}, {"c": v("http://www.wikidata.org/entity/Q6")}]}}),
                         {"Q5": ("Zebra", ["Z", "Zeb"]), "Q6": (None, [])})

    def test_a_request_cut_short_is_asked_again(self):
        import unittest.mock as mock
        calls = []
        class R:
            def __init__(s, code): s.status_code = code
            def json(s): return {"ok": 1}
        def do():
            calls.append(1)
            if len(calls) == 1: raise ConnectionError("Response ended prematurely")
            return R(502 if len(calls) == 2 else 200)
        with mock.patch.object(C.time, "sleep", lambda s: None):
            self.assertEqual(C.fetch_json(do), (200, {"ok": 1})); self.assertEqual(len(calls), 3)
            self.assertEqual(C.fetch_json(lambda: R(404)), (404, {}))
            self.assertEqual(C.fetch_json(lambda: (_ for _ in ()).throw(TimeoutError()))[0], "TimeoutError")

    def test_entries(self):
        v = lambda x: {"value": x}
        got = C.entries({"results": {"bindings": [{"lei": v(A), "c": v("http://www.wikidata.org/entity/Q5"), "site_": v("https://x.example"), "label_": v("Zebra"), "aliases": v("Z|Zeb| ")}, {"lei": v(A), "c": v("http://www.wikidata.org/entity/Q6")}]}})
        self.assertEqual(got, {A: ("Q5", "https://x.example", "Zebra", ["Z", "Zeb"])})


if __name__ == "__main__":
    unittest.main()
