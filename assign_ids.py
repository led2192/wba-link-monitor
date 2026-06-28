#!/usr/bin/env python3
"""
assign_ids.py — backfill the join keys on existing rows (idempotent; run once, re-run anytime).

monitored_links:  link_id        = link_id(wba_id, url)
detections:       detection_id   = detection_id(wba_id, found_on, document_url)
                  source_link_id = link_id(wba_id, found_on)   == the source row's link_id

Only fills rows where the id field is empty, so it is safe to re-run after active_search /
sitemap_discovery add new monitored_links rows (set IDS_FORCE=true to recompute everything).

Add the fields first:
  monitored_links -> link_id (Single line text)
  detections      -> detection_id (Single line text), source_link_id (Single line text)

Env: AIRTABLE_TOKEN, AIRTABLE_BASE,
     AIRTABLE_TABLE (default monitored_links), AIRTABLE_DETECTIONS_TABLE (default detections),
     IDS_FORCE (true = recompute all)
"""
import os, sys, time
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("pip install requests")
from ids import link_id

API   = "https://api.airtable.com/v0"
from monitor_core import airtable_request
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
LINKS = os.environ.get("AIRTABLE_TABLE", "monitored_links")
FORCE = os.environ.get("IDS_FORCE", "").lower() in ("true", "1", "yes")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def get_all(table):
    url=f"{API}/{BASE}/{quote(table)}"; params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=airtable_request("GET", url, HEADERS, params=params); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out

def patch(table, updates):
    url=f"{API}/{BASE}/{quote(table)}"
    for i in range(0,len(updates),10):
        r=airtable_request("PATCH", url, {**HEADERS,"Content-Type":"application/json"}, {"records":updates[i:i+10],"typecast":True})
        r.raise_for_status(); time.sleep(0.25)

def backfill_links():
    rows=get_all(LINKS); ups=[]
    for r in rows:
        f=r.get("fields",{})
        if not FORCE and f.get("link_id"): continue
        ups.append({"id":r["id"],"fields":{"link_id":link_id(f.get("wba_id",""), f.get("url",""))}})
    print(f"monitored_links: {len(ups)} of {len(rows)} rows to set link_id.")
    patch(LINKS, ups)

def main():
    print(f"Backfilling ids{' [FORCE]' if FORCE else ''} ...")
    backfill_links()
    print("Done.")

if __name__=="__main__":
    main()
