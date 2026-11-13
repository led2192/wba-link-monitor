#!/usr/bin/env python3
"""
db_locks.py: who is waiting for whom in the evidence database, and the release of a stuck schema statement.

    python db_locks.py                      list the sessions, what each runs and which lock it waits for
    python db_locks.py --release [--commit] end the sessions running a schema statement (alter table, create
                                            table, create index) that WAIT for a lock: a cancelled job's
                                            statement can stay queued for hours and, queued for an exclusive
                                            lock, it blocks every new reader of that table (9 Nov 2026).

    python db_locks.py --orphans [--commit] end the statements that have run longer than any job can live. A job is
                                            killed by GitHub at 360 minutes; its statement can go on in the database
                                            with nobody waiting for the answer (11 Nov 2026: the insert of a load
                                            killed at 14:03 UTC had been running for 7 hours, holding its lock on
                                            `documents` and the disk, and every install after it failed).

Dry run by default: --release and --orphans print what they would end; with --commit they end them
(pg_terminate_backend). --release never ends a reader or a writer of rows, only a waiting schema statement;
--orphans ends only statements older than ORPHAN_SECONDS (6 hours 30 minutes). Read-only otherwise.
"""
import argparse, os, sys

SCHEMA_VERBS = ("alter table", "create table", "create index", "create unique index", "drop index")


def sessions(cur):
    cur.execute("""
        select a.pid, a.usename, a.application_name, a.state, a.wait_event_type, a.wait_event,
               extract(epoch from now() - a.query_start)::int as seconds, left(a.query, 400)
        from pg_stat_activity a
        where a.datname = current_database() and a.pid <> pg_backend_pid() and a.state is not null and a.state <> 'idle'
        order by a.query_start""")
    return cur.fetchall()


def statement_text(q):
    """A query without its leading SQL comments (schema.sql starts with comment lines, so the running statement's
    text shown by pg_stat_activity starts with "-- Schema of the evidence database")."""
    import re
    return re.sub(r"^(\s*--[^\n]*\n?)+", "", q or "").strip().lower()


def waiting_schema(rows):
    """The sessions running a schema statement (or the whole schema.sql) and waiting for a lock."""
    out = []
    for r in rows:
        q = r[7] or ""
        if r[4] == "Lock" and (statement_text(q).startswith(SCHEMA_VERBS) or "schema of the evidence database" in q.lower()):
            out.append(r)
    return out


ORPHAN_SECONDS = 6 * 3600 + 1800          # no job lives 6 h 30 min: GitHub kills it at 6 h


def orphans(rows, seconds=ORPHAN_SECONDS):
    """The active statements older than any job: (pid, ..., seconds, query) rows of sessions(). seconds: a shorter
    age for the case where the job is known to be dead (cancelled, or killed by its own timeout), never under an hour."""
    return [r for r in rows if r[3] == "active" and (r[6] or 0) > max(3600, seconds)]


def index_progress(cur):
    """One line per index being built (13 Nov 2026: the build of the vector index ran for hours with nothing to
    say how far it was). pg_stat_progress_create_index gives the phase and how many blocks or rows are done."""
    try:
        cur.execute("select p.pid, coalesce(p.index_relid::regclass::text, p.relid::regclass::text), p.phase, p.blocks_done, p.blocks_total, "
                    "p.tuples_done, p.tuples_total from pg_stat_progress_create_index p")
        rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for pid, name, phase, bd, bt, td, tt in rows:
        share = f"{100 * bd / bt:.0f}% of blocks" if bt else (f"{100 * td / tt:.0f}% of rows" if tt else "no count yet")
        out.append(f"  index build pid {pid} on {name}: {phase}, {share} ({bd:,}/{bt:,} blocks, {td:,}/{tt:,} rows)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", action="store_true")
    ap.add_argument("--orphans", action="store_true")
    ap.add_argument("--older-than-minutes", type=int, default=ORPHAN_SECONDS // 60, help="with --orphans: the age that makes a statement an orphan (60 at least)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    import evidence_db
    conn = evidence_db.connect()
    cur = conn.cursor()
    cur.execute("set statement_timeout = '30s'")
    rows = sessions(cur)
    print(f"{len(rows)} active sessions")
    for pid, user, app, state, wtype, wev, secs, q in rows:
        import re
        print(f"  pid {pid} {user} [{app or '-'}] {state} {('waits ' + wtype + '/' + str(wev)) if wtype else ''} {secs}s: {re.sub(chr(92) + 's+', ' ', q)[:160]}")
    for line in index_progress(cur):
        print(line)
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
    if a.orphans:
        old = orphans(rows, a.older_than_minutes * 60)
        if not old:
            print("no statement has outlived its job")
        for r in old:
            print(f"{'ending' if a.commit else 'would end'} pid {r[0]}, running for {r[6] // 3600} h {r[6] % 3600 // 60} min")
            if a.commit:
                cur.execute("select pg_terminate_backend(%s)", (r[0],))
                print("  ended" if cur.fetchone()[0] else "  could not end (another role's session?)")
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
