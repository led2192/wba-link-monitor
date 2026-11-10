import collections, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import type_covers as T  # noqa: E402


class Prompt(unittest.TestCase):
    def test_every_type_is_offered_and_the_cover_is_cut(self):
        p = T.prompt("\u00e9" * 10000)
        for t in T.TYPES:
            self.assertIn(f"- {t}:", p)
        self.assertIn("- UNKNOWN:", p)
        self.assertEqual(p.count("\u00e9"), T.COVER_CHARS)

    def test_the_types_are_the_fields_choices(self):
        choices = {"Impact Report", "Statutory Documents", "Code of Conduct", "Policy Documents", "Financial Statement", "CSR Report",
                   "Annual Report", "Human Rights Policy", "Climate Report", "Environmental Reports", "Company Presentations",
                   "Integrated Report", "ESG Report", "Registration Document", "SASB Index", "Engagement Report", "Certifications",
                   "Interim Reports", "CDP Report", "GRI", "Social Reports", "10K Form", "Sustainability Report", "UNKNOWN"}
        self.assertEqual(set(T.TYPES) | {T.UNKNOWN}, choices)


class Reply(unittest.TestCase):
    def test_json_with_a_listed_type(self):
        self.assertEqual(T.parse_reply('{"type": "Annual Report", "confidence": "high", "title": "Informe anual 2024"}'),
                         ("Annual Report", "high", "Informe anual 2024"))

    def test_case_and_prose_are_forgiven_but_an_unlisted_type_is_not(self):
        self.assertEqual(T.parse_reply('Here: {"type": "sustainability report", "confidence": "LOW"}')[:2], ("Sustainability Report", "low"))
        self.assertEqual(T.parse_reply('{"type": "Brochure"}'), (None, None, None))
        self.assertEqual(T.parse_reply("I cannot tell"), (None, None, None))
        self.assertEqual(T.parse_reply('{"type": "UNKNOWN"}')[0], "UNKNOWN")

    def test_a_title_with_stray_quotes_still_gives_the_type(self):
        self.assertEqual(T.parse_reply('{"type": "Annual Report", "confidence": "low", "title": "Rapport "annuel" 2023"}'), ("Annual Report", "low", ""))

    def test_read_type_uses_the_reader_call(self):
        seen = {}

        def call(messages, usage, system=None, max_tokens=None):
            seen["system"], seen["max_tokens"], seen["prompt"] = system, max_tokens, messages[0]["content"]
            usage["calls"] += 1
            return '{"type": "Interim Reports", "confidence": "high", "title": "Q3 2024"}'
        usage = collections.Counter()
        self.assertEqual(T.read_type("Quarterly report Q3 2024", usage, call=call), ("Interim Reports", "high", "Q3 2024"))
        self.assertEqual(seen["system"], T.SYSTEM); self.assertEqual(seen["max_tokens"], 1000)
        self.assertIn("Quarterly report Q3 2024", seen["prompt"]); self.assertEqual(usage["calls"], 1)


class Queue(unittest.TestCase):
    def test_the_queue_is_live_untyped_rows_with_a_cover_and_not_english_by_default(self):
        f = T.queue_formula()
        for part in ("NOT({discard})", "{cover_text}!=''", "{source_type_check}=''", "{source_type}=''", "{doc_language}!='English'"):
            self.assertIn(part, f)
        self.assertNotIn("English", T.queue_formula("all"))
        c = T.queue_formula(check=True)
        self.assertIn("{source_type_check}!=''", c); self.assertNotIn("{source_type}=''", c)

    def test_payloads_set_the_type_ten_rows_at_a_time_with_typecast(self):
        bodies = list(T.payloads([(f"rec{i}", "Annual Report") for i in range(23)]))
        self.assertEqual([len(b["records"]) for b in bodies], [10, 10, 3])
        self.assertEqual(bodies[0]["records"][0], {"id": "rec0", "fields": {"source_type_check": "Annual Report"}})
        self.assertTrue(all(b["typecast"] for b in bodies))

    def test_agreement(self):
        share, by, dis = T.agreement([("Annual Report", "Annual Report"), ("Annual Report", "Integrated Report"), ("GRI", "GRI")])
        self.assertAlmostEqual(share, 2 / 3)
        self.assertEqual(by["Annual Report"], [2, 1]); self.assertEqual(dis[("Annual Report", "Integrated Report")], 1)


if __name__ == "__main__":
    unittest.main()
