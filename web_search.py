#!/usr/bin/env python3
"""
web_search.py: find a company's missing annual and sustainability reports with a web search (Serper,
Google results), when the robots that watch its pages and the stock-exchange filings have not brought
them in.

For each company it works out which recent years it lacks for each family:
  sustainability   Sustainability, ESG, CSR, Integrated or Impact Report; years held from `sust_years`
  annual           Annual Report, Integrated Report, 10-K; latest held from `coverage_breakdown`
Recent years are the three up to the latest year that should be out by now (company_coverage's
expected_year: in November 2026 that is 2023 to 2025).

For each family with missing years it asks one question first on the company's own sites:
    site:acme.com OR site:acme-ir.com (sustainability OR ESG OR ...) (2025 OR 2024) filetype:pdf
and, only when that finds nothing usable, one question by name:
    "Acme Group" (sustainability OR ESG OR ...) (2025 OR 2024) filetype:pdf

A result is passed on only when all three hold:
  - it is on one of the company's sites (its website and the domains of its monitored pages) or on a
    document host that serves many companies' reports (DOC_HOSTS: q4cdn, HKEXnews...);
  - its title, address or snippet names the family, in English or one of the common report languages;
  - it names one of the missing years.
Passed documents go to companies.research_link, so the inbox (inbox.py, every 30 minutes) adds them
with its duplicate and tombstone guards, attaches the file and has it typed and dated like any other
document, marked as research. At most MAX_PER_FAMILY per family and company. Results on another site
are never added: they are listed in web_search_note with the reason, for a person or a later rule.

Modes (web_searched_on says when a company was last searched):
  --mode recovery   companies with a missing recent year that were never searched: the one-off sweep
  --mode gaps       companies whose coverage says a recent report is missing or overdue (gap_pages,
                    overdue_reports), searched again after RECHECK_DAYS: the standing mode
  --mode auto       recovery while any company is left to sweep, then gaps (the schedule)
The inbox handles what this passes on in its own runs; when more than INBOX_BUSY companies still hold
research_link, a run waits (writes nothing) so the inbox is never handed more than it can do in one go.
Dry run by default: nothing is written, the report says what would be added. --commit writes
research_link, web_searched_on and web_search_note. --max-queries caps the Serper credits a run may
spend (one query = one credit); --limit caps the companies (a PR runs 30); --wba picks companies.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, SERPER_API_KEY; AIRTABLE_QA_TOKEN (optional).
Writes web_search.md (also posted on a pull request touching this file).
"""
import argparse, collections, json, os, re, sys, time, traceback
from datetime import date, timedelta

import qa

SERPER_URL = "https://google.serper.dev/search"
RECHECK_DAYS = 30                       # gaps mode: a company is searched again after this many days
INBOX_BUSY = 20                         # companies still holding research_link: the inbox has not caught up, wait
MAX_PER_FAMILY = 3                      # documents passed on per company and family in one search
YEARS_BACK = 3                          # recent years: expected_year() and the two before it
WRITE_PAUSE = 0.22

# Hosts that serve many listed companies' documents from a domain that is not the company's own.
DOC_HOSTS = {"q4cdn.com", "hkexnews.hk", "mziq.com", "sitecorecloud.io", "cision.com", "globenewswire.com",
             "annualreports.com", "amazonaws.com", "cloudfront.net", "azureedge.net", "sec.gov",
             "cninfo.com.cn", "sse.com.cn", "szse.cn", "tdnet.info", "jpx.co.jp", "edinet-fsa.go.jp", "dart.fss.or.kr",
             "bseindia.com", "nseindia.com", "londonstockexchange.com", "euronext.com", "asx.com.au"}
# Shared hosts where a file may belong to any company: added only when the company's name is in it.
NAME_CHECK_HOSTS = {"annualreports.com", "amazonaws.com", "cloudfront.net", "azureedge.net", "cision.com",
                    "globenewswire.com"}

FAMILIES = {
    "sustainability": {
        "query": '(sustainability OR ESG OR CSR OR "non-financial" OR "impact report" OR responsibility)',
        "words": ["sustainab", "esg", "csr", "non-financial", "nonfinancial", "impact report", "responsib",
                  "nachhaltig", "durabilit", "développement durable", "sostenib", "sustentab", "duurzaam",
                  "hållbar", "bærekraft", "bæredygtig", "vastuullisuus", "zrównoważon", "sürdürülebilir",
                  "integrated report", "可持续", "社会责任", "永續", "サステナビリティ", "統合報告", "지속가능"]},
    "annual": {
        "query": '("annual report" OR "integrated report" OR "universal registration document" OR "10-K")',
        "words": ["annual report", "annual-report", "annualreport", "integrated report", "10-k", "10k",
                  "registration document", "document d'enregistrement", "rapport annuel", "geschäftsbericht",
                  "geschaftsbericht", "jahresbericht", "informe anual", "relatório anual", "relatorio anual",
                  "relazione annuale", "relazione finanziaria annuale", "jaarverslag", "årsredovisning",
                  "årsrapport", "årsberetning", "vuosikertomus", "raport roczny", "faaliyet raporu",
                  "年度报告", "年报", "年報", "有価証券報告書", "統合報告", "사업보고서", "annual financial report"]},
}


def expected_year(today=None):
    today = today or date.today()
    return today.year - 1 if today.month >= 7 else today.year - 2


def recent_years(today=None):
    e = expected_year(today)
    return list(range(e, e - YEARS_BACK, -1))


def years_in(text):
    return {int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", text or "")}


def held_years(f):
    """{"sustainability": {years}, "annual": {years}} from the coverage fields. Annual: only the latest
    per type is known, so every year up to the latest counts as held."""
    sust = years_in(f.get("sust_years") or "")
    latest_annual = 0
    for line in (f.get("coverage_breakdown") or "").splitlines():
        t = line.split(":")[0].strip()
        m = re.search(r"latest (\d{4})", line)
        if m and t in ("Annual Report", "Integrated Report", "10K Form"):
            latest_annual = max(latest_annual, int(m.group(1)))
        if m and t == "Integrated Report":
            sust.add(int(m.group(1)))
    return {"sustainability": sust, "annual": set(range(2000, latest_annual + 1)) if latest_annual else set()}


def missing(f, years):
    held = held_years(f)
    return {fam: [y for y in years if y not in held[fam]] for fam in FAMILIES}


def in_gap(f):
    """The nightly mode's test: coverage says a recent report is missing or overdue."""
    gaps = set(f.get("gap_pages") or []) | set(f.get("overdue_reports") or [])
    return bool(gaps & {"Sustainability report (recent)", "Annual report (recent)", "Sustainability report", "Annual report"})


def company_domains(f, page_urls, reg_domain):
    """The company's own registered domains: its website first, then those of its monitored pages
    (most pages first), document hosts excluded. At most 4."""
    out = []
    w = (f.get("website") or "").strip()
    if w:
        d = reg_domain(w if "://" in w else "https://" + w)
        if d and d not in DOC_HOSTS:
            out.append(d)
    for d, _ in collections.Counter(reg_domain(u) for u in page_urls if u).most_common():
        if d and d not in out and d not in DOC_HOSTS and len(out) < 4:
            out.append(d)
    return out


def name_tokens(name):
    stop = {"the", "and", "group", "holding", "holdings", "company", "corp", "corporation", "inc", "ltd", "limited",
            "plc", "sa", "ag", "nv", "se", "spa", "co", "of", "asa", "ab", "oyj", "bhd", "tbk", "pt", "berhad", "s.a."}
    return [t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in stop and len(t) > 2]


def query(fam, years, domains=None, name=None):
    ys = " OR ".join(str(y) for y in sorted(years, reverse=True))
    ys = f"({ys})" if len(years) > 1 else ys
    if domains:
        where = " OR ".join(f"site:{d}" for d in domains)
        where = f"({where})" if len(domains) > 1 else where
    else:
        where = '"' + (name or "").replace('"', "") + '"'
    return f"{where} {FAMILIES[fam]['query']} {ys} filetype:pdf"


def judge(r, fam, years, domains, name, reg_domain):
    """(True, reason) when a search result is passed on, else (False, reason)."""
    link, text = r.get("link") or "", " ".join([r.get("title") or "", r.get("snippet") or "", r.get("link") or ""]).lower()
    d = reg_domain(link)
    if d not in domains and d not in DOC_HOSTS:
        return False, f"another site ({d})"
    if d in NAME_CHECK_HOSTS and not any(t in text for t in name_tokens(name)[:2]):
        return False, f"shared host without the company's name ({d})"
    if not any(w in text for w in FAMILIES[fam]["words"]):
        return False, "does not name the report"
    ys = years_in(text)
    if not ys & set(years):
        return False, "not one of the missing years" + (f" ({', '.join(str(y) for y in sorted(years_in(text))[-3:])})" if years_in(text) else "")
    return True, f"{fam} {max(ys & set(years))}"


class Serper:
    def __init__(self, key, max_queries, sess=None):
        import requests
        self.key, self.left, self.used, self.sess = key, max_queries, 0, sess or requests.Session()
        self.stopped = ""

    def search(self, q):
        """The organic results, or None when the budget is spent or the service refuses."""
        if self.stopped or self.left <= 0:
            self.stopped = self.stopped or "the query budget is spent"
            return None
        for attempt in range(3):
            try:
                r = self.sess.post(SERPER_URL, headers={"X-API-KEY": self.key, "Content-Type": "application/json"},
                                   data=json.dumps({"q": q, "num": 10}), timeout=30)
            except Exception:
                time.sleep(3 * (attempt + 1)); continue
            if r.status_code in (401, 402, 403):
                self.stopped = f"Serper refused the key or has no credits left (HTTP {r.status_code})"
                return None
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(5 * (attempt + 1)); continue
            self.used += 1; self.left -= 1
            return (r.json() or {}).get("organic") or [] if r.ok else []
        return []


def search_company(f, page_urls, years, api, reg_domain):
    """{"looked_for": [...], "found": [(url, why)], "not_added": [(url, why)], "queries": n}"""
    name = f.get("company_name") or ""
    domains = company_domains(f, page_urls, reg_domain)
    out = {"looked_for": [], "found": [], "not_added": [], "queries": 0}
    for fam, ys in missing(f, years).items():
        if not ys:
            continue
        out["looked_for"].append(f"{fam} {', '.join(str(y) for y in ys)}")
        tried = []
        if domains:
            tried.append(query(fam, ys, domains=domains))
        if name:
            tried.append(query(fam, ys, name=name))
        got = 0
        for q in tried:
            results = api.search(q)
            if results is None:
                out["cut"] = True
                return out
            out["queries"] += 1
            for r in results:
                ok, why = judge(r, fam, ys, domains, name, reg_domain)
                link = r.get("link") or ""
                if ok and got < MAX_PER_FAMILY and link not in {u for u, _ in out["found"]}:
                    out["found"].append((link, why)); got += 1
                elif not ok and len(out["not_added"]) < 8 and "does not name" not in why:
                    out["not_added"].append((link, why))
            if got:
                break                     # the company's own sites answered: no question by name
    return out


def note(today, res):
    lines = [f"{today}: looked for {'; '.join(res['looked_for']) or 'nothing (no recent year missing)'} "
             f"({res['queries']} searches)."]
    lines += [f"  passed to research_link: {u} ({why})" for u, why in res["found"]]
    lines += [f"  not added: {u} ({why})" for u, why in res["not_added"]]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--mode", default="auto", choices=["auto", "gaps", "recovery"])
    ap.add_argument("--limit", type=int, default=0, help="companies at most (0 = all due)")
    ap.add_argument("--max-queries", type=int, default=300, help="Serper credits this run may spend")
    ap.add_argument("--wba", default="", help="wba ids, comma-separated (ignores the waits)")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        open("web_search.md", "w").write("### Web search: SERPER_API_KEY is not set (a repository secret); nothing run.\n")
        return 0
    from propose_values import fetch
    from monitor_core import reg_domain
    base, H = os.environ["AIRTABLE_BASE"], {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    run = qa.Run("web_search.py")
    today = date.today()
    years = recent_years(today)
    comps = fetch(H, base, "companies", ["wba_id", "company_name", "website", "sust_years", "coverage_breakdown",
                                         "gap_pages", "overdue_reports", "web_searched_on", "web_search_note", "research_link"])
    wanted = {x.strip() for x in a.wba.split(",") if x.strip()}
    c = collections.Counter()
    busy = sum(1 for r in comps if (r["fields"].get("research_link") or "").strip())
    if a.commit and busy > INBOX_BUSY and not wanted:
        md = (f"### Web search for missing reports ({a.mode})\n\n{busy} companies still hold research_link from an "
              f"earlier run: the inbox has not caught up, so this run waits. Nothing searched, nothing written.\n")
        open("web_search.md", "w").write(md); print(md, flush=True)
        run.finish("skipped", rows_in=0, rows_out=0, counters={"inbox busy": busy})
        return 0

    def due_for(mode):
        out = []
        for r in comps:
            f = r["fields"]
            if not f.get("wba_id"):
                continue
            if wanted:
                if f["wba_id"] in wanted:
                    out.append(r)
                continue
            if not any(missing(f, years).values()):
                continue
            last = f.get("web_searched_on")
            if mode == "recovery" and not last:
                out.append(r)
            elif mode == "gaps" and in_gap(f) and (not last or (today - date.fromisoformat(last[:10])).days >= RECHECK_DAYS):
                out.append(r)
        return out

    mode = a.mode
    due = due_for("recovery" if mode in ("auto", "recovery") else "gaps")
    if mode == "auto":
        mode = "recovery" if due else "gaps"
        due = due or due_for("gaps")
    c["companies missing a recent year"] = sum(1 for r in comps if r["fields"].get("wba_id") and any(missing(r["fields"], years).values()))
    c["companies never searched (left to sweep)"] = sum(1 for r in comps if r["fields"].get("wba_id") and not r["fields"].get("web_searched_on")
                                                       and any(missing(r["fields"], years).values()))
    # the companies missing the most first, then a stable order
    due.sort(key=lambda r: (-sum(len(v) for v in missing(r["fields"], years).values()), r["fields"]["wba_id"]))
    if a.limit and not a.commit:          # a dry run's sample is spread over the companies, not the hardest first
        import hashlib
        due.sort(key=lambda r: hashlib.sha1(r["fields"]["wba_id"].encode()).hexdigest())
    if a.limit:
        due = due[:a.limit]
    c["companies due this run"] = len(due)
    print(f">>> {len(due)} companies to search ({mode}); years {years}", flush=True)
    links = fetch(H, base, "monitored_links", ["wba_id", "url"],
                  "OR(" + ",".join(f"{{wba_id}}='{r['fields']['wba_id']}'" for r in due[:150]) + ")") if 0 < len(due) <= 150 else \
        fetch(H, base, "monitored_links", ["wba_id", "url"])
    pages = collections.defaultdict(list)
    for l in links:
        pages[l["fields"].get("wba_id")].append(l["fields"].get("url") or "")
    api = Serper(key, a.max_queries)
    updates, examples = [], []
    for i, r in enumerate(due, 1):
        f = r["fields"]
        res = search_company(f, pages.get(f["wba_id"], []), years, api, reg_domain)
        if res.get("cut"):
            break                        # cut by the budget: not marked as searched, picked up next run
        c["companies searched"] += 1
        c["companies with a document found"] += bool(res["found"])
        c["documents passed on"] += len(res["found"])
        for _, why in res["found"]:
            c["found: " + why.split()[0]] += 1
        for _, why in res["not_added"]:
            c["not added: " + re.sub(r" \(.*", "", why)] += 1
        if len(examples) < 40 and (res["found"] or res["not_added"]):
            examples.append(f"**{f['wba_id']} {f.get('company_name', '')}**\n\n```\n{note(today.isoformat(), res)}\n```")
        upd = {"web_searched_on": today.isoformat(),
               "web_search_note": (note(today.isoformat(), res) + "\n\n" + (f.get("web_search_note") or ""))[:20000]}
        if res["found"]:
            upd["research_link"] = "\n".join([x for x in [(f.get("research_link") or "").strip()] if x] + [u for u, _ in res["found"]])
        updates.append({"id": r["id"], "fields": upd})
        if i % 50 == 0:
            print(f"  {i}/{len(due)}: {api.used} searches, {c['documents passed on']} documents", flush=True)
    c["searches (credits)"] = api.used
    if api.stopped:
        c["stopped: " + api.stopped] = 1
    written = 0
    if a.commit:
        from monitor_core import airtable_request
        for i in range(0, len(updates), 10):
            airtable_request("PATCH", f"https://api.airtable.com/v0/{base}/companies", H,
                             json={"records": updates[i:i + 10], "typecast": False})
            written += len(updates[i:i + 10]); time.sleep(WRITE_PAUSE)
    c["companies written"] = written
    md = [f"### Web search for missing reports ({mode})" + ("" if a.commit else " (dry run: nothing written)"), "",
          f"Recent years: {', '.join(str(y) for y in years)}. Serper searches used: {api.used} (one credit each)."
          + (f" Stopped: {api.stopped}." if api.stopped else ""), "", "| | |", "| --- | --: |"]
    md += [f"| {k} | {v:,} |" for k, v in sorted(c.items())]
    if c["companies searched"]:
        md += ["", f"Per company searched: {api.used / c['companies searched']:.1f} searches, "
                   f"{c['documents passed on'] / c['companies searched']:.2f} documents. A full sweep of "
                   f"{c['companies missing a recent year']:,} companies would use about {api.used / c['companies searched'] * c['companies missing a recent year']:,.0f} searches."]
    md += ["", "#### Examples (first 40 companies with a result)", ""] + examples
    text = "\n".join(md) + "\n"
    open("web_search.md", "w").write(text)
    print(text, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text)
    run.finish("ok" if not api.stopped or "budget" in api.stopped else "partial",
               rows_in=len(due), rows_out=c["documents passed on"], counters=dict(c))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        open("web_search.md", "w").write("### Web search: the run failed\n\n```\n" + traceback.format_exc()[-2500:] + "```\n")
        raise
