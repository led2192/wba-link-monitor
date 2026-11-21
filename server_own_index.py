#!/usr/bin/env python3
"""
server_own_index.py: passages by meaning for documents whose passages live on the server only.

The index of meaning (semantic_index.py) holds the main reports from 2024 on. Its passages are written to Supabase
and copied to the server every night. Policies, codes of conduct and human rights policies of every year are
42,673 documents and 568,792 pages more (counted 20 Nov 2026), and Supabase's disk has no room for their passages.
The owner's decision (21 Nov 2026): compute them on the server and keep them there.

What it does: for every document of the types in scope with text, of any year, that the server does not hold yet,
reads its pages from Supabase (read only), cuts them into passages exactly as semantic_index.py does, embeds them
with the same model on the server's processors, and writes each passage's key and half-precision embedding to
meaning.chunks and the document to meaning.chunk_filters with own = true. The passage's text is not kept anywhere:
the search shows the page's own text (meaning_remote_pages in supabase/search.sql).

Not searched until switched on. A document written here is not live: the service that answers the search reads only
live documents, so the search is exactly what it was. `--live on` makes them live (and `--live off` takes them out
again); the nightly refresh (server_meaning.py --refresh) keeps the choice, and keeps their company, year and type
current. Per the house rule, the search exam is run before and after the switch on the same questions.

A run stops cleanly after --minutes and the next one continues: a document is done when its row is in chunk_filters.

    python server_own_index.py [--commit] [--limit N] [--minutes M] [--partition k/N] [--remaining] [--live on|off]

Dry run by default: lists what it would do and embeds nothing. Prints counts and times only, no document or text.
Env: SUPABASE_DB_URL (read), LOCAL_DB_URL (the server's database).
"""
import argparse, os, sys, time

import qa
from semantic_index import DIMS, MODEL, chunk_pages, parse_partition

OWN_TYPES = ("Policy Documents", "Code of Conduct", "Human Rights Policy")
SCHEMA = "meaning"
SETTING = "own_live"


def ensure(d, schema=SCHEMA):
    """The server's tables as this script needs them. Adds, never drops. The run's role is a member of the role that
    owns the schema (the nightly refresh's), and acts as it here, so that whatever is made belongs to that one."""
    d.execute("select 1 from pg_roles where rolname = 'lantern_load' and pg_has_role(current_user, 'lantern_load', 'member')")
    if d.fetchone():
        d.execute("set role lantern_load")
    d.execute(f"create schema if not exists {schema}")
    d.execute(f"create table if not exists {schema}.chunks (library_id text not null, page int not null, n int not null, embedding halfvec({DIMS}) not null)")
    d.execute(f"create table if not exists {schema}.chunk_filters (library_id text primary key, wba_id text, year int, doc_type text, live boolean not null default true)")
    d.execute(f"alter table {schema}.chunk_filters add column if not exists own boolean not null default false")
    d.execute(f"create table if not exists {schema}.settings (name text primary key, is_on boolean not null default false, changed timestamptz not null default now())")
    d.execute(f"insert into {schema}.settings (name, is_on) values (%s, false) on conflict (name) do nothing", (SETTING,))


def own_live(d, schema=SCHEMA):
    d.execute(f"select is_on from {schema}.settings where name = %s", (SETTING,))
    row = d.fetchone()
    return bool(row and row[0])


def in_scope(s, types, partition):
    """[(library_id, wba_id, year, doc_type)] with text, of the types, in slice k of N; newest year first."""
    k, n = partition
    s.execute("""select d.library_id, d.wba_id, d.year, d.doc_type from public.documents d
                 where d.doc_type = any(%s) and d.text_status = 'ok' and (abs(hashtext(d.library_id)) %% %s) = %s
                 order by d.year desc nulls last, d.library_id""", (list(types), n, k))
    return s.fetchall()


def to_do(scope, held):
    """The documents in scope the server does not hold, in the scope's order."""
    held = set(held)
    return [r for r in scope if r[0] not in held]


def halfvec_literal(v):
    return "[" + ",".join(f"{float(x):.5f}" for x in v) + "]"


def write_document(d, doc, rows, live, schema=SCHEMA):
    """One document, whole: its passages (replaced if a stopped run left some) and its row. rows: [(page, n, vector)]."""
    lid, wba_id, year, doc_type = doc
    d.execute(f"delete from {schema}.chunks where library_id = %s", (lid,))
    if rows:
        with d.copy(f"copy {schema}.chunks (library_id, page, n, embedding) from stdin") as out:
            for page, n, vec in rows:
                out.write_row((lid, page, n, halfvec_literal(vec)))
    d.execute(f"""insert into {schema}.chunk_filters (library_id, wba_id, year, doc_type, live, own) values (%s, %s, %s, %s, %s, true)
                  on conflict (library_id) do update set wba_id = excluded.wba_id, year = excluded.year, doc_type = excluded.doc_type, own = true""",
              (lid, wba_id, year, doc_type, live))


def set_live(d, on, schema=SCHEMA):
    """Switch the server-only documents into or out of the search. Returns how many rows changed."""
    d.execute(f"update {schema}.settings set is_on = %s, changed = now() where name = %s", (on, SETTING))
    d.execute(f"update {schema}.chunk_filters set live = %s where own and live is distinct from %s", (on, on))
    return d.rowcount


def report(m, commit):
    lines = ["### Passages kept on the server only" + ("" if commit else " (dry run: nothing embedded or written)"), "",
             "| | |", "|---|---|",
             f"| Documents in scope with text | {m['scope']:,} |",
             f"| Already on the server | {m['held']:,} |",
             f"| Left before this run | {m['todo']:,} |",
             f"| Done in this run | {m['done']:,} |",
             f"| Passages written | {m['passages']:,} |",
             f"| Documents with no passage (a page of a few words) | {m['empty']:,} |",
             f"| Minutes | {m['minutes']:.1f} |",
             f"| Passages a second | {m['rate']:.1f} |",
             f"| Searched (live) | {'yes' if m['live'] else 'no'} |"]
    if m["done"] and m["todo"] > m["done"] and m["minutes"]:
        left = m["todo"] - m["done"]
        lines += ["", f"At this pace the {left:,} documents left take about {left / m['done'] * m['minutes'] / 60:.1f} hours of running."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="documents this run (0 = all that are left)")
    ap.add_argument("--minutes", type=float, default=330, help="stop cleanly after this many minutes")
    ap.add_argument("--partition", default="", help="k/N: this run takes slice k of N of the documents")
    ap.add_argument("--remaining", action="store_true", help="say how many are done and left, and stop")
    ap.add_argument("--live", choices=("on", "off"), default=None, help="switch the server-only documents into or out of the search, and stop (needs --commit)")
    ap.add_argument("--batch", type=int, default=64)
    a = ap.parse_args(argv)
    if not os.environ.get("SUPABASE_DB_URL") or not os.environ.get("LOCAL_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL and LOCAL_DB_URL.")
    partition = parse_partition(a.partition)
    import psycopg
    run = qa.Run("server_own_index.py")
    t0 = time.time()
    with psycopg.connect(os.environ["SUPABASE_DB_URL"]) as src, psycopg.connect(os.environ["LOCAL_DB_URL"]) as dst:
        s, d = src.cursor(), dst.cursor()
        s.execute("set default_transaction_read_only = on")          # Supabase is only read
        ensure(d); dst.commit()
        if a.live:
            if not a.commit:
                print(f">>> would switch the server-only documents {a.live} (dry run)")
                return 0
            changed = set_live(d, a.live == "on"); dst.commit()
            print(f">>> server-only documents switched {a.live}: {changed:,} rows changed")
            run.finish(status="ok", rows_in=0, rows_out=changed)
            return 0
        live = own_live(d)
        scope = in_scope(s, OWN_TYPES, partition); src.rollback()
        d.execute(f"select library_id from {SCHEMA}.chunk_filters where own")
        held = [r[0] for r in d.fetchall()]; dst.rollback()
        todo = to_do(scope, held)
        m = dict(scope=len(scope), held=len(scope) - len(todo), todo=len(todo), done=0, passages=0, empty=0, minutes=0.0, rate=0.0, live=live)
        print(f">>> in scope {m['scope']:,}; on the server {m['held']:,}; left {m['todo']:,}", flush=True)
        if a.remaining or not a.commit:
            print(report(m, a.commit))
            return 0
        if a.limit:
            todo = todo[:a.limit]
        from semantic_index import Embedder
        emb = Embedder(MODEL, a.batch)
        embed_s = 0.0
        for doc in todo:
            if (time.time() - t0) / 60 >= a.minutes:
                break
            try:
                s.execute("select page, body from public.pages where library_id = %s order by page", (doc[0],))
                chunks = chunk_pages(s.fetchall()); src.rollback()
                t = time.time()
                vectors = emb.embed_passages([c[2] for c in chunks]) if chunks else []
                embed_s += time.time() - t
                write_document(d, doc, [(c[0], c[1], v) for c, v in zip(chunks, vectors)], live); dst.commit()
            except Exception as e:                                   # one document's failure does not stop the run
                src.rollback(); dst.rollback()
                run.failure("DOC_UNREADABLE", f"server_own_index.py: a document's passages could not be made ({type(e).__name__})", key=doc[0])
                continue
            m["done"] += 1; m["passages"] += len(chunks); m["empty"] += 0 if chunks else 1
            if m["done"] % 500 == 0:
                print(f"    {m['done']:,} documents, {m['passages']:,} passages, {(time.time() - t0) / 60:.0f} min", flush=True)
        m["minutes"] = (time.time() - t0) / 60
        m["rate"] = m["passages"] / embed_s if embed_s else 0.0
        if m["done"]:
            d.execute(f"analyze {SCHEMA}.chunk_filters"); dst.commit()
    print(report(m, True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(report(m, True) + "\n")
    run.finish(status="ok", rows_in=m["todo"], rows_out=m["done"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
