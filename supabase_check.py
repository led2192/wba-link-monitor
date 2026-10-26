#!/usr/bin/env python3
"""
supabase_check.py: read-only check that the evidence database (Supabase project wba-evidence) is reachable
with the three secrets, before anything is loaded into it.

It checks, and reports in supabase_check.md (never printing a secret):
  1. SUPABASE_DB_URL: the address has the expected shape (session pooler, port 5432, user postgres.<ref>,
     no leftover [YOUR-PASSWORD]); Postgres answers; version, database size, memory settings, the
     extensions we need (vector for meaning search, pg_trgm for fuzzy text) available or installed, and how
     many tables the public schema holds.
  2. SUPABASE_URL + SUPABASE_SERVICE_KEY: the REST API answers with the key, and the key has secret-level
     rights (the auth admin endpoint, which a publishable key cannot read).
  3. The project ref in SUPABASE_URL matches the one in the database address.

Writes nothing to Supabase or Airtable. Exit code 0 when every check passes, 1 otherwise.
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_DB_URL.
"""
import os, re, sys, traceback
from urllib.parse import urlsplit, unquote

import requests

NEEDED_EXTENSIONS = ("vector", "pg_trgm")


def check_db_url(url):
    """Shape of the database address, without connecting. Returns (problems, ref)."""
    problems = []
    if not url:
        return ["SUPABASE_DB_URL is empty or missing"], None
    if url != url.strip():
        problems.append("SUPABASE_DB_URL has spaces or a line break at the start or end")
    url = url.strip()
    if "[YOUR-PASSWORD]" in url or "YOUR-PASSWORD" in url or "TU_CONTRASE" in url:
        problems.append("SUPABASE_DB_URL still holds the placeholder instead of the password")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return problems + ["SUPABASE_DB_URL is not a valid address (a symbol in the password may break it; "
                           "reset the password to letters and digits)"], None
    if p.scheme not in ("postgresql", "postgres"):
        problems.append(f"SUPABASE_DB_URL should start with postgresql:// (starts with {p.scheme}://)")
    host = (p.hostname or "").lower()
    if not host.endswith(".pooler.supabase.com"):
        problems.append("SUPABASE_DB_URL is not the session pooler address (host should end in "
                        ".pooler.supabase.com); GitHub cannot reach the direct connection")
    if port != 5432:
        problems.append(f"SUPABASE_DB_URL port is {port}, expected 5432 (session pooler; 6543 is the transaction pooler)")
    user = unquote(p.username or "")
    m = re.fullmatch(r"postgres\.([a-z0-9]{20})", user)
    if not m:
        problems.append("SUPABASE_DB_URL user should be postgres.<project ref>")
    if not p.password:
        problems.append("SUPABASE_DB_URL has no password")
    return problems, (m.group(1) if m else None)


def check_api_url(url):
    """Returns (problems, ref)."""
    if not url:
        return ["SUPABASE_URL is empty or missing"], None
    m = re.fullmatch(r"https://([a-z0-9]{20})\.supabase\.co/?", url.strip())
    if not m:
        return ["SUPABASE_URL should look like https://<project ref>.supabase.co"], None
    return [], m.group(1)


def key_kind(key):
    key = (key or "").strip()
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "publishable"
    if key.count(".") == 2 and key.startswith("eyJ"):
        return "legacy JWT"
    return "unknown" if key else "missing"


def db_facts(url):
    import psycopg
    out = {}
    with psycopg.connect(url.strip(), connect_timeout=20) as conn:
        cur = conn.cursor()
        cur.execute("SELECT version(), current_database(), pg_size_pretty(pg_database_size(current_database()))")
        out["version"], out["database"], out["size"] = cur.fetchone()
        for name in ("shared_buffers", "work_mem", "max_connections"):
            cur.execute(f"SHOW {name}")
            out[name] = cur.fetchone()[0]
        cur.execute("SELECT name, installed_version FROM pg_available_extensions WHERE name = ANY(%s)",
                    (list(NEEDED_EXTENSIONS),))
        out["extensions"] = {n: v for n, v in cur.fetchall()}
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        out["public_tables"] = cur.fetchone()[0]
    return out


def api_facts(url, key):
    base = url.strip().rstrip("/")
    h = {"apikey": key.strip()}
    if key_kind(key) == "legacy JWT":
        h["Authorization"] = f"Bearer {key.strip()}"
    rest = requests.get(f"{base}/rest/v1/", headers=h, timeout=30)
    admin = requests.get(f"{base}/auth/v1/admin/users", headers=h, params={"per_page": 1}, timeout=30)
    return {"rest": rest.status_code, "admin": admin.status_code}


def main():
    url, key, db = (os.environ.get(k, "") for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_DB_URL"))
    ok, fail, info = [], [], []

    p_api, ref_api = check_api_url(url)
    p_db, ref_db = check_db_url(db)
    fail += p_api + p_db
    if ref_api and ref_db:
        (ok if ref_api == ref_db else fail).append(
            "SUPABASE_URL and SUPABASE_DB_URL point to the same project" if ref_api == ref_db
            else "SUPABASE_URL and SUPABASE_DB_URL point to different projects")
    kind = key_kind(key)
    if kind in ("secret", "legacy JWT"):
        ok.append(f"SUPABASE_SERVICE_KEY is a {kind} key")
    else:
        fail.append(f"SUPABASE_SERVICE_KEY is a {kind} key; it should be the secret key (sb_secret_...)")

    if not p_db:
        try:
            f = db_facts(db)
            ok.append("Postgres answers through the session pooler")
            info += [f"Server: {f['version'].split(' on ')[0]}", f"Database {f['database']}, size {f['size']}",
                     f"Memory: shared_buffers {f['shared_buffers']}, work_mem {f['work_mem']}, "
                     f"max_connections {f['max_connections']} (Small compute shows about 512MB shared_buffers)",
                     f"Tables in the public schema: {f['public_tables']}"]
            for ext in NEEDED_EXTENSIONS:
                if ext not in f["extensions"]:
                    fail.append(f"Extension {ext} is not available")
                else:
                    v = f["extensions"][ext]
                    ok.append(f"Extension {ext} " + (f"installed ({v})" if v else "available (installed by the schema PR)"))
        except Exception as e:
            msg = str(e).replace(db.strip(), "<SUPABASE_DB_URL>")
            if "password authentication failed" in msg:
                msg = "password authentication failed: the password in SUPABASE_DB_URL is not the database password"
            fail.append(f"Postgres did not answer: {type(e).__name__}: {msg[:300]}")

    if not p_api and kind != "missing":
        try:
            a = api_facts(url, key)
            (ok if a["rest"] == 200 else fail).append(f"REST API with the key: HTTP {a['rest']}")
            (ok if a["admin"] == 200 else fail).append(
                f"Secret-level rights (auth admin endpoint): HTTP {a['admin']}"
                + ("" if a["admin"] == 200 else "; the key may be the publishable one"))
        except Exception as e:
            fail.append(f"REST API did not answer: {type(e).__name__}")

    verdict = "all checks passed" if not fail else f"{len(fail)} problem(s)"
    md = [f"### Supabase connection check: {verdict}", "", "Read-only: nothing was written.", ""]
    md += [f"- OK: {x}" for x in ok] + [f"- PROBLEM: {x}" for x in fail]
    if info:
        md += ["", "Facts:", ""] + [f"- {x}" for x in info]
    open("supabase_check.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    return 0 if not fail else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("supabase_check.md", "w").write("### Supabase connection check: the script failed\n\n```\n"
                                             + traceback.format_exc()[-1500:] + "```\n")
        raise
