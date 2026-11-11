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


UNHELD_FIELDS = ["library_id", "wba_id", "document_url", "doc_label", "effective_type", "file_status"]
UNHELD_FORMULA = "AND({file}=BLANK(), {document_url}!='', NOT({discard}))"
UNHELD_SQL = """create table if not exists public.unheld_documents (
                  library_id text primary key, wba_id text not null, url text not null, label text, doc_type text,
                  status text, updated timestamptz not null default now())"""


def unheld_rows(records):
    """The documents we know by their address and hold no file of (the site refused the robot, the link is dead):
    [(library_id, wba_id, url, label, type, status)]. The search shows them under a company's documents, with the
    link to the original, so that a person is not told "3 documents" where the library knows 12 (11 Nov 2026)."""
    out = []
    for rec in records:
        f = rec.get("fields", {})
        lid, wba, url = (f.get("library_id") or "").strip(), (f.get("wba_id") or "").strip(), (f.get("document_url") or "").strip()
        if not (lid and wba and url.lower().startswith(("http://", "https://"))) or f.get("file"):
            continue
        st = f.get("file_status")
        out.append((lid, wba, url, (f.get("doc_label") or "").strip() or None, (f.get("effective_type") or "").strip() or None,
                    (st.get("name") if isinstance(st, dict) else st) or None))
    return out


def upsert_unheld(conn, rows):
    """The table is the picture of today: rows of this run are written, rows of an earlier run are gone (a document
    that got its file since, or was discarded). A derived table, rebuilt whole; nothing of the library is deleted."""
    with conn.cursor() as c:
        c.execute(UNHELD_SQL)
        c.execute("alter table public.unheld_documents enable row level security")
        c.execute("create index if not exists unheld_documents_company on public.unheld_documents (wba_id)")
        c.execute("select now()"); started = c.fetchone()[0]
        c.executemany("""insert into public.unheld_documents (library_id, wba_id, url, label, doc_type, status)
                         values (%s, %s, %s, %s, %s, %s)
                         on conflict (library_id) do update set wba_id = excluded.wba_id, url = excluded.url, label = excluded.label,
                           doc_type = excluded.doc_type, status = excluded.status, updated = now()""", rows)
        c.execute("delete from public.unheld_documents where updated < %s", (started,))
    conn.commit()


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
    unheld = unheld_rows(sweep("report_library", UNHELD_FIELDS + ["file", "discard"], UNHELD_FORMULA))
    print(f"{len(unheld):,} documents known by their address with no file, of {len({r[1] for r in unheld}):,} companies", flush=True)
    if a.commit:
        with evidence_db.connect() as conn:
            upsert_unheld(conn, unheld)
        print(f"  {len(unheld):,} rows in public.unheld_documents", flush=True)
    run.finish("ok", rows_in=len(rows), rows_out=len(rows) if a.commit else 0,
               counters={"companies": len(rows), "documents known with no file": len(unheld), "industries": len(by_ind), "regions": len(by_reg), "without_industry": by_ind.get("(none)", 0)},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
