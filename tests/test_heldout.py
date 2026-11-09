import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import heldout as H  # noqa: E402


class HeldOut(unittest.TestCase):
    def test_development_attributes_are_never_held_out(self):
        dev = H.development()
        self.assertIn("GMC_00048.1", dev)          # revenue, tuned on
        self.assertIn("GMC_01933.1", dev)          # narrative pilot
        for c in dev:
            self.assertFalse(H.is_heldout(c))

    def test_about_a_quarter_held_out_and_stable(self):
        codes = [f"GMC_{i:05d}.1" for i in range(3000, 5000)]
        share = sum(H.is_heldout(c, set()) for c in codes) / len(codes)
        self.assertTrue(0.2 < share < 0.3, share)
        self.assertEqual([H.is_heldout(c, set()) for c in codes[:50]], [H.is_heldout(c, set()) for c in codes[:50]])

    def test_unit_family(self):
        self.assertEqual(H.unit_family(["USD", "USD", "EUR"]), "currency")
        self.assertEqual(H.unit_family(["tCO2e"]), "tco2e")
        self.assertEqual(H.unit_family(["%"]), "percent")
        self.assertEqual(H.unit_family(["MWh"]), "count")
        self.assertEqual(H.unit_family([]), "count")

    def test_labels_from_the_description_alone(self):
        self.assertEqual(H.auto_labels("Total water withdrawal (in m3)"),
                         ["total water withdrawal (in m3)", "total water withdrawal", "water withdrawal"])
        self.assertEqual(H.auto_labels("What is the company's total energy consumption?")[-1], "energy consumption")

    def test_core_terms_drop_generic_words(self):
        self.assertEqual(H.core_terms("Number of employees"), ["employees"])
        self.assertEqual(H.core_terms("Total revenue"), ["revenue"])
        self.assertEqual(H.core_terms("Scope 1 emissions"), ["scope 1 emissions"])
        s = H.auto_number_spec({"Description": "Number of employees"}, [""])
        self.assertEqual(s["more_terms"], ["employees"])
        self.assertIn("employees", s["labels"])

    def test_auto_queries_pairs(self):
        q = H.auto_queries("Does the company have a board committee responsible for climate?")
        self.assertIn("board committee", q)
        self.assertIn("climate", " ".join(q))
        self.assertLessEqual(len(q), 12)
        self.assertEqual(H.auto_queries("Number of employees"), ["employees"])

    def test_parse_spec_reply(self):
        l, e, t = H.parse_spec_reply('Here: {"labels": ["Total Revenue", "turnover", "x"], "exclude": ["per share"], "terms": ["net sales"]}')
        self.assertEqual(l, ["total revenue", "turnover"])
        self.assertEqual(e, ["per share"])
        self.assertEqual(t, ["net sales"])
        self.assertEqual(H.parse_spec_reply("not json"), ([], [], []))

    def test_model_spec_adds_to_the_built_spec_and_sees_no_answers(self):
        seen = {}
        def call(messages, usage, sess=None, system=None, max_tokens=None):
            seen["prompt"] = messages[0]["content"]
            return '{"labels": ["headcount", "employees"], "exclude": ["average"], "terms": ["number employees"]}'
        base = H.auto_number_spec({"Description": "Number of employees", "Definition": "People employed at year end."}, [""])
        s = H.model_spec(base, {}, call=call)
        self.assertEqual(s["labels"][0], base["labels"][0])
        self.assertIn("headcount", s["labels"])
        self.assertEqual(s["exclude"], ["average"])
        self.assertIn("number employees", s["more_terms"])
        self.assertEqual(s["spec_by"], "model")
        self.assertIn("People employed at year end.", seen["prompt"])
        self.assertEqual(H.model_spec(base, {}, call=lambda *a, **k: "")["spec_by"], "description")

    def test_spec_examples_come_from_development_specs_and_never_the_own(self):
        ex = H.spec_examples()
        self.assertIn("Revenue", ex); self.assertIn("scope 1", ex.lower()); self.assertIn("Answer: {", ex)
        self.assertNotIn("Revenue", H.spec_examples(exclude_code="GMC_00048.1"))
        seen = {}
        def call(messages, usage, sess=None, system=None, max_tokens=None):
            seen["prompt"] = messages[0]["content"]; return '{"labels": ["x"], "exclude": [], "terms": []}'
        base = H.auto_number_spec({"Description": "Number of mines", "Definition": "Operating mines."}, [""])
        H.model_spec(base, {}, call=call, code="GMC_00001.1")
        self.assertIn("Worked examples", seen["prompt"]); self.assertNotIn("Scope 1 emissions\nDefinition", seen["prompt"]); self.assertIn("Number of mines", seen["prompt"])
        H.model_spec(base, {}, call=call, examples=False)
        self.assertNotIn("Worked examples", seen["prompt"])

    def test_spec_has_no_hand_written_parts(self):
        s = H.auto_number_spec({"Description": "Total energy consumption", "Definition": "<p>All energy used.</p>"}, ["MWh"])
        self.assertEqual(s["exclude"], [])
        self.assertNotIn("reading_notes", s)
        self.assertNotIn("siblings", s)
        self.assertEqual(s["definition"], "All energy used.")
        self.assertEqual(s["status"], "heldout")
        self.assertIsNone(H.auto_number_spec({"Description": "Share of renewable energy"}, ["%"]))
        self.assertIsNone(H.auto_number_spec({}, ["USD"]))

    def test_number_codes_only_held_out_numbers(self):
        meta = {f"GMC_{i:05d}.1": {"Data Type": {"name": "Number" if i % 2 else "Boolean"}} for i in range(3000, 3400)}
        got = H.number_codes(meta, 5, dev=set())
        self.assertLessEqual(len(got), 15)
        for c in got:
            self.assertTrue(H.is_heldout(c, set()))
            self.assertEqual(H.type_of(meta[c]), "Number")


if __name__ == "__main__":
    unittest.main()
