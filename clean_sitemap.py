#!/usr/bin/env python3
"""
clean_sitemap.py — delete the junk that the first (too-loose) sitemap run added.

The original sitemap filter matched single tokens and every .pdf, so it swept in product
pages (Salesforce "CDP" = Customer Data Platform), press releases, earnings PDFs, forms and
even logo images. This deletes every source=sitemap row whose URL does NOT pass the strict
report-ish test (topic word + document word, or a policy doc; assets excluded). Rows that ARE
real reports/policies are kept, so you do NOT need to re-crawl. Only touches source=sitemap.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links),
     DRY_RUN (true = only count, no deletes)
"""
import os, re, sys, time
from urllib.parse import quote, urlsplit

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

API   = "https://api.airtable.com/v0"
from monitor_core import airtable_request
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

_TOPIC  = re.compile(r"sustainab|esg|csr|climate|environment|carbon|emission|net.?zero|responsib|"
                     r"non.?financial|materiality|tcfd|decarboni|biodiversit|ghg|integrated|annual|stewardship", re.I)
_DOCW   = re.compile(r"report|statement|review|disclosure|memoria|rapport|bericht|informe", re.I)
_POLICY = re.compile(r"code.?of.?conduct|policy|policies", re.I)
_ASSET  = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp4|mov|avi|zip|json|rss)$", re.I)
def report_ish(path):
    if _ASSET.search(path): return False
    if _TOPIC.search(path) and _DOCW.search(path): return True
    if _POLICY.search(path): return True
    return False

def get_all():
    url=f"{API}/{BASE}/{quote(TABLE)}"; params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=airtable_request("GET", url, HEADERS, params=params); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out

def delete(ids):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(ids),10):
        params=[("records[]",x) for x in ids[i:i+10]]
        r=airtable_request("DELETE", url, HEADERS, params=params); r.raise_for_status(); time.sleep(0.25)
        if (i//10)%50==0: print(f"  deleted {min(i+10,len(ids))}/{len(ids)}")

def main():
    rows=get_all()
    sm=[r for r in rows if r.get("fields",{}).get("source")=="sitemap"]
    junk=[r["id"] for r in sm if not report_ish(urlsplit(r.get("fields",{}).get("url","")).path.lower())]
    keep=len(sm)-len(junk)
    print(f"source=sitemap rows: {len(sm)}")
    print(f"   keep (real reports/policies): {keep}")
    print(f"   DELETE (junk): {len(junk)}")
    if DRY_RUN:
        print("DRY_RUN: nothing deleted."); return
    delete(junk)
    print("Done.")

if __name__=="__main__":
    main()
