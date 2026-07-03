#!/usr/bin/env python3
"""
active_search.py — MONTHLY active search (the breadth / safety-net job).

For each company it takes the homepage, crawls ONE hop in (a bounded set of internal
section pages), and ADDS to the Monitored Links table any page it discovers that either
(a) classifies into one of your types, or (b) links to at least one document (PDF / report).
Those new pages then get watched by the daily monitor, which is what actually detects new
documents on them. So nothing reachable from the homepage slips through.

It does NOT diff documents itself and it does NOT touch existing rows — it only APPENDS new
rows, with the same de-dup, so no duplicates with what you already have. New pages are added
with monitor=TRUE and no alert (a new *page* is not a new report; the monitor raises the
alert later when a document appears on it).

Runs anywhere with internet (built for GitHub Actions). Heavy job: ~1-2 h for 2,000 sites.

Required env:
  AIRTABLE_TOKEN   (GitHub secret)
  AIRTABLE_BASE    appXXXXXXXXXXXXXX
  AIRTABLE_TABLE   table name (default: monitored_links)
"""
import os, re, sys, time, datetime as dt
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict

import warnings
try:
    import requests
    from bs4 import BeautifulSoup
    import tldextract
except ImportError:
    sys.exit("pip install requests beautifulsoup4 tldextract")
try:  # silence the "looks like XML" noise from RSS feeds / sitemaps
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:
    pass

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

UA = "Mozilla/5.0 (compatible; WBA-LinkMonitor/1.0)"
TODAY = dt.date.today()
MAX_CRAWL = 20          # internal pages fetched per company homepage
MAX_NEW_PER_COMPANY = 10  # cap new monitored pages added per company per sweep
WORKERS = 15

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)
DOCISH   = re.compile(r"report|annual|sustainab|esg|/download|/publication|/disclosur", re.I)
JUNK     = re.compile(r"login|signin|sign-in|account|cart|basket|checkout|privacy|cookie|"
                      r"terms|legal-notice|sitemap|search|subscribe|newsletter|/wp-|\.(jpg|png|gif|svg|css|js)$", re.I)
SECTION  = re.compile(r"sustainab|esg|environment|csr|responsib|climate|impact|report|annual|"
                      r"financ|result|filing|investor|shareholder|news|press|media|about|"
                      r"governance|policy|policies|ethics|compliance|publication|download|disclosur", re.I)
# Rule order = priority: the FIRST matching rule wins, so the ESG rule outranks the generic
# report/IR words (a /diversity-report/ page is sustainability_page, not merely reports_hub).
# Terms beyond the original set were validated against the live "other" bucket (Jul 2026): each
# was checked for pull and for business-line false positives before inclusion. Deliberately NOT
# added: leadership/board/director (executive-bio pages), target/strategy (third-party SBTi
# dashboards), bare carbon (hydrocarbon), energy/water/waste (business lines for utilities and
# logistics), bare people (mixed with careers), announcement (ASX/RNS pages need their own call),
# privacy/legal/terms (boilerplate).
RULES=[("sustainability_page", re.compile(
            r"sustainab|esg|environment|csr|responsib|climate|impact|"
            r"sourcing|supplier|procure|supply.?chain|divers|inclusion|gender|"
            r"human.?right|modern.?slavery|forced.?labo|trafficking|"
            r"decarbon|net.?zero|emission|biodivers|circular.?econom|"
            r"health.?safety|\behs\b|wellbeing|community|philanthrop", re.I)),
       ("reports_hub",         re.compile(
            r"report|annual|financ|result|filing|disclosur|publication|download|"
            r"presentations|interim|half.?year|factbook|databook|/documents?(/|$)|librar", re.I)),
       ("investor_relations",  re.compile(
            r"investor|shareholder|/ir/|/ir$|stock|equity|"
            r"\bagm\b|general.?meeting|prospectus|dividend", re.I)),
       ("news",                re.compile(r"news|press|media|/article|story|release|/blog",re.I)),
       ("policies",            re.compile(
            r"policy|policies|code-of-conduct|governance|ethics|compliance|"
            r"whistleblow|anti.?corruption|anti.?bribery|conflict.?of.?interest|"
            r"remuneration|charter|\btax\b", re.I))]
FREQ={"news":"daily","reports_hub":"mon_wed_fri","sustainability_page":"mon_wed_fri",
      "investor_relations":"mon_wed_fri","policies":"mon_fri","other":"mon_fri"}
LABEL={"sustainability_page":"Sustainability","reports_hub":"Reports","news":"News",
       "investor_relations":"Investor Relations","policies":"Policies","other":"Other","homepage":"Homepage"}

def reg_domain(u):
    e=_EXTRACT(u); return f"{e.domain}.{e.suffix}".lower() if e.suffix else (e.domain or "").lower()

def normalize(url):
    try: s=urlsplit(url)
    except Exception: return None,None
    if s.scheme not in ("http","https"): return None,None
    host=(s.hostname or "").lower()
    disp_host=host
    if host.startswith("www."): host=host[4:]
    path=re.sub(r"/+$","",s.path)
    q=sorted((k,v) for k,v in parse_qsl(s.query) if not TRACKING.match(k))
    key=(host+path+("?"+urlencode(q) if q else "")).lower()
    clean=f"{s.scheme}://{disp_host}{path}"+("?"+urlencode(q) if q else "")
    return key, clean

def classify(url, text=""):
    blob=(urlsplit(url).path+" "+text).lower()
    for typ,rx in RULES:
        if rx.search(blob): return typ
    return "other"

def fetch(session, url, timeout=20):
    try:
        return session.get(url, allow_redirects=True, timeout=timeout, headers={"User-Agent":UA})
    except requests.exceptions.SSLError:
        try:
            import urllib3; urllib3.disable_warnings()
            return session.get(url, allow_redirects=True, timeout=timeout, headers={"User-Agent":UA}, verify=False)
        except Exception: return None
    except Exception: return None

def links_on(html, base):
    """Return [(clean_url, anchor_text)] for same-domain, non-junk links."""
    rdom=reg_domain(base); out=[]
    for a in BeautifulSoup(html,"html.parser").find_all("a",href=True):
        href=a["href"].strip()
        if href.startswith(("#","mailto:","tel:","javascript:")): continue
        absu=urljoin(base,href)
        if reg_domain(absu)!=rdom: continue
        if JUNK.search(absu): continue
        key,clean=normalize(absu)
        if not key: continue
        out.append((clean, a.get_text(" ",strip=True)[:80]))
    return out

def get_all_records():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=airtable_request("GET", url, HEADERS, params=params); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    return out

def post(records):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(records),10):
        r=airtable_request("POST", url, {**HEADERS,"Content-Type":"application/json"}, {"records":records[i:i+10],"typecast":True})
        r.raise_for_status(); time.sleep(0.25)

def crawl_company(sess, wba, name, homepage, have_keys):
    """Crawl homepage one hop in; return list of new page rows to add."""
    r=fetch(sess, homepage)
    if r is None or r.status_code>=400:
        return []
    base=r.url
    internal=links_on(r.text, base)
    # rank: section-like links first, cap the crawl
    internal.sort(key=lambda lt: 0 if SECTION.search(lt[0]) else 1)
    seen_local=set(); to_crawl=[]
    for clean,txt in internal:
        k,_=normalize(clean)
        if k in seen_local: continue
        seen_local.add(k); to_crawl.append((clean,txt))
        if len(to_crawl)>=MAX_CRAWL: break

    found=[]  # (clean, type) candidates to add
    per_type=Counter()
    def consider(clean, txt, has_doc):
        k,_=normalize(clean)
        if not k or k in have_keys: return
        typ=classify(clean, txt)
        if typ=="other" and not has_doc:   # only add 'other' pages if they bear a document
            return
        if per_type[typ]>=2: return
        have_keys.add(k); per_type[typ]+=1
        found.append((clean, typ))

    for clean,txt in to_crawl:
        if len(found)>=MAX_NEW_PER_COMPANY: break
        pr=fetch(sess, clean)
        if pr is None or pr.status_code>=400: continue
        docs=False
        for lk,_ in links_on(pr.text, pr.url):
            p=urlsplit(lk).path.lower()
            if p.endswith(".pdf") or DOCISH.search(p): docs=True; break
        consider(pr.url, txt, docs)

    rows=[]
    for clean,typ in found[:MAX_NEW_PER_COMPANY]:
        rows.append({"fields":{
            F_LINK:f"{name} \u2014 {LABEL.get(typ,'Other')}", F_WBA:wba, F_NAME:name,
            F_URL:clean, F_TYPE:typ, F_MON:True, F_FREQ:FREQ.get(typ,"mon_fri"),
            F_PDF:False, F_SRC:"sweep"}})
    return rows

def main():
    recs=get_all_records()
    # per company: existing url keys (dedup) + homepage url + name
    have=defaultdict(set); homepage={}; name={}; domains=defaultdict(Counter)
    for r in recs:
        f=r.get("fields",{}); wba=f.get(F_WBA); u=f.get(F_URL,"")
        if not wba: continue
        k,_=normalize(u)
        if k: have[wba].add(k)
        if f.get(F_NAME): name[wba]=f[F_NAME]
        if f.get(F_TYPE)=="homepage" and u: homepage[wba]=u
        d=reg_domain(u)
        if d: domains[wba][d]+=1
    companies=list(name.keys())
    for wba in companies:
        if wba not in homepage and domains[wba]:
            homepage[wba]="https://"+domains[wba].most_common(1)[0][0]  # reconstruct from domain
    print(f"{len(companies)} companies; crawling homepages (max {MAX_CRAWL} pages each) ...")

    sess=requests.Session(); new_rows=[]; done=0; companies_with_new=0
    def work(wba):
        hp=homepage.get(wba)
        if not hp: return []
        return crawl_company(sess, wba, name.get(wba,""), hp, have[wba])
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs={ex.submit(work,wba):wba for wba in companies}
        for fut in as_completed(futs):
            rows=fut.result()
            if rows: companies_with_new+=1; new_rows.extend(rows)
            done+=1
            if done%100==0: print(f"  {done}/{len(companies)} companies, {len(new_rows)} new pages so far")

    print(f"\nDiscovered {len(new_rows)} new pages across {companies_with_new} companies.")
    print("Writing them to Airtable ...")
    post(new_rows)
    print("Done. New pages added with monitor=TRUE; the daily monitor will start watching them.")

if __name__=="__main__":
    main()
