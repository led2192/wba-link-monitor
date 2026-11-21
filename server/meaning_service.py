#!/usr/bin/env python3
"""
meaning_service.py: the one small door in front of the server's database of passages (docs/server.md, "The service").

Supabase's search asks it for the passages nearest in meaning to a question; it answers with keys and distances,
never text. It listens on the machine itself only (127.0.0.1); the web server in front of it holds the certificate
and is the only thing the world reaches.

Two routes, and nothing else answers:
  GET  /health   "ok" when the database answers within a second. No secret needed; says nothing else. The web server
                 in front does not pass it on: it answers on the machine itself only, for the checker.
  POST /near     the nearest passages. Needs the secret (Authorization: Bearer ...). Body, JSON:
                   vec        384 numbers (required)
                   companies  a list of company ids, or absent
                   year_from, year_to   whole numbers, or absent
                   types      a list of document types, or absent
                   limit      1 to 400 (default 400)
                 Answer: {"rows": [[library_id, page, n, distance], ...]} nearest first.
                 With up to 50 companies: an exact search inside their documents. Otherwise the index, with the
                 companies, the year and the type applied while it is walked (pgvector's iterative scan), so a narrow
                 filter fills its list as far as the scan's own ceiling allows; the caller must not assume a full list.
                 The rows are sorted by distance before they are sent.

Guards:
  - the secret is compared in constant time; two secrets may be valid at once (the file holds one a line), so one can
    be replaced without a gap
  - every input is checked exactly; anything else is refused with 400 and no detail
  - the database role can only read the two tables; every statement stops after 2 seconds; a small fixed pool
  - a limit of requests a minute for each caller and for all callers together
  - the secret is checked before a body is read; a body of more than 64 KB, or one with no stated length, is refused
    unread and the connection closed, as it is after every answer that is not a success
  - any other method is refused and the connection closed
  - errors never carry an address, a path or SQL; the reason stays in the service's own log, without the vector

Env: MEANING_DB_URL (the read-only role), MEANING_SECRETS (path of the secrets file), MEANING_PORT (default 8377),
MEANING_SCHEMA (default meaning), MEANING_RATE (requests a minute for each caller, default 600), MEANING_RATE_ALL
(all callers, default 3000).
"""
import hmac, json, math, os, queue, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSIONS = 384
MAX_BODY = 64 * 1024
MAX_LIMIT = 400
MAX_IDS = 500
MAX_TYPES = 40
EXACT_IDS = 50                   # more companies than this: the index with the filter, not an exact search (it would not end in 2 s)
POOL = 4
EF_SEARCH = 400                  # the index looks at as many candidates as passages are asked for
ID = re.compile(r"^[A-Za-z0-9_.:\- ]{1,64}$")
TYPE = re.compile(r"^[A-Za-z0-9_.,&/()' \-]{1,80}$")


DOCS_DIR = os.environ.get("MEANING_DOCS", "/srv/lantern/docs")     # the project's documentation, copied here by the workflow Server docs
DOC_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}\.md$", re.I)
MAX_DOC = 600 * 1024


def docs_answer(body, folder=None):
    """The documentation kept on the server, for the owner's panel. {} lists the documents (name, first heading, size,
    time changed); {"name": "x.md"} gives one. Only a plain file name of the folder is ever opened."""
    folder = folder or DOCS_DIR
    try:
        j = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise Refused()
    if not isinstance(j, dict) or set(j) - {"name"}:
        raise Refused()
    name = j.get("name")
    if name is None:
        out = []
        try:
            listed = sorted(os.listdir(folder))
        except OSError:
            listed = []
        for n in listed:
            path = os.path.join(folder, n)
            if not DOC_NAME.match(n) or not os.path.isfile(path) or os.path.islink(path):
                continue
            title = n
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("# "):
                            title = line[2:].strip()[:120]
                            break
                st = os.stat(path)
                out.append({"name": n, "title": title, "size": st.st_size, "changed": int(st.st_mtime)})
            except OSError:
                continue
        return {"docs": out}
    if not isinstance(name, str) or not DOC_NAME.match(name):
        raise Refused()
    path = os.path.join(folder, name)
    if os.path.islink(path) or not os.path.isfile(path):
        return {"name": name, "missing": True}
    with open(path, encoding="utf-8", errors="replace") as fh:
        return {"name": name, "text": fh.read(MAX_DOC)}


class Refused(Exception):
    """An input that is not exactly what the route takes."""


def read_secrets(path):
    """The valid secrets: the non-empty lines of the file, at most two, each of 32 characters or more."""
    try:
        lines = [l.strip() for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    except OSError:
        return []
    return [l for l in lines if len(l) >= 32][:2]


def allowed(header, secrets):
    """True when the Authorization header carries one of the secrets. Every secret is compared, in constant time."""
    given = (header or "")
    given = given[7:] if given.startswith("Bearer ") else ""
    ok = False
    for s in secrets:
        ok = hmac.compare_digest(given.encode(), s.encode()) or ok
    return ok and bool(given)


def whole(v, low, high):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise Refused()
    return v


def names(v, pattern, most):
    if v is None:
        return None
    if not isinstance(v, list) or not 1 <= len(v) <= most or not all(isinstance(x, str) and pattern.match(x) for x in v):
        raise Refused()
    return v


def parse(raw):
    """The request, checked exactly: a dict with q (the vector as pgvector text), ids, y0, y1, types, limit."""
    try:
        j = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Refused()
    if not isinstance(j, dict) or set(j) - {"vec", "companies", "year_from", "year_to", "types", "limit"}:
        raise Refused()
    vec = j.get("vec")
    if not isinstance(vec, list) or len(vec) != DIMENSIONS:
        raise Refused()
    for x in vec:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > 1e3:
            raise Refused()
    limit = whole(j.get("limit", MAX_LIMIT), 1, MAX_LIMIT)
    if limit is None:                                   # "limit": null would be no limit at all
        raise Refused()
    return dict(q="[" + ",".join(repr(float(x)) for x in vec) + "]", ids=names(j.get("companies"), ID, MAX_IDS),
                y0=whole(j.get("year_from"), 1900, 2100), y1=whole(j.get("year_to"), 1900, 2100),
                types=names(j.get("types"), TYPE, MAX_TYPES), limit=limit)


def near_sql(schema, ids):
    """The search. The schema is the service's own setting, never the caller's. ids: how many companies were named."""
    with_companies = bool(ids) and ids <= EXACT_IDS
    by_company = "f.wba_id = any(%(ids)s) and " if ids else ""
    q = f"%(q)s::halfvec({DIMENSIONS})"
    cond = ("(%(y0)s::int is null or f.year >= %(y0)s) and (%(y1)s::int is null or f.year <= %(y1)s) "
            "and (%(types)s::text[] is null or f.doc_type = any(%(types)s))")
    if with_companies:
        return (f"select c.library_id, c.page, c.n, (c.embedding <=> {q}) + 0 as dist from {schema}.chunks c "
                f"where c.library_id in (select f.library_id from {schema}.chunk_filters f where f.live and f.wba_id = any(%(ids)s) and {cond}) "
                f"order by dist limit %(limit)s")
    return (f"select c.library_id, c.page, c.n, (c.embedding <=> {q}) as dist from {schema}.chunks c "
            f"join {schema}.chunk_filters f on f.library_id = c.library_id where f.live and {by_company}{cond} "
            f"order by c.embedding <=> {q} limit %(limit)s")


class Rate:
    """Requests a minute: one count a caller and one for all, in windows of a minute."""
    def __init__(self, each, everyone, now=time.time):
        self.each, self.everyone, self.now, self.lock = each, everyone, now, threading.Lock()
        self.window, self.counts, self.total = 0, {}, 0

    def take(self, who):
        with self.lock:
            w = int(self.now() // 60)
            if w != self.window:
                self.window, self.counts, self.total = w, {}, 0
            if self.total >= self.everyone or self.counts.get(who, 0) >= self.each:
                return False
            self.total += 1
            self.counts[who] = self.counts.get(who, 0) + 1
            return True


class Pool:
    """A few connections, each read-only with a 2 second limit a statement; one that fails is replaced."""
    def __init__(self, url, size=POOL, connect=None):
        self.url, self.q = url, queue.Queue()
        self.connect = connect or self._connect
        for _ in range(size):
            self.q.put(None)

    def _connect(self):
        import psycopg
        c = psycopg.connect(self.url, autocommit=True, connect_timeout=3, prepare_threshold=None)   # never a prepared, generic plan: it could leave the index
        c.execute("set statement_timeout = '2s'")
        c.execute("set default_transaction_read_only = on")
        c.execute(f"set hnsw.ef_search = {EF_SEARCH}")
        c.execute("set hnsw.iterative_scan = relaxed_order")
        return c

    def run(self, sql, args=None, wait=2.0):
        try:
            c = self.q.get(timeout=wait)
        except queue.Empty:
            raise TimeoutError("no connection free")
        try:
            for attempt in (1, 2):
                fresh = c is None
                try:
                    c = c or self.connect()
                    return c.execute(sql, args).fetchall()
                except Exception as e:
                    try:
                        c and c.close()
                    except Exception:
                        pass
                    c = None
                    # a connection kept from before the database restarted fails once: try again on a new one
                    if fresh or attempt == 2 or type(e).__name__ not in ("OperationalError", "InterfaceError"):
                        raise
        finally:
            self.q.put(c)


def handle(method, path, headers, body, *, pool, secrets, rate, schema, who="-", log=lambda *a: None):
    """One request -> (status, content type, bytes). Everything the service decides is here, so it is tested whole."""
    plain = lambda status, text: (status, "text/plain", text.encode())
    if path == "/health" and method == "GET":
        try:
            pool.run("select 1", wait=1.0)
            return plain(200, "ok")
        except Exception as e:
            log("health", type(e).__name__)
            return plain(503, "unavailable")
    if path not in ("/near", "/docs") or method != "POST":
        return plain(404, "not found")
    if not allowed(headers.get("Authorization"), secrets()):
        return plain(401, "not allowed")
    if not rate.take(who):
        return plain(429, "too many requests")
    if body is None:
        return plain(413, "too large")
    if path == "/docs":                                  # the documentation, behind the same secret; the database is not touched
        try:
            got = docs_answer(body)
        except Refused:
            return plain(400, "bad request")
        except Exception as e:
            log("docs failed", type(e).__name__)
            return plain(503, "unavailable")
        log("docs", "one" if "text" in got or got.get("missing") else len(got.get("docs", [])))
        return (200, "application/json", json.dumps(got, separators=(",", ":")).encode())
    try:
        args = parse(body)
    except Refused:
        return plain(400, "bad request")
    t = time.time()
    try:
        rows = sorted(pool.run(near_sql(schema, len(args["ids"] or [])), args), key=lambda r: r[3])   # the index's walk is not strictly in order
    except Exception as e:
        log("near failed", type(e).__name__, str(e)[:200].replace("\n", " "))
        return plain(503, "unavailable")
    log("near", len(rows), f"{(time.time() - t) * 1000:.0f}ms", "companies" if args["ids"] else "all")
    out = json.dumps({"rows": [[r[0], r[1], r[2], round(float(r[3]), 6)] for r in rows]}, separators=(",", ":"))
    return (200, "application/json", out.encode())


def make_handler(pool, secrets_path, rate, schema, log):
    """The request handler over a pool, a secrets file and a rate limit (made here so it can be tested on a real socket)."""
    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version, protocol_version = "lantern", "", "HTTP/1.1"
        timeout = 10

        def answer(self, body, status=None):
            who = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()[:64]
            if status:
                kind, data = "text/plain", b"refused"
            else:
                status, kind, data = handle(self.command, self.path.split("?")[0], self.headers, body, pool=pool,
                                            secrets=lambda: read_secrets(secrets_path), rate=rate, schema=schema, who=who, log=log)
            if status != 200:
                self.close_connection = True               # nothing unread is left for a next request on this connection
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if status != 200:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.answer(None)

        def do_POST(self):
            # the secret first: nobody without it makes the service wait for a body
            if self.path.split("?")[0] == "/near" and not allowed(self.headers.get("Authorization"), read_secrets(secrets_path)):
                return self.answer(None, 401)
            length = self.headers.get("Content-Length")
            if self.headers.get("Transfer-Encoding") or length is None or not length.isdigit():
                return self.answer(None, 411)              # a body with no stated length is never read
            n = int(length)
            if n > MAX_BODY:
                return self.answer(None)                   # too large: not read at all
            self.answer(self.rfile.read(n) if n else b"")

        def refuse(self):
            self.answer(None, 405)

        do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = refuse

        def log_message(self, *a):
            pass                                           # the service logs what it decided, never request lines

    return Handler


def serve():
    url, secrets_path = os.environ.get("MEANING_DB_URL"), os.environ.get("MEANING_SECRETS")
    if not url or not secrets_path:
        sys.exit("Set MEANING_DB_URL and MEANING_SECRETS.")
    schema = os.environ.get("MEANING_SCHEMA", "meaning")
    if not re.fullmatch(r"[a-z_]{1,30}", schema):
        sys.exit("MEANING_SCHEMA: small letters and underscores.")
    pool = Pool(url)
    rate = Rate(int(os.environ.get("MEANING_RATE", "600")), int(os.environ.get("MEANING_RATE_ALL", "3000")))
    log = lambda *a: print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("MEANING_PORT", "8377"))), make_handler(pool, secrets_path, rate, schema, log)).serve_forever()


if __name__ == "__main__":
    serve()
