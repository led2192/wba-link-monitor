#!/usr/bin/env python3
"""
year_eval.py — does a change to the year rules make the year more right or less? Measured, before
anything is written.

For every document with a sure ground-truth year in Pipeline QA (labels with true_year: the
analysts' Source Library years, corrections and reviewed daily cards), the script recomputes the
{year} formula of report_library twice, from the same inputs:
  main    with the rules as they are on the default branch (fiscal_year.py and date_documents.py
          copied into --baseline, see the workflow)
  branch  with the rules in this checkout
and compares both with the true year. The inputs are what each rule reads: the full text (text
store) or, without it, the first pages (cover_text) for the fiscal-year reader; the link, file name
and first pages for doc_year; the stored AI title, curated title and verified AI year as they are.
It also scores the year currently stored in the base ("live").

Output: year_eval.md (accuracy per variant, documents that flip right->wrong and wrong->right with
the evidence), posted on the pull request by the workflow. Reads only; writes nothing anywhere.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE, TEXT_STORE_REPO + TEXT_STORE_TOKEN.
"""
import argparse, collections, datetime as dt, gzip, importlib.util, json, os, re, sys, tempfile, time

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
POLICIES = {"Code of Conduct", "Human Rights Policy", "Policy Documents"}
REPORT_FAMILY = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report",
                 "Annual Report", "10K Form"}
DOC_FIELDS = ["library_id", "wba_id", "company_name", "document_url", "file", "first_seen", "cover_text",
              "curated_doc_title", "doc_year", "doc_year_from", "source_title", "ai_year", "ai_year_verified",
              "source_type_check", "source_type", "year", "year_source", "discard"]
DUE = re.compile(r"(?i)(due|matur[a-z]*|redeem[a-z]*|redemption|expir[a-z]*|until|callable)[^|;]{0,30}?20[0-9]{2}")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get(token, base, table, fields, formula=None):
    H = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields] + ([("filterByFormula", formula)] if formula else [])
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/{table}", H, params=p).json()
        for r in j.get("records", []):
            f = r.get("fields", {}); f["_id"] = r["id"]; out.append(f)
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.15)


def text_of(v):
    return (v.get("value") or "") if isinstance(v, dict) else (v or "")


def year_rules(source_title, doc_year, this_year):
    """The {year_rules} formula: first year in the AI title unless it is after doc_year + 1."""
    t = DUE.sub("", text_of(source_title))
    m = re.search(r"20[12][0-9]", t)
    dy = int(doc_year) if re.fullmatch(r"20[0-3]\d", doc_year or "") else None
    if m:
        y = int(m.group(0))
        cap = this_year + 1 if dy is None else min(dy + 1, this_year + 1)
        if y <= cap:
            return y
    return dy


def formula_year(f, fy, score, doc_year, doc_from, this_year):
    """The {year} formula of report_library, in Python (keep in step with the Airtable field)."""
    cur = re.search(r"20[0-3]\d", f.get("curated_doc_title") or "")
    if cur:
        return int(cur.group(0)), "1 your title"
    if doc_from == "filing" and re.match(r"20[0-3]\d", doc_year or ""):
        return int(doc_year[:4]), "2 regulator filing"
    if fy and score >= 80:
        return fy, "3 text (high)"
    if doc_from == "source library" and re.match(r"20[0-3]\d", doc_year or ""):
        return int(doc_year[:4]), "4 analyst"
    if fy and score >= 60:
        return fy, "5 text (medium)"
    ai = f.get("ai_year")
    if ai and f.get("ai_year_verified") and re.fullmatch(r"20[0-3]\d", str(ai)):
        return int(ai), "6 AI verified"
    y = year_rules(f.get("source_title"), doc_year, this_year)
    return y, "7 rules" if y else "none"


def evaluate(f, pages, from_cover, FY, DD, this_year):
    """(year, source, evidence) for one document with one pair of rule modules."""
    doc_from = f.get("doc_year_from") or ""
    doc_from = doc_from.get("name", "") if isinstance(doc_from, dict) else doc_from
    doc_year = str(f.get("doc_year") or "")
    if doc_from not in ("filing", "source library"):
        att = (f.get("file") or [{}])[0]
        y, rule = DD.decide(f.get("document_url") or "", att.get("filename") or "", f.get("cover_text") or "", this_year)
        if y:
            doc_year = y
    t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
    fy, score, quote = None, 0, ""
    if t not in POLICIES and pages:
        import fiscal_years as FYS
        cap = FYS.cap_for(f)
        fy, _, score, quote, _ = FY.decide(pages, cap)
        if from_cover and quote:
            quote = "first pages: " + quote[quote.find(":") + 1:].strip()
    y, src = formula_year(f, fy, score, doc_year, doc_from, this_year)
    ev = quote if src.startswith(("3", "5")) else f"doc_year {doc_year}" if src == "7 rules" else src
    return y, src, ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="folder with the default branch's fiscal_year.py and date_documents.py")
    ap.add_argument("--out", default="year_eval.md")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    tok, base = os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"]
    qtok, qbase = os.environ["AIRTABLE_QA_TOKEN"], os.environ.get("AIRTABLE_QA_BASE", qa.QA_BASE_DEFAULT)
    this_year = dt.date.today().year
    variants = {"main": (load("fy_main", os.path.join(a.baseline, "fiscal_year.py")),
                         load("dd_main", os.path.join(a.baseline, "date_documents.py"))),
                "branch": (load("fy_branch", "fiscal_year.py"), load("dd_branch", "date_documents.py"))}

    labels = get(qtok, qbase, qa.T_LABELS, ["record_id", "true_year", "stratum", "sure", "library_id"],
                 "AND({true_year}, {sure}, {record_id}!='')")
    truth = {}
    for l in labels:
        try:
            truth.setdefault(l["record_id"], (int(str(l["true_year"])[:4]), l.get("stratum") or ""))
        except (ValueError, KeyError):
            pass
    ids = list(truth)[: a.limit or None]
    print(f">>> {len(ids)} documents with a sure true year.", flush=True)
    docs = {}
    for i in range(0, len(ids), 40):
        formula = "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in ids[i:i + 40]) + ")"
        for f in get(tok, base, "report_library", DOC_FIELDS, formula):
            if not f.get("discard"):
                docs[f["_id"]] = f
    print(f">>> {len(docs)} of them live in report_library.", flush=True)

    # full text where the store has it, else the first pages
    import text_core as tc
    store = tc.open_store()
    folder = tempfile.mkdtemp(prefix="ye-")
    names = [n for n in store.assets() if tc.part_of(n)[0]]
    tc.sync_down(store, [n for n in names if n.startswith("docs-")], folder)
    index = tc.load_docs_index(folder, names)
    by_lid = {d.get("library_id"): rid for rid, d in docs.items() if d.get("library_id")}
    pages = {}
    want = collections.defaultdict(set)
    for lid, rid in by_lid.items():
        d = index.get(lid)
        if d and d.get("status") == "ok":
            want[d["part"]].add(lid)
    tc.sync_down(store, [f"pages-{p}.jsonl.gz" for p in want], folder)
    for p, lids in want.items():
        with gzip.open(os.path.join(folder, f"pages-{p}.jsonl.gz"), "rt") as fh:
            for line in fh:
                r = json.loads(line)
                if r["id"] in lids:
                    pages.setdefault(by_lid[r["id"]], []).append((r["p"], r.get("t") or ""))
    print(f">>> full text for {len(pages)} documents; the rest from their first pages.", flush=True)

    score = {v: collections.Counter() for v in ("live", "main", "branch")}
    flips = {"fixed": [], "broken": []}
    for rid, f in docs.items():
        true, stratum = truth[rid]
        t = (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()
        fam = "report family" if t in REPORT_FAMILY else "other"
        full = sorted(pages.get(rid, []))
        pg, from_cover = (full, False) if full else ([(1, f.get("cover_text") or "")] if f.get("cover_text") else [], True)
        res = {v: evaluate(f, pg, from_cover, FY, DD, this_year) for v, (FY, DD) in variants.items()}
        live = f.get("year")
        for v, y in (("live", live), ("main", res["main"][0]), ("branch", res["branch"][0])):
            ok = isinstance(y, (int, float)) and int(y) == true
            for key in ("all", fam):
                score[v][key, "n"] += 1
                score[v][key, "ok"] += ok
        m_ok, b_ok = res["main"][0] == true, res["branch"][0] == true
        if m_ok != b_ok:
            flips["fixed" if b_ok else "broken"].append(
                (f.get("company_name") or "", text_of(f.get("source_title"))[:70] or f.get("library_id"), true,
                 res["main"], res["branch"]))

    def pct(v, key):
        n = score[v][key, "n"]
        return f"{score[v][key, 'ok'] / n:.1%} ({score[v][key, 'ok']:,}/{n:,})" if n else "-"
    lines = ["## Year rules: main vs this branch, against the true years", "",
             f"{len(docs):,} documents with a sure true year (Source Library analysts, corrections, reviewed cards). "
             "Same inputs for both; only the rules differ. Nothing written.", "",
             "| | all documents | report family | other |", "|---|---:|---:|---:|"]
    for v, name in (("live", "stored in the base now"), ("main", "rules on main"), ("branch", "rules in this PR")):
        lines.append(f"| {name} | {pct(v, 'all')} | {pct(v, 'report family')} | {pct(v, 'other')} |")
    lines += ["", f"**{len(flips['fixed'])} documents fixed, {len(flips['broken'])} broken** by this branch.", ""]
    for kind in ("broken", "fixed"):
        if not flips[kind]:
            continue
        lines += [f"### {kind.capitalize()} (up to 40)", "", "| company | title | true | main | branch |", "|---|---|---|---|---|"]
        for co, title, true, m, b in flips[kind][:40]:
            cell = lambda r: f"{r[0]} ({r[1]}) {str(r[2])[:90]}".replace("|", "/")
            lines.append(f"| {co[:30]} | {title.replace('|', '/')} | {true} | {cell(m)} | {cell(b)} |")
        lines.append("")
    out = "\n".join(lines)
    print(out, flush=True)
    open(a.out, "w").write(out + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")


if __name__ == "__main__":
    main()
