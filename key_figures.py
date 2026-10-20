#!/usr/bin/env python3
"""
key_figures.py — each company's latest revenue and number of employees, with the exact source.

Footprint work (revenue, employees) starts from the latest annual report: Annual Report / 10-K
first, a sustainability report when there is no annual one. This job does the finding:

1. Official filing data (exact numbers, no reading):
     SEC companyfacts (data.sec.gov/api/xbrl/companyfacts) for companies with companies.sec_cik:
       revenue  = the latest full-year value of us-gaap Revenues / RevenueFromContractWith... /
                  SalesRevenueNet, or ifrs-full:Revenue for 20-F filers
       employees = dei:EntityNumberOfEmployees when the company tags it (few do)
     ESEF (filings.xbrl.org xBRL-JSON) for companies with companies.lei:
       revenue  = ifrs-full:Revenue, full year, no breakdown
       employees = any fact named like NumberOfEmployees (taxonomy or company extension)
2. Report pages, for everything still missing: the company's latest annual-family document of
   2023+ (else sustainability family) is read from the full-text store, every page is scored for
   revenue / employee statements, and the best page is kept with a CANDIDATE value and the
   passage. The source link opens the PDF on that page.

Written on companies: revenue, revenue_source, employees, employees_source, key_figures_note.
A company with key_figures_checked ticked is never overwritten. Weekly (key_figures.yml) and on
demand. Dry run by default; --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SEC_CONTACT, TEXT_STORE_REPO + TEXT_STORE_TOKEN (or TEXT_STORE_DIR).
"""
import argparse, collections, json, os, re, sys, tempfile, time
from datetime import date

import requests

import text_core as tc
from regulator_filings import at_get, at_write, SEC_UA, ESEF

ANNUAL = ["Annual Report", "10K Form", "Integrated Report"]
SUST = ["Sustainability Report", "ESG Report", "CSR Report", "Impact Report"]
MIN_YEAR = 2023
SEC_REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
               "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
               "SalesRevenueGoodsNet"]


# ------------------------------------------------------------------------------------- formatting
def human(v, unit):
    cur = unit.split(":")[-1].upper() if unit else ""
    a = abs(float(v))
    for div, word in ((1e9, "billion"), (1e6, "million")):
        if a >= div:
            return f"{cur} {float(v) / div:,.2f} {word}".strip()
    return f"{cur} {float(v):,.0f}".strip()


def _days(start, end):
    from datetime import date as d
    try:
        return (d.fromisoformat(end[:10]) - d.fromisoformat(start[:10])).days
    except Exception:
        return 0


# ------------------------------------------------------------------------------------------- SEC
def sec_figures(cik, sess):
    r = sess.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json", headers=SEC_UA, timeout=90)
    time.sleep(0.15)
    if not r.ok:
        return {}
    facts = r.json().get("facts", {})
    out = {}
    best = None
    for tax, names in (("us-gaap", SEC_REVENUE), ("ifrs-full", ["Revenue"])):
        for n in names:
            for unit, rows in (facts.get(tax, {}).get(n, {}).get("units", {}) or {}).items():
                for x in rows:
                    if x.get("form") not in ("10-K", "20-F", "40-F") or not x.get("start"):
                        continue
                    if not 330 <= _days(x["start"], x["end"]) <= 400:
                        continue
                    k = (x["end"], x.get("filed", ""))
                    if best is None or k > best[0]:
                        best = (k, x, unit)
    if best:
        _, x, unit = best
        acc = x["accn"]
        out["revenue"] = (f"{human(x['val'], unit)} (fiscal year ending {x['end']})",
                          f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm",
                          f"SEC filing data ({x['form']}, filed {x.get('filed', '')})")
    emp = facts.get("dei", {}).get("EntityNumberOfEmployees", {}).get("units", {})
    rows = [x for u in emp.values() for x in u if x.get("form") in ("10-K", "20-F", "40-F")]
    if rows:
        x = max(rows, key=lambda x: (x.get("end", ""), x.get("filed", "")))
        acc = x["accn"]
        out["employees"] = (f"{int(x['val']):,} ({x.get('end', '')[:4]})",
                            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm",
                            f"SEC filing data ({x['form']})")
    return out


# ------------------------------------------------------------------------------------------ ESEF
EMP_CONCEPT = re.compile(r"(NumberOf(FullTime)?Employees|Headcount|NumberOfStaff)", re.I)


def esef_figures(lei, sess):
    flt = json.dumps([{"name": "entity.identifier", "op": "eq", "val": lei}])
    r = sess.get(f"{ESEF}/api/filings", params={"filter": flt, "page[size]": "5", "sort": "-period_end"},
                 headers={"Accept": "application/vnd.api+json"}, timeout=60)
    time.sleep(0.3)
    if not r.ok or not r.json().get("data"):
        return {}
    a = r.json()["data"][0].get("attributes", {})
    ju = a.get("json_url") or ""
    view = a.get("viewer_url") or a.get("report_url") or ""
    view = view if view.startswith("http") else (ESEF + "/" + view.lstrip("/") if view else "")
    if not ju:
        return {}
    j = sess.get(ju if ju.startswith("http") else ESEF + "/" + ju.lstrip("/"), timeout=120)
    if not j.ok:
        return {}
    out = {}
    rev, emp = None, None
    for f in j.json().get("facts", {}).values():
        d = f.get("dimensions", {})
        if set(d) - {"concept", "entity", "period", "unit", "language"}:
            continue                                   # a breakdown (segment, region...), not the total
        c = d.get("concept", ""); per = d.get("period", "")
        try:
            v = float(f.get("value"))
        except (TypeError, ValueError):
            continue
        end = per.split("/")[-1]
        if end.endswith("T00:00:00"):            # xBRL-JSON period ends are exclusive midnights
            from datetime import date as _d, timedelta as _t
            try:
                end = (_d.fromisoformat(end[:10]) - _t(days=1)).isoformat()
            except ValueError:
                end = end[:10]
        end = end[:10]
        if c == "ifrs-full:Revenue" and "/" in per and 330 <= _days(per.split("/")[0], per.split("/")[1]) <= 400:
            if rev is None or end > rev[0]:
                rev = (end, v, d.get("unit", ""))
        elif EMP_CONCEPT.search(c.split(":")[-1]) and v >= 1:
            if emp is None or end > emp[0]:
                emp = (end, v, c)
    if rev:
        out["revenue"] = (f"{human(rev[1], rev[2])} (fiscal year ending {rev[0]})", view,
                          "ESEF filing data (ifrs-full:Revenue)")
    if emp:
        out["employees"] = (f"{int(emp[1]):,} ({emp[0][:4]})", view, f"ESEF filing data ({emp[2]})")
    return out


# ----------------------------------------------------------------------------------- report pages
NUM = r"(\d{1,3}(?:[,.\s]\d{3})+|\d{2,7})(?:[.,]\d+)?"
CUR = r"(?-i:[A-Z]{3}|US\$|[$€£¥₹]|RMB|Rp\.?|R\$|NT\$|HK\$|S\$|A\$|C\$|¥|R(?=\s?\d))"
SCALE = r"(?:\s?(?:million|billion|bn|mn|thousand|trillion|crore|lakh|m\b))"
EMP_STRONG = re.compile(r"\b(number of employees|total (?:number of )?employees|total workforce|total headcount|"
                        r"employees at (?:the )?(?:end of|year[- ]end)|full[- ]time employees|employee headcount)\b", re.I)
EMP_VALUE = re.compile(r"(?:(?:approximately|approx\.|about|around|over|more than|nearly|some|total of|c\.)\s*)?"
                       + NUM + r"\s+(?:full[- ]time\s+)?(?:employees|people|staff|team members|associates|colleagues|workers)\b"
                       r"|(?:number of employees|total (?:number of )?employees|total workforce|(?:employee )?headcount|"
                       r"group employees|employees \(total\))\b[^.\d%]{0,40}?" + NUM + r"(?!\s?%)", re.I)
REV_STRONG = re.compile(r"\b(total revenues?|net revenues?|net sales|revenues? for the year|consolidated revenues?|"
                        r"total income|sales revenue|operating revenues?|group revenue)\b", re.I)
REV_VALUE = re.compile(r"\b(?:total revenues?|net revenues?|net sales|revenues?|consolidated revenues?|sales|turnover)\b"
                       r"[^.]{0,60}?(" + CUR + r"\s?" + NUM + SCALE + r"?|" + NUM + SCALE + r")", re.I)
HILITE = re.compile(r"\b(key figures|highlights|at a glance|in brief|key facts|financial summary|"
                    r"performance overview|company profile|about (?:us|this report))\b", re.I)
JUNK_PAGE = re.compile(r"\.{5,}|\bGRI \d{3}\b|\btable of contents\b|\bcontents\b", re.I)
NOT_REV = re.compile(r"\b(per|intensity|ratio|growth|increase|decrease|% of|share of|from these products|"
                     r"turnover rate|employee turnover)\b", re.I)


def _number(txt):
    m = re.search(r"\d[\d,.\s]*\d|\d", txt)
    if not m:
        return None
    raw = re.sub(r"[,\s]", "", m.group(0))
    if raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _plausible(kind, txt):
    n = _number(txt)
    if n is None:
        return False
    if kind == "employees":
        if 1990 <= n <= 2035 or n < 50:          # a year, a page number or a footnote, not a headcount
            return False
        return not re.search(r"(supported|insured|customers|members|clients|at the companies)", txt, re.I)
    return not NOT_REV.search(txt)


def score_page(text, page, kind):
    strong, value = (EMP_STRONG, EMP_VALUE) if kind == "employees" else (REV_STRONG, REV_VALUE)
    if len(JUNK_PAGE.findall(text)) >= 3:        # tables of contents and GRI indexes name everything
        return 0, ""
    vals = []
    for m in value.finditer(text):
        ctx = text[max(0, m.start() - 40):m.end() + 40] if kind == "revenue" else text[m.start():m.end() + 40]
        if _plausible(kind, m.group(0)) and _plausible(kind, ctx):
            vals.append(m.group(0).strip())
    if not vals:
        return 0, ""
    s = len(strong.findall(text)) * 3 + 2 + min(len(vals), 3)
    if HILITE.search(text):
        s += 3
    if page <= 15:
        s += 2
    return s, vals[0][:90]


def passage(text, cand, width=160):
    i = text.find(cand)
    if i < 0:
        return text[:width * 2]
    return text[max(0, i - width):i + len(cand) + width].replace("\n", " ")


def target_docs(meta):
    """{wba: doc meta} the document to read per company: latest annual family 2023+, else the
    latest sustainability family 2023+."""
    by = collections.defaultdict(list)
    for m in meta.values():
        try:
            y = int(m.get("year"))
        except (TypeError, ValueError):
            continue
        if not m.get("wba") or y < MIN_YEAR:
            continue
        rank = 0 if m.get("type") in ANNUAL else 1 if m.get("type") in SUST else None
        if rank is not None:
            by[m["wba"]].append((rank, -y, m["id"], m))
    return {w: sorted(v)[0][3] for w, v in by.items()}


def pages_figures(wbas, work):
    """{wba: {kind: (value, page url, note)}} from the full-text store."""
    store = tc.open_store()
    listing = store.assets(refresh=True)
    docs_files, page_files = tc.complete_parts(listing)
    metas = sorted(n for n in listing if n.startswith("meta-"))
    if not metas:
        print(">>> no text store metadata: page search skipped.", flush=True)
        return {}
    tc.sync_down(store, docs_files + [metas[-1]], work)
    index = tc.load_docs_index(work, docs_files)
    meta = {m["id"]: m for m in tc.read_jsonl_gz(os.path.join(work, metas[-1]))}
    owner = {i: d["part"] for i, d in index.items() if d["status"] == tc.OK_STATUS and i in meta}
    targets = {w: m for w, m in target_docs({i: meta[i] for i in owner}).items() if w in wbas}
    want = {m["id"]: w for w, m in targets.items()}
    parts = {owner[i] for i in want}
    files = [n for n in page_files if tc.part_of(n)[1] in parts]
    print(f">>> pages: {len(targets)} companies have a 2023+ report in the text store; reading {len(files)} part(s).",
          flush=True)
    tc.sync_down(store, files, work)
    best = collections.defaultdict(dict)      # wba -> kind -> (score, page, cand, text)
    import gzip
    for n in files:
        part = tc.part_of(n)[1]
        with gzip.open(os.path.join(work, n), "rb") as fh:
            for line in fh:
                if len(line) < 12:
                    continue
                i = tc._line_id(line)
                if i not in want or owner.get(i) != part:
                    continue
                rec = json.loads(line); text = rec["t"] or ""
                for kind in ("revenue", "employees"):
                    s, cand = score_page(text, rec["p"], kind)
                    if s and s > best[want[i]].get(kind, (0,))[0]:
                        best[want[i]][kind] = (s, rec["p"], cand, text)
    out = {}
    for w, kinds in best.items():
        m = targets[w]
        for kind, (s, p, cand, text) in kinds.items():
            url = (m.get("url") or "").split("#")[0] + f"#page={p}" if m.get("url") else ""
            out.setdefault(w, {})[kind] = (
                f"{cand} (candidate, {m.get('year')})", url,
                f"Candidate from page {p} of \"{m.get('label') or m.get('type')}\": ...{passage(text, cand)}...")
    return out


# ------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--wba", default="")
    ap.add_argument("--no-pages", action="store_true", help="filings only, skip the report-page search")
    a = ap.parse_args()
    wbas = [w.strip() for w in a.wba.split(",") if w.strip()]
    formula = ("OR(" + ",".join(f"{{wba_id}}='{w}'" for w in wbas) + ")") if wbas else None
    comps = {c["fields"]["wba_id"]: c for c in at_get("companies", ["wba_id", "sec_cik", "lei", "key_figures_checked",
                                                                    "revenue", "employees"], formula)
             if c["fields"].get("wba_id") and not c["fields"].get("key_figures_checked")}
    print(f">>> {len(comps)} companies (checked ones skipped).", flush=True)
    sess = requests.Session()
    found = collections.defaultdict(dict); stats = collections.Counter()
    for w, c in comps.items():
        f = c["fields"]
        try:
            if f.get("sec_cik") and f["sec_cik"] != "none":
                for k, v in sec_figures(f["sec_cik"], sess).items():
                    found[w].setdefault(k, v); stats[f"{k} from SEC"] += 1
            if (f.get("lei") or "").strip() and len(found[w]) < 2:
                for k, v in esef_figures(f["lei"].strip(), sess).items():
                    if k not in found[w]:
                        found[w][k] = v; stats[f"{k} from ESEF"] += 1
        except Exception as e:
            print(f"    {w}: filing data failed ({type(e).__name__}: {e})", flush=True)
    if not a.no_pages:
        need = {w for w in comps if len(found[w]) < 2}
        try:
            work = os.environ.get("TEXT_WORK_DIR") or tempfile.mkdtemp(prefix="figures_")
            for w, kinds in pages_figures(need, work).items():
                for k, v in kinds.items():
                    if k not in found[w]:
                        found[w][k] = v; stats[f"{k} from report pages"] += 1
        except tc.StoreUnavailable as e:
            print(f">>> page search skipped: {e}", flush=True)
    print(f">>> {dict(stats)}", flush=True)
    today = date.today().isoformat()
    upd = []
    for w, c in comps.items():
        kinds = found.get(w, {})
        fields, notes = {}, []
        for k in ("revenue", "employees"):
            if k in kinds:
                val, src, note = kinds[k]
                fields[k] = val[:250]; fields[f"{k}_source"] = src or None
                notes.append(f"{k}: {note}")
            elif "(candidate" in (c["fields"].get(k) or ""):
                fields[k] = ""; fields[f"{k}_source"] = None      # an older, rejected candidate
        if not fields:
            continue
        fields["key_figures_note"] = (f"{today}\n" + "\n".join(notes)) if notes else ""
        upd.append({"id": c["id"], "fields": fields})
    for u in upd[:6]:
        print(f"    {u['fields'].get('revenue')} | {u['fields'].get('employees')}", flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(f"## Key figures\n\n{dict(stats)}\n\n{len(upd)} companies "
                     f"{'updated' if a.commit else 'would be updated (dry run)'}.\n")
    if a.commit:
        at_write("companies", "PATCH", upd)
        print(f">>> wrote key figures on {len(upd)} companies.", flush=True)
    else:
        print(">>> dry run, nothing written.", flush=True)


if __name__ == "__main__":
    main()
