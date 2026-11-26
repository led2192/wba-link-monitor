#!/usr/bin/env python3
"""
catalogue_feed.py: the wanted candidates of the catalogue become companies by themselves, some every night.

The owner marks the candidates he wants (Admin, Catalogue). Until 26 Nov 2026 he then pressed "Add 20 of the
wanted" himself, day after day. This script does it for him every night, the largest first, and only when there is
room and the earlier ones are being digested. Before adding it reads:
  - the size of the evidence database against MAX_DB_GB (its disk is finite: a company brings documents and pages);
  - how full the Disclosure Library is (the last row of base_health, base_full) against MAX_BASE_FULL: a company
    brings a row, its pages and its documents into a base that holds 500,000 records;
  - whether the companies it added three days ago or more have documents: when 20 or more are that old and fewer
    than half have one, the robot is not finding or fetching them, and adding more only lengthens the queue.
When a check fails nothing is added and the reason is kept. Either way one row a day is written to
public.catalogue_feed_log, which the admin pages "Today" and "Catalogue" show.

    python catalogue_feed.py [--most 20] [--commit]

Dry run by default: reads, decides and prints; adds nothing and writes no row. --commit adds through
public.catalogue_feed (the door a person's own request uses, twenty a day at most) and writes the day's row.
Deterministic ids, a company is never made twice (request_company refuses a duplicate). Prints counts only.
Env: SUPABASE_DB_URL; AIRTABLE_TOKEN and AIRTABLE_BASE to read base_health (without them the base is taken as unknown
and nothing is added).
"""
import argparse, os, sys

MAX_DB_GB = float(os.environ.get("MAX_DB_GB") or 140)
MAX_BASE_FULL = float(os.environ.get("MAX_BASE_FULL") or 0.90)
OLD_DAYS = 3

COUNTS_SQL = """
select (select pg_database_size(current_database()) / 1e9),
       (select count(*) from public.catalogue c where c.status = 'wanted' and c.match = 'lei' and c.duplicate_why is null
          and coalesce(c.owner_website, c.website) is not null and (c.last_year is null or c.last_year >= extract(year from now())::int - 2)),
       count(*) filter (where o.old), count(*) filter (where o.old and o.documents > 0),
       count(*) filter (where o.old and o.watched = 0), count(*) filter (where o.old and o.watched > 0 and o.documents = 0)
  from (select q.wba_id, min(q.at) < now() - make_interval(days => %s) as old,
               coalesce((select p.watched from public.company_pages p where p.wba_id = q.wba_id), 0) as watched,
               (select count(*) from public.documents d where d.wba_id = q.wba_id) as documents
          from public.company_requests q join public.companies c on c.wba_id = q.wba_id
         where q.client = 'catalogue' group by q.wba_id) o
"""


def decide(db_gb, base_full, old, old_with_docs, wanted):
    """(add: bool, why not) from the night's readings. base_full None = the base's fullness could not be read."""
    if not wanted:
        return False, "no wanted candidate is left to add"
    if db_gb is None or db_gb >= MAX_DB_GB:
        return False, f"the evidence database is at {db_gb:.0f} GB, at or over the {MAX_DB_GB:.0f} GB kept as its ceiling" if db_gb is not None else "the size of the evidence database could not be read"
    if base_full is None:
        return False, "how full the Disclosure Library is could not be read"
    if base_full >= MAX_BASE_FULL:
        return False, f"the Disclosure Library is {base_full:.0%} full, at or over {MAX_BASE_FULL:.0%}"
    if old >= 20 and old_with_docs * 2 < old:
        return False, f"only {old_with_docs} of the {old} companies added {OLD_DAYS} days ago or more have a document: the robot is not keeping up"
    return True, None


def base_full_now():
    """base_full of the newest base_health row, or None."""
    if not (os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE")):
        return None
    try:
        from base_health import sweep, HEALTH
        rows = [r.get("fields", {}) for r in sweep(HEALTH, ["date", "base_full"])]
        rows = [f for f in rows if f.get("date") and f.get("base_full") is not None]
        return float(max(rows, key=lambda f: f["date"])["base_full"]) if rows else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--most", type=int, default=20)
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("catalogue_feed.py")
    full = base_full_now()
    got = {"added": 0, "held": 0, "other": 0}
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(COUNTS_SQL, (OLD_DAYS,)); db_gb, wanted, old, old_docs, no_pages, no_docs = cur.fetchone(); db_gb = float(db_gb)
            go, why = decide(db_gb, full, old, old_docs, wanted)
            print(f"Evidence database {db_gb:.0f} GB (ceiling {MAX_DB_GB:.0f}); Disclosure Library {'?' if full is None else format(full, '.0%')} full (ceiling {MAX_BASE_FULL:.0%}); "
                  f"{wanted:,} wanted left; of {old:,} companies added {OLD_DAYS} days ago or more: {old_docs:,} with a document, {no_pages:,} with no page watched, {no_docs:,} with pages and no document.", flush=True)
            if go and a.commit:
                cur.execute("select public.catalogue_feed(%s)", (max(1, min(a.most, 20)),)); r = cur.fetchone()[0] or {}
                if not r.get("ok"):
                    go, why = False, r.get("why") or "the companies could not be added"
                else:
                    got = {k: int(r.get(k) or 0) for k in got}
                    why = r.get("stopped")
            if a.commit:
                cur.execute("""insert into public.catalogue_feed_log (day, added, held, other, waited_why, db_gb, base_full, wanted_left, no_pages, no_docs)
                               values (current_date, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                               on conflict (day) do update set added = public.catalogue_feed_log.added + excluded.added, held = public.catalogue_feed_log.held + excluded.held,
                                 other = public.catalogue_feed_log.other + excluded.other, waited_why = excluded.waited_why, db_gb = excluded.db_gb, base_full = excluded.base_full,
                                 wanted_left = excluded.wanted_left, no_pages = excluded.no_pages, no_docs = excluded.no_docs, at = now()""",
                            (got["added"], got["held"], got["other"], why, round(db_gb, 1), full, wanted - got["added"] - got["held"] - got["other"], no_pages, no_docs))
                conn.commit()
            else:
                conn.rollback()
    if not go:
        print(f"Nothing added: {why}.")
    elif not a.commit:
        print(f"Dry run: up to {min(a.most, 20, wanted)} would be added.")
    else:
        print(f"Added {got['added']}; found among the companies held {got['held']}; not added {got['other']}." + (f" Stopped: {why}." if why else ""))
    run.finish("ok", rows_in=wanted, rows_out=got["added"], notes=(why or "added")[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
