#!/usr/bin/env python3
"""
meaning_link.py: tells Supabase where lanterN's server is and how to be let in, and measures the way between them.
Run ON the server (it reads the server's own secret file), by the workflow "Server service".

  --link HOST   writes into Supabase's vault the address (https://HOST) and the secret the service expects, read here
                from /etc/lantern/meaning.secrets. The secret goes from that file to the vault over the database
                connection: it is never printed, never an argument, never in a log. Run again after a secret changes.
  --probe N     asks Supabase to call the server N times (public.meaning_remote_probe) and prints the times and what
                failed. Counts and milliseconds only.

  --storm N     a rehearsal: the same N searches at once on Supabase three times: without the server, with the option
                on for those sessions only, and without the server again (the first run of the three also warms
                Supabase's memory, so the third is the fair comparison). For rehearsing a server that is down: do people
                wait, do searches still answer. Counts and milliseconds only; nothing is kept.
  --breaker     a rehearsal of the breaker itself: asks for the server's rows six times in a row with the option on
                for this session, each committed as a real search's would be, and prints what each took and whether
                the server was asked. With the server down: three short failures, then it is not asked at all.

None switches anything on for anyone else: the search uses the server only when the option meaning_remote is on.

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
    return (f"{len(ms)} calls from Supabase to the server: {len(ms) - failed} answered, {failed} failed ({kinds}); first three {', '.join(f'{x:.0f}' for x in ms[:3])} ms; "
            f"median {statistics.median(ms):.0f} ms, 95th percentile {percentile(ms, 95):.0f} ms, slowest {max(ms):.0f} ms; "
            f"passages returned {got.get('rows', 0):,}; failures in a row on record {state.get('fails', 0)}, "
            f"paused {'yes' if state.get('skip_until') else 'no'}." if ms else "probe returned nothing.")


SEARCH = ("select count(*) from public.find_blend('eval', %s, %s::real[], null, null, null, null, 24, 'eval', null, null, null, 'rehearsal')")


def one_search(url, q, vec, option):
    """One search on its own connection, with the option set for that session. Returns (milliseconds, pages) or (ms, None) when it failed."""
    import psycopg, time
    t = time.time()
    try:
        with psycopg.connect(url) as c:
            cur = c.cursor()
            cur.execute("select set_config('lantern.meaning_remote', %s, false)", (option,))
            t = time.time()
            cur.execute(SEARCH, (q, vec))
            n = cur.fetchone()[0]
            took = (time.time() - t) * 1000
            c.rollback()                                   # the searches of a rehearsal are not kept in the logs
            return took, n
    except Exception:
        return (time.time() - t) * 1000, None


def storm(url, n, run=one_search):
    """The same n searches at once, three times: option off, on, off. Returns the three lists of (ms, pages)."""
    import psycopg
    from concurrent.futures import ThreadPoolExecutor
    with psycopg.connect(url) as c:
        cur = c.cursor()
        cur.execute("select q, vec from public.query_vectors where array_length(vec, 1) = 384 order by at desc limit %s", (n,))
        asked = cur.fetchall()
    if not asked:
        raise SystemExit("no kept question to search with")
    out = []
    for option in ("off", "on", "off"):
        with ThreadPoolExecutor(max_workers=len(asked)) as pool:
            out.append(list(pool.map(lambda a: run(url, a[0], a[1], option), asked)))
    return out


def storm_line(first, on, off, state):
    part = lambda rows: (f"{sum(1 for r in rows if r[1] is not None)} of {len(rows)} answered, median {statistics.median([r[0] for r in rows]):.0f} ms, "
                         f"slowest {max(r[0] for r in rows):.0f} ms, pages a search {min([r[1] for r in rows if r[1] is not None] or [0])} to {max([r[1] for r in rows if r[1] is not None] or [0])}")
    return (f"{len(on)} searches at once. Without the server (first, memory cold): {part(first)}. With the server asked first: {part(on)}. "
            f"Without the server again: {part(off)}. Failures in a row on record {state.get('fails', 0)}, paused {'yes' if state.get('skip_until') else 'no'}.")


def breaker(url, times=6):
    """The breaker asked `times` times in a row, each committed. Returns [(ms, got rows, fails after, paused after)]."""
    import psycopg, time
    out = []
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.cursor()
        cur.execute("select vec from public.query_vectors where array_length(vec, 1) = 384 order by at desc limit 1")
        vec = cur.fetchone()[0]
        cur.execute("select set_config('lantern.meaning_remote', 'on', false)")
        for _ in range(times):
            t = time.time()
            cur.execute("select public.meaning_remote_rows(%s::real[], null, null, null, null) is not null", (vec,))
            got = cur.fetchone()[0]
            ms = (time.time() - t) * 1000
            cur.execute("select fails, skip_until is not null and skip_until > now() from public.meaning_remote_state")
            fails, paused = cur.fetchone()
            out.append((ms, got, fails, paused))
    return out


def breaker_line(rows):
    return "the breaker, asked in a row: " + "; ".join(
        f"{i + 1}) {'rows' if got else 'nothing'} in {ms:.0f} ms, failures on record {fails}, {'paused' if paused else 'asking'}" for i, (ms, got, fails, paused) in enumerate(rows))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Link Supabase to lanterN's server, or measure the way between them.")
    ap.add_argument("--link", default="", metavar="HOST")
    ap.add_argument("--probe", type=int, default=0, metavar="N")
    ap.add_argument("--storm", type=int, default=0, metavar="N")
    ap.add_argument("--breaker", action="store_true")
    a = ap.parse_args(argv)
    if a.link and not HOST.match(a.link):
        sys.exit("--link takes a host name like api.example.org")
    if a.breaker:
        if not os.environ.get("SUPABASE_DB_URL"):
            sys.exit("Set SUPABASE_DB_URL.")
        print(breaker_line(breaker(os.environ["SUPABASE_DB_URL"])))
        return 0
    if not 0 <= a.probe <= 200 or not 0 <= a.storm <= 30 or not (a.link or a.probe or a.storm):
        sys.exit("--link HOST, --probe N (1 to 200) and/or --storm N (1 to 30)")
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
    if a.storm:
        first, on, off = storm(os.environ["SUPABASE_DB_URL"], a.storm)
        with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as conn:
            cur = conn.cursor()
            cur.execute("select fails, skip_until from public.meaning_remote_state")
            fails, skip = cur.fetchone()
        print(storm_line(first, on, off, {"fails": fails, "skip_until": skip}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
