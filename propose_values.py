#!/usr/bin/env python3
"""
propose_values.py: proposed attribute values in the Disclosure Library (table attribute_values), for an
analyst to accept or reject. First source: revenue from the companies' machine-readable filings.

The two XBRL routes were measured against the validated answers on 28 and 29 Oct 2026: the SEC's
companyfacts (sec_facts.py, the rule's pick matched 89 % of the answers with a fact) and the European
ESEF reports on filings.xbrl.org (esef_facts.py, 83 %). This script applies the same rules to every
company that has the identifier, not only to the ones with a validated answer, for the years asked:

  SEC XBRL   companies.sec_cik; the annual revenue fact chosen by sec_facts.choose for each WBA year;
  ESEF XBRL  companies.lei with an ISIN of an ESEF country; the same choice on the facts of the
             company's ESEF filings (group total, no segment).

One row per company, attribute, year and method, with a deterministic value_id
("PT_00001|GMC_00048.1|2024|SEC XBRL"), so a rerun updates instead of duplicating. A row whose status a
person set (accepted, rejected) is never touched again. Each row says where the value came from (concept,
form, period end, filing date, a link to the filing) and, when the analysts validated the same company,
attribute and year in the 2025 benchmark, that value and whether it matches (within 0.5 %, same unit).

The company link is set by record id from companies.wba_id, never by name. Dry run by default: prints what
it would write. Options: --years (default 2018- : 2018 to last year; or 2024, or 2022,2023), --limit N companies per method, --methods sec,esef,
--commit. Reads the gold bases with GOLD_TOKEN (read-only); writes attribute_values and the run's counters
in Pipeline QA.

Method "model" (--methods model): the model reader (model_reader.py) reads the company's reports for each
number attribute in attributes.json and year in --model-years, with exactly the retrieval the Generic reader
scores (extract_eval.read_values). One row per company, attribute, year with method "model reader", the
document linked, the page, a link opening the PDF at that page, and what was printed and at which scale.
Years are read newest first and one call returns every year the pages print (31 Oct 2026): a year already
read as a comparison column of a later report is not read again, and the years returned beyond the ones
asked are written too (the history); `detail` says which report a comparison figure came from.
Revenue is left to the filing where a SEC or ESEF row exists. --companies and --attributes narrow it;
--max-usd stops it at an estimated cost. Needs ANTHROPIC_API_KEY and SUPABASE_DB_URL.
"""
import argparse, collections, json, os, sys, time, traceback
from datetime import datetime, timezone

T0 = time.time()

import qa

API = "https://api.airtable.com/v0"
TABLE = "attribute_values"
REVENUE = "GMC_00048.1"
PERSON_STATUSES = {"accepted", "rejected"}
MAX_NEW = 20000                     # rows a run may add (the base holds at most 500,000; see base_health)
HERE = os.path.dirname(os.path.abspath(__file__))


DEFAULT_YEARS = "2018-"              # 2018 to last year: the years WBA's benchmarks and trends look at


def parse_years(spec, today=None):
    """'2024' / '2022,2023' / '2018-2021' / '2018-' (to last year) -> sorted list of years."""
    last = (today or datetime.now(timezone.utc)).year - 1
    out = set()
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            out.update(range(int(a), int(b or last) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def how_to_find(v):
    """What to search for in the report: the value as income statements print it, in millions and in
    thousands ("Search the report for 60,515 (in millions) or 60,515,000 (in thousands)")."""
    m, k = v / 1e6, v / 1e3
    ms = f"{m:,.0f}" if abs(m - round(m)) < 0.05 or m >= 1000 else f"{m:,.1f}"
    return f"Search the report for {ms} (in millions) or {k:,.0f} (in thousands), in the income statement."


def value_id(wba, attr, year, method):
    return f"{wba}|{attr}|{year}|{method}"


def sec_accession(companyfacts, fact):
    """The accession number of the filing a chosen SEC fact came from, or ''."""
    ns, concept = fact[1].split(":", 1)
    for rows in companyfacts.get("facts", {}).get(ns, {}).get(concept, {}).get("units", {}).values():
        for f in rows:
            if f.get("end") == fact[3] and f.get("filed") == fact[7] and float(f.get("val", "nan")) == fact[4]:
                return f.get("accn") or ""
    return ""


def sec_main_documents(cik, sess):
    """{accession number: the filing's main document} from the SEC's submissions list, or {}."""
    from regulator_filings import SEC_UA
    try:
        r = sess.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers=SEC_UA, timeout=60)
    except Exception:
        return {}
    time.sleep(0.15)
    if not r.ok:
        return {}
    rec = r.json().get("filings", {}).get("recent", {})
    return {acc: doc for acc, doc in zip(rec.get("accessionNumber", []), rec.get("primaryDocument", [])) if doc}


def fetch_json(sess, url, tries=3, timeout=180):
    """A filing's JSON, or None after `tries` attempts. filings.xbrl.org answers slowly for big reports and
    now and then not at all; one failed read used to look like a company with no facts (31 Oct 2026: the
    source "answered" for 146 companies one night and 134 the next morning, with no change on its side)."""
    for attempt in range(tries):
        try:
            r = sess.get(url, timeout=timeout)
            if r.ok:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception:
            pass
        time.sleep(5 * (attempt + 1))
    return None


def sec_url(cik, accn, doc=""):
    """The filing to read: its main document in the SEC's inline viewer (the report itself, figures
    highlighted) when known, else the filing's folder, else the company's list of 10-K filings."""
    if accn and doc:
        return f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{doc}"
    if accn:
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-K"


def row(wba, attr, attr_name, year, fact, method, url, gold, company_rec, now):
    """The Airtable fields of one proposal. fact: sec_facts' (rank, concept, year, end, value, currency,
    form, filed)."""
    from sec_facts import matches
    g = gold.get((wba, attr, year))
    f = {"value_id": value_id(wba, attr, year, method), "wba_id": wba, "attribute": attr,
         "attribute_name": attr_name, "year": year, "value": fact[4], "unit": fact[5], "method": method,
         "source_url": url, "last_run": now,
         "detail": f"{how_to_find(fact[4])} Concept {fact[1]}; {fact[6]}; period ending {fact[3]}; filed {fact[7] or 'unknown'}.",
         "matches_validated": "no validated answer", "validated_value": None}
    if company_rec:
        f["company"] = [company_rec]
    if g:
        f["validated_value"] = g["value"]
        f["matches_validated"] = "match" if matches(g["value"], g["unit"], fact) else "differs"
        f.update(validated_fields(g))
    return f


SCALE_WORDS = {1: "units", 1e3: "thousands", 1e4: "ten thousands", 1e5: "lakhs", 1e6: "millions", 1e7: "crores", 1e8: "hundred millions", 1e9: "billions"}


def printed_text(x):
    """A number as the report prints it, every digit kept: 41709.13 -> '41,709.13', 2804.0 -> '2,804'.
    (The format g keeps six significant digits and showed 41,709.13 as 41,709.1, 30 Oct 2026.)"""
    s = f"{x:,.6f}".rstrip("0").rstrip(".")
    return s


def validated_fields(g):
    """Where the analyst took the validated value from, for a reviewer to check it too: the cited
    document (name and link), the page as the analyst wrote it, the quoted evidence and a link to the
    answer in the analysts' editor. Empty fields stay unset."""
    src = (g.get("sources") or [{}])[0]
    f = {"validated_source": src.get("url") or None,
         "validated_source_name": "; ".join(s["name"] for s in g.get("sources") or [] if s.get("name"))[:250] or None,
         "validated_page": g.get("page") or None,
         "validated_quote": (g.get("quote") or "")[:2000] or None,
         "validated_record": g.get("editor_url") if (g.get("editor_url") or "").startswith("http") else None}
    return f


def model_row(wba, code, attr, year, got, doc, gold, company_rec, now):
    """The Airtable fields of one model-reader proposal. got: model_reader's checked reply; doc: the
    report_library row of its document ({"id", "url", "label"}) or {}."""
    g = gold.get((wba, code, year))
    unit = got.get("unit") or {"tco2e": "tCO2e", "count": "employees"}.get(attr.get("unit_family"), "")
    scale = SCALE_WORDS.get(got["scale"], f"x{got['scale']:g}")
    f = {"value_id": value_id(wba, code, year, "model reader"), "wba_id": wba, "attribute": code,
         "attribute_name": attr["name"], "year": year, "value": got["value"], "unit": unit, "method": "model reader",
         "library_id": got["library_id"], "page": int(got["page"]), "last_run": now,
         "source_url": (doc.get("url") + f"#page={int(got['page'])}") if doc.get("url") else None,
         "detail": f"Printed as {printed_text(got['printed'])} ({scale})"
                   + (f" in the column \"{got['column']}\"" if got.get("column") else "") + f" on page {int(got['page'])}"
                   + (f" of {doc['label']}" if doc.get("label") else "") + ". Read by a language model; the number was checked to be on that page."
                   + (f" A fiscal year ending January to June counts as the previous reporting year, so this is {year}." if got.get("column") and str(year) not in got["column"] else "")
                   + (f" Read from the pages of the {got['asked']} report, where it is a comparison figure for {year}; a value the {year} report "
                      f"itself prints may differ if restated." if got.get("asked") and got["asked"] != year else "")
                   + (" " + got["basis_note"] if got.get("basis_note") else ""),
         "matches_validated": "no validated answer", "validated_value": None}
    if company_rec:
        f["company"] = [company_rec]
    if doc.get("id"):
        f["document"] = [doc["id"]]
    if g:
        f["validated_value"] = g["value"]
        f["matches_validated"] = "match" if g["value"] and abs(got["value"] - g["value"]) / g["value"] <= 0.005 else "differs"
        f.update(validated_fields(g))
    return f


def library_docs(H, base, ids):
    """{library_id: {"id", "url", "label"}} for the report_library rows of these library ids."""
    out, ids = {}, sorted(set(i for i in ids if i))
    for i in range(0, len(ids), 40):
        part = ids[i:i + 40]
        formula = "OR(" + ",".join(f"{{library_id}}='{x}'" for x in part) + ")"
        for r in fetch(H, base, "report_library", ["library_id", "document_url", "doc_label"], formula):
            f = r["fields"]
            out[f.get("library_id")] = {"id": r["id"], "url": f.get("document_url") or "", "label": f.get("doc_label") or ""}
    return out


def plan(proposals, existing, now, scope=None):
    """(creates, updates, kept): new rows get status proposed and first_proposed; existing rows the robot
    owns are updated; rows a person decided are left alone. scope(value_id) -> bool says which existing rows
    this run covers (its methods and years): a covered row still "proposed" that the run no longer proposes
    (a rule changed) becomes "superseded", never deleted."""
    creates, updates, kept = [], [], 0
    seen = {f["value_id"] for f in proposals}
    for vid, ex in existing.items():
        if scope and scope(vid) and vid not in seen and ex["status"] == "proposed":
            updates.append((ex["id"], {"status": "superseded", "last_run": now}))
    for f in proposals:
        ex = existing.get(f["value_id"])
        if not ex:
            creates.append(dict(f, status="proposed", first_proposed=now))
        elif ex["status"] in PERSON_STATUSES:
            kept += 1
        elif ex["status"] == "superseded":              # proposed again (a source answered again, a rule came back)
            updates.append((ex["id"], dict(f, status="proposed")))
        else:
            updates.append((ex["id"], f))
    return creates, updates, kept


def fetch(H, base, table, fields, formula=None):
    from monitor_core import airtable_request
    params = [("pageSize", "100")] + [("fields[]", x) for x in fields] + ([("filterByFormula", formula)] if formula else [])
    out, offset = [], None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{table}", H, params=p).json()
        out += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


WRITE_PAUSE = 0.22                   # between write requests; longer when parallel shards share the base


def shard_of(wba, n):
    """Which of n slices a company falls in (0-based), fixed for a given id."""
    import hashlib
    return int(hashlib.sha1((wba or "").encode("utf-8")).hexdigest()[:8], 16) % n


def write(H, base, creates, updates):
    from monitor_core import airtable_request
    clean = lambda f: {k: v for k, v in f.items() if v is not None}
    for i in range(0, len(creates), 10):
        airtable_request("POST", f"{API}/{base}/{TABLE}", H, {"records": [{"fields": clean(f)} for f in creates[i:i + 10]]})
        time.sleep(WRITE_PAUSE)
    for i in range(0, len(updates), 10):
        airtable_request("PATCH", f"{API}/{base}/{TABLE}", H,
                         {"records": [{"id": rid, "fields": f} for rid, f in updates[i:i + 10]]})
        time.sleep(WRITE_PAUSE)


def model_proposals(a, spec, comps, rec, existing, gold, H, base, now, c):
    """Model-reader proposals for the companies, attributes and years asked. Revenue is skipped for a
    company and year that already has a SEC or ESEF row: the filing is the better source."""
    import psycopg
    from extract_eval import read_values
    from model_reader import cost
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        c["model reader: no ANTHROPIC_API_KEY"] += 1
        return []
    codes = [x for x in (a.attributes.split(",") if a.attributes else [k for k, v in spec.items() if v.get("type") == "number"]) if x in spec]
    years = parse_years(a.model_years)
    wanted = [x.strip() for x in a.companies.split(",") if x.strip()]
    companies = wanted or [r["fields"]["wba_id"] for r in comps if r["fields"].get("wba_id")]
    companies = companies[:a.limit] if a.limit else companies
    has_filing = {tuple(v.split("|")[:3]) for v in existing if v.split("|")[-1] in ("SEC XBRL", "ESEF XBRL")}
    full = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))
    usage, got_rows, versions = collections.Counter(), [], []
    stop = False
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '90s'")
        for wba in companies:
            for code in codes:
                # Newest first: its report prints the earlier years as comparisons. A comparison figure no longer
                # stops the year from being read: on the 31 Oct 2026 run it matched the validated answer 66% of the
                # time against 76% for a figure from the year's own read (restatements; the analysts take the
                # figure as first reported). A year asked is read; its own figure replaces the comparison one,
                # which stays only when the year's read finds nothing.
                own, picked = set(), {}                  # years with a figure from their own read; year -> entry
                for y in sorted(years, reverse=True):
                    if cost(usage) >= a.max_usd:
                        c["model reader: stopped at the cost limit"] = 1; stop = True
                        break
                    if time.time() - T0 > a.budget * 60:          # leave time to write what was read
                        c["model reader: stopped at the time budget"] = 1; stop = True
                        break
                    if y in own:
                        c["model reader: year read in an earlier call"] += 1; continue
                    if code == REVENUE and (wba, code, str(y)) in has_filing:
                        c["model reader: revenue left to the filing"] += 1; continue
                    c["model reader: asked"] += 1
                    try:
                        got = read_values(cur, wba, y, spec[code], full, usage)
                    except Exception as e:
                        if type(e).__name__ == "ModelRefused":
                            raise
                        if not c["model reader: errors"]:
                            traceback.print_exc()
                            c[f"model reader: first error {type(e).__name__}: {str(e)[:120]}"] = 1
                        c["model reader: errors"] += 1
                        conn.rollback(); cur.execute("set statement_timeout = '90s'")
                        continue
                    for e in got:
                        versions.append((wba, code, e))                      # every figure found, kept in value_versions
                        if code == REVENUE and (wba, code, str(e["year"])) in has_filing:
                            c["model reader: revenue left to the filing"] += 1; continue
                        if e["year"] in own or (e["year"] != y and e["year"] in picked):
                            continue
                        if e["year"] == y:
                            if y in picked:
                                c["model reader: comparison figure replaced by the year's own read"] += 1
                            own.add(y)
                        picked[e["year"]] = e
                for yy, e in sorted(picked.items(), reverse=True):
                    c["model reader: values for the year asked" if yy in own else "model reader: values for other years"] += 1
                    got_rows.append((wba, code, yy, e))
                if stop:
                    break
            if stop:
                break
        c["model reader: figures found (all versions)"] = len(versions)
        if getattr(a, "commit", False) and versions:
            try:
                c["model reader: versions written"] = write_versions(conn, versions, "model reader", now)
            except Exception as e:
                conn.rollback()
                c[f"model reader: versions not written ({type(e).__name__}: {str(e)[:100]})"] = 1
    docs = library_docs(H, base, [g["library_id"] for *_, g in got_rows] + [e["library_id"] for *_, e in versions])
    c["model reader: cost in USD (estimate)"] = round(cost(usage), 2)
    c["model reader: calls"] = usage["calls"]
    others = collections.defaultdict(list)
    for wba, code, e in versions:
        others[(wba, code, e["year"])].append(e)
    rows = []
    for wba, code, y, got in got_rows:
        f = model_row(wba, code, spec[code], y, got, docs.get(got["library_id"], {}), gold, rec.get(wba), now)
        f.update(other_reports(got, others[(wba, code, y)], docs))
        c["model reader: restated in another report"] += 1 if f["restated"] else 0
        rows.append(f)
    return rows


def version_id(wba, code, year, library_id, method):
    import hashlib
    return hashlib.sha1(f"{wba}|{code}|{year}|{library_id}|{method}".encode("utf-8")).hexdigest()


VERSIONS_SQL = """create table if not exists public.value_versions (
  version_id text primary key, wba_id text not null, attribute text not null, year int not null,
  value double precision not null, unit text, printed text, scale double precision, column_label text, basis text,
  library_id text not null, page int, read_for int, method text not null,
  first_seen timestamptz not null default now(), last_seen timestamptz not null default now())"""


def write_versions(conn, versions, method, now):
    """Upsert every figure found into value_versions (schema.sql); one row per company, attribute, year,
    document and method, so a figure read again updates its row. Returns the rows written."""
    rows = {}
    for wba, code, e in versions:
        k = version_id(wba, code, e["year"], e["library_id"], method)
        rows[k] = (k, wba, code, int(e["year"]), float(e["value"]), e.get("unit"), str(e.get("printed", "")), e.get("scale"),
                   e.get("column"), e.get("basis"), e["library_id"], int(e["page"]) if e.get("page") is not None else None,
                   e.get("asked"), method, now, now)
    cur = conn.cursor()
    cur.execute(VERSIONS_SQL)
    cur.executemany(
        "insert into public.value_versions (version_id, wba_id, attribute, year, value, unit, printed, scale, column_label, basis, "
        "library_id, page, read_for, method, first_seen, last_seen) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "on conflict (version_id) do update set value = excluded.value, unit = excluded.unit, printed = excluded.printed, "
        "scale = excluded.scale, column_label = excluded.column_label, basis = excluded.basis, page = excluded.page, "
        "read_for = excluded.read_for, last_seen = excluded.last_seen", list(rows.values()))
    conn.commit()
    return len(rows)


def other_reports(chosen, versions, docs):
    """{"other_reports": text, "restated": bool}: the same company, attribute and year as printed in other
    documents, one line each with the difference from the value shown. Restated = one differs by more than 0.5%."""
    lines, restated, seen = [], False, {chosen["library_id"]}
    for e in sorted(versions, key=lambda e: -(e.get("asked") or 0)):
        if e["library_id"] in seen:
            continue
        seen.add(e["library_id"])
        diff = (e["value"] - chosen["value"]) / chosen["value"] if chosen["value"] else 0.0
        restated = restated or abs(diff) > 0.005
        label = docs.get(e["library_id"], {}).get("label") or e["library_id"]
        lines.append(f"{label}, page {e.get('page')}: {e['value']:,.6g}" + (" (same)" if abs(diff) <= 0.005 else f" ({diff:+.1%})"))
    return {"other_reports": "\n".join(lines) or None, "restated": restated}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--years", default=DEFAULT_YEARS, help="e.g. 2024 or 2022,2023 or 2018- (2018 to last year)")
    ap.add_argument("--limit", type=int, default=0, help="companies per method (0 = all)")
    ap.add_argument("--methods", default="sec,esef", help="sec, esef, model (the model reader on the company's reports)")
    ap.add_argument("--model-years", default="2024", help="years the model reader reads (it costs per company and year)")
    ap.add_argument("--attributes", default="", help="model reader: GMC codes (blank = every number attribute)")
    ap.add_argument("--companies", default="", help="model reader: wba ids, comma-separated (blank = every company)")
    ap.add_argument("--max-usd", type=float, default=1.0, help="model reader: stop when the estimated cost reaches this")
    ap.add_argument("--budget", type=float, default=300, help="minutes: the model reader stops reading after this, "
                    "so what it read is still written before the job's limit (one answer takes about 15 to 20 s)")
    ap.add_argument("--shard", default="", help="i/N: only the i-th of N slices of the companies (parallel jobs)")
    a = ap.parse_args()
    shard_i, shard_n = (int(x) for x in a.shard.split("/")) if a.shard else (1, 1)
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    import requests
    from sec_facts import annual_facts as sec_annual, choose, companyfacts
    from esef_facts import ESEF_ISIN, annual_facts as esef_annual, filings
    years = parse_years(a.years)
    methods = set(a.methods.split(","))
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("propose_values.py")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    spec = json.load(open(os.path.join(HERE, "attributes.json"), encoding="utf-8"))["attributes"]
    attr_name = spec[REVENUE]["name"]

    gold = {}
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if token:
        from value_eval import gold_sources, gold_values
        cfg = json.load(open(os.path.join(HERE, "gold_attributes.json"), encoding="utf-8"))
        for b in cfg["bases"].values():
            try:
                srcs = gold_sources(token, b)
            except Exception as e:                  # a base without a readable Sources table: values still come
                print(f"  sources of {b} not read: {str(e)[:120]}", flush=True); srcs = {}
            for g in gold_values(token, b, [c for c, v in spec.items() if v.get("type") == "number"], sources=True):
                if g["value"]:
                    g["sources"] = [srcs[i] for i in g.get("source_ids", []) if i in srcs]
                    gold[(g["wba"], g["attribute"], g["year"])] = g
    comps = fetch(H, base, "companies", ["wba_id", "sec_cik", "lei", "isin"])
    if shard_n > 1:                                   # parallel jobs: each takes a fixed slice of the companies
        comps = [r for r in comps if shard_of(r["fields"].get("wba_id", ""), shard_n) == shard_i - 1]
        global WRITE_PAUSE
        WRITE_PAUSE = 0.22 * shard_n                  # N jobs share the base's five requests a second
    rec = {r["fields"]["wba_id"]: r["id"] for r in comps if r["fields"].get("wba_id")}
    existing = {r["fields"]["value_id"]: {"id": r["id"], "status": (r["fields"].get("status") or "")}
                for r in fetch(H, base, TABLE, ["value_id", "status"]) if r["fields"].get("value_id")}
    print(f">>> {len(rec):,} companies; {len(existing):,} rows already in {TABLE}; {len(gold):,} validated revenue answers; years {years}", flush=True)

    sess, c, proposals = requests.Session(), collections.Counter(), []
    answered = set()                                     # (wba_id, method) whose source returned revenue facts this run
    if "sec" in methods:
        ciks = [(r["fields"]["wba_id"], str(r["fields"]["sec_cik"])) for r in comps
                if r["fields"].get("wba_id") and str(r["fields"].get("sec_cik") or "").strip() not in ("", "none")]
        ciks = ciks[:a.limit] if a.limit else ciks
        for wba, cik in ciks:
            time.sleep(0.15)                                    # the SEC asks for at most ten requests a second
            cf = companyfacts(cik, sess) or {}
            facts = sec_annual(cf)
            docs = sec_main_documents(cik, sess) if facts else {}
            c["SEC companies"] += 1; c["SEC companies with revenue facts"] += bool(facts)
            if facts:
                answered.add((wba, "SEC XBRL"))
            for y in years:
                f = choose(facts, y)
                if f:
                    accn = sec_accession(cf, f)
                    proposals.append(row(wba, REVENUE, attr_name, y, f, "SEC XBRL", sec_url(cik, accn, docs.get(accn, "")), gold, rec.get(wba), now))
    if "esef" in methods:
        leis = [(r["fields"]["wba_id"], r["fields"]["lei"].strip()) for r in comps
                if r["fields"].get("wba_id") and (r["fields"].get("lei") or "").strip()
                and (r["fields"].get("isin") or "")[:2].upper() in ESEF_ISIN]
        leis = leis[:a.limit] if a.limit else leis
        for wba, lei in leis:
            facts, src = [], {}
            listed = filings(lei, sess, set(years))
            c["ESEF filings listed"] += len(listed)
            for pe, ju, added, viewer in listed:
                j = fetch_json(sess, ju)                   # a big report's JSON times out now and then: three tries
                if j is None:
                    c["ESEF filings not read"] += 1; continue
                c["ESEF filings read"] += 1
                try:
                    fs = esef_annual(j, added)
                except Exception:
                    c["ESEF filings not read"] += 1; continue
                facts += fs
                for f in fs:
                    src.setdefault((f[3], f[7]), viewer or ju.rsplit("/", 1)[0] + "/")
                time.sleep(0.3)
            c["ESEF companies"] += 1; c["ESEF companies with revenue facts"] += bool(facts)
            if facts:
                answered.add((wba, "ESEF XBRL"))
            for y in years:
                f = choose(facts, y)
                if f:
                    proposals.append(row(wba, REVENUE, attr_name, y, f, "ESEF XBRL", src.get((f[3], f[7]), ""), gold, rec.get(wba), now))

    if "model" in methods:
        proposals += model_proposals(a, spec, comps, rec, existing, gold, H, base, now, c)

    for p in proposals:
        c[f"{p['method']}: proposals"] += 1
        c[f"{p['method']}: {p['matches_validated']}"] += 1
    names = {"sec": "SEC XBRL", "esef": "ESEF XBRL"}
    run_methods = {names[m] for m in methods if m in names}
    def scope(vid):
        parts = vid.split("|")
        return len(parts) == 4 and parts[1] == REVENUE and parts[3] in run_methods and int(parts[2]) in years and \
            (not a.limit) and \
            (parts[0], parts[3]) in answered               # a source that did not answer for the company supersedes nothing
    creates, updates, kept = plan(proposals, existing, now, scope)
    c["new rows"], c["updated rows"], c["left alone (a person decided)"] = len(creates), len(updates), kept
    c["superseded (no longer proposed)"] = sum(1 for _, f in updates if f.get("status") == "superseded")
    if len(creates) > MAX_NEW:
        raise SystemExit(f"{len(creates):,} new rows is more than MAX_NEW ({MAX_NEW:,}); check base_health first and raise it on purpose")
    if a.commit:
        write(H, base, creates, updates)
    md = [f"### Proposed values ({', '.join(sorted(methods))})" + ("" if a.commit else " (dry run: nothing written)"), "",
          "| | Count |", "| --- | --: |"] + [f"| {k} | {v:,} |" for k, v in sorted(c.items())] + [""]
    open("propose_values.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(proposals), rows_out=len(creates) + len(updates), counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("propose_values.md", "w").write("### Proposed values: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
