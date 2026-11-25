"""Long questions: does choosing the six rarest words find more pages than taking the first six?

A person should not have to work out the perfect query (the owner, 25 Nov 2026). A search carries six words at most,
and of a question written in full the first six were taken, so the words that matter, which come late, were not
searched. The option terms_by_weight takes the six words that the fewest pages hold (public.word_freq).

  --freq      counts, on a sample of the pages, how many hold each word, into public.word_freq (needs --commit; a
              heavy read of the database: at night, or --by-day)
  (default)   takes the long questions people really typed (search_log: nine words or more, the last 30 days, not
              the comparisons' own), and runs each through find_pages with the option off and then on: pages found,
              seconds, and how many of the pages are the same. Read only; the searches are logged as 'timing'.

Prints counts and seconds only, never a question, a company or a page.

    python long_questions.py [--limit 60] [--freq --commit [--pct 2] [--by-day]]

Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT).
"""
import argparse, os, statistics, sys, time

OPTION = "terms_by_weight"


def summary(rows):
    """Counters of a comparison. rows: [(pages off, pages on, seconds off, seconds on, pages in both)]."""
    c = {"questions": len(rows), "more pages with the option": 0, "fewer": 0, "the same number": 0, "none before, some with the option": 0, "some before, none with the option": 0,
         "the same pages": 0}
    for off, on, _, _, both in rows:
        c["more pages with the option" if on > off else "fewer" if on < off else "the same number"] += 1
        c["none before, some with the option"] += off == 0 and on > 0
        c["some before, none with the option"] += off > 0 and on == 0
        c["the same pages"] += off == on == both
    med = lambda xs: round(statistics.median(xs), 2) if xs else 0.0
    c["median seconds off"], c["median seconds on"] = med([r[2] for r in rows]), med([r[3] for r in rows])
    c["median pages off"], c["median pages on"] = med([r[0] for r in rows]), med([r[1] for r in rows])
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--freq", action="store_true")
    ap.add_argument("--pct", type=float, default=2.0)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--by-day", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("long_questions.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            if a.freq:
                if not a.commit:
                    cur.execute("select count(*) from public.word_freq"); print(f"word_freq holds {cur.fetchone()[0]:,} words. Dry run: nothing counted (--commit counts a sample of {a.pct}% of the pages).")
                    run.finish("ok", rows_in=0, rows_out=0, notes="dry run"); return 0
                import quiet_hours
                wait = quiet_hours.held(True, a.by_day)
                if wait:
                    print(wait, flush=True); run.finish("ok", rows_in=0, rows_out=0, notes="by day: not started"); return 0
                t0 = time.time(); cur.execute("select public.refresh_word_freq(%s::real)", (a.pct,)); n = cur.fetchone()[0]; conn.commit()
                print(f"{n:,} words counted on a sample of {a.pct}% of the pages, in {(time.time() - t0) / 60:.1f} min.")
                run.finish("ok", rows_in=n, rows_out=n, notes="word_freq"); return 0
            cur.execute("select count(*) from public.word_freq"); words = cur.fetchone()[0]
            cur.execute("""select q, company, year_from, year_to from (
                             select distinct on (lower(l.q)) l.q, l.company, l.year_from, l.year_to, l.at from public.search_log l
                             where l.at > now() - interval '30 days' and coalesce(l.client, '') !~* 'trial|timing|eval|exam'
                               and array_length(regexp_split_to_array(btrim(l.q), '\\s+'), 1) >= 9 and l.q !~ '"'
                             order by lower(l.q), l.at desc) x order by at desc limit %s""", (a.limit,))
            asked = cur.fetchall(); conn.rollback()
            rows = []
            for q, company, y0, y1 in asked:
                got = {}
                for arm in ("off", "on"):
                    try:
                        cur.execute("select set_config(%s, %s, false)", (f"lantern.{OPTION}", arm))
                        t0 = time.time()
                        cur.execute("select coalesce(array_agg(library_id || '|' || page), '{}') from public.find_pages('timing', %s, %s, %s, %s, null, 50, 'timing')", (q, company, y0, y1))
                        got[arm] = (set(cur.fetchone()[0]), time.time() - t0); conn.rollback()
                    except Exception:
                        conn.rollback(); got[arm] = (set(), 25.0)
                rows.append((len(got["off"][0]), len(got["on"][0]), got["off"][1], got["on"][1], len(got["off"][0] & got["on"][0])))
            cur.execute("select set_config(%s, '', false)", (f"lantern.{OPTION}",)); conn.rollback()
    c = summary(rows)
    print(f"### Long questions people typed, with the option {OPTION} off and on (word_freq holds {words:,} words)\n")
    for k, v in c.items():
        print(f"- {k}: {v}")
    print("\nNothing written.", flush=True)
    run.finish("ok", rows_in=len(rows), rows_out=0, counters={k.replace(" ", "_").replace(",", "")[:40]: v for k, v in c.items() if isinstance(v, int)}, notes="comparison")
    return 0


if __name__ == "__main__":
    sys.exit(main())
