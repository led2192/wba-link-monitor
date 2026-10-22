#!/usr/bin/env python3
"""
sl_labels.py — the Source Library as ground truth: measures the pipeline's type and year against
the analysts' own, on every document both hold, and writes the result to Pipeline QA.

The Source Library bases are READ ONLY, with the read-only SOURCE_LIBRARY_TOKEN, through
import_source_library.get_all (GET only). Nothing is ever written to them.

Matching (same company, same file): a Source Library record and a Disclosure Library row are the same
document when their attachments have the same byte size, or the same file name when that name is
specific (not "annual-report.pdf", "report.pdf", "download.pdf"...). Rows the Disclosure Library
imported FROM the Source Library are skipped: they agree by construction.

For each matched pair it writes one labels row (stratum source_library, labelled_by source_library,
reviewed): true_type = the analyst's type (when it is one of our 24 types), true_year = the analyst's
year (reports only; for policies the year is a version), next to what the pipeline says (robot_value =
URL type, ai_value = AI type, year_value = the year formula).

The analysts' work was reviewed but can still hold errors, so a label is marked NOT sure, and kept out
of the evaluation, when the pipeline has strong contrary evidence:
  year   the full-text reader found a different year with year_score >= 80 (99% right when tested)
  type   the URL guess and the AI agree with each other on a type that satisfies a different
         completeness check than the analyst's type
Those cases are where a person should look: one side is wrong, and it is worth knowing which.
Only the 2025 base counts as sure by default (it was reviewed); 2024 labels are written not sure.

Then it scores the pipeline on the sure labels and writes one evals row per target (verdict baseline):
  url_classifier   source_type          ai_type_prompt  source_type_check (rows the AI typed)
  arbitration      the effective type the coverage uses (AI first, URL as fallback)
  year_rule        the year formula
Each with exact accuracy, check-level accuracy (does the prediction satisfy the same completeness check
as the truth: the number that moves completeness) as high_stakes_accuracy, and a per-class table.

Modes: dry run by default (prints the matching and the scores); --commit writes labels and evals.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SOURCE_LIBRARY_TOKEN, AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE.
"""
import argparse, collections, datetime as dt, json, os, re, sys

import qa
from qa_sync import checks_of
import import_source_library as SL

OUR_TYPES = {"Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report",
             "Annual Report", "10K Form", "Financial Statement", "Interim Reports", "Registration Document",
             "Statutory Documents", "Company Presentations", "Climate Report", "Environmental Reports",
             "Social Reports", "CDP Report", "GRI", "SASB Index", "Engagement Report", "Certifications",
             "Code of Conduct", "Human Rights Policy", "Policy Documents"}
POLICIES = {"Code of Conduct", "Human Rights Policy", "Policy Documents", "Certifications"}
GENERIC = re.compile(r"^(annual[-_ ]?report|sustainability[-_ ]?report|report|download|document|file|"
                     r"esg[-_ ]?report|integrated[-_ ]?report|policy|code[-_ ]?of[-_ ]?conduct|index|"
                     r"[0-9a-f-]{8,}|\d+)(\.pdf)?$", re.I)
SURE_BASES = {"2025"}
DL_FIELDS = ["library_id", "wba_id", "company_name", "doc_label", "document_url", "file", "source_type",
             "source_type_check", "year", "year_source", "fiscal_year", "year_score", "doc_year_from",
             "discard", "doc_language"]


# ---- pure helpers ----------------------------------------------------------------------------------
def specific_name(name):
    n = (name or "").strip().lower()
    return len(n) > 12 and not GENERIC.match(n)


def to_year(v):
    try:
        y = int(str(v)[:4])
        return y if 2000 <= y <= 2035 else None
    except (TypeError, ValueError):
        return None


def match(sources, rows):
    """[(source, row)] pairs: same company and same byte size, or same specific file name."""
    by_size, by_name = {}, {}
    for r in rows:
        w = (r.get("wba_id") or "").strip()
        for a in (r.get("file") or [])[:1]:
            if a.get("size"):
                by_size.setdefault((w, a["size"]), r)
            if specific_name(a.get("filename")):
                by_name.setdefault((w, a["filename"].lower()), r)
    out, used = [], set()
    for s in sources:
        r = by_size.get((s["wba"], s["size"])) or \
            (by_name.get((s["wba"], s["filename"].lower())) if specific_name(s["filename"]) else None)
        if r and r["_id"] not in used:
            used.add(r["_id"])
            out.append((s, r))
    return out


def eff_type(r):
    return (r.get("source_type_check") or "").strip() or (r.get("source_type") or "").strip()


def doubt(s, r):
    """Why this analyst label should not be trusted blindly ('' = sure)."""
    why = []
    ty, sc = to_year(r.get("fiscal_year")), r.get("year_score")
    if s["type"] not in POLICIES and s["year"] and ty and isinstance(sc, (int, float)) and sc >= 80 and ty != s["year"]:
        why.append(f"full text says {ty} (score {int(sc)}), analyst {s['year']}")
    kw, ai = (r.get("source_type") or "").strip(), (r.get("source_type_check") or "").strip()
    if kw and ai and kw == ai and checks_of(ai) != checks_of(s["type"]):
        why.append(f"URL and AI both say {ai}, analyst {s['type']}")
    return "; ".join(why)


def label_row(s, r, today):
    lid = r.get("library_id") or r["_id"]
    why = doubt(s, r)
    sure = s["src"] in SURE_BASES and not why
    y = r.get("year")
    row = {"target": "document", "field": "document", "stratum": "source_library",
           "company_name": r.get("company_name"), "wba_id": r.get("wba_id"),
           "doc_label": (r.get("doc_label") or "")[:250], "document_url": r.get("document_url"),
           "library_id": lid, "record_id": r["_id"], "doc_language": r.get("doc_language"),
           "robot_value": (r.get("source_type") or "").strip(), "ai_value": (r.get("source_type_check") or "").strip(),
           "year_value": str(int(y)) if isinstance(y, (int, float)) else "",
           "true_type": s["type"], "sure": sure, "labelled_by": "source_library", "labelled_on": today,
           "reviewed": True, "split": "eval",
           "reason": (f"Source Library {s['src']} record {s['rec']}: {s['type']} {s['year'] or ''}".strip()
                      + (f". Doubt: {why}" if why else ""))[:500]}
    if s["type"] not in POLICIES and s["year"]:
        row["true_year"] = s["year"]
    return f"{lid}|source_library|{s['src']}", row


def score(pairs, predict, truth_of):
    """(n, exact accuracy, check-level accuracy, per_class) over pairs where predict() gives a value."""
    n = exact = chk = 0
    per = collections.defaultdict(lambda: {"n": 0, "correct": 0, "confusions": collections.Counter()})
    for s, r in pairs:
        p, t = predict(r), truth_of(s)
        if p in (None, "") or t in (None, ""):
            continue
        n += 1
        ok = p == t
        exact += ok
        chk += (checks_of(p) == checks_of(t)) if isinstance(t, str) else ok
        per[str(t)]["n"] += 1
        per[str(t)]["correct"] += ok
        if not ok:
            per[str(t)]["confusions"][str(p)] += 1
    table = {k: {"n": v["n"], "correct": v["correct"],
                 "top_confusion": (v["confusions"].most_common(1)[0][0] if v["confusions"] else "")}
             for k, v in sorted(per.items(), key=lambda kv: -kv[1]["n"])}
    return n, (exact / n if n else 0), (chk / n if n else 0), table


def read_sources_all(token, tag):
    """Every Source Library record of one base whose type is one of ours, with its file. GET only."""
    c = SL.SOURCE_LIBRARIES[tag]
    cos = SL.get_all(token, c["base"], c["companies"], [c["co_id"]])
    wba_of = {r["id"]: (r["fields"].get(c["co_id"]) or "").strip() for r in cos}
    recs = SL.get_all(token, c["base"], c["sources"], [c[k] for k in ("type", "year", "file", "name", "wba")])
    out = []
    for r in recs:
        f = r["fields"]; t = f.get(c["type"]); att = (f.get(c["file"]) or [None])[0]
        links = f.get(c["wba"]) or []
        wba = wba_of.get(links[0], "") if links else ""
        if t in OUR_TYPES and att and att.get("size") and wba:
            out.append({"src": tag, "rec": r["id"], "wba": wba, "type": t, "year": to_year(f.get(c["year"])),
                        "filename": att.get("filename") or "", "size": att["size"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--bases", default="2025,2024")
    ap.add_argument("--min-year", type=int, default=2019, help="ignore analyst records older than this")
    args = ap.parse_args()
    tok, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    sl_tok, qtok = os.environ.get("SOURCE_LIBRARY_TOKEN"), os.environ.get("AIRTABLE_QA_TOKEN")
    if not (tok and base and sl_tok and qtok):
        sys.exit("Set AIRTABLE_TOKEN, AIRTABLE_BASE, SOURCE_LIBRARY_TOKEN and AIRTABLE_QA_TOKEN.")
    if base in SL.SL_BASES:
        sys.exit("AIRTABLE_BASE is a Source Library base: refusing to run.")
    if args.commit:
        os.environ["QA_COMMIT"] = "true"
    today = dt.date.today().isoformat()
    run = qa.Run("sl_labels.py", token=qtok, base=os.environ.get("AIRTABLE_QA_BASE", qa.QA_BASE_DEFAULT))

    order = [b.strip() for b in args.bases.split(",") if b.strip()]
    sources = []
    for tag in order:
        got = [s for s in read_sources_all(sl_tok, tag) if not s["year"] or s["year"] >= args.min_year]
        print(f">>> Source Library {tag}: {len(got):,} records of our types with a file", flush=True)
        sources += got
    rows = []
    for rec in SL.get_all(tok, base, "report_library", DL_FIELDS, "NOT({discard})"):
        f = rec["fields"]; f["_id"] = rec["id"]
        if (f.get("doc_year_from") or "") != "source library":
            rows.append(f)
    print(f">>> Disclosure Library: {len(rows):,} live rows not imported from the Source Library", flush=True)

    pairs = match(sources, rows)
    print(f">>> matched {len(pairs):,} documents held by both", flush=True)
    sure_pairs = []
    for s, r in pairs:
        lid, row = label_row(s, r, today)
        run.label(lid, **row)
        if row["sure"]:
            sure_pairs.append((s, r))
    n_doubt = sum(1 for s, r in pairs if doubt(s, r))
    print(f">>> sure labels: {len(sure_pairs):,}; in doubt (pipeline has strong contrary evidence): {n_doubt:,}",
          flush=True)

    targets = [("url_classifier", "source_type (URL keywords)", lambda r: (r.get("source_type") or "").strip(),
                lambda s: s["type"]),
               ("ai_type_prompt", "source_type_check (GPT-4o mini on cover)", lambda r: (r.get("source_type_check") or "").strip(),
                lambda s: s["type"]),
               ("arbitration", "ai_first (live rule)", eff_type, lambda s: s["type"]),
               ("year_rule", "year formula", lambda r: int(r["year"]) if isinstance(r.get("year"), (int, float)) else None,
                lambda s: s["year"] if s["type"] not in POLICIES else None)]
    evals, lines = [], ["", "| target | n | exact | check-level |", "| --- | ---: | ---: | ---: |"]
    for tgt, version, pred, truth in targets:
        n, acc, chk, table = score(sure_pairs, pred, truth)
        lines.append(f"| {tgt} | {n:,} | {acc:.1%} | {chk:.1%} |")
        evals.append({"eval_id": f"{tgt}@source_library@{today}", "run_on": qa._now(), "target": tgt,
                      "version": version, "commit_sha": run.commit_sha, "n_cases": n, "accuracy": round(acc, 4),
                      "high_stakes_accuracy": round(chk, 4), "baseline_accuracy": round(acc, 4),
                      "per_class": json.dumps(dict(list(table.items())[:30]), ensure_ascii=False)[:90000],
                      "verdict": "baseline",
                      "notes": f"Scored against {n:,} sure Source Library labels ({','.join(sorted(SURE_BASES))} base). "
                               "check-level = the prediction satisfies the same completeness check as the analyst's type."})
        print(f"    {tgt:16} n={n:6,}  exact={acc:6.1%}  check-level={chk:6.1%}", flush=True)
        for cls, v in list(table.items())[:8]:
            print(f"        {cls:28} {v['correct']:5}/{v['n']:<5} most confused with: {v['top_confusion']}", flush=True)
    counters = {"sources": len(sources), "library_rows": len(rows), "matched": len(pairs),
                "sure": len(sure_pairs), "doubt": n_doubt}
    if args.commit and qtok:
        existing = run._existing_by_key(qa.T_EVALS, "eval_id", [e["eval_id"] for e in evals], ["eval_id"])
        run._write(qa.T_EVALS, [{"fields": e} for e in evals if e["eval_id"] not in existing], "POST")
    run.finish("ok", rows_in=len(sources), rows_out=len(pairs), counters=counters)
    md = run.summary_markdown("ok", counters, title="Source Library labels") + "\n".join(lines) + "\n"
    for target in (os.environ.get("GITHUB_STEP_SUMMARY"), "sl_labels_summary.md"):
        if target:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(md)


if __name__ == "__main__":
    main()
