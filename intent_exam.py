#!/usr/bin/env python3
"""
intent_exam.py: how well the page reads what is typed, on texts it was not written for.

tests/use_cases.json holds the real texts that went wrong, and the page must read every one right. This exam is the
other half: tests/use_case_variants.json, variants of those texts (other companies, Spanish, other topics, other
wordings) labelled with the way each should take (catalogue, fact, question, overview, search). The page's own
parse() and intent() are run in node on each, and the count of those read right is printed, by way and by field.
It is the yardstick for any change to how the text is read: rules, or a model step tried beside them.

    python intent_exam.py            the table and the texts read wrong
    python intent_exam.py --json     the same as JSON (for a comparison)

Reads only the two files and site/search.html. Writes nothing. Needs node.
"""
import argparse, collections, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
WAYS = ("catalogue", "fact", "question", "overview", "search")


def load():
    return json.load(open(os.path.join(HERE, "tests", "use_case_variants.json"), encoding="utf-8"))


def read_by_page(data):
    """What the page makes of each text (tests/test_use_cases.run_page with this file's companies and topics)."""
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import test_use_cases as T
    saved = T.CASES
    try:
        T.CASES = data
        return T.run_page(data["cases"])
    finally:
        T.CASES = saved


def score(cases, got):
    """{'n', 'intent', 'company', 'years', 'all', 'by_way': {way: [right, of]}, 'wrong': [...]}; a field is counted
    only where the case states it."""
    r = dict(n=len(cases), intent=0, company=[0, 0], years=[0, 0], all=0, by_way={w: [0, 0] for w in WAYS}, wrong=[], confusion=collections.Counter())
    for c, g in zip(cases, got):
        ok_i = c["intent"] == g["intent"]
        r["intent"] += ok_i; r["by_way"][c["intent"]][1] += 1; r["by_way"][c["intent"]][0] += ok_i
        ok_c = True
        if "typo" in c:
            ok_c = c["typo"] == g["typo"]; r["company"][1] += 1; r["company"][0] += ok_c
        elif "company" in c:
            ok_c = c["company"] == g["company"]; r["company"][1] += 1; r["company"][0] += ok_c
        ok_y = True
        if "from" in c:
            ok_y = (c["from"], c["to"]) == (g["from"], g["to"]); r["years"][1] += 1; r["years"][0] += ok_y
        r["all"] += ok_i and ok_c and ok_y
        if not ok_i:
            r["confusion"][(c["intent"], g["intent"])] += 1
        if not (ok_i and ok_c and ok_y):
            r["wrong"].append({"typed": c["typed"], "should": c["intent"], "read": g["intent"], "company_ok": ok_c, "years_ok": ok_y})
    r["confusion"] = {f"{a} read as {b}": n for (a, b), n in sorted(r["confusion"].items(), key=lambda x: -x[1])}
    return r


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--json", action="store_true"); a = ap.parse_args()
    data = load(); r = score(data["cases"], read_by_page(data))
    if a.json:
        print(json.dumps(r, ensure_ascii=False)); return 0
    pct = lambda x, n: f"{x} of {n} ({round(100 * x / n) if n else 0}%)"
    print(f"### Intent exam: {r['n']} variants\n")
    print("| What | Read right |\n|---|---|")
    print(f"| The way it takes | {pct(r['intent'], r['n'])} |")
    for w in WAYS:
        print(f"| ... {w} | {pct(*r['by_way'][w])} |")
    print(f"| The company (or the near name) | {pct(*r['company'])} |")
    print(f"| The years | {pct(*r['years'])} |")
    print(f"| Everything | {pct(r['all'], r['n'])} |")
    if r["confusion"]:
        print("\nWays mistaken: " + "; ".join(f"{k} {n}" for k, n in r["confusion"].items()))
    for w in r["wrong"]:
        print(f"  {w['typed']!r}: should be {w['should']}, read as {w['read']}" + ("" if w["company_ok"] else "; company wrong") + ("" if w["years_ok"] else "; years wrong"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
