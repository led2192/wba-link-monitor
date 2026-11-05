#!/usr/bin/env python3
"""
plausibility.py: flag proposed values that cannot be right, with bounds learned from the data, not set by hand.

5 Nov 2026: proposals like revenue 2.11 KRW (the validated answer: 2,068,192,612,476 KRW; the page printed
"2.11 trillion") and revenue 2,043,127 against 2,043,127,000,000 (printed in millions) were left as plain
proposals. Every company in the universe is large; a revenue of a few won, or a company of 50 people, is a
reading error, and most such errors are a scale (thousands, millions, billions, trillions) read wrong.

Four checks per proposed value, each with bounds taken from the validated answers already in
attribute_values (validated_value), so nobody sets a margin:
  range      the value (in US dollars for currencies, at today's ECB rates; as is for counts and tCO2e)
             against the validated answers of that attribute: below the 1st percentile divided by RANGE_SLACK,
             or above the 99th percentile times RANGE_SLACK;
  history    the same company's validated other years for that attribute, same unit: more than
             HISTORY_RATIO times away from their median;
  scale      when history fails and the ratio to the company's own median is close to a power of ten a page
             can print in (10^3, 10^4, 10^5, 10^6, 10^7, 10^8, 10^9, 10^12), the value rescaled is suggested
             (never from the range alone: companies differ a thousand times in size);
  per head   revenue per employee (same company and year, US dollars) against the validated pairs, with the
             same percentile rule.
A flag never changes value or status: it writes `plausibility` (the reasons, plain words) for a person to
look at. No rescaled number is written: on the second dry run none of 7 equalled the validated answer.

Measured before anything is written: of the flagged rows that have a validated answer, how many differ
from it (the flag's precision), and of the rows that differ, how many are flagged (its recall).
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; AIRTABLE_QA_TOKEN for the run record. Dry run by default; --commit
writes the two fields. Writes plausibility.md and plausibility.csv.
"""
import argparse, collections, csv, math, os, re, statistics, sys, time, traceback

import requests

import qa
from anchor_eval import fetch

RANGE_SLACK = 5.0          # outside [p1 / 5, p99 * 5] of the validated answers
HISTORY_RATIO = 10.0       # more than 10x away from the company's own validated other years (first dry run, 5 Nov
                           # 2026: 4x against other proposals flagged right values as often as wrong ones)
MIN_LEARN = 30             # validated answers needed before an attribute (or a per-head pair) gets a range
SCALE_POWERS = (3, 4, 5, 6, 7, 8, 9, 12)
SCALE_TOLERANCE = 0.3      # |log10(ratio) - power| below this counts as a scale slip
COUNT_UNITS = {"people", "persons", "person", "employees", "employee", "headcount", "fte", "ftes", "number"}
REVENUE, EMPLOYEES = "GMC_00048.1", "GMC_02526.1"
ECB = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def usd_rates(get=requests.get):
    """{currency: units per US dollar} from the ECB's daily reference rates (EUR based); {} when unreachable."""
    try:
        xml = get(ECB, timeout=20).text
    except requests.RequestException:
        return {}
    per_eur = {m.group(1): float(m.group(2)) for m in re.finditer(r"currency='([A-Z]{3})' rate='([0-9.]+)'", xml)}
    per_eur["EUR"] = 1.0
    if "USD" not in per_eur:
        return {}
    return {c: r / per_eur["USD"] for c, r in per_eur.items()}


def kind(unit):
    """'count', a currency code, or the unit lower case (tco2e ...)."""
    u = (unit or "").strip()
    if u.lower() in COUNT_UNITS:
        return "count"
    if re.fullmatch(r"[A-Za-z]{3}", u):
        return u.upper()
    return u.lower()


def comparable(value, unit, rates):
    """The value on one scale per attribute: US dollars for a currency (None without a rate), as is otherwise."""
    k = kind(unit)
    if value is None or value <= 0:
        return None
    if re.fullmatch(r"[A-Z]{3}", k):
        r = rates.get(k)
        return value / r if r else None
    return value


def bounds(values):
    """(low, high) from validated values: 1st and 99th percentiles widened by RANGE_SLACK; None if too few."""
    v = sorted(x for x in values if x and x > 0)
    if len(v) < MIN_LEARN:
        return None
    q = lambda p: v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))]
    return q(0.01) / RANGE_SLACK, q(0.99) * RANGE_SLACK


def scale_slip(ratio):
    """The power of ten a ratio is close to (value too big: positive), or None."""
    if not ratio or ratio <= 0:
        return None
    lg = math.log10(ratio)
    for p in SCALE_POWERS:
        for sign in (1, -1):
            if abs(lg - sign * p) < SCALE_TOLERANCE:
                return sign * p
    return None


def check(row, learned, history, per_head, rates):
    """(reasons, suggested value or None) for one proposed row."""
    reasons, suggested = [], None
    v, unit, code = row["value"], row["unit"], row["attribute"]
    if v is None:
        return reasons, None
    if v <= 0:
        return ["zero or negative"], None
    c = comparable(v, unit, rates)
    b = learned.get(code)
    ref = None          # a scale is suggested only against the company's own other years: companies differ
                        # by a thousand times in size, so the middle of the range says nothing about one of them
    if c is not None and b:
        lo, hi = b
        if c < lo:
            reasons.append(f"far below every validated {row['attribute_name'] or code} "
                           f"({fmt(c)}{' USD' if c != v else ''}, lowest plausible {fmt(lo)})")
        elif c > hi:
            reasons.append(f"far above every validated {row['attribute_name'] or code} "
                           f"({fmt(c)}{' USD' if c != v else ''}, highest plausible {fmt(hi)})")
    h = history.get((row["wba_id"], code, kind(unit)))
    if h:
        others = [x for y, x in h if y != row["year"]]
        if others:
            med = statistics.median(others)
            r = v / med
            if r > HISTORY_RATIO or r < 1 / HISTORY_RATIO:
                reasons.append(f"{r:.3g} times the company's other years (median {fmt(med)} {unit})")
                ref = med
    if ref:
        p = scale_slip(v / ref)
        if p:
            suggested = v / 10 ** p
            reasons.append(f"looks like a scale slip of 10^{abs(p)} ({'too big' if p > 0 else 'too small'}): "
                           f"check the page's unit line")
    if code in (REVENUE, EMPLOYEES) and per_head.get("bounds"):
        pair = per_head["pairs"].get((row["wba_id"], row["year"]))
        if pair:
            rev = pair.get(REVENUE) if code == EMPLOYEES else comparable(v, unit, rates)
            emp = v if code == EMPLOYEES else pair.get(EMPLOYEES)
            if rev and emp:
                lo, hi = per_head["bounds"]
                rpe = rev / emp
                if rpe < lo or rpe > hi:
                    reasons.append(f"revenue per employee {fmt(rpe)} USD is outside every validated company's "
                                   f"({fmt(lo)} to {fmt(hi)})")
    return reasons, suggested


CHECKS = ("zero", "range", "history", "scale slip", "per head")


def kinds_of(reasons):
    """The checks behind a list of reasons, by their wording."""
    out = set()
    for r in reasons:
        out.add("zero" if r.startswith("zero") else "range" if r.startswith("far ") else
                "scale slip" if r.startswith("looks like a scale") else "per head" if r.startswith("revenue per") else "history")
    return sorted(out)


def fmt(x):
    if x is None:
        return ""
    if abs(x) >= 1e12:
        return f"{x / 1e12:.3g} trillion"
    if abs(x) >= 1e9:
        return f"{x / 1e9:.3g} billion"
    if abs(x) >= 1e6:
        return f"{x / 1e6:.3g} million"
    return f"{x:,.4g}"


def learn(rows, rates):
    """Ranges per attribute, the companies' histories and the per-head pairs, from validated values first."""
    by_attr = collections.defaultdict(list)
    history = collections.defaultdict(list)
    seen = set()
    for r in rows:
        if r["validated"] is not None:
            c = comparable(r["validated"], r["unit"], rates)
            if c is not None:
                by_attr[r["attribute"]].append(c)
            key = (r["wba_id"], r["attribute"], kind(r["unit"]), r["year"])
            if key not in seen:
                seen.add(key)
                history[key[:3]].append((r["year"], r["validated"]))
    # Only validated years make a company's history: the first dry run used its other proposals too, and a
    # wrong proposal for 2022 then flagged a right one for 2024.
    learned = {a: b for a, v in by_attr.items() if (b := bounds(v))}
    pairs = collections.defaultdict(dict)
    vpairs = collections.defaultdict(dict)
    for r in rows:
        if r["attribute"] not in (REVENUE, EMPLOYEES):
            continue
        val = r["validated"] if r["validated"] is not None else r["value"]
        c = comparable(val, r["unit"], rates) if r["attribute"] == REVENUE else val
        if c:
            pairs[(r["wba_id"], r["year"])].setdefault(r["attribute"], c)
        if r["validated"] is not None:
            cv = comparable(r["validated"], r["unit"], rates) if r["attribute"] == REVENUE else r["validated"]
            if cv:
                vpairs[(r["wba_id"], r["year"])][r["attribute"]] = cv
    rpe = [p[REVENUE] / p[EMPLOYEES] for p in vpairs.values() if p.get(REVENUE) and p.get(EMPLOYEES)]
    return learned, history, {"bounds": bounds(rpe), "pairs": pairs, "n": len(rpe)}


def load(H, base):
    out = []
    for rec in fetch(H, base, "attribute_values", ["value_id", "wba_id", "attribute", "attribute_name", "year", "value",
                                                   "unit", "method", "status", "validated_value", "matches_validated",
                                                   "plausibility"]):
        f = rec["fields"]
        st = f.get("status")
        st = st.get("name") if isinstance(st, dict) else st
        m = f.get("matches_validated")
        m = m.get("name") if isinstance(m, dict) else m
        meth = f.get("method")
        meth = meth.get("name") if isinstance(meth, dict) else meth
        out.append({"id": rec["id"], "value_id": f.get("value_id", ""), "wba_id": f.get("wba_id", ""),
                    "attribute": f.get("attribute", ""), "attribute_name": f.get("attribute_name", ""),
                    "year": f.get("year"), "value": f.get("value"), "unit": f.get("unit", ""), "method": meth or "",
                    "status": st or "", "validated": f.get("validated_value"), "matches": m or "",
                    "plausibility": f.get("plausibility") or ""})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write plausibility")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("plausibility.py")
    rates = usd_rates()
    rows = load(H, base)
    learned, history, per_head = learn(rows, rates)
    print(f">>> {len(rows):,} attribute values; {len(rates)} ECB rates; ranges learned for {len(learned)} attributes; "
          f"{per_head['n']} validated revenue/employee pairs", flush=True)
    c = collections.Counter()
    out, writes = [], []
    for r in rows:
        if r["status"] != "proposed":
            continue
        reasons, sug = check(r, learned, history, per_head, rates)
        c["proposed"] += 1
        flagged = bool(reasons)
        c["flagged"] += flagged
        if r["matches"] in ("match", "differs"):
            c[f"{'flagged' if flagged else 'clear'} {r['matches']}"] += 1
            if sug and r["matches"] == "differs" and r["validated"]:
                c["suggestion right"] += abs(sug - r["validated"]) <= 0.005 * abs(r["validated"])
                c["suggestions with a validated answer"] += 1
        if flagged:
            out.append({**{k: r[k] for k in ("value_id", "attribute", "year", "value", "unit", "method", "validated", "matches")},
                        "suggested_value": sug or "", "plausibility": "; ".join(reasons)})
        # suggested_value is not written: on the second dry run (5 Nov 2026) none of 7 rescaled values equalled
        # the validated answer, so a number would mislead; the reason names the slip instead
        new = "; ".join(reasons)
        if new != r.get("plausibility", ""):                    # only rows whose flag changed are written
            writes.append({"id": r["id"], "fields": {"plausibility": new or None}})
        if r["matches"] in ("match", "differs"):
            for name in kinds_of(reasons):
                c[f"check {name} {r['matches']}"] += 1
    fd, fm, cd, cm = c["flagged differs"], c["flagged match"], c["clear differs"], c["clear match"]
    md = ["### Plausibility of proposed values (bounds learned from the validated answers)", "",
          f"{c['proposed']:,} proposed values; {c['flagged']:,} flagged ({c['flagged'] / (c['proposed'] or 1):.0%}). "
          f"Ranges from the validated answers for {len(learned)} attributes; revenue per employee from {per_head['n']} validated pairs.", "",
          "Against the validated answers (rows that have one):", "",
          "| | Differs | Matches |", "| --- | --: | --: |",
          f"| Flagged | {fd:,} | {fm:,} |", f"| Not flagged | {cd:,} | {cm:,} |", "",
          f"Precision (flagged rows that are wrong): {fd / ((fd + fm) or 1):.0%}. Recall (wrong rows that are flagged): "
          f"{fd / ((fd + cd) or 1):.0%}. Scale suggestions on wrong rows: {c['suggestion right']:,} of "
          f"{c['suggestions with a validated answer']:,} give the validated answer exactly.", "",
          "Each check on its own (rows with a validated answer; a row can fail several):", "",
          "| Check | Flags | Wrong among them | Precision |", "| --- | --: | --: | --: |"]
    for name in CHECKS:
        d, m = c[f"check {name} differs"], c[f"check {name} match"]
        md.append(f"| {name} | {d + m:,} | {d:,} | {d / ((d + m) or 1):.0%} |")
    md += ["", "Ranges learned (US dollars for currencies):", ""]
    md += [f"- {k}: {fmt(lo)} to {fmt(hi)}" for k, (lo, hi) in sorted(learned.items())]
    if per_head["bounds"]:
        md.append(f"- revenue per employee: {fmt(per_head['bounds'][0])} to {fmt(per_head['bounds'][1])} USD")
    md += ["", "Examples:", ""] + [f"- {o['value_id']}: {fmt(o['value'])} {o['unit']} (validated {fmt(o['validated'])}): {o['plausibility']}"
                                   for o in out[:25]]
    if a.commit:
        from monitor_core import airtable_request
        from urllib.parse import quote
        url = f"https://api.airtable.com/v0/{base}/{quote('attribute_values')}"
        for i in range(0, len(writes), 10):
            airtable_request("PATCH", url, {**H, "Content-Type": "application/json"}, payload={"records": writes[i:i + 10]})
            time.sleep(0.22)
        md += ["", f"Written: plausibility changed on {len(writes):,} proposed rows (set or cleared)."]
    else:
        md += ["", "Dry run: nothing written (--commit writes plausibility)."]
    with open("plausibility.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["value_id", "attribute", "year", "value", "unit", "method", "validated",
                                           "matches", "suggested_value", "plausibility"])
        w.writeheader(); w.writerows(out)
    open("plausibility.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=c["proposed"], rows_out=c["flagged"], counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        open("plausibility.md", "w").write("### Plausibility: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        qa.record_exception("plausibility.py", e)
        raise
