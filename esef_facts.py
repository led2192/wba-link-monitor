#!/usr/bin/env python3
"""
esef_facts.py: revenue from the European machine-readable annual reports (ESEF), scored against the
validated answers. The European counterpart of sec_facts.py.

Listed companies in the EU (and the UK and others that adopted the format) publish their annual financial
report in ESEF: a report whose figures are tagged in XBRL with the IFRS taxonomy. filings.xbrl.org collects
them and serves each one also as xBRL-JSON (`json_url`), a plain list of facts. This script reads, for every
validated 2025-benchmark revenue answer of a company with an LEI (companies.lei), the company's filings for
the answer's year and the next, keeps the annual revenue facts with no other dimension (the group total,
not a segment), and applies sec_facts.py's rule and score unchanged: concept by priority (ESEF_CONCEPTS),
WBA's fiscal year (a period ending January to June counts as the previous year), the earliest filing that
reports the period, a match within 0.5 % in the same currency.

xBRL-JSON writes a duration as "start/end" with the end as the instant after the last day
("2024-01-01T00:00:00/2025-01-01T00:00:00" is the year 2024), and values in full units (the "decimals"
attribute says how they were rounded, not their scale).

Read-only: the gold bases (GOLD_TOKEN), the companies' lei, filings.xbrl.org. Writes esef_facts.md and
the run's counters in Pipeline QA. Options: --limit N answers, --commit (Pipeline QA).
"""
import argparse, collections, json, os, sys, time, traceback
from datetime import date, datetime, timedelta

import qa
from sec_facts import REVENUE, choose, matches, score, wba_year

ESEF = "https://filings.xbrl.org"
ESEF_CONCEPTS = ["ifrs-full:Revenue", "ifrs-full:RevenueFromContractsWithCustomers", "ifrs-full:InsuranceRevenue",
                 "ifrs-full:RevenueFromInterest", "ifrs-full:InterestRevenueCalculatedUsingEffectiveInterestMethod"]
CORE_DIMS = {"concept", "entity", "period", "unit", "language"}


def period_days(period):
    """(start date, end date as the last day, days) of an xBRL-JSON duration, or None for an instant."""
    if "/" not in (period or ""):
        return None
    a, b = period.split("/")
    s = datetime.fromisoformat(a.replace("Z", ""))
    e = datetime.fromisoformat(b.replace("Z", ""))
    last = (e - timedelta(days=1)).date() if (e.hour, e.minute, e.second) == (0, 0, 0) else e.date()
    return s.date(), last, (last - s.date()).days + 1


def annual_facts(oim, filed=""):
    """Facts in sec_facts.py's shape: [(rank, concept, wba year, end, value, currency, form, filed)]."""
    out = []
    for f in ((oim or {}).get("facts") or {}).values():
        dims = f.get("dimensions") or {}
        concept = dims.get("concept")
        if concept not in ESEF_CONCEPTS or set(dims) - CORE_DIMS:      # a segment or another breakdown
            continue
        unit = dims.get("unit") or ""
        if not unit.startswith("iso4217:"):
            continue
        p = period_days(dims.get("period"))
        if not p or not 330 <= p[2] <= 380:
            continue
        try:
            v = float(f.get("value"))
        except (TypeError, ValueError):
            continue
        out.append((ESEF_CONCEPTS.index(concept), concept, wba_year(p[1].isoformat()), p[1].isoformat(), v,
                    unit.split(":", 1)[1].upper(), "ESEF", filed))
    return out


def filings(lei, sess, years):
    """[(period end, json url, date added)] of the LEI's ESEF filings whose period ends in the given years."""
    flt = json.dumps([{"name": "entity.identifier", "op": "eq", "val": lei},
                      {"name": "period_end", "op": "ge", "val": f"{min(years)}-01-01"}])
    try:
        r = sess.get(f"{ESEF}/api/filings", params={"filter": flt, "page[size]": "50", "sort": "-period_end"},
                     headers={"Accept": "application/vnd.api+json"}, timeout=60)
    except Exception:
        return []
    time.sleep(0.3)
    if not r.ok:
        return []
    out = []
    for d in r.json().get("data", []):
        a = d.get("attributes", {})
        pe, ju = a.get("period_end") or "", a.get("json_url") or ""
        if pe and ju and int(pe[:4]) <= max(years) + 1:
            out.append((pe, ju if ju.startswith("http") else ESEF + "/" + ju.lstrip("/"), (a.get("date_added") or "")[:10]))
    return out


def company_leis(token, base):
    """{wba_id: lei} for companies with an LEI."""
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100"), ("filterByFormula", "{lei}!=''"), ("fields[]", "wba_id"), ("fields[]", "lei")]
    out, offset = {}, None
    while True:
        p = params + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"https://api.airtable.com/v0/{base}/companies", H, params=p).json()
        for r in j.get("records", []):
            f = r["fields"]
            if f.get("wba_id") and (f.get("lei") or "").strip():
                out[f["wba_id"]] = f["lei"].strip()
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("esef_facts.md", "w").write("### ESEF revenue: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    import requests
    from value_eval import gold_values
    run = qa.Run("esef_facts.py")
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json"), encoding="utf-8"))
    answers = [g for base in cfg["bases"].values() for g in gold_values(token, base, [REVENUE]) if g["value"]]
    leis = company_leis(os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"])
    todo = [g for g in answers if g["wba"] in leis]
    if a.limit:
        todo = todo[:a.limit]
    print(f">>> {len(answers):,} revenue answers with a value; {len(todo):,} of companies with an LEI", flush=True)
    sess = requests.Session()
    years = collections.defaultdict(set)
    for g in todo:
        years[g["wba"]].update({g["year"], g["year"] + 1})
    cache, c, wrong, concepts = {}, collections.Counter(), [], collections.Counter()
    for g in todo:
        w = g["wba"]
        if w not in cache:
            facts = []
            fl = filings(leis[w], sess, years[w])
            for pe, ju, added in fl:
                try:
                    r = sess.get(ju, timeout=120)
                    if r.ok:
                        facts += annual_facts(r.json(), added)
                except Exception:
                    pass
                time.sleep(0.3)
            cache[w] = (bool(fl), facts)
        has_filings, facts = cache[w]
        c["answers"] += 1
        if not has_filings:
            c["no ESEF filing"] += 1; continue
        s = score(g, facts)
        if not s["has_year"]:
            c["filings, but no revenue fact for the year"] += 1; continue
        c["fact for the year"] += 1
        if s["match"]:
            c["match"] += 1; concepts[s["chosen"][1]] += 1
        elif s["any_match"]:
            c["another concept matches"] += 1
        else:
            c["no match"] += 1
            if len(wrong) < 15:
                f = s["chosen"]
                wrong.append(f"{g['id']}: validated {g['value']:,.0f} {g['unit']}; ESEF {f[4]:,.0f} {f[5]} ({f[1]}, period ending {f[3]}, added {f[7]})")
    n = c["fact for the year"] or 1
    md = ["### ESEF revenue against the validated answers (no model)", "",
          f"Revenue answers with a value: {len(answers):,}; of companies with an LEI: {c['answers']:,} ({len(cache):,} companies).", "",
          "| | Answers | Share of answers with a fact |", "| --- | --: | --: |",
          f"| ESEF has an annual revenue fact for the year | {c['fact for the year']:,} | |",
          f"| the rule's choice matches (0.5 %) | {c['match']:,} | {c['match'] / n:.0%} |",
          f"| another revenue concept would match | {c['another concept matches']:,} | {c['another concept matches'] / n:.0%} |",
          f"| no revenue fact matches | {c['no match']:,} | {c['no match'] / n:.0%} |",
          f"| no ESEF filing for the company | {c['no ESEF filing']:,} | |",
          f"| filings, but no revenue fact for the year | {c['filings, but no revenue fact for the year']:,} | |", "",
          "Concepts of the matches: " + ", ".join(f"{k} {v:,}" for k, v in concepts.most_common()), ""]
    if wrong:
        md += ["No match (first 15):", ""] + [f"- {x}" for x in wrong] + [""]
    open("esef_facts.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=c["answers"], rows_out=c["match"], counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("esef_facts.md", "w").write("### ESEF revenue: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
