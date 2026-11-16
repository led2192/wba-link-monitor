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
               good(types=["a" * 81]), good(types=[1]), good(other=1), good(companies=["a"] * 501), good(limit=None),
               b'{"vec": ' + b"[" * 100000 + b"]" * 100000 + b"}",
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
        (status, _, _), pool, _ = ask(body=good(companies=[f"PT_{i}" for i in range(51)]))
        self.assertNotIn("+ 0 as dist", pool.calls[0][0])                                          # many companies: the index with the filter, not an exact search
        self.assertIn("where f.live and f.wba_id = any(%(ids)s) and", pool.calls[0][0])
        (_, _, data), _, _ = ask(body=good(), pool=FakePool(rows=[("B", 1, 0, 0.5), ("A", 2, 0, 0.1)]))
        self.assertEqual([r[0] for r in json.loads(data)["rows"]], ["A", "B"])                     # sorted before it is sent
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
        self.assertNotRegex(INSTALL, r"pip3? install")                                            # the library comes from the system's packages
        self.assertIn('if ! sudo test -s "$ETC/meaning.env"; then', INSTALL)                      # a second install keeps the password: no gap
        self.assertIn("method POST", INSTALL)
        self.assertNotIn("path /health", INSTALL)                                                 # the health route is not passed on by the web server
        self.assertIn("caddy validate", INSTALL)
        self.assertIn("Nothing opened.", INSTALL)                                                 # the name must point here before a certificate is asked for
        for guard in ("IPAddressDeny=any", "TasksMax=200", "CapabilityBoundingSet=", "connection limit 6"):
            self.assertIn(guard, INSTALL)
        self.assertNotRegex(INSTALL, r"echo .*\$PW")                                               # the password is never printed
        for line in INSTALL.splitlines():
            if "ufw allow" in line:
                self.assertRegex(line, r"ufw allow (80|443)/tcp")


class OnASocket(unittest.TestCase):
    """The handler on a real socket: what a caller can do before and around handle()."""
    @classmethod
    def setUpClass(cls):
        import tempfile, threading
        from http.server import ThreadingHTTPServer
        f = tempfile.NamedTemporaryFile("w", delete=False); f.write(SECRET + "\n"); f.close()
        cls.secrets, cls.pool = f.name, FakePool(rows=[("L1", 3, 0, 0.1)])
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(cls.pool, f.name, S.Rate(1000, 1000), "meaning", lambda *a: None))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); os.unlink(cls.secrets)

    def raw(self, text, wait=3):
        import socket
        with socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=wait) as c:
            c.sendall(text if isinstance(text, bytes) else text.encode())
            data = b""
            try:
                while True:
                    part = c.recv(65536)
                    if not part:
                        break
                    data += part
            except OSError:
                pass
        return data

    def test_a_good_request(self):
        body = good().decode()
        out = self.raw(f"POST /near HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {SECRET}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}")
        self.assertTrue(out.startswith(b"HTTP/1.1 200"))
        self.assertIn(b'{"rows":[["L1",3,0,0.1]]}', out)
        self.assertIn(b"Server: lantern", out)                                                    # no version of anything

    def test_without_the_secret_no_body_is_waited_for(self):
        import time
        t = time.time()
        out = self.raw("POST /near HTTP/1.1\r\nHost: x\r\nContent-Length: 60000\r\n\r\n")      # says a body is coming and sends none
        self.assertTrue(out.startswith(b"HTTP/1.1 401"))
        self.assertLess(time.time() - t, 2)                                                       # answered at once, connection closed
        self.assertIn(b"Connection: close", out.replace(b"connection", b"Connection"))

    def test_other_methods_and_bodies_with_no_length_close_the_connection(self):
        auth = f"Authorization: Bearer {SECRET}\r\n"
        smuggle = "GET /health HTTP/1.1\r\nHost: x\r\n\r\n"
        for first, status in ((f"DELETE /near HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n\r\nabcd", b"405"),
                              (f"PUT /near HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: 4\r\n\r\nabcd", b"405"),
                              (f"POST /near HTTP/1.1\r\nHost: x\r\n{auth}Transfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n", b"411"),
                              (f"POST /near HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: -5\r\n\r\n", b"411"),
                              (f"POST /near HTTP/1.1\r\nHost: x\r\n{auth}\r\n", b"411"),
                              (f"POST /near HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: 99999999\r\n\r\n", b"413")):
            out = self.raw(first + smuggle)
            self.assertTrue(out.startswith(b"HTTP/1.1 " + status), out[:40])
            self.assertEqual(out.count(b"HTTP/1.1 "), 1, first[:30])                              # what follows on the connection is never read as a request


class NoOutsideCodeOnTheServer(unittest.TestCase):
    def test_no_workflow_that_a_pull_request_starts_runs_on_the_server(self):
        """The runners on the server can act as root there: a job started by a pull request must never reach them."""
        import glob, re
        for path in glob.glob(os.path.join(HERE, ".github", "workflows", "*.yml")):
            text = open(path, encoding="utf-8").read()
            if re.search(r"^\s*pull_request(_target)?\s*:", text, re.M) or re.search(r"on:\s*\[[^\]]*pull_request", text):
                self.assertNotIn("self-hosted", text, path)
                self.assertNotIn("LANTERN_JOBS", text, path)


if __name__ == "__main__":
    unittest.main()
