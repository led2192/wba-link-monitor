#!/usr/bin/env python3
"""
review_score.py: what the reviewers' decisions say about each method and attribute.

Every row of attribute_values an analyst accepts or rejects is a labelled answer, from the person whose
judgement the pipeline is meant to match. This script reads those rows (read-only) and writes
review_score.md:

  1. per method and attribute: rows reviewed, accepted, rejected, and the acceptance rate;
  2. the same against the validated 2025 answer: how often a row that matched it was still rejected
     (the validated answer and ours were both wrong, or the reviewer wants another line) and how often a
     row that differed was accepted (the validated answer was wrong);
  3. what the rejection notes say, grouped into the patterns worth fixing (another line of the statement,
     the wrong year, a scale or unit, a segment instead of the group, the validated answer itself wrong,
     the figure not found), with the notes under each;
  4. the corrected values (both ours and the validated one wrong), one line each;
  5. the pace: rows reviewed per week over the last eight weeks.

Nothing is written to Airtable; the run's counters go to Pipeline QA with --commit. No reviewer names
are printed (the report is a public run artifact). Runs weekly (review_score.yml) and on demand.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN (optional).
"""
import argparse, collections, datetime as dt, os, re, sys, traceback

import qa

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = "attribute_values"
FIELDS = ["value_id", "wba_id", "attribute", "attribute_name", "year", "method", "status", "matches_validated",
          "reviewer_note", "corrected_value", "value", "validated_value", "unit", "reviewed_on"]
PATTERNS = [                           # (what the note means, words that say it), first match wins
    ("the validated answer is wrong", ["validated value is wrong", "validated is wrong", "validated value is incorrect",
                                       "validated figure is wrong", "wrong validated", "validated value wrong"]),
    ("another line of the statement (total, net sales, gross)", ["total", "net sales", "gross", "sales", "operating revenue",
                                                                  "other revenue", "contract", "excise", "line"]),
    ("a scale or unit", ["thousand", "million", "billion", "unit", "scale", "currency", "x1000", "x 1000", "×1000", "factor",
                         "parsing", "tco2", "ktco2", "decimal"]),
    ("the wrong year", ["wrong year", "fiscal year", "fy20", "fy2", "previous year", "prior year", "next year", "year end",
                        "it's 20", "is 20", "for 20", "of 20"]),
    ("a segment or subsidiary, not the group", ["segment", "subsidiary", "parent", "only", "region", "division", "standalone"]),
    ("the figure was not found", ["unable to find", "not found", "cannot find", "couldn't find", "can't find", "not in the report"]),
]


def name_of(f):
    m = (f.get("method") or {})
    return m.get("name") if isinstance(m, dict) else (m or "")


def choice(f, k):
    v = f.get(k)
    return v.get("name") if isinstance(v, dict) else (v or "")


def pattern_of(note):
    n = (note or "").lower()
    for label, words in PATTERNS:
        if any(w in n for w in words):
            return label
    return "other"


def score(rows, today=None):
    """{"by": {(method, attribute): Counter}, "notes": {pattern: [note lines]}, "corrected": [...], "weeks": Counter}"""
    today = today or dt.date.today()
    by = collections.defaultdict(collections.Counter)
    notes = collections.defaultdict(list)
    corrected, weeks = [], collections.Counter()
    for r in rows:
        f = r.get("fields", r)
        st = choice(f, "status")
        if st not in ("accepted", "rejected"):
            continue
        key = (name_of(f) or "?", f"{f.get('attribute', '?')} {f.get('attribute_name', '')}".strip())
        c = by[key]
        c["reviewed"] += 1; c[st] += 1
        mv = choice(f, "matches_validated")
        if mv in ("match", "differs"):
            c[f"{mv} {st}"] += 1
        if st == "rejected" and (f.get("reviewer_note") or "").strip():
            notes[pattern_of(f["reviewer_note"])].append(
                f"{f.get('wba_id', '?')} {f.get('attribute_name', f.get('attribute', ''))} {f.get('year', '')} ({name_of(f)}): "
                f"\"{f['reviewer_note'].strip()[:160]}\"")
        if f.get("corrected_value") not in (None, ""):
            corrected.append(f"{f.get('wba_id', '?')} {f.get('attribute_name', '')} {f.get('year', '')}: ours {f.get('value')}, "
                             f"validated {f.get('validated_value')}, corrected to {f.get('corrected_value')} {f.get('unit', '')}")
        when = (f.get("reviewed_on") or "")[:10]
        if when:
            try:
                d = dt.date.fromisoformat(when)
                if 0 <= (today - d).days < 56:
                    weeks[(today - d).days // 7] += 1
            except ValueError:
                pass
    return {"by": dict(by), "notes": dict(notes), "corrected": corrected, "weeks": weeks}


def report(s, dry=False):
    md = ["### What the reviewers' decisions say" + (" (dry run)" if dry else ""), "",
          f"{sum(c['reviewed'] for c in s['by'].values()):,} rows reviewed in attribute_values (accepted or rejected). "
          "A reviewed row is never changed by a run; these are the pipeline's own labelled answers.", "",
          "| Method | Attribute | Reviewed | Accepted | Rejected | Accepted | Matched the validated answer and was rejected | Differed from it and was accepted |",
          "| --- | --- | --: | --: | --: | --: | --: | --: |"]
    for (m, a), c in sorted(s["by"].items()):
        md.append(f"| {m} | {a} | {c['reviewed']:,} | {c['accepted']:,} | {c['rejected']:,} | {c['accepted'] / (c['reviewed'] or 1):.0%} | "
                  f"{c['match rejected']:,} of {c['match accepted'] + c['match rejected']:,} | {c['differs accepted']:,} of {c['differs accepted'] + c['differs rejected']:,} |")
    md += ["", "A row that matched the validated answer and was still rejected means both were wrong, or the reviewer wants another line; "
           "a row that differed and was accepted means the validated answer was wrong. Both are worth a look in the gold base.", ""]
    md += ["#### What the rejection notes say", ""]
    if not s["notes"]:
        md.append("No rejection notes yet.")
    for label, lines in sorted(s["notes"].items(), key=lambda kv: -len(kv[1])):
        md += [f"**{label}**: {len(lines)}", ""] + [f"- {l}" for l in lines[:15]] + ([f"- ... and {len(lines) - 15} more"] if len(lines) > 15 else []) + [""]
    md += ["#### Corrected values (ours and the validated one both wrong)", ""]
    md += [f"- {l}" for l in s["corrected"]] or ["None yet."]
    md += ["", "#### Pace", "", "| Week | Rows reviewed |", "| --- | --: |"]
    for w in range(8):
        md.append(f"| {'this week' if w == 0 else f'{w} week{'s' if w > 1 else ''} ago'} | {s['weeks'][w]:,} |")
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write the run's counters to Pipeline QA")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    from propose_values import fetch
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("review_score.py")
    rows = fetch(H, base, TABLE, FIELDS, "OR({status}='accepted',{status}='rejected')")
    s = score(rows)
    md = report(s, dry=not a.commit)
    open("review_score.md", "w").write(md)
    print(md, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(md)
    counters = {f"{m} | {a_}": {k: v for k, v in c.items()} for (m, a_), c in s["by"].items()}
    counters["notes by pattern"] = {k: len(v) for k, v in s["notes"].items()}
    run.finish("ok", rows_in=len(rows), rows_out=sum(c["reviewed"] for c in s["by"].values()), counters=counters)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("review_score.md", "w").write("### Review score: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
