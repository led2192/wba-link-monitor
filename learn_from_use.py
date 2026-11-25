#!/usr/bin/env python3
"""
learn_from_use.py: what people did with the results becomes a signal the search can use (option learn_from_use).

Every opening of a document and every mark beside a result ("useful", "not") is kept in public.search_clicks with
the search it followed. public.refresh_use_signal sums them by search (the company, the type and the words typed, in
any order) and document: marked useful +2, marked not -2, opened +1, one voice a person. With the option on,
find_pages_ranked moves a document with 2 or more up and one with -2 or less down for that same search
(supabase/search.sql). The option is off until it is compared with today's order on the same answers.

    python learn_from_use.py [--days 365] [--commit]

Dry run by default: the signal is counted and nothing is written. --commit replaces public.use_signal with the new
count (the marks themselves are kept, so the count can always be made again). Light: one statement over the click
log, a few thousand rows. Prints counts only, never what anyone searched.
"""
import argparse, os, sys


def line(r):
    """The counts of a run, in one sentence."""
    return (f"{r.get('searches', 0):,} searches with a mark or an opening, {r.get('documents', 0):,} documents among them: "
            f"{r.get('up', 0):,} would move up, {r.get('down', 0):,} down. " + ("Written." if r.get("written") else "Dry run: nothing written."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("learn_from_use.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select public.refresh_use_signal(%s, %s)", (a.days, a.commit)); r = cur.fetchone()[0]
            conn.commit() if a.commit else conn.rollback()
    print(line(r))
    run.finish("ok", rows_in=r.get("documents", 0), rows_out=r.get("documents", 0) if a.commit else 0, notes="use_signal" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
