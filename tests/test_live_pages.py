import collections, os, tempfile, unittest

os.environ.setdefault("AIRTABLE_TOKEN", "test")
os.environ.setdefault("AIRTABLE_BASE", "appTEST")
import company_coverage as CC  # noqa: E402


def stats(page_types, live):
    s = CC.empty_stats()
    s["urls_total"] = s["urls_monitored"] = 3
    s["page_types"] = collections.Counter(page_types)
    s["live_page_types"] = collections.Counter(live)
    return s


class LivePages(unittest.TestCase):
    def test_gone_hub_passes_v2_fails_v3(self):
        s = stats({"reports_hub": 1, "sustainability_page": 1}, {"sustainability_page": 1})
        v2, v3 = CC.compose(s, 2024, "v2"), CC.compose(s, 2024, "v3")
        self.assertNotIn("Report hub page", v2["gap_pages"])
        self.assertIn("Report hub page", v3["gap_pages"])
        self.assertNotIn("Sustainability page", v3["gap_pages"])
        self.assertLess(v3["completeness"], v2["completeness"])

    def test_compare_reports_losses(self):
        s = stats({"reports_hub": 1}, {})
        comp = {"PT_1": (None, {"company_name": "Acme"})}
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c.md")
            CC.compare(comp, {"PT_1": s}, 2024, out=out, a="v2", b="v3")
            text = open(out).read()
        self.assertIn("Report hub page: 1 companies lost it", text)
        self.assertIn("1 page(s) of the type, 0 watched and not gone", text)


if __name__ == "__main__":
    unittest.main()
