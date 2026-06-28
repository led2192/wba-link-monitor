#!/usr/bin/env python3
"""
sitemap_discovery.py — ask each company's site for its own index (sitemap.xml).

For every homepage row in monitored_links it tries to locate a sitemap (via robots.txt and
the common paths), follows a sitemap index to its children, and pulls the URLs that look
report/sustainability-related. It then:
  - sets on the homepage row:  has_sitemap (checkbox), sitemap_url (text),
    sitemap_report_urls (the report-ish URLs as text — NOT the whole XML, which is huge)
  - appends the new report-ish URLs to monitored_links (source=sitemap, deduped), so report
    PAGES get monitored and report PDFs get recorded.

A cleaner, cheaper discovery than crawling the homepage blind: the site hands you its index.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links)
Add to monitored_links first:
  has_sitemap -> Checkbox   sitemap_url -> Single line text   sitemap_report_urls -> Long text
"""
import os, re, sys, time, warnings, datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, Counter

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

F_LINK="link_name"; F_WBA="wba_id"; F_NAME="company_name"; F_URL="url"; F_TYPE="type"
F_MON="monitor"; F_FREQ="frequency"; F_PDF="is_pdf"; F_SRC="source"
F_HAS="has_sitemap"; F_SMURL="sitemap_url"; F_SMREP="sitemap_report_urls"

UA = "Mozilla/5.0 (compatible; WBA-LinkMonitor/1.0)"
WORKERS=10; MAX_CHILDREN=25; MAX_LOCS=20000; MAX_REPORT=60
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)
# Strict: a URL is report-ish only if it pairs a TOPIC with a DOC word, or is a policy doc.
# (Loose single tokens like "report"/"annual"/"cdp" swept in product pages, press releases,
#  logos and earnings PDFs. "cdp" especially matched Customer Data Platform marketing.)
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
RULES=[("sustainability_report", re.compile(r"sustainab|\besg\b|\bcsr\b|environment|climat|responsib|impact",re.I)),
       ("reports_hub",           re.compile(r"report|annual|integrated|financ|result|disclosur|publication|download",re.I)),
       ("policies",              re.compile(r"policy|policies|code.?of.?conduct|governance|ethic|compliance",re.I))]
FREQ={"sustainability_report":"mon_wed_fri","reports_hub":"mon_wed_fri","policies":"mon_fri","other":"mon_fri"}
LABEL={"sustainability_report":"Sustainability","reports_hub":"Reports","policies":"Policies","other":"Other"}

def reg_domain(u):
    e=_EXTRACT(u); return f"{e.domain}.{e.suffix}".lower() if e.suffix else (e.domain or "").lower()
def normalize(url):
    try: s=urlsplit(url)
    except Exception: return None,None
    if s.scheme not in ("http","https"): return None,None
    host=(s.hostname or "").lower(); disp=host
    if host.startswith("www."): host=host[4:]
    path=re.sub(r"/+$","",s.path)
    q=sorted((k,v) for k,v in parse_qsl(s.query) if not TRACKING.match(k))
    key=(host+path+("?"+urlencode(q) if q else "")).lower()
    return key, f"{s.scheme}://{disp}{path}"+("?"+urlencode(q) if q else "")
def classify(url):
    p=urlsplit(url).path.lower()
    for t,rx in RULES:
        if rx.search(p): return t
    return "other"
def fetch(session,url,timeout=20):
    try: return session.get(url,timeout=timeout,headers={"User-Agent":UA},allow_redirects=True)
    except Exception: return None

def parse_locs(content):
    try: root=ET.fromstring(content)
    except Exception: return [], False
    is_index = root.tag.lower().endswith("sitemapindex")
    out=[el.text.strip() for el in root.iter() if el.tag.lower().endswith("loc") and el.text]
    return out, is_index

def find_sitemap(session, homepage):
    s=urlsplit(homepage); root=f"{s.scheme}://{s.hostname}"
    candidates=[]
    rb=fetch(session, root+"/robots.txt")
    if rb is not None and rb.status_code<400:
        candidates += [l.split(":",1)[1].strip() for l in rb.text.splitlines() if l.lower().startswith("sitemap:")]
    candidates += [root+"/sitemap.xml", root+"/sitemap_index.xml", root+"/sitemap-index.xml"]
    for sm in candidates:
        r=fetch(session, sm)
        if r is None or r.status_code>=400 or not r.content: continue
        locs, is_index = parse_locs(r.content)
        if not locs: continue
        all_locs=[]
        if is_index:
            for child in locs[:MAX_CHILDREN]:
                cr=fetch(session, child)
                if cr is not None and cr.status_code<400 and cr.content:
                    ll,_=parse_locs(cr.content); all_locs += ll
                if len(all_locs)>=MAX_LOCS: break
        else:
            all_locs=locs
        return sm, all_locs[:MAX_LOCS]
    return None, []

def work(rec):
    f=rec.get("fields",{}); hp=f.get(F_URL,""); rdom=reg_domain(hp)
    sess=requests.Session()
    sm_url, locs = find_sitemap(sess, hp)
    if not sm_url:
        return rec["id"], {F_HAS:False}, []
    reps=[]
    seen=set()
    for u in locs:
        if reg_domain(u)!=rdom: continue
        if not report_ish(urlsplit(u).path): continue
        k,clean=normalize(u)
        if not k or k in seen: continue
        seen.add(k); reps.append(clean)
        if len(reps)>=MAX_REPORT: break
    upd={F_HAS:True, F_SMURL:sm_url[:255], F_SMREP:"\n".join(reps)[:90000]}
    return rec["id"], upd, [(f.get(F_WBA,""), f.get(F_NAME,""), r) for r in reps]

def get_homepages_and_keys():
    url=f"{API}/{BASE}/{quote(TABLE)}"; params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=airtable_request("GET", url, HEADERS, params=params); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    have=defaultdict(set)
    for r in out:
        k,_=normalize(r.get("fields",{}).get(F_URL,""))
        if k: have[r.get("fields",{}).get(F_WBA,"")].add(k)
    homepages=[r for r in out if r.get("fields",{}).get(F_TYPE)=="homepage"]
    return homepages, have

def patch(updates):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(updates),10):
        r=airtable_request("PATCH", url, {**HEADERS,"Content-Type":"application/json"}, {"records":updates[i:i+10],"typecast":True}); r.raise_for_status(); time.sleep(0.25)
def post(records):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(records),10):
        r=airtable_request("POST", url, {**HEADERS,"Content-Type":"application/json"}, {"records":records[i:i+10],"typecast":True}); r.raise_for_status(); time.sleep(0.25)

def main():
    homepages, have = get_homepages_and_keys()
    print(f"{len(homepages)} homepages to check for sitemap.xml ...")
    updates=[]; new_rows=[]; found=0; done=0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(work,r) for r in homepages]
        for fut in as_completed(futs):
            rid, upd, reps = fut.result()
            if upd.get(F_HAS): found+=1
            updates.append({"id":rid,"fields":upd})
            for wba,name,u in reps:
                k,_=normalize(u)
                if k in have[wba]: continue
                have[wba].add(k)
                typ=classify(u); is_pdf=urlsplit(u).path.lower().endswith(".pdf")
                new_rows.append({"fields":{F_LINK:f"{name} \u2014 {LABEL.get(typ,'Other')}", F_WBA:wba, F_NAME:name,
                    F_URL:u, F_TYPE:typ, F_MON:(not is_pdf), F_FREQ:FREQ.get(typ,"mon_fri"),
                    F_PDF:is_pdf, F_SRC:"sitemap"}})
            done+=1
            if done%100==0: print(f"  {done}/{len(homepages)}")
    print(f"Sitemaps found: {found}/{len(homepages)}. New report URLs to add: {len(new_rows)}.")
    print("Writing sitemap fields ..."); patch(updates)
    print("Adding discovered report URLs ..."); post(new_rows)
    print("Done.")

if __name__=="__main__":
    main()
