#!/usr/bin/env python3
"""
vault_secret.py: puts a secret that GitHub holds into the database's vault, under a name, without showing it.

    HF_TOKEN=... python vault_secret.py --name hf_token --env HF_TOKEN            says what it would do
    HF_TOKEN=... python vault_secret.py --name hf_token --env HF_TOKEN --commit   stores it, then tries it

Functions in the database that call a service (query_vector, the query's vector from Hugging Face) read their token
from Supabase Vault, so the page and this repository never hold it. The value goes to the database as a bound
parameter, never inside the statement's text, and is never printed: the output is the name, whether the secret was
created or replaced, and its length. Only the names in ALLOWED can be written. Dry run by default.
With --try-query the script then asks public.query_vector_made for three vectors and prints only whether each came
back and how long it took.
"""
import argparse, os, sys, time

ALLOWED = {"hf_token": "HF_TOKEN"}      # vault name -> the environment variable it may come from


def store(cur, name, value):
    """'created' or 'replaced'. One secret a name."""
    cur.execute("select id from vault.secrets where name = %s", (name,))
    row = cur.fetchone()
    if row:
        cur.execute("select vault.update_secret(%s, %s)", (row[0], value))
        return "replaced"
    cur.execute("select vault.create_secret(%s, %s)", (value, name))
    return "created"


def try_vector(cur, question="how many people work at the company"):
    """(what came back, seconds): 'a vector of 384 numbers', or the function's own short reason."""
    t0 = time.time()
    cur.execute("select public.query_vector_made(%s)", (question,))
    got = cur.fetchone()[0] or {}
    if isinstance(got.get("vec"), list):
        return f"a vector of {len(got['vec'])} numbers" + (" (already held)" if got.get("held") else ""), time.time() - t0
    return "no vector: " + str(got.get("why") or got.get("error") or ("busy" if got.get("busy") else "no reason given"))[:160], time.time() - t0


def main(argv=None, connect=None, env=os.environ, out=print):
    ap = argparse.ArgumentParser(description="Put a secret GitHub holds into the database's vault, without showing it.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--try-query", action="store_true", help="after storing, ask query_vector for one vector")
    a = ap.parse_args(argv)
    if ALLOWED.get(a.name) != a.env:
        out(f"Not allowed: the vault name {a.name!r} is not written from {a.env!r}.")
        return 1
    value = (env.get(a.env) or "").strip()
    if not value:
        out(f"{a.env} is empty: nothing to store.")
        return 1
    if not a.commit:
        out(f"Dry run: would store a secret of {len(value)} characters in the vault as {a.name!r}.")
        return 0
    if connect is None:
        import psycopg
        connect = lambda: psycopg.connect(env["SUPABASE_DB_URL"], autocommit=True)
    with connect() as conn:
        cur = conn.cursor()
        did = store(cur, a.name, value)
        out(f"Vault: {a.name!r} {did} ({len(value)} characters).")
        if a.try_query:
            for i in (1, 2, 3):                                  # the hosted model may be asleep: the first call wakes it
                what, s = try_vector(cur, f"how many people work at the company {i}")
                out(f"query_vector, try {i}: {what}, {s:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
