"""The use-case exam (18 Nov 2026): real things people typed, and what the page must make of them.

In two days of real use a colleague's searches and the owner's found failures the search exam had never shown, because
that exam asks one thing (is the confirmed page among the pages found). Here the page's own code, parse(), isQuestion()
and intent(), is run in node on every case of tests/use_cases.json: the company read, the years, a near name, and
which of the five ways the text takes (catalogue, fact, question, overview, search); a near name is taken as
the company before the way is decided, as the page does when the library does not know the word. A search that goes wrong in real use is
added to the file with what should have happened. Run in node when it is there (it is on the tests' runner).
"""
import json, os, shutil, subprocess, unittest

HERE = os.path.dirname(__file__)
PAGE = open(os.path.join(HERE, "..", "site", "search.html"), encoding="utf-8").read()
CASES = json.load(open(os.path.join(HERE, "use_cases.json"), encoding="utf-8"))


def run_page(cases):
    """What the page reads in each text: [{company, from, to, type, typo, q, intent}]."""
    consts = PAGE[PAGE.index("  const TYPES = ["):PAGE.index("  const $ = id => document.getElementById(id);")]
    parser = PAGE[PAGE.index("  const WORD_NAMES = new Set("):PAGE.index("  function renderChips() {")]
    asked = PAGE[PAGE.index("  function isQuestion(text) {"):PAGE.index("  const go = async () =>")]
    typo = PAGE[PAGE.index("  function applyTypo(parsed) {"):PAGE.index("  const typoWanted = ")]
    script = ("const RealDate = Date; globalThis.Date = class extends RealDate { getFullYear() { return %d; } };"
              "const document = { createElement: () => ({}), getElementById: () => ({}) }; const $ = () => ({ value: '' });"
              "let companies = %s; let industries = ['Metals & Mining', 'Oil & Gas'], regions = [];\n%s\n%s\n%s\n%s\ntopics = new Set(%s); metricNames = new Set(%s); flags = { answer_topics: true };\n"
              "console.log(JSON.stringify(%s.map(t => { const r = parse(t); return { company: r.company || null, from: r.from || null, to: r.to || null, "
              "type: r.type || null, hint: r.typeHint || null, typo: (r.typo && r.typo.name) || null, q: (r.q || '').replace(/\\s+/g, ' ').trim(), "
              "intent: intent(t, r.typo ? applyTypo({ ...r }) : r) }; })))"
              % (CASES["this_year"], json.dumps(CASES["companies"]), consts, parser, asked, typo, json.dumps(CASES["topics"]), json.dumps(CASES.get("metrics", [])), json.dumps([c["typed"] for c in cases])))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr[-1500:])
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UseCases(unittest.TestCase):
    def test_every_real_case_is_read_as_it_should_be(self):
        cases = CASES["cases"]; got = run_page(cases); wrong = []
        for case, g in zip(cases, got):
            for field in ("company", "from", "to", "typo", "intent", "q"):
                if field in case and case[field] != g[field]:
                    wrong.append(f"{case['typed']!r}: {field} is {g[field]!r}, should be {case[field]!r}")
            if case.get("typed_type") and not g["type"]:
                wrong.append(f"{case['typed']!r}: no document type read")
        self.assertEqual(wrong, [])

    def test_the_word_year_before_the_year_typed_is_not_searched(self):
        """v128: "steel production of X in year 2023" required the word "year" on the page."""
        name = CASES["companies"][0]["name"] if isinstance(CASES["companies"][0], dict) else CASES["companies"][0]
        got = run_page([{"typed": t} for t in (f"What was the steel production of {name} in year 2023?", f"{name} revenue for the fiscal year 2024",
                                              f"{name} emissions base year 2019", f"{name} year on year growth", f"{name} target year 2030 net zero")])
        self.assertNotIn("year", got[0]["q"].lower()); self.assertIn("steel production", got[0]["q"]); self.assertEqual(got[0]["from"], 2023)
        self.assertNotIn("year", got[1]["q"].lower()); self.assertNotIn("fiscal", got[1]["q"].lower()); self.assertEqual(got[1]["from"], 2024)
        self.assertIn("base year", got[2]["q"]); self.assertIn("year on year", got[3]["q"]); self.assertIn("target year", got[4]["q"])

    def test_a_kind_of_document_next_to_a_subject_is_a_preference_not_a_filter(self):
        """v123: "microsoft's policy on living wage" was searched only inside Policy Documents."""
        name = CASES["companies"][0]["name"] if isinstance(CASES["companies"][0], dict) else CASES["companies"][0]
        typed = [f"{name}'s policy on living wage", f"{name} code of conduct forced labour", f"{name} sustainability report scope 3",
                 f"{name} annual report 2024", f"latest {name} code of conduct", f"the {name} sustainability reports"]
        got = run_page([{"typed": t} for t in typed])
        for g in got[:3]:
            self.assertIsNone(g["type"], g); self.assertTrue(g["hint"], g)
        self.assertEqual(got[0]["hint"], "Policy Documents"); self.assertNotIn("policy", got[0]["q"].lower()); self.assertIn("living wage", got[0]["q"])
        self.assertEqual(got[1]["hint"], "Code of Conduct"); self.assertIn("forced labour", got[1]["q"])
        for g in got[3:]:
            self.assertTrue(g["type"], g); self.assertIsNone(g["hint"], g)
        self.assertIn("const TYPE_VAGUE = new Set([\"Policy Documents\"])", PAGE)
        self.assertIn("return rows.filter(is).concat(rows.filter(r => !is(r)));", PAGE)

    def test_the_file_names_no_person_and_every_case_says_its_intent(self):
        self.assertGreaterEqual(len(CASES["cases"]), 20)
        for case in CASES["cases"]:
            self.assertIn(case["intent"], ("catalogue", "fact", "question", "overview", "search"), case["typed"])


if __name__ == "__main__":
    unittest.main()
