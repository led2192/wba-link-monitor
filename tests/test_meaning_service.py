import json, os, sys, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "server"))
import meaning_service as S

INSTALL = open(os.path.join(HERE, "server", "service_install.sh"), encoding="utf-8").read()
SECRET = "s" * 40
OTHER = "t" * 40


class FakePool:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or [], fail, []

    def run(self, sql, args=None, wait=2.0):
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("connection to 10.0.0.9 refused: select secret from /etc/x")
        return [(1,)] if sql == "select 1" else self.rows


def ask(method="POST", path="/near", body=None, secret=SECRET, pool=None, rate=None, secrets=(SECRET,), who="a"):
    headers = {"Authorization": f"Bearer {secret}"} if secret is not None else {}
    logs = []
    pool = pool or FakePool(rows=[("L1", 3, 0, 0.123456789), ("L2", 9, 1, 0.2)])
    out = S.handle(method, path, headers, body, pool=pool, secrets=lambda: list(secrets), rate=rate or S.Rate(100, 100), schema="meaning", who=who, log=lambda *a: logs.append(a))
    return out, pool, logs


def good(**extra):
    return json.dumps({"vec": [0.01] * 384, **extra}).encode()


class Service(unittest.TestCase):
    def test_only_two_routes_answer(self):
        self.assertEqual(ask("GET", "/health", secret=None)[0], (200, "text/plain", b"ok"))
        for method, path in (("GET", "/near"), ("POST", "/health"), ("GET", "/"), ("POST", "/admin"), ("DELETE", "/near")):
            self.assertEqual(ask(method, path, good())[0][0], 404)
        self.assertEqual(ask("GET", "/health", secret=None, pool=FakePool(fail=True))[0], (503, "text/plain", b"unavailable"))

    def test_the_secret_two_at_once_and_never_an_empty_one(self):
        self.assertEqual(ask(body=good())[0][0], 200)
        self.assertEqual(ask(body=good(), secret=OTHER, secrets=(SECRET, OTHER))[0][0], 200)       # the new one beside the old: no gap
        for wrong in (None, "", "x" * 40, SECRET[:-1], SECRET + "x"):
            self.assertEqual(ask(body=good(), secret=wrong)[0][0], 401)
        self.assertEqual(ask(body=good(), secret="", secrets=())[0][0], 401)                       # no secret kept: nobody enters
        self.assertFalse(S.allowed("Bearer ", [""]))
        self.assertFalse(S.allowed(SECRET, [SECRET]))                                              # the scheme is part of it

    def test_the_secrets_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(f"{SECRET}\n\nshort\n{OTHER}\n{'u' * 40}\n")
        self.assertEqual(S.read_secrets(f.name), [SECRET, OTHER])                                  # two at most, short lines ignored
        self.assertEqual(S.read_secrets("/nonexistent/file"), [])
        os.unlink(f.name)

    def test_inputs_are_checked_exactly(self):
        bad = [b"", b"not json", b"[]", json.dumps({"vec": [0.1] * 383}).encode(), json.dumps({"vec": ["a"] * 384}).encode(),
               json.dumps({"vec": [True] * 384}).encode(), json.dumps({"vec": [1e9] * 384}).encode(), good(limit=0), good(limit=401), good(limit="5"),
               good(year_from=1800), good(year_to="2024"), good(companies=[]), good(companies=["ok", "x'; drop table"]), good(companies="PT_1"),
               good(types=["a" * 81]), good(types=[1]), good(other=1), good(companies=["a"] * 501),
               b'{"vec": [' + b"NaN," * 383 + b"NaN]}"]
        for body in bad:
            (status, kind, data), pool, _ = ask(body=body)
            self.assertEqual((status, data), (400, b"bad request"), body[:40])
            self.assertEqual(pool.calls, [])                                                       # nothing reaches the database
        self.assertEqual(ask(body=None)[0][0], 413)                                                # a body too large is never read

    def test_the_search_and_what_comes_back(self):
        (status, kind, data), pool, logs = ask(body=good(year_from=2024, year_to=2025, types=["Annual Report"], limit=50))
        self.assertEqual((status, kind), (200, "application/json"))
        self.assertEqual(json.loads(data), {"rows": [["L1", 3, 0, 0.123457], ["L2", 9, 1, 0.2]]})  # keys and distances, never text
        sql, args = pool.calls[0]
        self.assertIn("join meaning.chunk_filters f on f.library_id = c.library_id where f.live and", sql)   # filtered while the index is walked
        self.assertIn("order by c.embedding <=> %(q)s::halfvec(384) limit %(limit)s", sql)
        self.assertEqual((args["y0"], args["y1"], args["types"], args["limit"], args["ids"]), (2024, 2025, ["Annual Report"], 50, None))
        self.assertTrue(args["q"].startswith("[0.01,0.01,"))
        (status, _, _), pool, _ = ask(body=good(companies=["PT_00123", "PT_9"]))
        self.assertIn("f.wba_id = any(%(ids)s)", pool.calls[0][0])
        self.assertIn("+ 0 as dist", pool.calls[0][0])                                             # exact inside the companies' documents
        self.assertNotIn("0.01", " ".join(map(str, logs)))                                         # the vector is never logged

    def test_errors_say_nothing(self):
        (status, kind, data), _, logs = ask(body=good(), pool=FakePool(fail=True))
        self.assertEqual((status, data), (503, b"unavailable"))                                   # no address, path or SQL to the caller
        self.assertIn("near failed", logs[0])

    def test_the_rate_limit(self):
        clock = [0.0]
        rate = S.Rate(2, 3, now=lambda: clock[0])
        self.assertEqual([rate.take("a"), rate.take("a"), rate.take("a"), rate.take("b"), rate.take("c")], [True, True, False, True, False])
        clock[0] = 61.0
        self.assertTrue(rate.take("a"))                                                            # a new minute
        self.assertEqual(ask(body=good(), rate=S.Rate(0, 10))[0][0], 429)

    def test_the_installer_keeps_the_door_shut_until_asked(self):
        install = INSTALL.split("install() {")[1].split("\n}\n")[0]
        self.assertNotIn("ufw allow", install)                                                     # install opens nothing
        self.assertIn("systemctl disable --now caddy", install)
        self.assertIn("grant select on meaning.chunks, meaning.chunk_filters to meaning_read;", INSTALL)
        self.assertNotRegex(INSTALL, r"grant (insert|update|delete|all)")
        self.assertIn("chmod 640", INSTALL)
        self.assertIn('ThreadingHTTPServer(("127.0.0.1"', open(os.path.join(HERE, "server", "meaning_service.py")).read())
        self.assertNotIn("5432/tcp", INSTALL)
        self.assertNotRegex(INSTALL, r"echo .*\$PW")                                               # the password is never printed
        for line in INSTALL.splitlines():
            if "ufw allow" in line:
                self.assertRegex(line, r"ufw allow (80|443)/tcp")


if __name__ == "__main__":
    unittest.main()
