#!/usr/bin/env python3
"""
unmonitor_shared.py — stop monitoring third-party URLs shared across many companies.

The original build swept up footer/membership links (sciencebasedtargets.org, weps.org,
github.com, sec.gov/edgar/search, ...) and attached them to every company that linked them.
A URL that appears under many DIFFERENT companies is not any single company's report; it is a
shared third-party resource. Monitoring it once per company is wasteful, hammers that site,
and fires a fake "big bang" of detections for hundreds of companies whenever it changes.

This sets monitor=false on every row whose URL appears under >= THRESHOLD distinct companies.
It is REVERSIBLE (it only flips the checkbox; nothing is deleted). Run once.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links),
     THRESHOLD (default 3), DRY_RUN (true = only print, no writes)
"""
import os, re, sys, time, collections
from urllib.parse import quote, urlsplit, parse_qsl, urlencode

try:
    import requests, tldextract
except ImportError:
    sys.exit("pip install requests tldextract")

API   = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
THRESHOLD = int(os.environ.get("THRESHOLD", "3"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

F_URL="url"; F_WBA="wba_id"; F_MON="monitor"
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)

def normalize(url):
    try: s=urlsplit(url)
    except Exception: return None
    if s.scheme not in ("http","https"): return None
    host=(s.hostname or "").lower()
    if host.startswith("www."): host=host[4:]
    path=re.sub(r"/+$","",s.path)
    q=sorted((k,v) for k,v in parse_qsl(s.query) if not TRACKING.match(k))
    return (host+path+("?"+urlencode(q) if q else "")).lower()

def truthy(v): return str(v).strip().lower() in ("checked","true","1","yes")

def get_all():
    url=f"{API}/{BASE}/{quote(TABLE)}"; params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=requests.get(url,headers=HEADERS,params=params,timeout=30); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out

def patch(updates):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(updates),10):
        r=requests.patch(url,headers={**HEADERS,"Content-Type":"application/json"},
                         json={"records":updates[i:i+10],"typecast":True},timeout=30)
        r.raise_for_status(); time.sleep(0.25)

def main():
    rows=get_all()
    companies=collections.defaultdict(set)
    for r in rows:
        k=normalize(r.get("fields",{}).get(F_URL,""))
        if k: companies[k].add(r.get("fields",{}).get(F_WBA,""))
    shared={k for k,c in companies.items() if len(c)>=THRESHOLD}
    updates=[]
    for r in rows:
        f=r.get("fields",{}); k=normalize(f.get(F_URL,""))
        if k in shared and truthy(f.get(F_MON)):
            updates.append({"id":r["id"],"fields":{F_MON:False}})
    print(f"Shared URLs (>= {THRESHOLD} distinct companies): {len(shared)}")
    print(f"Monitored rows to switch off: {len(updates)}")
    if DRY_RUN:
        print("DRY_RUN: no changes written."); return
    patch(updates)
    print("Done. Set monitor=true again on any you want back.")

if __name__=="__main__":
    main()
