#!/usr/bin/env python3
"""
value_eval.py: is the validated value anywhere in what we hold for the company?

The gold evaluation (gold_eval.py) asks whether the search finds the page the analyst cited. The owner's
point of 28 Oct 2026: what matters is the right answer, not the analyst's document; a revenue figure in
the 10-K counts as much as the same figure in the annual report the analyst used. This script measures
that ceiling for the number attributes, without any model:

  for every validated 2025-benchmark answer with a value (no quote needed), the number is looked for in
  ALL the company's pages in the evidence database, written the ways reports write it (in units,
  thousands, millions, billions, lakhs, crores; "513,727", "513 727", "2.561.249", "30,142.0"):
    1. found anywhere          on any page of any of the company's documents;
    2. found in the year       in a document of the answer's year or the next (a report on fiscal year
                               2024 is labelled 2024, or 2025 when the year ends early in 2025);
    3. found with its words    on a page that also names the attribute ("revenue", "net sales",
                               "turnover"; "scope 1"; "scope 2"; "capex", "capital expenditure").

Candidate pages come from the word index (the number's digit groups as a phrase); each candidate page is
then read and the number must be there exactly (no digit before or after it), so "1,513,727" does not
count for 513,727. A short number (fewer than four significant digits, such as "9.83 billion" or "858"
thousand) is reported apart: it is found by chance more often.

Read-only everywhere: the gold bases (GOLD_TOKEN), the evidence database (SUPABASE_DB_URL). Writes
value_eval.md and the run's counters in Pipeline QA. Options: --limit N answers per attribute (dry run
default 50), --attributes codes, --budget minutes (100), --commit (record the run in Pipeline QA).
"""
import argparse, collections, json, os, re, sys, time, traceback

import qa
from gold_eval import gold_id
from import_source_library import get_all

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_attributes.json")
CURRENCY_SCALES = (1, 1e3, 1e5, 1e6, 1e7, 1e8, 1e9)      # units, thousands, lakhs, millions, crores, 亿, billions
EMISSION_SCALES = (1, 1e3, 1e6)
MAX_PAGES = 40                                            # candidate pages read per answer
# a number with thousands groups separated by , . or ' (no spaces), one with groups separated by spaces
# (French, South African and Swiss style), and a plain number; tables put numbers side by side with
# spaces, so the spaced form is read apart from the others ("7 102,502 106,848" holds 102,502)
NUMBERS = [re.compile(r"(?<![\w.,])\d{1,3}(?:[,.'’]\d{3})+(?:[.,]\d+)?(?!\d)"),
           re.compile(r"(?<![\w.,])\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?(?!\d)"),     # Indian grouping: 7,50,616
           re.compile(r"(?<![\w.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![\d,.]\d)"),
           re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?!\d)")]


# ----------------------------------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------------------------------
def parse_value(text):
    """'513727000000 ARS' -> (513727000000.0, 'ARS'); placeholders ('1 ZAR', ' ') -> (None, unit)."""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(\S*)", text or "")
    if not m:
        return None, ""
    v = float(m.group(1))
    return (abs(v) if abs(v) > 1 else None), m.group(2)


def printed_forms(value, scales):
    """[(number as printed at some scale, significant digits, decimals)] the value can appear as: 513,727 for
    513727000000 in millions; 9.83 for 9,830,000,000 in billions; at most two decimals, and only when
    the rounding loses less than 0.05 %."""
    out = []
    for s in scales:
        x = value / s
        if x < 1:
            continue
        # every rounding within the tolerance, not only the first: 248,248.44 is printed with its
        # decimals although 248,248 is within 0.05 % of it (28 Oct 2026: such values were searched
        # without their decimals and missed)
        for d in (0, 1, 2):
            r = round(x, d)
            if r and abs(r - x) / x <= 0.0005 and r not in [o[0] for o in out]:
                out.append((r, len(f"{r:.{d}f}".replace(".", "").strip("0")), d))
    return out


def tsquery_for(r, d=0):
    """The word-index query that finds a printed number: its digit groups as a phrase ('513 <-> 727'),
    the plain digits ('513727'), the dotted form ('2.561.249'), and a whole number printed with zero
    decimals ('30,142.0', which the index holds as '30' then '142.0')."""
    ip, _, dec = f"{r:.{d}f}".partition(".")
    groups = f"{int(ip):,}".split(",")
    alts = []
    decs = [dec] if dec else ["", "0", "00"]
    for dd in decs:
        last = groups[-1] + ("." + dd if dd else "")
        alts.append(" <-> ".join(groups[:-1] + [last]))
        alts.append(ip + ("." + dd if dd else ""))
    if not dec and len(groups) > 1:
        alts.append(".".join(groups))
    if len(ip) > 5:                                     # Indian grouping: 7,50,616 / 1,25,00,000
        head, tail3 = ip[:-3], ip[-3:]
        ind = [head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1] + [tail3]
        last = ind[-1] + ("." + dec if dec else "")
        alts.append(" <-> ".join(ind[:-1] + [last]))
    alts = list(dict.fromkeys(alts))
    return " | ".join(f"({a})" if "<->" in a else a for a in alts)


def numbers_on(text):
    """Every number printed on a page, read both ways when the separator is ambiguous ('1,234' may be
    1234 or 1.234; '30,142.0' is 30142.0; '2.561.249' is 2561249; '65 534' is 65534)."""
    vals = set()
    for pat in NUMBERS:
        for t in pat.findall(text or ""):
            t = re.sub(r"[\u00a0\u202f]", " ", t).replace("’", "'")
            seps = re.findall(r"[,. ']", t)
            if not seps:
                vals.add(float(t)); continue
            last = seps[-1]
            head, _, tail = t.rpartition(last)
            dh = re.sub(r"[,. ']", "", head)
            if len(tail) == 3 and (len(seps) > 1 or last in " '"):
                vals.add(float(dh + tail))                       # 2,561,249 / 65 534
                if len(seps) > 1 and last in ",." and seps.count(last) == 1:
                    vals.add(float(dh + "." + tail))             # 1.234,567? rare; kept as an alternative
            elif len(tail) == 3:                                 # one separator before three digits: 1,234 or 1.234
                vals.add(float(dh + tail)); vals.add(float(dh + "." + tail))
            elif last in ",." and seps.count(last) == 1:
                vals.add(float(dh + "." + tail))                 # 799,366.15 / 30,142.0 / 9.83
    return vals


def found_on(text, r):
    return any(abs(v - r) < 1e-9 for v in numbers_on(text))


def attr_words(code, cfg):
    """Words that name the attribute on a page (any of them)."""
    name = (cfg["attributes"].get(code, {}).get("name") or "").lower()
    if "revenue" in name:
        return ["revenue", "revenues", "sales", "turnover", "income"]
    if "scope 1" in name:
        return ["scope 1", "scope i", "direct emissions"]
    if "scope 2" in name:
        return ["scope 2", "scope ii", "indirect emissions"]
    if "capex" in name or "capital" in name:
        return ["capex", "capital expenditure", "capital expenditures", "investment", "investments"]
    return [w for w in re.findall(r"[a-z]{4,}", name)]


def has_words(text, words):
    t = (text or "").lower()
    return any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z0-9])", t) for w in words)


# ----------------------------------------------------------------------------------------------
def gold_values(token, base, codes, sources=False):
    """Validated answers with a value: [{id, wba, attribute, year, value, unit}]. No quote needed.
    sources=True adds where the analyst took it from: quote (the Evidence text), page (as written),
    source_ids (Sources record ids), editor_url (the answer in the analysts' own editor)."""
    formula = "AND({Data Reviewed}, {Selected Value}!='', OR(" + ",".join(f"FIND('{c}', {{Metadata ID}})" for c in codes) + "))"
    fields = ["Metadata ID", "Selected Value"] + (["Evidence", "Page Number", "Source Link", "Editor Interface Full URL"] if sources else [])
    out = []
    for r in get_all(token, base, "Metadata", fields, formula):
        f = r["fields"]
        wba, code, year = gold_id(f.get("Metadata ID"))
        if code in codes:
            v, unit = parse_value(f.get("Selected Value"))
            g = {"id": f.get("Metadata ID"), "wba": wba, "attribute": code, "year": year, "value": v, "unit": unit}
            if sources:
                g.update(quote=(f.get("Evidence") or "").strip(), page=str(f.get("Page Number") or "").strip(),
                         source_ids=f.get("Source Link") or [], editor_url=str(f.get("Editor Interface Full URL") or "").strip())
            out.append(g)
    return out


def gold_sources(token, base):
    """{Sources record id: {name, url, year}} of a gold base (read-only): the documents analysts cited.
    Only fields every gold base has (Source Name is missing in some)."""
    out = {}
    for r in get_all(token, base, "Sources", ["Source ID", "Source Link", "Source Year"]):
        f = r["fields"]
        out[r["id"]] = {"name": (f.get("Source ID") or "").strip(),
                        "url": (f.get("Source Link") or "").strip(), "year": f.get("Source Year") or ""}
    return out


def evaluate(cur, g, words, scales):
    """{forms, found_any, found_year, found_words, short} for one answer."""
    forms = printed_forms(g["value"], scales)
    res = {"forms": len(forms), "found_any": False, "found_year": False, "found_words": False, "short": True}
    if not forms:
        return res
    q = " | ".join(f"({tsquery_for(r, d)})" for r, _, d in forms)
    cur.execute("select p.library_id, p.page, p.year, p.body from public.pages p "
                "where p.wba_id = %s and p.words @@ to_tsquery('simple', %s) limit %s", (g["wba"], q, MAX_PAGES))
    for lid, page, year, body in cur.fetchall():
        for r, sig, _ in forms:
            if found_on(body, r):
                res["found_any"] = True
                res["short"] = res["short"] and sig < 4
                in_year = year in (g["year"], g["year"] + 1)
                res["found_year"] |= in_year
                res["found_words"] |= in_year and has_words(body, words)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="answers per attribute (dry run default 50; 0 = all)")
    ap.add_argument("--attributes", default="")
    ap.add_argument("--budget", type=int, default=100, help="minutes")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    limit = a.limit or (0 if a.commit else 50)
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    codes = [c for c, v in cfg["attributes"].items() if v.get("type") == "number" and (not a.attributes or c in a.attributes.split(","))]
    token = os.environ.get("GOLD_TOKEN", "").strip()
    if not token:
        open("value_eval.md", "w").write("### Value evaluation: GOLD_TOKEN is not set; nothing run.\n")
        return 0
    run = qa.Run("value_eval.py")
    import psycopg
    answers = []
    for name, base in cfg["bases"].items():
        got = gold_values(token, base, codes)
        answers += got
        print(f">>> {name}: {len(got):,} validated answers with a value", flush=True)
    by = collections.defaultdict(list)
    for g in answers:
        by[g["attribute"]].append(g)
    queues = [by[c][:limit] if limit else by[c] for c in codes]
    todo = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]

    stats = {c: collections.Counter() for c in codes}
    misses = collections.defaultdict(list)
    t0, cut = time.time(), False
    with psycopg.connect(os.environ["SUPABASE_DB_URL"].strip(), connect_timeout=30) as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '30s'")
        for i, g in enumerate(todo, 1):
            if time.time() - t0 > a.budget * 60:
                cut = True; print(f">>> time budget reached after {i - 1:,} answers", flush=True); break
            if i % 100 == 0:
                print(f"  {i:,}/{len(todo):,} answers in {(time.time() - t0) / 60:.0f} min", flush=True)
            s = stats[g["attribute"]]
            s["answers"] += 1
            if g["value"] is None:
                s["no usable value"] += 1; continue
            s["with a value"] += 1
            scales = EMISSION_SCALES if g["unit"].lower().startswith("tco2") else CURRENCY_SCALES
            try:
                r = evaluate(cur, g, attr_words(g["attribute"], cfg), scales)
            except Exception as e:
                conn.rollback(); cur.execute("set statement_timeout = '30s'")
                s["database did not answer"] += 1; continue
            for k in ("found_any", "found_year", "found_words"):
                if r[k]:
                    s[k] += 1
                    if not r["short"]:
                        s[k + " (4+ digits)"] += 1
            if not r["found_any"] and len(misses[g["attribute"]]) < 12:
                misses[g["attribute"]].append(f"{g['id']}: {g['value']:,.2f} {g['unit']} not found in the company's pages")

    md = ["### Value evaluation: is the validated number anywhere in what we hold for the company?" + ("" if a.commit else " (dry run, sample)"), "",
          f"{sum(s['answers'] for s in stats.values()):,} validated answers" + (f" ({limit} per attribute)" if limit else "")
          + (f"; stopped at the time budget of {a.budget} minutes" if cut else "") + ". Percentages are of the answers with a usable value.", "",
          "| Attribute | Answers | With a value | Found anywhere | in that year's documents | with the attribute's words | same, 4+ digits only |",
          "| --- | --: | --: | --: | --: | --: | --: |"]
    for c in codes:
        s = stats[c]; n = s["with a value"] or 1
        pct = lambda k: f"{s[k]:,} ({s[k] / n:.0%})"
        md.append(f"| {c} {cfg['attributes'][c]['name']} | {s['answers']:,} | {s['with a value']:,} | {pct('found_any')} | "
                  f"{pct('found_year')} | {pct('found_words')} | {pct('found_words (4+ digits)')} |")
    md.append("")
    for c in codes:
        if misses[c]:
            md += [f"Not found, {c} (first {len(misses[c])}):", ""] + [f"- {m}" for m in misses[c]] + [""]
    open("value_eval.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    run.finish("ok", rows_in=len(todo), rows_out=sum(s["answers"] for s in stats.values()),
               counters={c: dict(stats[c]) for c in codes})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("value_eval.md", "w").write("### Value evaluation: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
