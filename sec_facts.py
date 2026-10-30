#!/usr/bin/env python3
"""
sec_facts.py: revenue from the SEC's machine-readable filings, scored against the validated answers.

Companies that file with the SEC (10-K, 20-F, 40-F) tag every figure of their financial statements in
XBRL, and the SEC publishes all of a company's tagged facts as one JSON file (the "companyfacts" API, free,
no key). For revenue that means an exact number, its period and the filing it came from, with no reading
at all. This script measures how far that route goes, before anything is written anywhere (first step of
the "retrieval and rules before any model" plan of 28 Oct 2026):

  for every validated 2025-benchmark revenue answer (GMC_00048.1) of a company with an SEC CIK
  (companies.sec_cik), the annual revenue facts of the company are read and the one for the answer's
  year is chosen by rule:
    - annual: a 10-K, 20-F or 40-F (or their amendments), a period of 330 to 380 days;
    - the year by WBA's convention: a period ending January to June counts as the previous year;
    - the largest of the revenue concepts reported for the year (REVENUE_CONCEPTS), each as first filed: the top line;
    - the earliest filing that reports the period (what the analyst read; later filings may restate it).
  A match is the same currency and a value within MATCH_TOLERANCE of the validated one. The table also
  says how often some other revenue concept would have matched, which is how the priority list is tuned.

Read-only: the gold bases (GOLD_TOKEN), the Disclosure Library's companies (sec_cik), the SEC. Writes
sec_facts.md and the run's counters in Pipeline QA. Nothing is written to the library or the evidence
database yet: that comes once the rule is measured. Options: --limit N answers, --commit (Pipeline QA).
"""
import argparse, collections, os, sys, time, traceback
from datetime import date

import qa

REVENUE = "GMC_00048.1"
MATCH_TOLERANCE = 0.005                    # 0.5 %: analysts copy the printed figure; rounding stays inside
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}
# the concepts a revenue figure can be tagged with; choose() takes the largest the filing reports for the year
# (the top line), the order only breaks ties
REVENUE_CONCEPTS = [
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ("us-gaap", "SalesRevenueNet"),
    ("us-gaap", "SalesRevenueGoodsNet"),
    ("us-gaap", "SalesRevenueServicesNet"),
    ("us-gaap", "RevenuesNetOfInterestExpense"),
    ("ifrs-full", "Revenue"),
    ("ifrs-full", "RevenueFromContractsWithCustomers"),
    ("ifrs-full", "InsuranceRevenue"),
]


def wba_year(end):
    """WBA's fiscal year of a period end: January to June counts as the previous year."""
    d = date.fromisoformat(end)
    return d.year - 1 if d.month <= 6 else d.year


def annual_facts(companyfacts):
    """[(concept rank, concept, wba year, end, value, currency, form, filed)] of every annual revenue fact."""
    out = []
    facts = (companyfacts or {}).get("facts", {})
    for rank, (ns, concept) in enumerate(REVENUE_CONCEPTS):
        units = facts.get(ns, {}).get(concept, {}).get("units", {})
        for cur, rows in units.items():
            if "/" in cur:                   # per-share and ratio units
                continue
            for f in rows:
                if f.get("form") not in ANNUAL_FORMS or not f.get("start") or not f.get("end"):
                    continue
                days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
                if not 330 <= days <= 380:
                    continue
                out.append((rank, f"{ns}:{concept}", wba_year(f["end"]), f["end"], float(f["val"]), cur.upper(),
                            f.get("form"), f.get("filed") or ""))
    return out


def choose(facts, year):
    """The fact the rule picks for a WBA year: in the company's main reporting currency (the one most of
    its revenue facts use; some filings add a convenience translation), each concept as first filed, then
    the largest of them: the top line. WBA takes total revenue, gross sales before excise taxes or royalties
    deducted below them, income net of interest expense for a card issuer, and the narrower concepts
    (revenue from contracts with customers, net sales) are always part of it. Ranking concepts instead
    took contract revenue over the total for 4 of the first 5 SEC rows an analyst reviewed (30 Oct 2026).
    Equal values keep the concept order."""
    if not facts:
        return None
    main = collections.Counter(f[5] for f in facts).most_common(1)[0][0]
    cand = [f for f in facts if f[2] == year and f[5] == main] or [f for f in facts if f[2] == year]
    if not cand:
        return None
    first = {}
    for f in sorted(cand, key=lambda f: (f[7], -f[4])):        # each concept as first filed, not restated
        first.setdefault(f[1], f)
    return max(first.values(), key=lambda f: (f[4], -f[0]))


def matches(value, currency, fact, tol=MATCH_TOLERANCE):
    return bool(fact) and fact[5] == (currency or "").upper() and value and abs(fact[4] - value) / value <= tol


def score(answer, facts):
    """{'has_year', 'match', 'any_match', 'chosen'} for one gold answer against a company's facts."""
    chosen = choose(facts, answer["year"])
    same_year = [f for f in facts if f[2] == answer["year"]]
    return {"has_year": bool(chosen), "chosen": chosen,
            "match": matches(answer["value"], answer["unit"], chosen),
            "any_match": any(matches(answer["value"], answer["unit"], f) for f in same_year)}


def companyfacts(cik, sess):
    """The SEC's companyfacts JSON for a CIK, or None."""
    from regulator_filings import SEC_UA
    try:
        r = sess.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json", headers=SEC_UA, timeout=60)
    except Exception:
        return None
    return r.json() if r.status_code == 200 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="answers (0 = all)")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("sec_facts.md", "w").write("### SEC revenue: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    import json, requests
    from pub_dates import company_ciks
    from value_eval import gold_values
    run = qa.Run("sec_facts.py")
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json"), encoding="utf-8"))
    answers = [g for base in cfg["bases"].values() for g in gold_values(token, base, [REVENUE]) if g["value"]]
    ciks = company_ciks(os.environ["AIRTABLE_TOKEN"], os.environ["AIRTABLE_BASE"])
    with_cik = [g for g in answers if g["wba"] in ciks]
    if a.limit:
        with_cik = with_cik[:a.limit]
    print(f">>> {len(answers):,} revenue answers with a value; {len(with_cik):,} of companies with an SEC CIK "
          f"({len({g['wba'] for g in with_cik}):,} companies)", flush=True)
    sess = requests.Session()
    cache, c, wrong, concepts = {}, collections.Counter(), [], collections.Counter()
    for g in with_cik:
        w = g["wba"]
        if w not in cache:
            time.sleep(0.15)                    # the SEC asks for at most ten requests a second
            cache[w] = annual_facts(companyfacts(ciks[w], sess))
        facts = cache[w]
        c["answers"] += 1
        if not facts:
            c["no SEC revenue facts"] += 1; continue
        s = score(g, facts)
        if not s["has_year"]:
            c["no fact for the year"] += 1; continue
        c["fact for the year"] += 1
        if s["match"]:
            c["match"] += 1; concepts[s["chosen"][1]] += 1
        elif s["any_match"]:
            c["another concept matches"] += 1
        else:
            c["no match"] += 1
            if len(wrong) < 15:
                f = s["chosen"]
                wrong.append(f"{g['id']}: validated {g['value']:,.0f} {g['unit']}; SEC {f[4]:,.0f} {f[5]} ({f[1]}, {f[6]} filed {f[7]}, period ending {f[3]})")
    n = c["fact for the year"] or 1
    md = ["### SEC revenue against the validated answers (no model)", "",
          f"Revenue answers with a value: {len(answers):,}; of companies with an SEC CIK: {c['answers']:,} "
          f"({len(cache):,} companies).", "",
          "| | Answers | Share of answers with a fact |", "| --- | --: | --: |",
          f"| SEC has an annual revenue fact for the year | {c['fact for the year']:,} | |",
          f"| the rule's choice matches (0.5 %) | {c['match']:,} | {c['match'] / n:.0%} |",
          f"| another revenue concept would match | {c['another concept matches']:,} | {c['another concept matches'] / n:.0%} |",
          f"| no revenue fact matches | {c['no match']:,} | {c['no match'] / n:.0%} |",
          f"| no SEC revenue facts at all | {c['no SEC revenue facts']:,} | |",
          f"| facts, but none for the year | {c['no fact for the year']:,} | |", "",
          "Concepts of the matches: " + ", ".join(f"{k} {v:,}" for k, v in concepts.most_common()), ""]
    if wrong:
        md += ["No match (first 15):", ""] + [f"- {x}" for x in wrong] + [""]
    open("sec_facts.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=c["answers"], rows_out=c["match"], counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("sec_facts.md", "w").write("### SEC revenue: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
