#!/usr/bin/env python3
"""
library_mirror.py: the documents table in lanterN's own database, beside Airtable's report_library.

Phase 2 of leaving Airtable (claude plan of 11 Nov 2026; field list approved by the owner on 13 Nov). Every night
this script reads report_library and writes each document to public.library with the fields that move (FIELDS), and
works out by itself the two values Airtable computes with formulas, the document's year and its type, with the same
rules written here in Python (year_of, type_of). It then compares its own result with Airtable's on every row and
reports how many differ, by the rule that decided. Nothing reads public.library yet: it runs BESIDE Airtable until
the two agree for a stated number of days. That is the plan's one rule.

Not copied: `file` (the attachment: its links expire; the stored file is named by stored_file), `cover_text` (six
pages of text a document, already in the full text), the links between tables and the fields approved as dropped.
A document gone from Airtable is not deleted here: `gone` gets the day it was last seen missing.

    python library_mirror.py [--limit N] [--commit]

Prints counts only, and the library ids of a few rows that differ (ids, not names). Env: AIRTABLE_TOKEN,
AIRTABLE_BASE, SUPABASE_DB_URL; AIRTABLE_QA_TOKEN for the run record.
"""
import argparse, collections, datetime as dt, json, os, re, sys, time

import qa

FIELDS = [
    # identity and file
    "library_id", "wba_id", "company_name", "document_url", "stored_file", "file_status", "page_count", "file_size_mb", "content_hash",
    "doc_language", "first_seen", "created_time", "origin", "found_on", "link_text", "page_type", "source_link_id",
    # type
    "source_type", "source_type_check", "source_subtypes", "match_confidence", "robot_type", "curated_doc_title", "auto_title", "robot_title", "source_title",
    # year
    "doc_year", "doc_year_from", "fiscal_year", "year_score", "year_basis", "year_evidence", "period_end", "robot_year",
    "ai_fiscal_year", "ai_year_verified",            # the AI year test: still part of Airtable's year rule, so read while it is
    # dates of the file and of publication
    "pub_date", "pub_date_precision", "pub_date_source", "pub_date_evidence", "file_created", "file_modified", "file_dates_from",
    # content
    "sustainability_class", "class_reason", "sustainability_share", "frameworks", "scope", "scope_reason", "doc_scope", "assurance",
    "cover_status", "cover_ocr", "entity_check", "entity_name", "evidence_profile", "evidence_version",
    # review and lifecycle
    "discard", "discard_reason", "discarded_on", "verified", "Reviewer notes", "analyst_flag", "analyst_flag_note", "retry_note", "retried_on", "archive_url",
]
COMPARED = ["year", "year_source", "effective_type"]         # Airtable's own results, read only to compare

TABLE_SQL = """
create table if not exists public.library (
  library_id text primary key,
  record_id  text,
  wba_id     text,
  discard    boolean not null default false,
  fields     jsonb not null,
  year_ours  int, year_airtable int, year_rule text,
  type_ours  text, type_airtable text,
  swept      timestamptz not null default now(),
  gone       date);
alter table public.library enable row level security;
create index if not exists library_wba on public.library (wba_id)"""

BOND = re.compile(r"(?i)(due|matur[a-z]*|redeem[a-z]*|redemption|expir[a-z]*|until|callable)[^|;]{0,30}?20[0-9]{2}")


def text(v):
    """A cell as text: '' for empty; an AI field's value; a select's name."""
    if v is None or v is False:
        return ""
    if isinstance(v, dict):
        return str(v.get("value") if "value" in v else (v.get("name") or v.get("text") or "")) if v.get("state", "generated") != "error" else ""
    if isinstance(v, list):
        return ", ".join(text(x) for x in v)
    return str(v)


def number(s):
    """Airtable's VALUE() of a text: the number its digits spell, None when there is none."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(s or "").replace(",", ""))
    return (int(float(m.group())) if m else None)


def year_rules(source_title, doc_year, this_year):
    """Airtable's year_rules: the first year in the document's title that is not a bond's maturity and not later than
    the year after doc_year (or after this year); else doc_year."""
    dy = number(doc_year) if text(doc_year) else None
    t = BOND.sub("", text(source_title))
    m = re.search(r"20[12][0-9]", t)
    if m:
        cap = this_year + 1 if dy is None else min(dy + 1, this_year + 1)
        if int(m.group()) <= cap:
            return int(m.group())
    return dy


def year_of(f, this_year=None):
    """(year, rule) as Airtable's `year` and `year_source` formulas give them (formulas read on 14 Nov 2026):
    1 a year typed in curated_doc_title; 2 a regulator filing's doc_year; 3 the full text's fiscal_year with a score
    of 80 or more; 4 the Source Library's doc_year; 5 fiscal_year with 60 or more; 6 the AI year when its quote was
    verified; 7 the rules on the title and the address. (None, 'none') when nothing gives one."""
    this_year = this_year or dt.date.today().year
    curated, doc_year, src = text(f.get("curated_doc_title")), text(f.get("doc_year")), text(f.get("doc_year_from"))
    fiscal, score = f.get("fiscal_year") or 0, f.get("year_score") or 0
    m = re.search(r"20[0-3][0-9]", curated)
    if m:
        return int(m.group()), "1 your title"
    starts = re.match(r"20[0-3][0-9]", doc_year)
    if src == "filing" and starts:
        return int(doc_year[:4]), "2 regulator filing"
    if fiscal and score >= 80:
        return int(fiscal), "3 document text (high)"
    if src == "source library" and starts:
        return int(doc_year[:4]), "4 analyst (Source Library)"
    if fiscal and score >= 60:
        return int(fiscal), "5 document text (medium)"
    ai = re.match(r"20[0-3][0-9]", text(f.get("ai_fiscal_year")).strip())
    if ai and f.get("ai_year_verified"):
        return int(ai.group()), "6 AI (quote verified)"
    y = year_rules(f.get("source_title"), f.get("doc_year"), this_year)
    return (y, "7 rules (title / address)") if y else (None, "none")


def type_of(f):
    """Airtable's effective_type: the type a person (or the AI field) set, else the address classifier's."""
    return text(f.get("source_type_check")) or text(f.get("source_type"))


def row_of(rec, this_year=None):
    """(library_id, record id, wba_id, discard, fields json, year_ours, year_airtable, rule, type_ours, type_airtable), or None with no id."""
    f = rec.get("fields", {})
    lid = text(f.get("library_id")).strip()
    if not lid:
        return None
    y, rule = year_of(f, this_year)
    ya = f.get("year")
    keep = {k: f[k] for k in FIELDS if k in f and f[k] not in (None, "", [])}
    return (lid, rec.get("id"), text(f.get("wba_id")).strip() or None, bool(f.get("discard")), json.dumps(keep, ensure_ascii=False),
            y, int(ya) if isinstance(ya, (int, float)) and ya else None, rule, type_of(f) or None, text(f.get("effective_type")) or None)


def compare(rows):
    """Counts of agreement between our year and type and Airtable's, the year by the rule that decided here."""
    c = collections.Counter(); differ = collections.defaultdict(list)
    for r in rows:
        lid, y, ya, rule, t, ta = r[0], r[5], r[6], r[7], r[8], r[9]
        c["documents"] += 1
        if y == ya:
            c["year: the same"] += 1
        else:
            c["year: differs"] += 1; c[f"year differs, our rule {rule}"] += 1
            if len(differ["year"]) < 12:
                differ["year"].append(f"{lid} ours {y} Airtable {ya} ({rule})")
        if (t or "") == (ta or ""):
            c["type: the same"] += 1
        else:
            c["type: differs"] += 1
            if len(differ["type"]) < 12:
                differ["type"].append(lid)
    return c, differ


def write(conn, rows, complete):
    """Upsert in batches, only the rows that changed; with a complete sweep, the rows not seen get `gone`."""
    n = 0
    with conn.cursor() as c:
        for stmt in TABLE_SQL.split(";"):
            c.execute(stmt)
        c.execute("select now()"); started = c.fetchone()[0]
    conn.commit()
    cols = "library_id, record_id, wba_id, discard, fields, year_ours, year_airtable, year_rule, type_ours, type_airtable"
    rest = cols.split(", ")[1:]
    for i in range(0, len(rows), 5000):
        with conn.cursor() as c:
            c.execute("create temp table tmp_library (like public.library including defaults) on commit drop")
            with c.copy(f"copy tmp_library ({cols}) from stdin") as cp:
                for r in rows[i:i + 5000]:
                    cp.write_row(r)
            c.execute(f"insert into public.library as l ({cols}) select distinct on (library_id) {cols} from tmp_library "
                      f"on conflict (library_id) do update set " + ", ".join(f"{k} = excluded.{k}" for k in rest) + ", swept = now(), gone = null "
                      f"where (" + ", ".join(f"l.{k}" for k in rest) + ") is distinct from (" + ", ".join(f"excluded.{k}" for k in rest) + ") or l.gone is not null")
            n += c.rowcount
        conn.commit()
    gone = 0
    if complete:
        with conn.cursor() as c:
            c.execute("create temp table tmp_seen (library_id text primary key) on commit drop")
            with c.copy("copy tmp_seen (library_id) from stdin") as cp:
                for lid in {r[0] for r in rows}:
                    cp.write_row((lid,))
            c.execute("update public.library l set gone = current_date where l.gone is null and not exists (select 1 from tmp_seen s where s.library_id = l.library_id)")
            gone = c.rowcount
        conn.commit()
    return n, gone


def main(argv=None):
    ap = argparse.ArgumentParser(description="The documents table in lanterN's database, beside Airtable")
    ap.add_argument("--limit", type=int, default=0, help="documents at most (0 = all; a partial sweep never marks a document gone)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("library_mirror.py")
    t0 = time.time()
    try:
        from base_health import sweep
        rows, seen = [], 0
        for rec in sweep("report_library", FIELDS + COMPARED):
            seen += 1
            r = row_of(rec)
            if r:
                rows.append(r)
            if seen % 50000 == 0:
                print(f"  [{(time.time() - t0) / 60:4.0f} min] {seen:,} rows read", flush=True)
            if a.limit and seen >= a.limit:
                break
        c, differ = compare(rows)
        print(f">>> {seen:,} rows read from report_library in {(time.time() - t0) / 60:.0f} min; {len(rows):,} documents with an id", flush=True)
        for k in sorted(c):
            print(f"  {k}: {c[k]:,}", flush=True)
        for kind, ids in differ.items():
            print(f"  first documents whose {kind} differs: " + "; ".join(ids), flush=True)
        wrote = gone = 0
        if a.commit:
            import evidence_db
            with evidence_db.connect() as conn:
                wrote, gone = write(conn, rows, complete=not a.limit)
            print(f">>> public.library: {wrote:,} rows written (new or changed), {gone:,} no longer in Airtable", flush=True)
        if c["documents"] and c["year: differs"] > 0.01 * c["documents"]:
            run.failure("DATA_CHECK", f"The year worked out in the database differs from Airtable's on {c['year: differs']:,} of {c['documents']:,} documents", key="library_year_differs")
    except Exception as e:
        run.failure("RUN_FAILED", f"library_mirror.py stopped: {type(e).__name__}", key="library_mirror", message=str(e)[:300])
        run.finish(status="failed")
        raise
    run.finish(status="ok", rows_in=seen, rows_out=wrote, counters=dict(c), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
