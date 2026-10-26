#!/usr/bin/env python3
"""
load_supabase.py: copy the full-text store (private repository wba-text-store) into the evidence
database (Supabase project wba-evidence), so it can be searched by word and, later, by meaning.

What it loads, in this order, each step resumable:
  1. schema      supabase/schema.sql (safe to run again).
  2. documents   one row per document the store has tried (docs-<part> files, newest part wins),
                 with the latest metadata snapshot (meta-<run>): company, type, year, language, title,
                 address, pages, characters, extraction outcome. Upserted every run (about 190,000 rows).
  3. pages       the text of each page from pages-<part> files not loaded yet (table load_log), oldest
                 part first. Only the pages of documents whose newest part is this one are copied; a
                 document's earlier pages are replaced, so a re-extracted document never keeps stale
                 pages.
  4. passages    evidence passages from evidence-<run> files not loaded yet (complete only when the
                 matching evidence-docs-<run> exists); a re-tagged document's passages are replaced.
  5. word index  with --index, once no pages file is left: the full-text index on pages (built once
                 after the first big load; later inserts keep it up to date).

Safety:
  - Dry run by default: connects, checks the schema inside a transaction that is rolled back, and
    reports what is waiting to be loaded. --commit writes.
  - --max-gb: stops loading pages when the database reaches this size (the project's disk must be
    bigger: a full disk puts the database in read-only mode). --minutes: time budget.
  - Prints counts only, never document text (the workflow logs of this repository are public).

Env: SUPABASE_DB_URL; TEXT_STORE_REPO + TEXT_STORE_TOKEN (or TEXT_STORE_DIR); AIRTABLE_QA_TOKEN (optional).
"""
import argparse, collections, json, os, re, shutil, sys, tempfile, time, traceback

import qa
import text_core as tc

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase", "schema.sql")
SEARCH_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supabase", "search.sql")
EVIDENCE = re.compile(r"^evidence-(\d{8}T\d{6})\.jsonl\.gz$")
EVIDENCE_DOCS = re.compile(r"^evidence-docs-(\d{8}T\d{6})\.jsonl\.gz$")
MAX_BODY = 1_000_000                       # characters of one page or passage kept


# ----------------------------------------------------------------------------------------------
# Planning (pure, tested offline)
# ----------------------------------------------------------------------------------------------
def evidence_files(listing):
    """[(stamp, passages file, docs file)] for complete evidence runs, oldest first."""
    ev = {m.group(1): n for n in listing for m in [EVIDENCE.match(n)] if m}
    dd = {m.group(1): n for n in listing for m in [EVIDENCE_DOCS.match(n)] if m}
    return [(s, ev[s], dd[s]) for s in sorted(ev) if s in dd]


def plan(listing, loaded):
    """Store files not yet in the database: (pages files, evidence runs), oldest first."""
    _, pages = tc.complete_parts(listing)
    return ([n for n in pages if n not in loaded],
            [e for e in evidence_files(listing) if e[1] not in loaded])


def newest_meta(listing):
    metas = sorted(n for n in listing if n.startswith("meta-"))
    return metas[-1] if metas else None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def clean(s, limit=MAX_BODY):
    return (s or "").replace("\x00", "")[:limit]


def document_row(d, m):
    """The documents row for docs-index line d and metadata line m (or {})."""
    return (d["id"], d.get("rec") or m.get("rec"), m.get("wba") or None, m.get("co") or None,
            m.get("type") or None, _int(m.get("year")), m.get("lang") or None, clean(m.get("label"), 2000) or None,
            m.get("url") or None, _int(d.get("pages")) if d.get("pages") is not None else _int(m.get("pages")),
            _int(d.get("chars")), d.get("status"), d.get("part"), d.get("at"))


def current_pages(rows, index, part):
    """Page rows of a pages file that are the newest text of their document: (id, page, text)."""
    for r in rows:
        d = index.get(r["id"])
        if d and d.get("part") == part:
            yield r["id"], int(r["p"]), clean(r.get("t"))


def passage_row(ln, lexicon):
    return (ln["id"], int(ln["p"]), int(ln["n"]), list(ln.get("dims") or []),
            json.dumps(ln.get("topics") or {}, ensure_ascii=False), json.dumps(ln.get("terms") or {}, ensure_ascii=False),
            ln.get("area"), list(ln.get("integration") or []), bool(ln.get("number")), clean(ln.get("t")), lexicon)


# ----------------------------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------------------------
def connect(url):
    import psycopg
    conn = psycopg.connect(url.strip(), connect_timeout=30)
    with conn.cursor() as c:
        c.execute("set statement_timeout = 0")
    conn.commit()
    return conn


def db_gb(conn):
    with conn.cursor() as c:
        c.execute("select pg_database_size(current_database())")
        return c.fetchone()[0] / 1e9


def loaded_names(conn):
    with conn.cursor() as c:
        c.execute("select to_regclass('public.load_log')")
        if c.fetchone()[0] is None:
            return set()
        c.execute("select name from public.load_log")
        return {r[0] for r in c.fetchall()}


def apply_schema(conn, commit):
    """schema.sql, then search.sql where Supabase's auth schema exists (it is not in a plain Postgres)."""
    with conn.cursor() as c:
        c.execute(open(SCHEMA, encoding="utf-8").read())
        c.execute("select to_regnamespace('auth') is not null")
        if c.fetchone()[0]:
            c.execute(open(SEARCH_SQL, encoding="utf-8").read())
    (conn.commit if commit else conn.rollback)()


def upsert_documents(conn, rows):
    with conn.cursor() as c:
        c.execute("create temp table tmp_docs (like public.documents including defaults) on commit drop")
        cols = ("library_id, record_id, wba_id, company, doc_type, year, lang, label, url, pages, chars, "
                "text_status, text_part, extracted_at")
        with c.copy(f"copy tmp_docs ({cols}) from stdin") as cp:
            for r in rows:
                cp.write_row(r)
        sets = ", ".join(f"{k} = excluded.{k}" for k in cols.split(", ")[1:])
        c.execute(f"insert into public.documents ({cols}) select distinct on (library_id) {cols} from tmp_docs "
                  f"on conflict (library_id) do update set {sets}, loaded_at = now()")
        n = c.rowcount
    conn.commit()
    return n


def load_pages(conn, name, rows):
    """rows: iterable of (id, page, text). One transaction per file."""
    ids, n = set(), 0
    with conn.cursor() as c:
        c.execute("create temp table tmp_pages (library_id text, page int, body text) on commit drop")
        with c.copy("copy tmp_pages (library_id, page, body) from stdin") as cp:
            for lid, p, t in rows:
                cp.write_row((lid, p, t)); ids.add(lid); n += 1
        c.execute("delete from public.pages where library_id in (select distinct library_id from tmp_pages)")
        c.execute("insert into public.pages (library_id, page, body) "
                  "select distinct on (library_id, page) library_id, page, body from tmp_pages "
                  "on conflict (library_id, page) do nothing")
        c.execute("insert into public.load_log (name, kind, documents, rows) values (%s, 'pages', %s, %s) "
                  "on conflict (name) do update set documents = excluded.documents, rows = excluded.rows, "
                  "loaded_at = now()", (name, len(ids), n))
    conn.commit()
    return len(ids), n


def load_passages(conn, name, tagged_ids, rows):
    """tagged_ids: every document the evidence run tagged (its passages are replaced, even when it
    now has none). rows: passage tuples."""
    n = 0
    with conn.cursor() as c:
        c.execute("create temp table tmp_pass (like public.passages) on commit drop")
        cols = "library_id, page, n, dims, topics, terms, area, integration, has_number, body, lexicon"
        with c.copy(f"copy tmp_pass ({cols}) from stdin") as cp:
            for r in rows:
                cp.write_row(r); n += 1
        c.execute("delete from public.passages where library_id = any(%s)", (list(tagged_ids),))
        c.execute(f"insert into public.passages ({cols}) select distinct on (library_id, page, n) {cols} "
                  f"from tmp_pass where library_id in (select library_id from public.documents) "
                  f"on conflict (library_id, page, n) do nothing")
        kept = c.rowcount
        c.execute("insert into public.load_log (name, kind, documents, rows) values (%s, 'evidence', %s, %s) "
                  "on conflict (name) do update set documents = excluded.documents, rows = excluded.rows, "
                  "loaded_at = now()", (name, len(tagged_ids), kept))
    conn.commit()
    return kept, n - kept


def build_word_index(conn):
    with conn.cursor() as c:
        c.execute("set maintenance_work_mem = '512MB'")
        c.execute("create index if not exists pages_words on public.pages using gin (public.page_words(body))")
    conn.commit()


def table_counts(conn):
    out = {}
    with conn.cursor() as c:
        for t in ("documents", "pages", "passages", "datapoints"):
            c.execute("select to_regclass(%s)", (f"public.{t}",))
            if c.fetchone()[0] is None:
                out[t] = None
                continue
            c.execute(f"select count(*) from public.{t}")
            out[t] = c.fetchone()[0]
        c.execute("select to_regclass('public.pages_words')")
        out["word index"] = "built" if c.fetchone()[0] else "not built"
    return out


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--minutes", type=float, default=300, help="time budget for loading pages")
    ap.add_argument("--parts", type=int, default=0, help="at most this many pages files (0 = all pending)")
    ap.add_argument("--max-gb", type=float, default=6.0, help="stop loading pages at this database size")
    ap.add_argument("--index", action="store_true", help="build the word index on pages when it is missing and no pages file is left")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    t0 = time.time()
    run = qa.Run("load_supabase.py")

    store = tc.open_store()
    listing = store.assets()
    conn = connect(os.environ["SUPABASE_DB_URL"])
    apply_schema(conn, a.commit)
    loaded = loaded_names(conn)
    pending_pages, pending_ev = plan(listing, loaded)
    size0 = db_gb(conn)
    mb = lambda names: sum(listing[n]["size"] for n in names) / 1e6
    print(f">>> store: {len(listing):,} files | pending: {len(pending_pages):,} pages files ({mb(pending_pages):,.0f} MB), "
          f"{len(pending_ev):,} evidence runs | database {size0:.2f} GB", flush=True)

    work = tempfile.mkdtemp(prefix="sb-")
    docs_names, _ = tc.complete_parts(listing)
    tc.sync_down(store, docs_names, work)
    index = tc.load_docs_index(work, docs_names)
    meta = {}
    mname = newest_meta(listing)
    if mname:
        tc.sync_down(store, [mname], work)
        meta = {m["id"]: m for m in tc.read_jsonl_gz(os.path.join(work, mname))}
    status = collections.Counter(d.get("status") for d in index.values())
    print(f">>> documents in the store: {len(index):,} ({', '.join(f'{k} {v:,}' for k, v in status.most_common())}); "
          f"metadata snapshot {mname or 'none'} ({len(meta):,})", flush=True)

    c = collections.Counter(); stopped = ""
    if a.commit:
        c["documents upserted"] = upsert_documents(conn, (document_row(d, meta.get(i, {})) for i, d in index.items()))
        todo = pending_pages[:a.parts] if a.parts else pending_pages
        for k, name in enumerate(todo, 1):
            if (time.time() - t0) / 60 > a.minutes:
                stopped = f"time budget of {a.minutes:.0f} minutes reached"; break
            gb = db_gb(conn)
            if gb >= a.max_gb:
                stopped = f"database reached {gb:.1f} GB (limit {a.max_gb:g} GB)"; break
            part = tc.part_of(name)[1]
            tc.sync_down(store, [name], work)
            path = os.path.join(work, name)
            docs_n, rows_n = load_pages(conn, name, current_pages(tc.read_jsonl_gz(path), index, part))
            os.remove(path)
            c["pages files"] += 1; c["documents with pages"] += docs_n; c["pages"] += rows_n
            if k % 10 == 0 or k == len(todo):
                print(f"    {k:,}/{len(todo):,} files, {c['pages']:,} pages, {db_gb(conn):.2f} GB, "
                      f"{(time.time() - t0) / 60:.0f} min", flush=True)
        for stamp, ev_name, dd_name in pending_ev:
            if stopped:
                break
            tc.sync_down(store, [ev_name, dd_name], work)
            tagged = {d["id"]: d.get("v") for d in tc.read_jsonl_gz(os.path.join(work, dd_name))}
            lex = next(iter(tagged.values()), None)
            kept, orphan = load_passages(conn, ev_name, set(tagged),
                                         (passage_row(ln, tagged.get(ln["id"], lex)) for ln in tc.read_jsonl_gz(os.path.join(work, ev_name))))
            for n in (ev_name, dd_name):
                os.remove(os.path.join(work, n))
            c["evidence runs"] += 1; c["passages"] += kept; c["passages without document"] += orphan
        # built once everything waiting has been copied: building it once is far faster than
        # updating it row by row through the first big load
        if a.index and not stopped and len(todo) == len(pending_pages):
            print(">>> building the word index on pages (if missing)", flush=True)
            build_word_index(conn)
            c["word index built"] = 1
    shutil.rmtree(work, ignore_errors=True)

    counts = table_counts(conn)
    size1 = db_gb(conn)
    conn.close()
    md = ["### Evidence database load" + ("" if a.commit else " (dry run)"), "",
          f"Store: {len(index):,} documents tried ({', '.join(f'{k} {v:,}' for k, v in status.most_common())}), "
          f"metadata for {len(meta):,}.", "",
          f"Waiting at the start: {len(pending_pages):,} pages files ({mb(pending_pages):,.0f} MB compressed) and "
          f"{len(pending_ev):,} evidence runs.", ""]
    if a.commit:
        md += ["Loaded this run: " + (", ".join(f"{k} {v:,}" for k, v in c.items()) or "nothing") + ".", ""]
        if stopped:
            md += [f"Stopped early: {stopped}. The next run continues where this one stopped.", ""]
    else:
        md += ["Schema checked inside a transaction and rolled back: nothing was written.", ""]
    md += [f"Database: {size0:.2f} GB at the start, {size1:.2f} GB at the end. Tables: "
           + ", ".join(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v if v is not None else 'not created yet'}"
                       for k, v in counts.items()) + ".", ""]
    open("load_supabase.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(index), rows_out=c["pages"] + c["passages"],
               counters={**dict(c), "stopped": stopped, "db_gb": round(size1, 2)})
    run.write_step_summary("ok", dict(c), title="Evidence database load")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        open("load_supabase.md", "w").write("### Evidence database load: the run failed\n\n```\n"
                                            + traceback.format_exc()[-2500:] + "```\n")
        raise
