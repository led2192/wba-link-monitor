#!/usr/bin/env python3
"""
meaning_link.py: tells Supabase where lanterN's server is and how to be let in, and measures the way between them.
Run ON the server (it reads the server's own secret file), by the workflow "Server service".

  --link HOST   writes into Supabase's vault the address (https://HOST) and the secret the service expects, read here
                from /etc/lantern/meaning.secrets. The secret goes from that file to the vault over the database
                connection: it is never printed, never an argument, never in a log. Run again after a secret changes.
  --probe N     asks Supabase to call the server N times (public.meaning_remote_probe) and prints the times and what
                failed. Counts and milliseconds only.

Neither switches anything on: the search uses the server only when the option meaning_remote is on.

Env: SUPABASE_DB_URL.
"""
import argparse, math, os, re, statistics, sys

SECRETS = "/etc/lantern/meaning.secrets"
HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def first_secret(path=SECRETS):
    """The newest secret the service accepts: the last line of its file of 32 characters or more."""
    lines = [l.strip() for l in open(path, encoding="utf-8").read().splitlines() if len(l.strip()) >= 32]
    if not lines:
        raise SystemExit("The service has no secret yet: install it first.")
    return lines[-1]


def keep(cur, name, value, description):
    """One named secret in the vault: made, or replaced when it is already there."""
    cur.execute("select id from vault.secrets where name = %s", (name,))
    row = cur.fetchone()
    if row:
        cur.execute("select vault.update_secret(%s, %s, %s, %s)", (row[0], value, name, description))
        return "replaced"
    cur.execute("select vault.create_secret(%s, %s, %s)", (value, name, description))
    return "made"


def percentile(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(p / 100 * len(v)) - 1))] if v else 0


def probe_line(got):
    """What a probe found, as one line of counts and milliseconds."""
    if got.get("error"):
        return f"probe not run: {got['error']}"
    ms = [float(x) for x in got.get("ms") or []]
    failed = sum((got.get("errors") or {}).values())
    kinds = ", ".join(f"{k} x{v}" for k, v in sorted((got.get("errors") or {}).items())) or "none"
    state = got.get("state") or {}
    return (f"{len(ms)} calls from Supabase to the server: {len(ms) - failed} answered, {failed} failed ({kinds}); "
            f"median {statistics.median(ms):.0f} ms, 95th percentile {percentile(ms, 95):.0f} ms, slowest {max(ms):.0f} ms; "
            f"passages returned {got.get('rows', 0):,}; failures in a row on record {state.get('fails', 0)}, "
            f"paused {'yes' if state.get('skip_until') else 'no'}." if ms else "probe returned nothing.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Link Supabase to lanterN's server, or measure the way between them.")
    ap.add_argument("--link", default="", metavar="HOST")
    ap.add_argument("--probe", type=int, default=0, metavar="N")
    a = ap.parse_args(argv)
    if a.link and not HOST.match(a.link):
        sys.exit("--link takes a host name like api.example.org")
    if not 0 <= a.probe <= 200 or not (a.link or a.probe):
        sys.exit("--link HOST and/or --probe N (1 to 200)")
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import psycopg
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
        cur = conn.cursor()
        if a.link:
            secret = first_secret()
            did = (keep(cur, "meaning_remote_url", f"https://{a.link}", "lanterN's server: the address of the search by meaning"),
                   keep(cur, "meaning_remote_secret", secret, "lanterN's server: the secret its service expects"))
            conn.commit()
            print(f"vault: the address {did[0]}, the secret {did[1]} ({len(secret)} characters, not shown).")
        if a.probe:
            import json
            cur.execute("set statement_timeout = '10min'")
            cur.execute("select public.meaning_remote_probe(%s)", (a.probe,))
            got = cur.fetchone()[0]
            conn.commit()
            print(probe_line(got if isinstance(got, dict) else json.loads(got)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
