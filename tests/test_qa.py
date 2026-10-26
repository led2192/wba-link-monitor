"""Offline tests for qa.py and qa_sync.py: a fake Airtable in memory, no network.

Run:  QA_STRICT=true python3 -m unittest tests/test_qa.py -v
"""
import datetime as dt, json, os, re, sys, unittest

os.environ["QA_STRICT"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qa, qa_sync  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeAirtable:
    """Just enough of the records API for qa.py: GET with the two formula shapes it sends,
    POST (create) and PATCH (update by id). Select values are plain strings, as in the REST API."""

    def __init__(self):
        self.tables = {}
        self.calls = []
        self.n = 0

    def _table(self, url):
        return url.rstrip("/").split("/")[-1].replace("%20", " ")

    def _match(self, formula, fields):
        if not formula:
            return True
        m = re.fullmatch(r"OR\((.*)\)", formula)
        if m and "IS_BEFORE" not in formula:
            pairs = re.findall(r"\{(\w+)\}='((?:[^'\\]|\\.)*)'", m.group(1))
            return any(fields.get(k) == v.replace("\\'", "'") for k, v in pairs)
        m = re.fullmatch(r"AND\(\{status\}='open', IS_BEFORE\(\{last_seen\}, '([\d-]+)'\)(?:, \{script\}='([^']*)')?\)", formula)
        if m:
            cutoff, script = m.group(1), m.group(2)
            ok = fields.get("status") == "open" and (fields.get("last_seen") or "9999") < cutoff
            if script:
                ok = ok and fields.get("script") == script
            return ok
        raise AssertionError(f"unsupported formula in fake: {formula}")

    def request(self, method, url, headers, payload=None, params=None, timeout=60, tries=5):
        table = self._table(url)
        rows = self.tables.setdefault(table, [])
        self.calls.append((method, table))
        if method == "GET":
            params = dict(params or [])
            formula = params.get("filterByFormula")
            out = [r for r in rows if self._match(formula, r["fields"])]
            return FakeResponse({"records": [{"id": r["id"], "fields": dict(r["fields"])} for r in out]})
        if method == "POST":
            made = []
            for rec in payload["records"]:
                self.n += 1
                row = {"id": f"rec{self.n:014d}", "fields": dict(rec["fields"])}
                rows.append(row); made.append(row)
            return FakeResponse({"records": made})
        if method == "PATCH":
            made = []
            by_id = {r["id"]: r for r in rows}
            for rec in payload["records"]:
                by_id[rec["id"]]["fields"].update(rec["fields"]); made.append(by_id[rec["id"]])
            return FakeResponse({"records": made})
        raise AssertionError(method)


class QaTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAirtable()
        qa._request = lambda method, url, headers, payload=None, params=None: \
            self.fake.request(method, url, headers, payload, params)
        os.environ["QA_COMMIT"] = "true"
        os.environ["AIRTABLE_QA_TOKEN"] = "pat_test"
        os.environ["GITHUB_SHA"] = "abc1234def"
        os.environ["GITHUB_WORKFLOW"] = "Monitor links (daily)"
        os.environ["GITHUB_RUN_ID"] = "42"
        os.environ["GITHUB_EVENT_NAME"] = "schedule"

    def test_key_is_deterministic_and_case_insensitive(self):
        a = qa.key("PAGE_GONE", "https://x.com/Reports/", "")
        b = qa.key("PAGE_GONE", "https://x.com/reports/ ", "")
        self.assertEqual(a, b)
        self.assertNotEqual(a, qa.key("PAGE_UNREACHABLE", "https://x.com/Reports/", ""))

    def test_unknown_code_raises_in_strict(self):
        run = qa.Run("x.py")
        with self.assertRaises(ValueError):
            run.failure("NOT_A_CODE", "x", key="k")
        with self.assertRaises(ValueError):
            run.failure("PAGE_GONE", "404", key="k", bogus_field=1)

    def test_same_key_counted_once_per_run(self):
        run = qa.Run("x.py")
        run.failure("PAGE_GONE", "404", key="https://a/b", page_url="https://a/b")
        run.failure("PAGE_GONE", "404", key="https://a/b", page_url="https://a/b", message="again")
        self.assertEqual(len(run._failures), 1)
        self.assertEqual(run.counts["PAGE_GONE"], 1)

    def test_dry_run_writes_nothing(self):
        os.environ["QA_COMMIT"] = "false"
        run = qa.Run("x.py")
        run.failure("PAGE_GONE", "404", key="https://a/b", page_url="https://a/b")
        self.assertIsNone(run.finish("ok"))
        self.assertEqual(self.fake.calls, [])

    def test_create_then_update_increments_occurrences_and_keeps_first_seen(self):
        run = qa.Run("x.py")
        run.failure("PAGE_GONE", "404", key="https://a/b", page_url="https://a/b", wba_id="PT_1")
        run.finish("ok", rows_in=10, rows_out=3, counters={"dead": 1})
        runs, fails = self.fake.tables["runs"], self.fake.tables["failures"]
        self.assertEqual(len(runs), 1); self.assertEqual(len(fails), 1)
        f = fails[0]["fields"]
        self.assertEqual(f["status"], "open"); self.assertEqual(f["occurrences"], 1)
        self.assertEqual(f["first_commit"], "abc1234"); self.assertEqual(f["last_run"], [runs[0]["id"]])
        self.assertEqual(runs[0]["fields"]["run_key"], "Monitor links (daily)#42")
        self.assertEqual(runs[0]["fields"]["trigger"], "schedule")
        self.assertEqual(json.loads(runs[0]["fields"]["counters"]), {"dead": 1})
        # next night, new commit, same failure
        fails[0]["fields"]["first_seen"] = "2026-01-01"
        os.environ["GITHUB_SHA"] = "fedcba9"; os.environ["GITHUB_RUN_ID"] = "43"
        run2 = qa.Run("x.py")
        run2.failure("PAGE_GONE", "404", key="https://a/b", page_url="https://a/b", wba_id="PT_1")
        run2.finish("ok")
        f = self.fake.tables["failures"][0]["fields"]
        self.assertEqual(len(self.fake.tables["failures"]), 1)
        self.assertEqual(f["occurrences"], 2); self.assertEqual(f["first_seen"], "2026-01-01")
        self.assertEqual(f["first_commit"], "abc1234"); self.assertEqual(f["last_commit"], "fedcba9")
        self.assertEqual(len(self.fake.tables["runs"]), 2)

    def test_fixed_failure_that_recurs_reopens(self):
        self.fake.tables["failures"] = [{"id": "recF", "fields": {
            "failure_key": qa.key("PAGE_GONE", "https://a/b", ""), "status": "fixed", "occurrences": 4,
            "first_seen": "2026-01-01", "review_note": "2026-02-01: fixed in PR 9"}}]
        run = qa.Run("x.py")
        run.failure("PAGE_GONE", "404", key="https://a/b", page_url="https://a/b")
        run.finish("ok")
        f = self.fake.tables["failures"][0]["fields"]
        self.assertEqual(f["status"], "open"); self.assertEqual(f["occurrences"], 5)
        self.assertIn("recurred after being marked fixed", f["review_note"])
        self.assertIn("fixed in PR 9", f["review_note"])

    def test_close_silent_only_closes_old_open_rows_of_the_script(self):
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        new = (dt.date.today() - dt.timedelta(days=5)).isoformat()
        self.fake.tables["failures"] = [
            {"id": "r1", "fields": {"failure_key": "k1", "status": "open", "last_seen": old, "script": "qa_sync.py"}},
            {"id": "r2", "fields": {"failure_key": "k2", "status": "open", "last_seen": new, "script": "qa_sync.py"}},
            {"id": "r3", "fields": {"failure_key": "k3", "status": "open", "last_seen": old, "script": "harvest_reports.py"}},
            {"id": "r4", "fields": {"failure_key": "k4", "status": "fixed", "last_seen": old, "script": "qa_sync.py"}},
        ]
        run = qa.Run("qa_sync.py")
        self.assertEqual(run.close_silent(30, script="qa_sync.py"), 1)
        st = {r["id"]: r["fields"]["status"] for r in self.fake.tables["failures"]}
        self.assertEqual(st, {"r1": "resolved_silently", "r2": "open", "r3": "open", "r4": "fixed"})

    def test_labels_never_overwrite_existing(self):
        self.fake.tables["labels"] = [{"id": "L1", "fields": {"label_id": "x|type|correction", "verdict": "AI is right"}}]
        run = qa.Run("qa_sync.py")
        run.label("x|type|correction", field="type", verdict="Neither", true_type="Annual Report")
        run.label("y|type|correction", field="type", verdict="Neither", true_type="Annual Report")
        run.finish("ok")
        rows = self.fake.tables["labels"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["fields"]["verdict"], "AI is right")   # untouched

    def test_summary_markdown_counts_by_code_and_severity(self):
        os.environ["QA_COMMIT"] = "false"
        run = qa.Run("qa_sync.py")
        run.failure("PAGE_GONE", "404", key="a", page_url="https://a", severity="High")
        run.failure("PAGE_GONE", "404", key="b", page_url="https://b", severity="Medium")
        run.failure("CO_GAP", "no_hub", key="PT_1", wba_id="PT_1")
        md = run.summary_markdown("ok", {"swept": {"pages": 2}}, title="QA sync")
        self.assertIn("## QA sync: ok, dry run", md)
        self.assertIn("| PAGE_GONE | 1 | 1 | 0 | 2 |", md)
        self.assertIn("| CO_GAP | 0 | 1 | 0 | 1 |", md)
        self.assertIn("| **total** | 1 | 2 | 0 | 3 |", md)
        self.assertIn('"pages": 2', md)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.md")
            run.write_step_summary("ok", None, title="QA sync", path=path)
            self.assertIn("PAGE_GONE", open(path, encoding="utf-8").read())

    def test_write_failure_does_not_raise_outside_strict(self):
        os.environ["QA_STRICT"] = "false"
        qa.STRICT = False
        try:
            def boom(*a, **k):
                raise RuntimeError("airtable down")
            qa._request = boom
            run = qa.Run("x.py")
            run.failure("PAGE_GONE", "404", key="k", page_url="https://a/b")
            self.assertIsNone(run.finish("ok"))      # swallowed, printed
        finally:
            qa.STRICT = True
            os.environ["QA_STRICT"] = "true"


class SyncDeriveTests(unittest.TestCase):
    def test_stakes(self):
        self.assertEqual(qa_sync.stakes("Integrated Report", "Sustainability Report"), "high_stakes")  # loses ANNUAL
        self.assertEqual(qa_sync.stakes("Annual Report", "Statutory Documents"), "high_stakes")
        self.assertEqual(qa_sync.stakes("ESG Report", "Sustainability Report"), "low_stakes")
        self.assertEqual(qa_sync.stakes("Financial Statement", "Interim Reports"), "low_stakes")
        self.assertEqual(qa_sync.stakes("Code of Conduct", "Policy Documents"), "high_stakes")

    def test_page_derivations(self):
        base = {"monitor": True, "type": "reports_hub", "url": "https://www.acme.com/ir/reports", "wba_id": "PT_1",
                "company_name": "Acme", "_id": "recP"}
        self.assertEqual(qa_sync.derive_page(dict(base, monitor=False, status="dead", http_status=404)), [])
        self.assertEqual(qa_sync.derive_page(dict(base, type="news", status="dead", http_status=404)), [])
        gone = qa_sync.derive_page(dict(base, status="dead", http_status=404))
        self.assertEqual([(c, d, k, s) for c, d, k, s, _ in gone], [("PAGE_GONE", "404", base["url"], "High")])
        # real cases of the 26 Oct review: a gone report list is High, a gone topic page Medium
        lst = qa_sync.derive_page(dict(base, type="sustainability_page", status="dead", http_status=404,
                                       url="https://www.hanwha.com/sustainability/reports.do"))
        self.assertEqual(lst[0][3], "High")
        topic = qa_sync.derive_page(dict(base, type="sustainability_page", status="dead", http_status=404,
                                         url="https://www.tescoplc.com/sustainability/cocoa"))
        self.assertEqual(topic[0][3], "Medium")
        ir = qa_sync.derive_page(dict(base, type="investor_relations", status="dead", http_status=404,
                                      url="https://www.safaricom.co.ke/investor-relations-category/annual-reports"))
        self.assertEqual(ir[0][3], "High")
        news = qa_sync.derive_page(dict(base, type="investor_relations", status="error",
                                        url="https://www.stifel.com/investor-relations/press-releases"))
        self.assertEqual(news[0][3], "Medium")
        err = qa_sync.derive_page(dict(base, type="policies", status="error"))
        self.assertEqual([(c, d, s) for c, d, k, s, _ in err], [("PAGE_UNREACHABLE", "no_response", "Medium")])
        blk = qa_sync.derive_page(dict(base, status="blocked", http_status=403))
        self.assertEqual([(c, d, k) for c, d, k, s, _ in blk], [("PAGE_BLOCKED", "403", "acme.com")])
        empty = qa_sync.derive_page(dict(base, status="ok", possible_issue="No document links found on the page"))
        self.assertEqual([(c, d) for c, d, k, s, _ in empty], [("PAGE_EMPTY", "no_links")])
        unh = qa_sync.derive_page(dict(base, status="ok", unharvested_links=5,
                                       unharvested_sample="https://a/esg-2025.pdf\nhttps://a/b.pdf"))
        self.assertEqual([(c, d, s) for c, d, k, s, _ in unh], [("PAGE_UNHARVESTED", "5", "High")])
        self.assertIn("esg-2025", unh[0][4]["message"])

    def test_doc_derivations(self):
        today = dt.date(2026, 10, 22)
        base = {"library_id": "PT_1-abc", "wba_id": "PT_1", "company_name": "Acme", "_id": "recD",
                "document_url": "https://acme.com/sustainability-report-2025.pdf", "created_time": "2026-09-01T00:00:00.000Z",
                "cover_status": "ok", "doc_language": "English"}
        # high-stakes disagreement, report shaped -> row + snapshot patch
        f = dict(base, source_type="Integrated Report", source_type_check="Sustainability Report", type_mismatch="1",
                 year=2025, source_title={"state": "generated", "value": "Integrated Annual Report 2025"})
        fails, labels, patch, counts = qa_sync.derive_doc(f, today)
        self.assertEqual([(c, d, s) for c, d, k, s, _ in fails], [("TYPE_DISAGREE", "high_stakes", "High")])
        self.assertEqual(patch, {"robot_type": "Sustainability Report", "robot_year": "2025",
                                 "robot_title": "Integrated Annual Report 2025"})
        self.assertEqual(labels, [])
        # low-stakes disagreement: counted, no row
        f = dict(base, source_type="Financial Statement", source_type_check="Interim Reports", type_mismatch="1", year=2025)
        fails, _, _, counts = qa_sync.derive_doc(f, today)
        self.assertEqual(fails, []); self.assertEqual(counts["disagree_low_stakes"], 1)
        # untyped non-English report-shaped row older than 7 days
        f = dict(base, document_url="https://acme.de/nachhaltigkeitsbericht-2024.pdf", doc_language="German")
        fails, _, patch, counts = qa_sync.derive_doc(f, today)
        self.assertEqual([(c, d) for c, d, k, s, _ in fails], [("TYPE_MISSING", "non_english")])
        self.assertEqual(patch, {})
        # untyped but not report-shaped: counted only
        f = dict(base, document_url="https://acme.com/media/12345.pdf", doc_language="German")
        fails, _, _, counts = qa_sync.derive_doc(f, today)
        self.assertEqual(fails, []); self.assertEqual(counts["untyped_non_english"], 1)
        # untyped, created 2 days ago: nothing yet
        f = dict(base, created_time="2026-10-21T00:00:00.000Z", doc_language="German")
        self.assertEqual(qa_sync.derive_doc(f, today)[0], [])
        # fetch failure on a 2023+ annual report -> High; on an old one -> Medium
        f = dict(base, source_type_check="Annual Report", year=2024, file_status="blocked")
        fails = qa_sync.derive_doc(f, today)[0]
        self.assertEqual([(c, d, s) for c, d, k, s, _ in fails], [("DOC_FETCH_FAIL", "blocked", "High")])
        f = dict(base, source_type_check="Annual Report", year=2019, file_status="gone", document_url="https://acme.com/annual-report.pdf")
        fails = qa_sync.derive_doc(f, today)[0]
        self.assertEqual(fails, [])   # not report-shaped: 2019 is before MIN_YEAR
        # unreadable
        f = dict(base, source_type_check="Sustainability Report", year=2025, cover_status="no_text")
        fails = qa_sync.derive_doc(f, today)[0]
        self.assertEqual([(c, d) for c, d, k, s, _ in fails], [("DOC_UNREADABLE", "no_text")])
        # discarded rows
        f = dict(base, discard=True, discard_reason="Duplicate of another document")
        fails = qa_sync.derive_doc(f, today)[0]
        self.assertEqual([(c, d, s) for c, d, k, s, _ in fails], [("DOC_DUPLICATE", "discard", "Low")])
        f = dict(base, discard=True, discard_reason="Wrong company / misattributed")
        self.assertEqual(qa_sync.derive_doc(f, today)[0][0][:2], ("DOC_WRONG_ROW", "misattributed"))

    def test_correction_capture(self):
        today = dt.date(2026, 10, 22)
        f = {"library_id": "PT_1-abc", "wba_id": "PT_1", "company_name": "Acme", "_id": "recD", "year": 2024,
             "document_url": "https://acme.com/annual-report-2024.pdf", "created_time": "2026-09-01T00:00:00.000Z",
             "cover_status": "ok", "doc_language": "English", "source_type": "Annual Report",
             "source_type_check": "Annual Report", "robot_type": "Statutory Documents",
             "robot_year": "2025", "robot_title": "Statutory report", "curated_doc_title": "Annual Report 2024"}
        fails, labels, patch, _ = qa_sync.derive_doc(f, today)
        self.assertEqual(sorted((c, d) for c, d, k, s, _ in fails),
                         [("LABEL_CORRECTION", "title"), ("LABEL_CORRECTION", "type"), ("LABEL_CORRECTION", "year")])
        self.assertEqual(patch, {})
        by_field = {l["field"]: l for l in labels}
        self.assertEqual(by_field["type"]["true_type"], "Annual Report")
        self.assertEqual(by_field["type"]["robot_value"], "Statutory Documents")
        self.assertEqual(by_field["year"]["true_value"], "2024")
        self.assertEqual(by_field["title"]["true_value"], "Annual Report 2024")
        self.assertTrue(all(l["labelled_by"] == "correction" and l["reviewed"] for l in labels))
        # different keys for the three facts
        self.assertEqual(len({qa.key(c, k, d) for c, d, k, s, _ in fails}), 3)

    def test_analyst_flags(self):
        today = dt.date(2026, 10, 22)
        base = {"library_id": "PT_1-abc", "wba_id": "PT_1", "company_name": "Acme", "_id": "recD", "year": 2025,
                "document_url": "https://acme.com/annual-report-2025.pdf", "created_time": "2026-09-01T00:00:00.000Z",
                "cover_status": "ok", "doc_language": "English", "source_type_check": "Annual Report"}
        # a costly flag on a report-shaped row is High, with the analyst's words as the message
        f = dict(base, analyst_flag="Wrong type", analyst_flag_note="It is the integrated report")
        fails, _, _, counts = qa_sync.derive_doc(f, today)
        self.assertEqual([(c, d, s) for c, d, k, s, _ in fails], [("DOC_FLAGGED", "wrong_type", "High")])
        self.assertEqual(fails[0][4]["message"], "Wrong type: It is the integrated report")
        self.assertEqual(counts["flag_wrong_type"], 1)
        # a duplicate flag is Medium; a flag on a row that is not report-shaped is Medium
        f = dict(base, analyst_flag="Duplicate")
        self.assertEqual(qa_sync.derive_doc(f, today)[0][0][1:4:2], ("duplicate", "Medium"))
        f = dict(base, document_url="https://acme.com/media/1.pdf", source_type_check="Company Presentations",
                 year=2019, analyst_flag="Wrong year")
        self.assertEqual([(c, s) for c, d, k, s, _ in qa_sync.derive_doc(f, today)[0]], [("DOC_FLAGGED", "Medium")])
        # a discarded row no longer counts as flagged (discarding resolved it)
        f = dict(base, analyst_flag="Not a disclosure", discard=True, discard_reason="Not a disclosure")
        self.assertEqual([c for c, d, k, s, _ in qa_sync.derive_doc(f, today)[0]], ["DOC_WRONG_ROW"])
        # the fix becomes a correction label that remembers the flag
        f = dict(base, analyst_flag="Wrong type", analyst_flag_note="integrated", robot_type="Annual Report",
                 source_type_check="Integrated Report")
        fails, labels, _, _ = qa_sync.derive_doc(f, today)
        self.assertEqual(sorted(c for c, d, k, s, _ in fails), ["DOC_FLAGGED", "LABEL_CORRECTION"])
        self.assertIn("an analyst had flagged it (Wrong type: integrated)", labels[0]["reason"])

    def test_company_gaps(self):
        f = {"wba_id": "PT_9", "company_name": "Zed", "_id": "recC",
             "gap_pages": ["Report hub page", "Sustainability report (recent)"], "overdue_reports": ["Annual report"],
             "gaps": "No report hub page watched.\nNo sustainability report from 2024 or later", "overdue_note": "Annual report 2025 expected"}
        out = qa_sync.derive_company(f)
        self.assertEqual([(d, s) for c, d, k, s, _ in out],
                         [("no_hub", "Medium"), ("no_sust_report", "High"), ("overdue_annual", "High")])
        self.assertTrue(all(k == "PT_9" for c, d, k, s, _ in out))
        self.assertEqual(qa_sync.derive_company({"company_name": "no id"}), [])



class RunPart(unittest.TestCase):
    def test_repeated_runs_in_one_job_get_their_own_row(self):
        os.environ["QA_RUN_PART"] = "2"
        try:
            self.assertTrue(qa.Run("pc_fetch.py").run_key.endswith(".2"))
        finally:
            del os.environ["QA_RUN_PART"]
        self.assertFalse(qa.Run("pc_fetch.py").run_key.endswith(".2"))


if __name__ == "__main__":
    unittest.main()
