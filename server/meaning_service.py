#!/usr/bin/env python3
"""
meaning_service.py: the one small door in front of the server's database of passages (docs/server.md, "The service").

Supabase's search asks it for the passages nearest in meaning to a question; it answers with keys and distances,
never text. It listens on the machine itself only (127.0.0.1); the web server in front of it holds the certificate
and is the only thing the world reaches.

Two routes, and nothing else answers:
  GET  /health   "ok" when the database answers within a second. No secret needed; says nothing else.
  POST /near     the nearest passages. Needs the secret (Authorization: Bearer ...). Body, JSON:
                   vec        384 numbers (required)
                   companies  a list of company ids, or absent
                   year_from, year_to   whole numbers, or absent
                   types      a list of document types, or absent
                   limit      1 to 400 (default 400)
                 Answer: {"rows": [[library_id, page, n, distance], ...]} nearest first.
                 With companies: an exact search inside those companies' documents. Without: the index, with the
                 year and the type applied while it is walked (pgvector's iterative scan), so a narrow filter still
                 fills its list.

Guards:
  - the secret is compared in constant time; two secrets may be valid at once (the file holds one a line), so one can
    be replaced without a gap
  - every input is checked exactly; anything else is refused with 400 and no detail
  - the database role can only read the two tables; every statement stops after 2 seconds; a small fixed pool
  - a limit of requests a minute for each caller and for all callers together
  - a body of more than 64 KB is refused before it is read
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
POOL = 4
EF_SEARCH = 400                  # the index looks at as many candidates as passages are asked for
ID = re.compile(r"^[A-Za-z0-9_.:\- ]{1,64}$")
TYPE = re.compile(r"^[A-Za-z0-9_.,&/()' \-]{1,80}$")


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
    except (ValueError, UnicodeDecodeError):
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
    return dict(q="[" + ",".join(repr(float(x)) for x in vec) + "]", ids=names(j.get("companies"), ID, MAX_IDS),
                y0=whole(j.get("year_from"), 1900, 2100), y1=whole(j.get("year_to"), 1900, 2100),
                types=names(j.get("types"), TYPE, MAX_TYPES), limit=limit)


def near_sql(schema, with_companies):
    """The search. The schema is the service's own setting, never the caller's."""
    q = f"%(q)s::halfvec({DIMENSIONS})"
    cond = ("(%(y0)s::int is null or f.year >= %(y0)s) and (%(y1)s::int is null or f.year <= %(y1)s) "
            "and (%(types)s::text[] is null or f.doc_type = any(%(types)s))")
    if with_companies:
        return (f"select c.library_id, c.page, c.n, (c.embedding <=> {q}) + 0 as dist from {schema}.chunks c "
                f"where c.library_id in (select f.library_id from {schema}.chunk_filters f where f.live and f.wba_id = any(%(ids)s) and {cond}) "
                f"order by dist limit %(limit)s")
    return (f"select c.library_id, c.page, c.n, (c.embedding <=> {q}) as dist from {schema}.chunks c "
            f"join {schema}.chunk_filters f on f.library_id = c.library_id where f.live and {cond} "
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
        c = psycopg.connect(self.url, autocommit=True, connect_timeout=3)
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
            c = c or self.connect()
            rows = c.execute(sql, args).fetchall()
        except Exception:
            try:
                c and c.close()
            except Exception:
                pass
            c = None
            raise
        finally:
            self.q.put(c)
        return rows


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
    if path != "/near" or method != "POST":
        return plain(404, "not found")
    if not allowed(headers.get("Authorization"), secrets()):
        return plain(401, "not allowed")
    if not rate.take(who):
        return plain(429, "too many requests")
    if body is None:
        return plain(413, "too large")
    try:
        args = parse(body)
    except Refused:
        return plain(400, "bad request")
    t = time.time()
    try:
        rows = pool.run(near_sql(schema, args["ids"] is not None), args)
    except Exception as e:
        log("near failed", type(e).__name__, str(e)[:200].replace("\n", " "))
        return plain(503, "unavailable")
    log("near", len(rows), f"{(time.time() - t) * 1000:.0f}ms", "companies" if args["ids"] else "all")
    out = json.dumps({"rows": [[r[0], r[1], r[2], round(float(r[3]), 6)] for r in rows]}, separators=(",", ":"))
    return (200, "application/json", out.encode())


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

    class Handler(BaseHTTPRequestHandler):
        server_version, sys_version, protocol_version = "lantern", "", "HTTP/1.1"
        timeout = 10

        def answer(self, body):
            who = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()[:64]
            status, kind, data = handle(self.command, self.path.split("?")[0], self.headers, body, pool=pool,
                                        secrets=lambda: read_secrets(secrets_path), rate=rate, schema=schema, who=who, log=log)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.answer(None)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = MAX_BODY + 1
            if n > MAX_BODY:
                self.close_connection = True              # the body is not read at all
                self.answer(None)
                return
            self.answer(self.rfile.read(n) if n else b"")

        def log_message(self, *a):
            pass                                           # the service logs what it decided, never request lines

    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("MEANING_PORT", "8377"))), Handler).serve_forever()


if __name__ == "__main__":
    serve()
