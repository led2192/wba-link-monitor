#!/usr/bin/env python3
"""
retag_homepages.py — ONE-OFF. Sets type="homepage" on every row whose URL is a company's
bare main domain (no path, e.g. https://www.acme.com) but that is currently tagged
something else (other / investor_relations / news / ...). Also sets their frequency to
mon_wed_fri so they line up with the homepages you imported.

A "homepage URL" = host is the registered domain (with or without www) AND there is no path.
  https://www.acme.com/        -> yes
  https://acme.com             -> yes
  https://investors.acme.com   -> no  (subdomain, e.g. IR)
  https://www.acme.com/news    -> no  (has a path)

It only changes `type` and `frequency`. It never deletes anything and never touches rows
that have a path (real section pages). Run it ONCE.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links).
"""
import os, re, sys, time
from urllib.parse import urlsplit, quote

try:
    import requests, tldextract
except ImportError:
    sys.exit("pip install requests tldextract")

API   = "https://api.airtable.com/v0"
from monitor_core import airtable_request
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

F_URL="url"; F_TYPE="type"; F_FREQ="frequency"
NEW_TYPE="homepage"; NEW_FREQ="mon_wed_fri"
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

def reg_domain(u):
    e=_EXTRACT(u); return f"{e.domain}.{e.suffix}".lower() if e.suffix else (e.domain or "").lower()

def is_homepage(u):
    try: s=urlsplit(u)
    except Exception: return False
    if s.scheme not in ("http","https"): return False
    host=(s.hostname or "").lower()
    h=host[4:] if host.startswith("www.") else host
    path=re.sub(r"/+$","",s.path)
    return h==reg_domain(u) and path==""

def get_all():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=airtable_request("GET", url, HEADERS, params=params); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out

def patch(updates):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(updates),10):
        r=airtable_request("PATCH", url, {**HEADERS,"Content-Type":"application/json"}, {"records":updates[i:i+10],"typecast":True})
        r.raise_for_status(); time.sleep(0.25)

def main():
    recs=get_all()
    updates=[]
    for r in recs:
        f=r.get("fields",{}); u=f.get(F_URL,"")
        if u and f.get(F_TYPE)!=NEW_TYPE and is_homepage(u):
            updates.append({"id":r["id"], "fields":{F_TYPE:NEW_TYPE, F_FREQ:NEW_FREQ}})
    print(f"{len(recs)} rows read. {len(updates)} bare-domain homepages to re-tag.")
    for r in updates[:10]:
        print("   ->", r["fields"][F_TYPE], "|", next(x for x in recs if x['id']==r['id'])['fields'].get(F_URL))
    if not updates:
        print("Nothing to change."); return
    patch(updates)
    print(f"Done. Re-tagged {len(updates)} rows to type=homepage, frequency=mon_wed_fri.")

if __name__=="__main__":
    main()
