"""Offline tests for import_source_library.py: which analyst documents are added and which are skipped, with
no Airtable. 2 Nov 2026: every document type comes in, not only reports; policies are told apart by file."""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import import_source_library as I  # noqa: E402


def src(t, year, size, filename, wba="PT_1", tag="2026"):
    return {"src": tag, "rec": f"rec{size}", "wba": wba, "type": t, "fam": I.family(t), "year": year, "link": "",
            "name": filename, "file_url": "u", "filename": filename, "size": size}


HELD = {"size": {("PT_1", 999)}, "name": {("PT_1", "held.pdf")}, "fam_years": {("PT_1", "SUST"): {2024}},
        "types": {"PT_1": {"Code of Conduct"}}}


class Plan(unittest.TestCase):
    def verdicts(self, sources, held=HELD, order=("2026", "2025")):
        return [(s["filename"], v) for s, v in I.plan(sources, held, set(), list(order))]

    def test_policies_come_in_even_when_one_of_the_type_is_held(self):
        got = dict(self.verdicts([src("Code of Conduct", 2025, 10, "supplier-code.pdf"), src("Policy Documents", 2021, 11, "anti-bribery.pdf"),
                                  src("Policy Documents", 2021, 12, "whistleblowing.pdf"), src("Human Rights Policy", 2020, 13, "hr.pdf")]))
        self.assertEqual(set(got.values()), {"add"})

    def test_the_same_file_is_never_added_twice(self):
        got = self.verdicts([src("Policy Documents", 2025, 999, "x.pdf"), src("Policy Documents", 2025, 20, "held.pdf"),
                             src("Policy Documents", 2025, 21, "a.pdf"), src("Policy Documents", 2025, 21, "a-copy.pdf", tag="2025")])
        got = {f: v.split(":")[0] for f, v in got}
        self.assertEqual(got, {"x.pdf": "skip", "held.pdf": "skip", "a.pdf": "add", "a-copy.pdf": "skip"})   # 2026 preferred

    def test_reports_keep_their_year_guards(self):
        got = dict(self.verdicts([src("Sustainability Report", 2024, 30, "sr24.pdf"), src("Sustainability Report", 2025, 31, "sr25.pdf"),
                                  src("Climate Report", 2024, 32, "tcfd24.pdf")]))
        self.assertTrue(got["sr24.pdf"].startswith("skip:we hold this report type"))
        self.assertTrue(got["sr25.pdf"].startswith("review"))
        self.assertEqual(got["tcfd24.pdf"], "add")                    # another type: told apart by file

    def test_families(self):
        self.assertEqual(I.family("ESG Report"), "SUST"); self.assertEqual(I.family("Registration Document"), "DOC:Registration Document")
        self.assertEqual(I.family(""), "")
        self.assertIn("Policy Documents", I.STANDING); self.assertNotIn("Climate Report", I.STANDING)
        self.assertIn("appLkf0N4Tc26ojYD", I.SL_BASES)                # the 2026 base is guarded as read only too


class Purged(unittest.TestCase):
    def test_a_purged_row_never_comes_back(self):
        # 2 Nov 2026: purge_discarded.py deletes old discards (often misattributed reports); the library no longer
        # holds them, so only the tombstone's library_id stops the next morning's import from adding them again
        a, b = src("Climate Report", 2025, 50, "wales.pdf"), src("Climate Report", 2024, 51, "ok.pdf")
        got = {s["filename"]: v for s, v in I.plan([a, b], HELD, set(), ["2026"], {I.library_id(a)})}
        self.assertEqual(got, {"wales.pdf": "skip:discarded and purged", "ok.pdf": "add"})

    def test_reads_the_purge_tombstones(self):
        import gzip, tempfile
        p = os.path.join(tempfile.mkdtemp(), "t.tsv.gz")
        with gzip.open(p, "wt") as fh:
            fh.write("PT_1-doc\tPT_1-abc\t2026-11-02\tWrong company\nPT_2-doc\t\t2026-11-02\t\n")
        self.assertEqual(I.read_purged(p), {"PT_1-abc"})
        self.assertEqual(I.read_purged(p + ".missing"), set())


class Origin(unittest.TestCase):
    def test_new_rows_say_where_they_came_from(self):
        row = I.row_for(src("Policy Documents", 2025, 10, "p.pdf", tag="2026"), {"PT_1": "Acme"})
        self.assertEqual(row["origin"], "Source Library 2026")

    def test_older_rows_from_their_notes(self):
        self.assertEqual(I.origin_from_notes("From Source Library 2025 (record recX): Annual Report 2024"), "Source Library 2025")
        self.assertEqual(I.origin_from_notes("Found by research (2026-11-02)"), "web search or research")
        self.assertIsNone(I.origin_from_notes("Discarded: not a report"))


class Guards(unittest.TestCase):
    """2 Nov 2026: the whole base counts against Airtable's 500,000 records (389,675 then, of them 292,737 report rows),
    and an import's attachments are capped because the API cannot read the base's attachment space (760.8 GB of 1,000)."""
    def run_main(self, n_adds, size, others, extra=()):
        import tempfile
        from unittest import mock
        adds = [src("Policy Documents", 2025, 1000 + i, f"p{i}.pdf") for i in range(n_adds)]
        for s in adds:
            s["size"] = size
        out = os.path.join(tempfile.mkdtemp(), "plan.csv")
        env = {"AIRTABLE_TOKEN": "t", "AIRTABLE_BASE": "appTEST", "SOURCE_LIBRARY_TOKEN": "r"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(I, "read_sources", return_value=[]), \
             mock.patch.object(I, "read_library", return_value=(HELD, 300_000)), \
             mock.patch.object(I, "read_deleted", return_value=set()), \
             mock.patch.object(I, "plan", return_value=[(s, "add") for s in adds]), \
             mock.patch.object(I, "base_records", return_value=others), \
             mock.patch.object(I, "get_all", side_effect=AssertionError("no write expected")), \
             mock.patch.object(sys, "argv", ["x", "--bases", "2026", "--commit", "--plan-out", out, *extra]):
            I.main()

    def test_the_whole_base_counts(self):
        with self.assertRaises(SystemExit) as e:
            self.run_main(5, 10, others=149_999)
        self.assertIn("other tables", str(e.exception))

    def test_attachments_are_capped(self):
        with self.assertRaises(SystemExit) as e:
            self.run_main(2, 40 * 2 ** 30, others=0)
        self.assertIn("--max-gb", str(e.exception))

    def test_a_raised_cap_lets_it_through_to_the_record_check(self):
        with self.assertRaises(SystemExit) as e:
            self.run_main(2, 40 * 2 ** 30, others=149_999, extra=("--max-gb", "100"))
        self.assertIn("other tables", str(e.exception))


if __name__ == "__main__":
    unittest.main()
