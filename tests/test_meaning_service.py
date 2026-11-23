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
        site = INSTALL.split("\nsite() {")[1].split("\n}\n")[0]                                  # the page under the owner's name (19 Nov 2026)
        self.assertIn("Caddyfile.before-site", site); self.assertIn("Put back as it was.", site)   # an invalid configuration never stays
        self.assertIn('points_here "$HOST" ||', site); self.assertIn("run the workflow Search page first", site)
        self.assertIn("rewrite @home /search.html", INSTALL); self.assertIn("respond 405", INSTALL)
        wf = open(os.path.join(HERE, ".github", "workflows", "search_page.yml"), encoding="utf-8").read()
        self.assertIn("runs-on: [self-hosted, lantern]", wf); self.assertNotIn("pull_request", wf)
        self.assertEqual(wf.count("placeholders left in the extension"), 2)                        # both copies are built the same way
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


class Link(unittest.TestCase):
    def test_the_secret_is_never_shown_and_the_vault_is_written_by_name(self):
        import tempfile
        import meaning_link as L
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(f"{SECRET}\nshort\n{OTHER}\n")
        self.assertEqual(L.first_secret(f.name), OTHER)                                           # the newest line
        os.unlink(f.name)

        class Cur:
            def __init__(self, have): self.have, self.done, self.row = have, [], None
            def execute(self, sql, args=None):
                self.done.append((sql, args)); self.row = ("id-1",) if "from vault.secrets" in sql and args[0] in self.have else None
            def fetchone(self): return self.row
        cur = Cur({"meaning_remote_url"})
        self.assertEqual((L.keep(cur, "meaning_remote_url", "https://h.example", "d"), L.keep(cur, "meaning_remote_secret", SECRET, "d")), ("replaced", "made"))
        self.assertIn("vault.update_secret", cur.done[1][0]); self.assertIn("vault.create_secret", cur.done[3][0])
        src = open(os.path.join(HERE, "server", "meaning_link.py"), encoding="utf-8").read()
        self.assertNotRegex(src, r"print\([^)]*\bsecret\b(?!\))[^)]*\{secret\}")
        self.assertIn("({len(secret)} characters, not shown)", src)
        with self.assertRaises(SystemExit):
            L.main(["--link", "bad host; x"])

    def test_a_probe_is_reported_as_counts(self):
        import meaning_link as L
        line = L.probe_line({"ms": [10, 12, 30, 1500], "errors": {"no answer": 1}, "rows": 1200, "state": {"fails": 1, "skip_until": None}})
        self.assertIn("4 calls from Supabase to the server: 3 answered, 1 failed (no answer x1)", line)
        self.assertIn("median 21 ms, 95th percentile 1500 ms", line)
        self.assertIn("paused no", line)
        self.assertEqual(L.probe_line({"error": "no kept question vector"}), "probe not run: no kept question vector")
        self.assertIn("first three 10, 12, 30 ms", line)
        line = L.storm_line([(60.0, 24)] * 3, [(310.0, 24), (305.0, 20), (900.0, None)], [(40.0, 24), (50.0, 24), (45.0, 24)], {"fails": 3, "skip_until": "x"})
        self.assertIn("With the server asked first: 2 of 3 answered, median 310 ms, slowest 900 ms, pages a search 20 to 24.", line)
        self.assertIn("Without the server again: 3 of 3 answered, median 45 ms", line)
        self.assertIn("3) nothing in 24 ms, failures on record 3, paused", L.breaker_line([(25.0, False, 1, False), (24.0, False, 2, False), (24.0, False, 3, True)]))
        runs = L.storm.__code__.co_consts
        self.assertIn(("off", "on", "off"), runs)                                                 # the fair comparison: without, with, without again
        self.assertIn("paused yes", line)
        self.assertIn("'rehearsal')", L.SEARCH)


if __name__ == "__main__":
    unittest.main()


class TheWebServerLetsTheDocsThrough(unittest.TestCase):
    def test_both_routes_are_passed_on(self):
        sh = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "service_install.sh"), encoding="utf-8").read()
        self.assertIn("path /near /docs", sh)          # 22 Nov 2026: the service answered /docs and the web server in front of it did not pass it on


class Documentation(unittest.TestCase):
    """The documentation path of the door: behind the same secret, plain names only, the database untouched."""
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        for name, text in (("README.md", "intro\n# The index\nbody"), ("server.md", "# The server\ntext"), ("notes.txt", "no"), (".hidden.md", "no")):
            open(os.path.join(self.dir, name), "w", encoding="utf-8").write(text)
        os.symlink("/etc/hostname", os.path.join(self.dir, "link.md"))
        self.old = S.DOCS_DIR; S.DOCS_DIR = self.dir

    def tearDown(self):
        S.DOCS_DIR = self.old

    def test_the_list_and_one_document(self):
        got = S.docs_answer(b"{}")
        self.assertEqual([(d["name"], d["title"]) for d in got["docs"]], [("README.md", "The index"), ("server.md", "The server")])   # not the text file, the hidden one or the link
        self.assertEqual(S.docs_answer(b'{"name": "server.md"}')["text"], "# The server\ntext")
        self.assertTrue(S.docs_answer(b'{"name": "absent.md"}')["missing"])
        self.assertTrue(S.docs_answer(b'{"name": "link.md"}')["missing"])                    # a link is never followed

    def test_only_a_plain_name_is_taken(self):
        for bad in (b'{"name": "../etc/passwd"}', b'{"name": "a/b.md"}', b'{"name": "x.txt"}', b'{"name": 5}', b'{"name": "a.md", "more": 1}', b"[]", b"not json", b'{"name": "..md"}'):
            with self.assertRaises(S.Refused, msg=bad):
                S.docs_answer(bad)

    def test_the_door_asks_for_the_secret_and_never_the_database(self):
        class NoDatabase:
            def run(self, *a, **k): raise AssertionError("the database was asked")
        (status, kind, data), _, logs = ask(path="/docs", body=b"{}", pool=NoDatabase())
        self.assertEqual((status, kind), (200, "application/json")); self.assertEqual(len(json.loads(data)["docs"]), 2)
        self.assertEqual(ask(path="/docs", body=b"{}", secret="wrong" * 8, pool=NoDatabase())[0][0], 401)
        self.assertEqual(ask(path="/docs", body=b'{"name": "../x.md"}', pool=NoDatabase())[0][0], 400)
        self.assertEqual(ask(method="GET", path="/docs", pool=NoDatabase())[0][0], 404)
        self.assertNotIn("The index", " ".join(str(x) for l in logs for x in l))                # the log holds counts, not text
