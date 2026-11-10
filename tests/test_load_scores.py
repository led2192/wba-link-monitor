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

    def test_columns_in_either_shape(self):
        self.assertEqual(L.describe([{"name": "a", "type": "str"}]), ["a (str)"])
        self.assertEqual(L.describe({"cols": [{"name": "b", "type": "float"}]}), ["b (float)"])
        self.assertEqual(L.rows_of([{"x": 1}]), ([{"x": 1}], {}))


if __name__ == "__main__":
    unittest.main()
