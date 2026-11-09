#!/usr/bin/env python3
"""
evidence_health.py: does the evidence database (Supabase) look the way it should? One read-only report a day.

base_health.py watches the Airtable base; nothing watched the evidence database, so a map built for the wrong
years (9 Nov 2026: 2% to 5% of the evaluation answers had a map) or a bulk-filled date were found hours later by
the evaluations, not by a look at the tables. This job looks at the tables every day and after every build:
  1. rows per table (exact for the small tables, the planner's estimate for pages and passages, which are millions);
  2. documents: by text status, with and without pages, by year bucket, without a year or a type;
  3. page_map: lines, with an embedding, documents mapped, lines by the document's year, lines written in 24 hours;
  4. value_versions: by method, in 24 hours; model_reads: in 24 hours and the found share;
  5. checks, each a plain sentence when it fails and a DATA_CHECK failure in Pipeline QA (key = the check's name):
       map_embeddings     page_map lines without an embedding after a build
       map_years          page_map lines whose document year is 2025 or later outnumber 2023 and 2024 (the build order bug)
       docs_without_pages documents with text_status ok and no page
       pages_unindexed    pages whose words are null (the index backfill behind)
       stale_versions     no value_versions in 3 days (only with --nightly-values: Propose values runs by hand today)
       orphan_versions    value_versions whose library_id is not in documents
Writes evidence_health.md (the report); the Pipeline QA run row carries the counters. Never writes to the database.
Env: SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
"""
import argparse, collections, os, sys, time
from datetime import datetime, timezone

BIG_TABLES = ("pages", "passages")            # counted from the planner's estimate: an exact count scans millions of rows
YEAR_BUCKETS = (("2021 or earlier", "year <= 2021"), ("2022", "year = 2022"), ("2023", "year = 2023"), ("2024", "year = 2024"),
                ("2025", "year = 2025"), ("2026 or later", "year >= 2026"), ("no year", "year is null or year = 0"))


def one(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def rows(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchall()


def table_counts(cur):
    """{table: rows}: exact counts, the planner's estimate for the big tables."""
    names = [r[0] for r in rows(cur, "select tablename from pg_tables where schemaname = 'public' order by tablename")]
    out = {}
    for t in names:
        if t in BIG_TABLES:
            out[t] = int(one(cur, "select coalesce(reltuples, 0)::bigint from pg_class where oid = %s::regclass", (f"public.{t}",)))
        else:
            out[t] = one(cur, f"select count(*) from public.{t}")
    return out


def documents(cur):
    d = {"total": one(cur, "select count(*) from public.documents"),
         "by_status": dict(rows(cur, "select coalesce(text_status, '(none)'), count(*) from public.documents group by 1 order by 2 desc")),
         "with_pages": one(cur, "select count(*) from public.documents d where exists (select 1 from public.pages p where p.library_id = d.library_id)"),
         "no_type": one(cur, "select count(*) from public.documents where coalesce(doc_type, '') = ''"),
         "by_year": {name: one(cur, f"select count(*) from public.documents where {cond}") for name, cond in YEAR_BUCKETS}}
    d["ok_without_pages"] = one(cur, "select count(*) from public.documents d where text_status = 'ok' and not exists (select 1 from public.pages p where p.library_id = d.library_id)")
    return d


def page_map(cur):
    m = {"lines": one(cur, "select count(*) from public.page_map"),
         "embedded": one(cur, "select count(embedding) from public.page_map"),
         "documents": one(cur, "select count(distinct library_id) from public.page_map"),
         "last_24h": one(cur, "select count(*) from public.page_map where written > now() - interval '24 hours'"),
         "by_year": dict(rows(cur, "select coalesce(d.year, 0), count(*) from public.page_map m join public.documents d using (library_id) group by 1 order by 1"))}
    return m


def values(cur):
    return {"by_method": dict(rows(cur, "select method, count(*) from public.value_versions group by 1 order by 2 desc")),
            "last_24h": one(cur, "select count(*) from public.value_versions where last_seen > now() - interval '24 hours'"),
            "last_3d": one(cur, "select count(*) from public.value_versions where last_seen > now() - interval '3 days'"),
            "orphans": one(cur, "select count(*) from public.value_versions v where not exists (select 1 from public.documents d where d.library_id = v.library_id)"),
            "reads_24h": one(cur, "select count(*) from public.model_reads where read_on > now() - interval '24 hours'"),
            "reads_found_24h": one(cur, "select count(*) from public.model_reads where read_on > now() - interval '24 hours' and found")}


def unindexed_pages(cur):
    """Pages whose word index is null, as the planner sees it: cheap, approximate."""
    try:
        return one(cur, "select count(*) from (select 1 from public.pages where words is null limit 10001) s")
    except Exception:
        return None


def checks(docs, pmap, vals, unindexed, nightly_values=True):
    """[(name, sentence)] for every check that fails. Thresholds are stated, not tuned."""
    out = []
    if pmap["lines"] and pmap["embedded"] < pmap["lines"]:
        out.append(("map_embeddings", f"{pmap['lines'] - pmap['embedded']:,} page_map lines have no embedding (run doc_map embed)."))
    recent = sum(n for y, n in pmap["by_year"].items() if y and y >= 2025)
    core = sum(n for y, n in pmap["by_year"].items() if y in (2023, 2024))
    if pmap["lines"] and recent > core:
        out.append(("map_years", f"page_map holds {recent:,} lines of 2025 or later documents against {core:,} of 2023 and 2024: the map covers the comparatives, not the years the answers read."))
    if docs["ok_without_pages"]:
        out.append(("docs_without_pages", f"{docs['ok_without_pages']:,} documents have text_status ok and no page in public.pages."))
    if unindexed:
        out.append(("pages_unindexed", f"{'10,000 or more' if unindexed > 10000 else format(unindexed, ',')} pages have no word index (words is null)."))
    if nightly_values and vals["last_3d"] == 0:
        out.append(("stale_versions", "no value_versions row seen in 3 days while Propose values runs nightly."))
    if vals["orphans"]:
        out.append(("orphan_versions", f"{vals['orphans']:,} value_versions rows cite a library_id that is not in public.documents."))
    return out


def report_md(counts, docs, pmap, vals, unindexed, failed, today):
    md = [f"# Evidence database health ({today.isoformat()})", "",
          ("Every check passed." if not failed else f"{len(failed)} check(s) failed:"), ""]
    md += [f"- **{name}**: {text}" for name, text in failed]
    md += ["", "## Rows per table (pages and passages estimated)", "", "| Table | Rows |", "| --- | --: |"]
    md += [f"| {t} | {n:,} |" for t, n in sorted(counts.items(), key=lambda x: -x[1])]
    md += ["", "## Documents", "",
           f"{docs['total']:,} documents; {docs['with_pages']:,} with pages; {docs['no_type']:,} without a type; {docs['ok_without_pages']:,} with text_status ok and no page.", "",
           "| Text status | Documents |", "| --- | --: |"] + [f"| {s} | {n:,} |" for s, n in docs["by_status"].items()]
    md += ["", "| Year | Documents |", "| --- | --: |"] + [f"| {y} | {n:,} |" for y, n in docs["by_year"].items()]
    md += ["", "## Page map", "",
           f"{pmap['lines']:,} lines ({pmap['embedded']:,} with an embedding) over {pmap['documents']:,} documents; {pmap['last_24h']:,} lines written in the last 24 hours.", "",
           "| Document year | Lines |", "| --- | --: |"] + [f"| {y or 'no year'} | {n:,} |" for y, n in pmap["by_year"].items()]
    md += ["", "## Values and reads", "",
           f"value_versions: {sum(vals['by_method'].values()):,} rows (" + ", ".join(f"{m} {n:,}" for m, n in vals["by_method"].items()) + f"); {vals['last_24h']:,} seen in 24 hours, {vals['last_3d']:,} in 3 days; {vals['orphans']:,} orphans.",
           f"model_reads: {vals['reads_24h']:,} in 24 hours, {vals['reads_found_24h']:,} found the year's figure.",
           "", f"Pages without a word index: {'10,000 or more' if (unindexed or 0) > 10000 else format(unindexed or 0, ',')}.", ""]
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write the run row and the failures to Pipeline QA")
    ap.add_argument("--nightly-values", action="store_true", help="run the stale_versions check (only once Propose values is scheduled; today it runs by hand, in passes)")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    import qa, evidence_db
    run = qa.Run("evidence_health.py")
    today = datetime.now(timezone.utc).date()
    t0 = time.time()
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '120s'")
        counts = table_counts(cur)
        docs = documents(cur)
        pmap = page_map(cur)
        vals = values(cur)
        unindexed = unindexed_pages(cur)
    failed = checks(docs, pmap, vals, unindexed, nightly_values=a.nightly_values)
    for name, text in failed:
        run.failure("DATA_CHECK", text, key=name, stage="evidence_health")
    md = report_md(counts, docs, pmap, vals, unindexed, failed, today)
    open("evidence_health.md", "w", encoding="utf-8").write(md)
    print(md)
    c = {"documents": docs["total"], "with_pages": docs["with_pages"], "map_lines": pmap["lines"], "map_embedded": pmap["embedded"],
         "map_documents": pmap["documents"], "versions_24h": vals["last_24h"], "reads_24h": vals["reads_24h"], "checks_failed": len(failed),
         "seconds": int(time.time() - t0)}
    run.finish("ok" if not failed else "partial", rows_in=docs["total"], rows_out=0, counters=c,
               notes=("every check passed" if not failed else "; ".join(n for n, _ in failed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
