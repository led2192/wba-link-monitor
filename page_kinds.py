#!/usr/bin/env python3
"""
page_kinds.py: marks the pages that are not content (a table of contents, a GRI index), so the search can put them
down the list (option page_kinds of supabase/search.sql, off until compared).

The rule is public.page_kind(body) in supabase/search.sql; this script only calls public.page_kinds_step again
and again, a batch of documents a call, until every document with text was read with today's rule or the minutes
run out. Each document is read once; when the rule changes (page_kind_rule) all are read again.

    python page_kinds.py                       how far the reading is (writes nothing)
    python page_kinds.py --commit [--minutes 40] [--batch 200]

Writes: public.page_kinds (one row a marked page: about one page in 200) and public.page_kind_docs (one row a
document read). Never removes a row. Prints counts only. Env: SUPABASE_DB_URL.
"""
import argparse, os, sys, time

import qa


def state_line(state):
    kinds = ", ".join(f"{k} {n:,}" for k, n in sorted((state.get("kinds") or {}).items())) or "none"
    return (f"documents with text {state.get('documents', 0):,}; read with today's rule {state.get('read', 0):,} "
            f"({state.get('pages', 0):,} pages); pages marked: {kinds}")


def run(cur, minutes, batch, out=print, clock=time.time, commit=lambda: None):
    """Steps until nothing is left or the minutes are over. Returns (documents, pages, marked, finished)."""
    end = clock() + minutes * 60
    docs = pages = marked = 0
    while True:
        cur.execute("select public.page_kinds_step(%s)", (batch,))
        r = cur.fetchone()[0]
        commit()
        if not r.get("documents"):
            return docs, pages, marked, True
        docs += r["documents"]; pages += r.get("pages", 0); marked += r.get("marked", 0)
        if docs % (batch * 25) < batch:
            out(f"  {docs:,} documents, {pages:,} pages, {marked:,} marked")
        if clock() >= end:
            return docs, pages, marked, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=40)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL", "").strip():
        print("SUPABASE_DB_URL is not set; nothing run.")
        return 0
    import evidence_db
    log = qa.Run("page_kinds.py")
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '150s'")
        cur.execute("select public.page_kinds_state()")
        print("before: " + state_line(cur.fetchone()[0]))
        if not a.commit:
            print("dry run: pass --commit to read the documents not read yet.")
            return 0
        docs, pages, marked, finished = run(cur, a.minutes, max(1, min(a.batch, 2000)), commit=conn.commit)
        cur.execute("select public.page_kinds_state()")
        print("after:  " + state_line(cur.fetchone()[0]))
        print(f"this run: {docs:,} documents, {pages:,} pages, {marked:,} marked; " + ("every document is read." if finished else "the minutes ran out: the next run goes on."))
    log.finish("ok", rows_in=pages, rows_out=marked, counters={"documents": docs, "pages": pages, "marked": marked, "finished": int(finished)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
