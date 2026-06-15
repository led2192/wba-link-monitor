#!/usr/bin/env python3
"""
classify_detections.py — turn raw detections into answerable events.

A detection is just "a new link appeared on a watched page". That is not the same as
"the company published a new sustainability report". This job reads the detections table
and fills three fields so you can filter detections into real events:

  doc_type   what kind of document the link is, from its URL + title:
             sustainability_report / annual_report / policy / financial_report /
             press_release / other
  doc_year   the year the document refers to (prefers the year in the title over the URL,
             because URLs often carry version/timestamp years that are not the doc's year)
  recent     checkbox: doc_year is this year or last year (the current reporting cycle)

Then your questions are saved views, e.g.
  "new sustainability report"  -> doc_type=sustainability_report AND recent AND status=new
  "new policy"                 -> doc_type=policy AND status=new

It only classifies rows whose doc_type is still empty, so it is incremental and safe to run
daily (first run backfills everything, later runs handle just the new detections).

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_DETECTIONS_TABLE (default: detections)
Add these fields to the detections table first:
  doc_type  -> Single select   doc_year -> Single line text   recent -> Checkbox
"""
import os, re, sys, time, datetime as dt
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

API   = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

CUR = dt.date.today().year
RECENT_SINCE = CUR - 1            # this year or last year = current reporting cycle
F_URL="document_url"; F_TITLE="title"; F_TYPE="doc_type"; F_YEAR="doc_year"; F_RECENT="recent"

YEAR = re.compile(r"(20[12]\d)")
STRONG_PRESS = re.compile(r"appoint|announce|\bnames?\b|awarded|\bwins?\b|launch|partners?.with|"
                          r"to.acquire|completes|joins|welcomes|celebrat", re.I)
RULES = [("sustainability_report", re.compile(r"sustainab|\besg\b|\bcsr\b|environment|climat|responsib|"
                                              r"\bimpact\b|carbon|\btcfd\b|\bghg\b|net.?zero|emission", re.I)),
         ("annual_report",         re.compile(r"annual.?report|integrated.?report|jahresbericht|"
                                              r"rapport.?annuel|informe.?anual|\bannual\b|\b10-?k\b|\b20-?f\b", re.I)),
         ("policy",                re.compile(r"policy|policies|code.?of.?conduct|governance|\bethic|"
                                              r"charter|compliance|modern.?slavery|human.?rights|\bcodigo\b", re.I)),
         ("financial_report",      re.compile(r"financ|\bresults?\b|quarterly|half.?year|interim|"
                                              r"\bq[1-4]\b|earnings|trading.?statement", re.I))]

def year_of(title, url):
    for blob in (title, url):                 # title first; URLs carry timestamp noise
        ys=[int(y) for y in YEAR.findall(blob or "") if 2010 <= int(y) <= CUR+1]
        if ys: return max(ys)
    return None

def classify(title, url):
    blob=f"{title} {url}"
    if STRONG_PRESS.search(blob): return "press_release"
    for t,rx in RULES:
        if rx.search(blob): return t
    if re.search(r"press|news|media|release|/blog", blob, re.I): return "press_release"
    return "other"

def get_unclassified():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=requests.get(url,headers=HEADERS,params=params,timeout=30); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return [r for r in out if not (r.get("fields",{}).get(F_TYPE))]

def patch(updates):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(updates),10):
        r=requests.patch(url,headers={**HEADERS,"Content-Type":"application/json"},
                         json={"records":updates[i:i+10],"typecast":True},timeout=30)
        r.raise_for_status(); time.sleep(0.25)

def main():
    recs=get_unclassified()
    print(f"{len(recs)} unclassified detections.")
    updates=[]; hi=0
    import collections; bytype=collections.Counter()
    for r in recs:
        f=r.get("fields",{}); title=f.get(F_TITLE,""); url=f.get(F_URL,"")
        typ=classify(title,url); yr=year_of(title,url)
        recent=bool(yr and yr>=RECENT_SINCE)
        bytype[typ]+=1
        if recent and typ in ("sustainability_report","annual_report","policy"): hi+=1
        fields={F_TYPE:typ, F_RECENT:recent}
        if yr: fields[F_YEAR]=str(yr)
        updates.append({"id":r["id"],"fields":fields})
    if not updates:
        print("Nothing to classify."); return
    patch(updates)
    print("doc_type:", dict(bytype.most_common()))
    print(f"High-signal (recent + sustainability/annual/policy): {hi}")
    print(f'Filter views on doc_type + recent + status to read real events.')

if __name__=="__main__":
    main()
