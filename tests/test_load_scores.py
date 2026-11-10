import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import load_scores as L  # noqa: E402


class Resp:
    def __init__(self, status, body): self.status_code, self._b = status, body
    def json(self): return self._b


class Session:
    def __init__(self, resp): self.resp, self.calls = resp, []
    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, headers, params)); return self.resp


class Api(unittest.TestCase):
    def test_get_sends_the_key_as_a_bearer_and_waits(self):
        s, waits = Session(Resp(200, {"data": [{"company_id": "PT_1"}], "meta": {"total": 1}})), []
        out = L.get("/datasets/benchmarks", "k", {"per_page": 2}, session=s, sleep=waits.append)
        self.assertEqual(s.calls[0][0], L.BASE + "/datasets/benchmarks"); self.assertEqual(s.calls[0][1], {"Authorization": "Bearer k"})
        self.assertEqual(waits, [L.PAUSE]); self.assertEqual(L.rows_of(out), ([{"company_id": "PT_1"}], {"total": 1}))

    def test_a_refusal_says_the_status_and_never_the_key(self):
        s = Session(Resp(401, {"detail": "Not authenticated"}))
        with self.assertRaises(RuntimeError) as e:
            L.get("/datasets", "secret-key", session=s, sleep=lambda x: None)
        self.assertIn("401", str(e.exception)); self.assertIn("Not authenticated", str(e.exception)); self.assertNotIn("secret-key", str(e.exception))

    def test_the_contact_column_is_never_asked_for(self):
        for cols in (L.BENCH_COLS, L.AREA_COLS):
            self.assertFalse([c for c in cols if "poc" in c or "contact" in c or "name" == c])
        src = open(L.__file__, encoding="utf-8").read()
        self.assertNotIn("json.dumps(rows", src)             # no row content in the output

    def test_pages_until_a_short_one_with_only_the_named_columns(self):
        class Paged:
            def __init__(self): self.calls = []
            def get(self, url, headers=None, params=None, timeout=None):
                self.calls.append(params); n = 1000 if params["page"] == 1 else 3
                return Resp(200, {"data": [{"benchmark_score_id": f"s{params['page']}-{i}", "company_id": "PT_1"} for i in range(n)], "meta": {}})
        s = Paged(); rows = L.fetch_all("benchmarks", L.BENCH_COLS, "k", "benchmark_score_id", session=s, sleep=lambda x: None)
        self.assertEqual(len(rows), 1003); self.assertEqual(len(s.calls), 2)
        self.assertEqual(s.calls[0]["include"], ",".join(L.BENCH_COLS)); self.assertEqual(s.calls[0]["per_page"], 1000)

    def test_rows_keep_the_id_the_company_and_the_number(self):
        r = L.bench_rows([{"benchmark_score_id": "PT_1-CSI-2025", "company_id": "PT_1", "benchmark_id": "CSI", "benchmark_name": "Social Benchmark",
                           "methodology_id": "CSI-2025", "methodology_year": "2025", "benchmark_score_numerical": 23.6}, {"company_id": "PT_2"}])
        self.assertEqual(len(r), 1); self.assertEqual(r[0][:2], ("PT_1-CSI-2025", "PT_1")); self.assertEqual(r[0][5], 2025); self.assertEqual(r[0][7], 23.6)
        a = L.area_rows([{"measurement_area_score_id": "PT_1-NAB-2025_C", "company_id": "PT_1", "methodology_year": "2025", "measurement_area_score_numerical": "0.0"}])
        self.assertEqual(a[0][4], 2025); self.assertEqual(a[0][7], 0.0); self.assertIsNone(L.num("n/a")); self.assertIsNone(L.year_of(None))


if __name__ == "__main__":
    unittest.main()
