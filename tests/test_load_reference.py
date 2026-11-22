"""The reference list's loader (load_reference.py): what it accepts from the file, the aliases it adds, and its
workflow. Invented entries only: the list itself is private and is not in this repository. No network, no database."""
import gzip, json, os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import load_reference as R

FLOW = open(os.path.join(ROOT, ".github", "workflows", "load_reference.yml"), encoding="utf-8").read()
SRC = open(os.path.join(ROOT, "load_reference.py"), encoding="utf-8").read()

ENTRIES = [
    {"id": "norm:aaaa", "kind": "norm", "name": "Invented Reporting Rule (IRR)", "aliases": ["IRR"], "year": 2019, "url": "https://rule.example/irr",
     "text_en": "A rule  invented for a test.", "issuers": ["Invented Board", "Invented Board"], "ai": True},
    {"id": "term:bbbb", "kind": "term", "name": "Made-up Materiality", "text_en": "x" * 20000, "year": "2019", "url": "rule.example"},
    {"id": "term:bbbb", "kind": "term", "name": "Made-up Materiality again"},
    {"id": "thing:cccc", "kind": "thing", "name": "Unknown kind"},
    {"id": "org:dddd", "kind": "organisation", "name": " "},
]


class File(unittest.TestCase):
    def test_gzip_and_plain_text_are_read_alike(self):
        text = "\n".join(json.dumps(e) for e in ENTRIES).encode("utf-8") + b"\n\n"
        self.assertEqual(R.parse(text), ENTRIES)
        self.assertEqual(R.parse(gzip.compress(text)), ENTRIES)

    def test_rows_are_cleaned_and_bad_entries_counted_not_loaded(self):
        rows, skipped = R.entry_rows(ENTRIES)
        self.assertEqual(skipped, 3)                                              # a repeated id, an unknown kind, no name
        self.assertEqual([r[0] for r in rows], ["norm:aaaa", "term:bbbb"])
        norm, term = (dict(zip(R.COLUMNS, r)) for r in rows)
        self.assertEqual(norm["aliases"], ["IRR", "Invented Reporting Rule"])     # the bracket's acronym was there; the bare name is added
        self.assertEqual((norm["text_en"], norm["issuers"], norm["year"], norm["url"]), ("A rule invented for a test.", ["Invented Board"], 2019, "https://rule.example/irr"))
        self.assertEqual((len(term["text_en"]), term["year"], term["url"], term["aliases"], term["ai"]), (R.TEXT_MAX, None, None, [], True))

    def test_a_bracket_becomes_aliases_only_when_it_reads_as_an_acronym(self):
        self.assertEqual(R.with_bracket_aliases("Clean Thing Mechanism (CTM)", []), ["CTM", "Clean Thing Mechanism"])
        self.assertEqual(R.with_bracket_aliases("Emissions (direct and indirect, of every kind)", ["X"]), ["X"])
        self.assertEqual(R.with_bracket_aliases("No bracket", []), [])

    def test_nothing_of_the_list_is_printed_and_nothing_is_deleted(self):
        self.assertNotIn("delete from", SRC.lower())
        self.assertIn("set gone = true", SRC)
        self.assertNotRegex(SRC, r"print\([^)]*\b(name|text_en|rows\[)")


class Workflow(unittest.TestCase):
    def test_it_runs_on_the_server_dry_by_hand_and_is_never_started_by_a_pull_request(self):
        self.assertIn("runs-on: [self-hosted, lantern]", FLOW); self.assertNotIn("pull_request", FLOW)
        self.assertIn('default: "dry_run"', FLOW); self.assertIn("TEXT_STORE_TOKEN: ${{ secrets.TEXT_STORE_TOKEN }}", FLOW)


if __name__ == "__main__":
    unittest.main()
