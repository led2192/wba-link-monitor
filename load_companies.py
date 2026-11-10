#!/usr/bin/env python3
"""
load_companies.py: the company profiles into the evidence database, for Lantern's filters.

The evidence database knows which company a page belongs to, not what the company is. Airtable's companies
table holds, for the SDG2000 list, the WBA industry (one of 36), the headquarters country and the World Bank
region (load_company_master.py). The owner wants to search "oil and gas companies that mention WBA" or
"banks in Japan": the industry, the country and the region become filters the box recognises, like a company
name. This script copies those columns into public.companies every night (2,000 rows, seconds), so the
search functions join on them. Read-only on Airtable; upsert by wba_id in Postgres; never deletes.

    python load_companies.py [--commit]

Dry run prints the counts by industry and region. Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SUPABASE_DB_URL;
AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, os, sys, time

FIELDS = ["wba_id", "company_name", "wba_industry", "hq_country", "hq_region", "website"]


def company_rows(records):
    """[(wba_id, name, industry, country, region, website)] from Airtable records; rows without a wba_id are skipped."""
    out = []
    for rec in records:
        f = rec.get("fields", {})
        wba = (f.get("wba_id") or "").strip()
        if not wba:
            continue
        out.append((wba, (f.get("company_name") or "").strip() or None, (f.get("wba_industry") or "").strip() or None,
                    (f.get("hq_country") or "").strip() or None, (f.get("hq_region") or "").strip() or None,
                    (f.get("website") or "").strip() or None))
    return out


def upsert(conn, rows):
    with conn.cursor() as c:
        c.execute("""create table if not exists public.companies (
                       wba_id text primary key, name text, industry text, country text, region text, website text,
                       updated timestamptz not null default now())""")
        c.execute("alter table public.companies enable row level security")
        c.execute("create index if not exists companies_industry on public.companies (industry)")
        c.execute("create index if not exists companies_country on public.companies (country)")
        c.execute("create index if not exists companies_region on public.companies (region)")
        c.executemany("""insert into public.companies (wba_id, name, industry, country, region, website)
                         values (%s, %s, %s, %s, %s, %s)
                         on conflict (wba_id) do update set name = excluded.name, industry = excluded.industry,
                           country = excluded.country, region = excluded.region, website = excluded.website, updated = now()""", rows)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE", "SUPABASE_DB_URL"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa, evidence_db
    from base_health import sweep
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("load_companies.py")
    t0 = time.time()
    rows = company_rows(sweep("companies", FIELDS))
    by_ind = collections.Counter(r[2] or "(none)" for r in rows)
    by_reg = collections.Counter(r[4] or "(none)" for r in rows)
    print(f"{len(rows):,} companies read from Airtable in {time.time() - t0:.0f}s; {len(by_ind):,} industries, {len(by_reg):,} regions", flush=True)
    print("  industries: " + ", ".join(f"{k} {n}" for k, n in by_ind.most_common(40)), flush=True)
    print("  regions: " + ", ".join(f"{k} {n}" for k, n in by_reg.most_common()), flush=True)
    if a.commit:
        with evidence_db.connect() as conn:
            upsert(conn, rows)
        print(f"  {len(rows):,} rows upserted into public.companies", flush=True)
    run.finish("ok", rows_in=len(rows), rows_out=len(rows) if a.commit else 0,
               counters={"companies": len(rows), "industries": len(by_ind), "regions": len(by_reg), "without_industry": by_ind.get("(none)", 0)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
