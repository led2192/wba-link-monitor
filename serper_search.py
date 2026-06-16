#!/usr/bin/env python3
"""
serper_search.py — ask Google (via Serper.dev) for each company's sustainability report.

For every company it runs a site-restricted Google search for the company's sustainability /
ESG / CSR report and writes the candidate results into a SEPARATE table (report_search),
one row per result, deduped. This is the "what's the latest out there" radar, complementing
the page monitoring.

IMPORTANT — the date filter is NOT done here. Serper/Google dates are unreliable for PDFs.
This job only finds CANDIDATES. You then add an AI field in Airtable (that navigates the URL)
to read the document's real PUBLICATION date, and filter to those published in 2026 — i.e.
the FY2025 report released in 2026 counts; one released in 2025 does not.

Env: SERPER_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE,
     REPORT_SEARCH_TABLE (default report_search)
Create the report_search table with:
  wba_id -> Single line text   company_name -> Single line text   title -> Single line text
  url -> URL   snippet -> Long text   google_date -> Single line text
  found -> Single line text (or Date)   status -> Single select (new, reviewed, dismissed)
Then add an AI field (navigates URL):
  pub_date -> AI: "Open the URL, find this document's PUBLICATION date, output only YYYY-MM
              or YYYY, or NONE."  and a view filtering pub_date that starts with 2026.
"""
import os, re, sys, time, datetime as dt
from urllib.parse import urlsplit, parse_qsl, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter

try:
    import requests, tldextract
except ImportError:
    sys.exit("pip install requests tldextract")

API   = "https://api.airtable.com/v0"
SERPER= "https://google.serper.dev/search"
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
KEY   = os.environ.get("SERPER_API_KEY")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
RTABLE= os.environ.get("REPORT_SEARCH_TABLE", "report_search")
if not (TOKEN and BASE and KEY):
    sys.exit("Set AIRTABLE_TOKEN, AIRTABLE_BASE and SERPER_API_KEY environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

TODAY = dt.date.today().isoformat()
WORKERS = 6
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)
REPORTISH = re.compile(r"sustainab|\besg\b|\bcsr\b|non.?financial|responsib|integrated|environment", re.I)

def reg_domain(u):
    e=_EXTRACT(u); return f"{e.domain}.{e.suffix}".lower() if e.suffix else (e.domain or "").lower()
def norm(u):
    try: s=urlsplit(u)
    except Exception: return ""
    if s.scheme not in ("http","https"): return ""
    host=(s.hostname or "").lower()
    if host.startswith("www."): host=host[4:]
    path=re.sub(r"/+$","",s.path)
    q=sorted((k,v) for k,v in parse_qsl(s.query) if not TRACKING.match(k))
    return (host+path+("?"+urlencode(q) if q else "")).lower()

def serper(query):
    try:
        r=requests.post(SERPER, headers={"X-API-KEY":KEY,"Content-Type":"application/json"},
                        json={"q":query, "num":10, "tbs":"qdr:y"}, timeout=30)  # qdr:y = bias to last 12 months
        r.raise_for_status()
        return r.json().get("organic", [])
    except Exception as e:
        return []

def search_company(wba, name, rdom):
    q = f'(sustainability OR ESG OR CSR OR "non-financial") report site:{rdom}' if rdom else \
        f'"{name}" (sustainability OR ESG OR CSR) report'
    out=[]
    for res in serper(q):
        link=res.get("link","")
        if not link: continue
        if rdom and reg_domain(link)!=rdom: continue
        blob=(res.get("title","")+" "+res.get("snippet","")+" "+link)
        if not REPORTISH.search(blob): continue
        out.append({"wba_id":wba, "company_name":name, "title":(res.get("title") or "")[:150],
                    "url":link, "snippet":(res.get("snippet") or "")[:500],
                    "google_date":(res.get("date") or ""), "found":TODAY, "status":"new"})
    return out

def get_companies():
    url=f"{API}/{BASE}/{quote(TABLE)}"; params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=requests.get(url,headers=HEADERS,params=params,timeout=30); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    name={}; dom=defaultdict(Counter)
    for r in out:
        f=r.get("fields",{}); wba=f.get("wba_id")
        if not wba: continue
        if f.get("company_name"): name[wba]=f["company_name"]
        d=reg_domain(f.get("url",""))
        if d: dom[wba][d]+=1
    return [(wba, name.get(wba,""), dom[wba].most_common(1)[0][0] if dom[wba] else "") for wba in name]

def existing_urls():
    url=f"{API}/{BASE}/{quote(RTABLE)}"; params={"pageSize":100}; seen=set(); offset=None
    while True:
        if offset: params["offset"]=offset
        try:
            r=requests.get(url,headers=HEADERS,params=params,timeout=30); r.raise_for_status()
        except Exception:
            return seen   # table empty / new
        j=r.json()
        for rec in j.get("records",[]): seen.add(norm(rec.get("fields",{}).get("url","")))
        offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return seen

def post(records):
    url=f"{API}/{BASE}/{quote(RTABLE)}"
    for i in range(0,len(records),10):
        r=requests.post(url,headers={**HEADERS,"Content-Type":"application/json"},
                        json={"records":[{"fields":x} for x in records[i:i+10]],"typecast":True},timeout=30)
        r.raise_for_status(); time.sleep(0.25)

def main():
    companies=get_companies(); seen=existing_urls()
    print(f"{len(companies)} companies; searching Serper (already have {len(seen)} candidates) ...")
    rows=[]; done=0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(search_company, w, n, d) for w,n,d in companies]
        for fut in as_completed(futs):
            for cand in fut.result():
                k=norm(cand["url"])
                if k and k not in seen:
                    seen.add(k); rows.append(cand)
            done+=1
            if done%100==0: print(f"  {done}/{len(companies)}, {len(rows)} new candidates")
    print(f"New candidates: {len(rows)}. Writing to '{RTABLE}' ...")
    post(rows)
    print("Done. Add the pub_date AI field and filter to 2026 publication dates.")

if __name__=="__main__":
    main()
