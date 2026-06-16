#!/usr/bin/env python3
"""
playwright_monitor.py — WEEKLY browser-based monitor for the hard pages.

Targets the rows that plain requests can't read: status dead or error (mostly 403/429
bot-blocks, timeouts) plus every row already marked needs_browser. Renders each one in a
real headless Chromium, which passes most bot challenges and executes the JavaScript that
builds reports lists (your year-dropdown case: the default view, i.e. the newest year,
gets read).

What it writes back (same contract as the daily monitor):
  status / http_status / final_url / last_checked / content_hash
  seen_links / new_links / last_change / alert_status="new" on detection
And one thing of its own:
  needs_browser = True on every page it successfully reads. From then on the DAILY
  requests monitor skips that row (it excludes needs_browser) and this weekly job owns it.
  Pages still >=400 in a real browser keep their dead status (true 404s stay dead).

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_TABLE (default monitored_links)
Airtable: add a checkbox field  needs_browser  before running.

Setup on GitHub Actions (see playwright.yml):
  pip install -r requirements.txt playwright
  playwright install --with-deps chromium
"""
import os, re, sys, time, hashlib, random, threading, warnings, datetime as dt
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
    import tldextract
except ImportError:
    sys.exit("pip install requests beautifulsoup4 tldextract")
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:
    pass
from ids import link_id, detection_id   # deterministic ids: detection -> source link
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("pip install playwright  &&  playwright install --with-deps chromium")

API   = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
DETECTIONS_TABLE = os.environ.get("AIRTABLE_DETECTIONS_TABLE", "detections")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

F_WBA="wba_id"; F_NAME="company_name"; F_URL="url"; F_TYPE="type"; F_STATUS="status"; F_HTTP="http_status"; F_FINAL="final_url"
F_CHECKED="last_checked"; F_HASH="content_hash"; F_SEEN="seen_links"
F_NEW="new_links"; F_CHANGE="last_change"; F_ALERT="alert_status"; F_BROWSER="needs_browser"

TODAY = dt.date.today()
WORKERS = 3              # one Chromium per worker thread; runner has 4 vCPU / 16 GB
NAV_TIMEOUT_MS = 15000
SETTLE_MS = 1500
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)
DOCISH   = re.compile(r"report|annual|sustainab|esg|/download|/publication|/disclosur", re.I)

def reg_domain(u):
    e=_EXTRACT(u); return f"{e.domain}.{e.suffix}".lower() if e.suffix else (e.domain or "").lower()

def normalize(url):
    try: s=urlsplit(url)
    except Exception: return None
    if s.scheme not in ("http","https"): return None
    host=(s.hostname or "").lower()
    if host.startswith("www."): host=host[4:]
    path=re.sub(r"/+$","",s.path)
    q=sorted((k,v) for k,v in parse_qsl(s.query) if not TRACKING.match(k))
    return (host+path+("?"+urlencode(q) if q else "")).lower()

def doc_links(html, base):
    """Same-domain links that are PDFs or whose URL PATH looks report-ish.
    Returns {normalized_url: anchor_text}. Anchor text is annotation only;
    it no longer triggers a match (anchor-based matching produced noise)."""
    rdom=reg_domain(base); out={}
    for a in BeautifulSoup(html,"html.parser").find_all("a",href=True):
        href=a["href"].strip()
        if href.startswith(("#","mailto:","tel:","javascript:")): continue
        absu=urljoin(base,href)
        if reg_domain(absu)!=rdom: continue
        path=urlsplit(absu).path.lower()
        if path.endswith(".pdf") or DOCISH.search(path):
            n=normalize(absu)
            if n and n not in out:
                out[n]=(absu, a.get_text(" ",strip=True)[:80])
    return out

def get_targets():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    formula=("AND({monitor}=TRUE(), OR({status}='dead', {status}='error', {%s}=TRUE()))" % F_BROWSER)
    params={"pageSize":100, "filterByFormula":formula}
    out=[]; offset=None
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

_tl = threading.local()
def get_page():
    """One Playwright+Chromium per worker thread, reused across its pages."""
    if not hasattr(_tl, "ctx"):
        _tl.pw = sync_playwright().start()
        _tl.browser = _tl.pw.chromium.launch(headless=True)
        _tl.ctx = _tl.browser.new_context(user_agent=UA, viewport={"width":1366,"height":900})
        _tl.ctx.route(re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|mp4|webm)(\?|$)", re.I),
                      lambda route: route.abort())
    return _tl.ctx

def detection_fields(f, upd, current, had_baseline):
    """Shared bookkeeping: diff vs seen, annotate with anchor text, append history,
    gate the alert. Returns (changed, high_signal)."""
    cur=set(current)
    upd[F_HASH]=hashlib.md5("\n".join(sorted(cur)).encode()).hexdigest()
    seen=set((f.get(F_SEEN) or "").split("\n")) - {""}
    new=sorted(cur - seen)
    upd[F_SEEN]="\n".join(sorted(cur))[:90000]
    if not (had_baseline and new):
        return False, False, []
    entries=[]; docs=[]
    for n in new:
        disp, t = current.get(n, ("",""))
        t=(t or "").strip()
        entries.append(n + (f"  [{t[:70]}]" if t else ""))
        _doc = disp or ("https://"+n); _page = f.get(F_URL,""); _wba = f.get(F_WBA,"")
        docs.append({"detected":TODAY.isoformat(), "wba_id":_wba,
                     "company_name":f.get(F_NAME,""), "document_url":_doc,
                     "title":t, "found_on":_page, "page_type":f.get(F_TYPE,""),
                     "is_pdf":n.endswith(".pdf"), "status":"new",
                     "source_link_id":link_id(_wba,_page),
                     "detection_id":detection_id(_wba,_page,_doc)})
    line=f"{TODAY.isoformat()}: " + " ; ".join(entries)
    old=(f.get(F_NEW) or "").strip()
    upd[F_NEW]=(line + ("\n"+old if old else ""))[:90000]
    upd[F_CHANGE]=TODAY.isoformat()
    high = any(n.endswith(".pdf") for n in new) or (f.get(F_TYPE) in ("reports_hub","sustainability_page"))
    if high: upd[F_ALERT]="new"
    return True, high, docs

def process(rec):
    f=rec.get("fields",{}); u=f.get(F_URL,"")
    upd={F_CHECKED:TODAY.isoformat()}
    page=None
    try:
        page=get_page().new_page()
        resp=page.goto(u, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_MS)
        code=resp.status if resp else None
        if code and code>=400:
            upd[F_STATUS]="dead"; upd[F_HTTP]=str(code)
            return rec["id"], upd, False, False, [], False
        final=page.url
        upd[F_HTTP]=str(code or "")
        upd[F_FINAL]=final
        upd[F_STATUS]="redirected" if normalize(final)!=normalize(u) else "ok"
        current=doc_links(page.content(), final)
        had_baseline=bool(f.get(F_HASH))          # content_hash present = visited before
        changed, high, docs = detection_fields(f, upd, current, had_baseline)
        upd[F_BROWSER]=True                        # browser owns this page from now on
        return rec["id"], upd, changed, high, docs, True
    except Exception:
        upd[F_STATUS]="error"
        return rec["id"], upd, False, False, [], False
    finally:
        if page:
            try: page.close()
            except Exception: pass

def post_detections(rows):
    """One row per detected document, into the detections table. Non-fatal if missing."""
    if not rows: return
    url=f"{API}/{BASE}/{quote(DETECTIONS_TABLE)}"
    try:
        for i in range(0,len(rows),10):
            r=requests.post(url,headers={**HEADERS,"Content-Type":"application/json"},
                            json={"records":[{"fields":x} for x in rows[i:i+10]],"typecast":True},timeout=30)
            r.raise_for_status(); time.sleep(0.25)
        print(f"Logged {len(rows)} detections to '{DETECTIONS_TABLE}'.")
    except Exception as e:
        print(f"WARNING: could not write detections to '{DETECTIONS_TABLE}' ({e}). Create that table in Airtable.")

def main():
    recs=get_targets()
    random.shuffle(recs)   # avoid hammering one domain in a burst (the 429s)
    print(f"{len(recs)} hard pages to render with a real browser ({WORKERS} workers).")
    updates=[]; rescued=changed=still_dead=0; done=0
    alerted=0; detections=[]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(process,r) for r in recs]
        for fut in as_completed(futs):
            rid, upd, ch, high, docs, ok = fut.result()
            if ok: rescued+=1
            elif upd.get(F_STATUS) in ("dead","error"): still_dead+=1
            if ch: changed+=1
            if high: alerted+=1
            detections.extend(docs)
            updates.append({"id":rid,"fields":upd}); done+=1
            if done%100==0: print(f"  {done}/{len(recs)}")
    print(f"Writing {len(updates)} updates back to Airtable ...")
    patch(updates)
    post_detections(detections)
    print(f"Done. Readable in a real browser: {rescued}.  Still dead/error: {still_dead}.  "
          f"Pages with new doc links: {changed} (high-signal alerts: {alerted}).")
    print("Rescued pages are now marked needs_browser and owned by this weekly job.")

if __name__=="__main__":
    main()
