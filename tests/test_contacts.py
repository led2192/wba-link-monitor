"""Offline tests for contacts.py: which addresses are found, which are the company's own, how the
mailbox name decides the kind, and that rows are updated, never duplicated or deleted."""
import os, sys, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import contacts as C  # noqa: E402


class Kinds(unittest.TestCase):
    def test_role_mailboxes(self):
        cases = {"ir": "investor relations", "investor.relations": "investor relations", "investors": "investor relations",
                 "ir.team": "investor relations", "sustainability": "sustainability", "esg": "sustainability",
                 "csr-office": "sustainability", "group.sustainability": "sustainability", "nachhaltigkeit": "sustainability",
                 "press": "media", "mediarelations": "media", "info": "general", "contact": "general",
                 "compliance": "ethics and compliance", "speakup": "ethics and compliance", "dpo": "privacy",
                 "privacy": "privacy", "careers": "people", "hr": "people"}
        for local, kind in cases.items():
            self.assertEqual(C.kind_of(local), (kind, False), local)

    def test_people_and_the_rest(self):
        self.assertEqual(C.kind_of("jane.doe"), ("personal", True))
        self.assertEqual(C.kind_of("maria_garcia_lopez"), ("personal", True))
        self.assertEqual(C.kind_of("x7a9"), ("other", False))
        # first dry run (27 Oct 2026): role mailboxes with a dot were taken for people
        self.assertEqual(C.kind_of("cosec.admin"), ("general", False))
        self.assertEqual(C.kind_of("eci_administrator"), ("general", False))
        self.assertEqual(C.kind_of("infogrupoacs"), ("general", False))
        self.assertEqual(C.kind_of("2645.7085"), ("other", False))
        # the first full run (2,143 addresses): service and function mailboxes were taken for people or left as other
        for local in ("cyber.security", "sola_doc", "bnicall", "mandiricare", "halobca", "callbri", "corporate.secretary"):
            self.assertEqual(C.kind_of(local), ("general", False), local)
        self.assertEqual(C.kind_of("humas"), ("media", False))
        self.assertEqual(C.kind_of("halo_sdm"), ("people", False))      # "Halo SDM": the HR helpline (Indonesian)
        # names that only contain a role word inside are not that role
        self.assertEqual(C.kind_of("desgagne.paul"), ("personal", True))
        self.assertEqual(C.kind_of("grigorios.papa"), ("personal", True))


class Finding(unittest.TestCase):
    def test_addresses_and_their_sentence(self):
        pages = [(3, "Contact Investor Relations at IR@Example.com or +44 20 1234. Images: logo@2x.png. Ref 2645.7085@example.com"),
                 (88, "For questions about this report please write to sustainability@example.com."),
                 (90, "Auditor: KPMG, audit.contact@kpmg.co.uk. Again ir@example.com.")]
        got = C.emails_in(pages)
        self.assertEqual(set(got), {"ir@example.com", "sustainability@example.com", "audit.contact@kpmg.co.uk"})
        self.assertEqual(got["ir@example.com"][0], 3)                     # first occurrence wins
        self.assertIn("Contact Investor Relations", got["ir@example.com"][1])

    def test_own_domains(self):
        companies = {"PT_1": {"id": "rec1", "fields": {"website": "www.example.com"}},
                     "PT_2": {"id": "rec2", "fields": {"website": "https://other.org"}}}
        pages = [{"fields": {"wba_id": "PT_1", "url": "https://investors.example-group.com/reports"}},
                 {"fields": {"wba_id": "PT_1", "url": "https://lobbymap.org/a"}},
                 {"fields": {"wba_id": "PT_2", "url": "https://lobbymap.org/b"}},
                 {"fields": {"wba_id": "X", "url": "https://lobbymap.org/c"}}]
        mine, owners = C.company_domains(companies, pages)
        self.assertEqual(mine["PT_1"], {"example.com", "example-group.com"})     # lobbymap.org: 3 companies
        own = C.own_domains("PT_1", mine, owners, "https://d1.cloudfront.net/ar.pdf")
        self.assertIn("cloudfront.net", own)                                     # nobody else's pages use it
        own = C.own_domains("PT_1", mine, owners, "https://lobbymap.org/x.pdf")
        self.assertNotIn("lobbymap.org", own)

    def test_left_out_report_names_the_likely_own_domains_and_the_shared_ones(self):
        left_out = {"PT_1": {"example-holding.com": {"a", "b", "c"}, "bigaudit.com": {"a"}},
                    "PT_2": {"bigaudit.com": {"d"}, "agency.io": {"d"}},
                    "PT_3": {"bigaudit.com": {"e"}}}
        md, rows = C.left_out_report(left_out, {"PT_3"}, {"PT_1": {"example.com"}}, {"PT_1": "Example", "PT_2": "Two"})
        self.assertEqual(rows[0], ("wba_id", "company", "domain", "reports", "own_domains_by_rule", "has_own_address"))
        self.assertEqual(rows[1], ("PT_1", "Example", "example-holding.com", 3, "example.com", False))   # most reports first
        self.assertEqual([r[0] for r in rows[1:]], ["PT_1", "PT_1", "PT_2", "PT_2", "PT_3"])                # companies with an address last
        text = "\n".join(md)
        self.assertIn("without an own-domain address", text)
        self.assertIn("| PT_1 Example | example.com | example-holding.com | 3 |", text)
        self.assertNotIn("| PT_3", text)                                                                  # it has an address already
        self.assertIn("| bigaudit.com | 3 | 3 |", text)                                                   # printed by three companies


class DomainMail(unittest.TestCase):
    def test_verdicts_from_dns(self):
        from unittest import mock
        class R:
            def __init__(self, body, status=200):
                self.status_code, self._b = status, body
            def json(self):
                return self._b
        sess = mock.Mock()
        sess.get.return_value = R({"Status": 0, "Answer": [{"type": 15, "data": "10 mx1.example.com."}]})
        self.assertEqual(C.domain_accepts_mail("example.com", sess), "accepts")
        sess.get.return_value = R({"Status": 3})
        self.assertEqual(C.domain_accepts_mail("gone.example", sess), "none")
        sess.get.return_value = R({"Status": 0, "Answer": [{"type": 15, "data": "0 ."}]})
        self.assertEqual(C.domain_accepts_mail("nomail.example", sess), "none")      # null MX
        sess.get.return_value = R({"Status": 0})
        self.assertEqual(C.domain_accepts_mail("web-only.example", sess), "none")    # no MX published
        sess.get.return_value = R({}, status=500)
        self.assertEqual(C.domain_accepts_mail("x.example", sess), "unknown")
        sess.get.side_effect = Exception("timeout")
        self.assertEqual(C.domain_accepts_mail("x.example", sess), "unknown")

    def test_verdict_written_with_the_row(self):
        found = {("PT_1", "ir@example.com"): [(2025, "recB", 5, "new")]}
        creates, _ = C.merge(found, "2026-10-27", {}, {"example.com": "accepts"})
        self.assertEqual((creates[0]["fields"]["domain_mail"], creates[0]["fields"]["domain_checked"]), ("accepts", "2026-10-27"))
        creates, _ = C.merge(found, "2026-10-27", {})
        self.assertNotIn("domain_mail", creates[0]["fields"])                        # --no-dns: untouched


class Writing(unittest.TestCase):
    def test_create_then_update_never_twice(self):
        found = {("PT_1", "ir@example.com"): [(2024, "recA", 3, "old"), (2025, "recB", 5, "new")]}
        creates, updates = C.merge(found, "2026-10-27", {})
        self.assertEqual(len(creates), 1); self.assertEqual(updates, [])
        f = creates[0]["fields"]
        self.assertEqual((f["contact_key"], f["kind"], f["source_document"], f["source_year"], f["reports_with_it"],
                          f["first_seen"], f["last_seen"], f["evidence"]),
                         ("PT_1|ir@example.com", "investor relations", ["recB"], 2025, 2, "2026-10-27", "2026-10-27", "p.5: new"))
        existing = {"PT_1|ir@example.com": ("recRow", {k: v for k, v in f.items() if k not in ("contact_key", "first_seen")})}
        creates, updates = C.merge(found, "2026-10-27", existing)
        self.assertEqual((creates, updates), ([], []))                           # nothing changed
        creates, updates = C.merge(found, "2026-11-03", existing)
        self.assertEqual(creates, [])
        self.assertEqual(updates[0]["id"], "recRow")
        self.assertNotIn("first_seen", updates[0]["fields"])                     # first_seen is kept


if __name__ == "__main__":
    unittest.main()
