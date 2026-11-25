#!/usr/bin/env python3
"""
catalogue_size.py: how large each candidate of the catalogue is, from its own annual report.

The catalogue (catalogue_build.py) lists the companies that file a European machine-readable annual report (ESEF)
and that lantrNa does not hold. To choose among two thousand of them the owner needs their size. Each report says
it: its revenue is tagged (esef_facts.py reads it the same way for the companies held). For every candidate still to
be chosen (status candidate or wanted, no duplicate found, a report for 2024 or later) this script asks
filings.xbrl.org for the company's latest filings, reads the newest one's facts and keeps the group's annual revenue:
the amount, its currency, the year, and the amount in euros at a fixed rough rate (RATES), which is for ordering a
list and for nothing else.

A bank or an insurer that tags no revenue concept gets none: the column stays empty, and the list says so.

    python catalogue_size.py [--limit N] [--again] [--commit]

Dry run by default: reads and counts, writes nothing. --commit writes the four columns of public.catalogue, every
25 companies, so a run cut short keeps what it read; a candidate that has its size is not asked again unless
--again. One company every 10 seconds (the site answers 429 to more); the run stops after five refusals in a row.
Prints counts only.
"""
import argparse, collections, os, sys, time

PAUSE = 10
# Euros for one unit, rounded, of November 2026: for ordering companies by size only, never shown as a figure.
RATES = {"EUR": 1.0, "SEK": 0.09, "DKK": 0.134, "NOK": 0.085, "GBP": 1.15, "USD": 0.92, "CHF": 1.06, "PLN": 0.235, "CZK": 0.04,
         "HUF": 0.0025, "RON": 0.2, "BGN": 0.511, "ISK": 0.0068, "JPY": 0.0058, "CNY": 0.127, "ZAR": 0.05, "CAD": 0.66, "AUD": 0.6}


def newest(facts):
    """(value, currency, year) of the latest annual revenue among esef_facts.annual_facts rows, the preferred concept
    first; None when the report tags none. A row: (rank, concept, wba year, end, value, currency, form, filed)."""
    good = [f for f in facts if f[4] is not None and f[4] > 0]
    if not good:
        return None
    end = max(f[3] for f in good)
    f = min((f for f in good if f[3] == end), key=lambda f: (f[0], -f[4]))
    return f[4], f[5], int(end[:4])


def in_euros(value, currency):
    """The amount in euros at the rough rate, or None for a currency not in RATES."""
    r = RATES.get((currency or "").upper())
    return round(value * r) if r and value is not None else None


def size_of(lei, sess, get_filings, get_json, year_min=2024):
    """("ok", (value, currency, year)) | ("no filing" | "no facts" | "no revenue" | "refused", None) for one LEI."""
    fl = get_filings(lei, sess, {year_min, year_min + 2})
    if not fl:
        return "no filing", None
    status, oim = get_json(sorted(fl, reverse=True)[0][1])
    if status == 429:
        return "refused", None
    if status != 200 or not oim:
        return "no facts", None
    from esef_facts import annual_facts
    got = newest(annual_facts(oim))
    return ("ok", got) if got else ("no revenue", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--again", action="store_true")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import requests, qa, evidence_db, robot_identity as bot
    from esef_facts import filings
    from catalogue_build import fetch_json
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("catalogue_size.py")
    sess = requests.Session(); sess.headers["User-Agent"] = bot.BOT_UA
    get_json = lambda url: fetch_json(lambda: sess.get(url, timeout=120), tries=2)
    seen, refused, pending = collections.Counter(), 0, []
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""select lei from public.catalogue where status in ('candidate', 'wanted') and duplicate_why is null and coalesce(last_year, 0) >= 2024
                             and (%s or size_at is null) order by (status = 'wanted') desc, lei""", (a.again,))
            todo = [r[0] for r in cur.fetchall()]; conn.rollback()
            if a.limit:
                todo = todo[:a.limit]
            print(f"{len(todo):,} candidates to size, one every {PAUSE} s (about {len(todo) * (PAUSE + 3) / 3600:.1f} h).", flush=True)

            def write():
                if a.commit and pending:
                    cur.executemany("update public.catalogue set revenue = %s, revenue_currency = %s, revenue_year = %s, revenue_eur = %s, size_at = now() where lei = %s", pending)
                    conn.commit()
                pending.clear()

            for n, lei in enumerate(todo, 1):
                try:
                    what, got = size_of(lei, sess, filings, get_json)
                except Exception as e:
                    what, got = "failed", None
                seen[what] += 1
                refused = refused + 1 if what == "refused" else 0
                if refused >= 5:
                    print("The site refused five times in a row: stopped. Run it again later; what was read is kept.", flush=True); break
                if what == "ok":
                    pending.append((got[0], got[1], got[2], in_euros(got[0], got[1]), lei))
                elif what in ("no filing", "no revenue", "no facts"):
                    pending.append((None, None, None, None, lei))          # asked: not asked again without --again
                if n % 25 == 0:
                    write(); print(f"  {n:,} asked: " + ", ".join(f"{k} {v:,}" for k, v in seen.most_common()), flush=True)
                time.sleep(PAUSE)
            write()
    print("Sized: " + (", ".join(f"{k} {v:,}" for k, v in seen.most_common()) or "none") + ("." if a.commit else ". Dry run: nothing written."))
    run.finish("ok", rows_in=sum(seen.values()), rows_out=seen["ok"] if a.commit else 0, notes="catalogue size")
    return 0


if __name__ == "__main__":
    sys.exit(main())
