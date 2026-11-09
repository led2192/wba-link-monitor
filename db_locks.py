#!/usr/bin/env python3
"""
db_locks.py: who is waiting for whom in the evidence database, and the release of a stuck schema statement.

    python db_locks.py                      list the sessions, what each runs and which lock it waits for
    python db_locks.py --release [--commit] end the sessions running a schema statement (alter table, create
                                            table, create index) that WAIT for a lock: a cancelled job's
                                            statement can stay queued for hours and, queued for an exclusive
                                            lock, it blocks every new reader of that table (9 Nov 2026).

Dry run by default: --release prints what it would end; with --commit it ends them (pg_terminate_backend).
Never ends a reader or a writer of rows, only a waiting schema statement. Read-only otherwise.
"""
import argparse, os, sys

SCHEMA_VERBS = ("alter table", "create table", "create index", "create unique index", "drop index")


def sessions(cur):
    cur.execute("""
        select a.pid, a.usename, a.application_name, a.state, a.wait_event_type, a.wait_event,
               extract(epoch from now() - a.query_start)::int as seconds, left(regexp_replace(a.query, '\\s+', ' ', 'g'), 160)
        from pg_stat_activity a
        where a.datname = current_database() and a.pid <> pg_backend_pid() and a.state is not null and a.state <> 'idle'
        order by a.query_start""")
    return cur.fetchall()


def waiting_schema(rows):
    """The sessions running a schema statement and waiting for a lock."""
    return [r for r in rows if r[4] == "Lock" and (r[7] or "").strip().lower().startswith(SCHEMA_VERBS)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    import evidence_db
    conn = evidence_db.connect()
    cur = conn.cursor()
    cur.execute("set statement_timeout = '30s'")
    rows = sessions(cur)
    print(f"{len(rows)} active sessions")
    for pid, user, app, state, wtype, wev, secs, q in rows:
        print(f"  pid {pid} {user} [{app or '-'}] {state} {('waits ' + wtype + '/' + str(wev)) if wtype else ''} {secs}s: {q}")
    stuck = waiting_schema(rows)
    if a.release:
        if not stuck:
            print("no schema statement is waiting for a lock")
        for r in stuck:
            print(f"{'ending' if a.commit else 'would end'} pid {r[0]}: {r[7]}")
            if a.commit:
                cur.execute("select pg_terminate_backend(%s)", (r[0],))
                print("  ended" if cur.fetchone()[0] else "  could not end (another role's session?)")
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
