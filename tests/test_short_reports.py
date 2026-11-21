import unittest

import short_reports as S


def rec(rid, **f):
    return {"id": rid, "fields": dict({"library_id": "L-" + rid, "page_count": 1}, **f)}


class Select(unittest.TestCase):
    def test_only_short_unverified_live_periodic_reports(self):
        records = [rec("bio", source_type_check="Annual Report", source_type="Statutory Documents"),
                   rec("addr", source_type="Integrated Report", page_count=2),                    # no AI type: the address's
                   rec("verified", source_type_check="Annual Report", verified=True),
                   rec("discarded", source_type_check="Annual Report", discard=True),
                   rec("long", source_type_check="Annual Report", page_count=3),
                   rec("nocount", source_type_check="Annual Report", page_count=None),
                   rec("policy", source_type_check="Policy Documents"),
                   rec("ai_says_other", source_type_check="Policy Documents", source_type="Annual Report")]
        self.assertEqual(S.select(records), [("bio", "L-bio", "Annual Report", "source_type_check"),
                                             ("addr", "L-addr", "Integrated Report", "source_type")])

    def test_the_formula_names_every_guard(self):
        f = S.formula()
        for part in ("NOT({discard})", "NOT({verified})", "{page_count}>=1", "{page_count}<=2", "{effective_type}='Annual Report'",
                     "{effective_type}='10K Form'", "{effective_type}='Impact Report'"):
            self.assertIn(part, f)

    def test_the_record_of_what_it_was_is_written_before_airtable(self):
        src = open(S.__file__, encoding="utf-8").read()
        self.assertLess(src.index("insert into public.type_corrections"), src.index('airtable_request("PATCH"'))
        self.assertEqual(S.WRITE, {"source_type_check": "UNKNOWN", "robot_type": "UNKNOWN"})   # the only fields written
        self.assertIn('"fields": WRITE}', src)


if __name__ == "__main__":
    unittest.main()
