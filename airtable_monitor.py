#!/usr/bin/env python3
"""
airtable_monitor.py — DAILY monitor. Keeps the Monitored Links alive and detects new reports.

Reads the links from Airtable, fetches each page that is DUE TODAY (by weekday, see below),
and in one request resolves both:
  - is the page still alive?   -> status / http_status / last_checked
  - did a NEW document appear? -> compares the report/PDF links on the page with what it saw
    last time; new links go to new_links + last_change, and alert_status is set to "new".

Due logic is by weekday, NOT "days since last check" (because last_checked is already filled
from the local run, which would otherwise suppress the first run):
  daily        -> every day
  mon_wed_fri  -> Mon / Wed / Fri
  mon_fri      -> Mon / Fri
  weekly       -> Mondays
  monthly      -> the 1st of the month
  quarterly    -> the 1st (legacy; reassign these to one of the above)
Set MONITOR_FORCE_ALL=true to check everything regardless of weekday (use once to baseline).

Required env:
  AIRTABLE_TOKEN   (GitHub secret) - PAT with data.records:read + data.records:write
  AIRTABLE_BASE    appXXXXXXXXXXXXXX
  AIRTABLE_TABLE   table name (default: monitored_links)
  MONITOR_FORCE_ALL  optional: "true" to ignore the weekday schedule

Fields used: url, frequency, monitor (checkbox), status, http_status, last_checked,
content_hash, seen_links, new_links, last_change, alert_status.
"""
import os, re, sys, time, hashlib, datetime as dt
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
    import tldextract
except ImportError:
    sys.exit("pip install requests beautifulsoup4 tldextract")

API   = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN")
BASE  = os.environ.get("AIRTABLE_BASE")
TABLE = os.environ.get("AIRTABLE_TABLE", "monitored_links")
FORCE_ALL = os.environ.get("MONITOR_FORCE_ALL", "").lower() in ("true", "1", "yes")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# field names (edit if yours differ)
F_URL="url"; F_FREQ="frequency"; F_MON="monitor"; F_STATUS="status"
F_HTTP="http_status"; F_CHECKED="last_checked"; F_HASH="content_hash"
F_SEEN="seen_links"; F_NEW="new_links"; F_CHANGE="last_change"; F_ALERT="alert_status"

UA = "Mozilla/5.0 (compatible; WBA-LinkMonitor/1.0)"
TODAY = dt.date.today()
WD = TODAY.weekday()  # Mon=0 .. Sun=6
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|ref$)", re.I)
DOCISH   = re.compile(r"report|annual|sustainab|esg|/download|/publication|/disclosur", re.I)

def due_today(freq):
    f=(freq or "").strip().lower()
    if FORCE_ALL: return True
    if f=="daily": return True
    if f=="mon_wed_fri": return WD in (0,2,4)
    if f=="mon_fri": return WD in (0,4)
    if f=="weekly": return WD==0
    if f in ("monthly","quarterly"): return TODAY.day==1
    return WD==0  # unknown/empty -> treat as weekly

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
    rdom=reg_domain(base); out=set()
    for a in BeautifulSoup(html,"html.parser").find_all("a",href=True):
        href=a["href"].strip()
        if href.startswith(("#","mailto:","tel:","javascript:")): continue
        absu=urljoin(base,href)
        if reg_domain(absu)!=rdom: continue
        path=urlsplit(absu).path.lower()
        if path.endswith(".pdf") or DOCISH.search(path+" "+a.get_text(" ",strip=True).lower()):
            n=normalize(absu)
            if n: out.add(n)
    return out

def fetch(session, url, timeout=20):
    try:
        return session.get(url, allow_redirects=True, timeout=timeout, headers={"User-Agent":UA}), None
    except requests.exceptions.SSLError:
        try:
            import urllib3; urllib3.disable_warnings()
            return session.get(url, allow_redirects=True, timeout=timeout, headers={"User-Agent":UA}, verify=False), None
        except Exception: return None, "ssl"
    except requests.exceptions.Timeout: return None, "timeout"
    except requests.exceptions.ConnectionError: return None, "connection"
    except Exception: return None, "error"

def get_monitored():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    params={"pageSize":100, "filterByFormula":"{%s}=TRUE()"%F_MON}  # monitor is a checkbox
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

def process(rec, sess):
    f=rec.get("fields",{}); u=f.get(F_URL,"")
    r,err=fetch(sess,u)
    upd={F_CHECKED:TODAY.isoformat()}
    if r is None or r.status_code>=400:
        upd[F_STATUS]="dead" if r is not None else "error"
        upd[F_HTTP]=str(r.status_code) if r is not None else ""
        return rec["id"], upd, None
    upd[F_STATUS]="redirected" if r.history else "ok"
    upd[F_HTTP]=str(r.status_code)
    current=doc_links(r.text, r.url)
    upd[F_HASH]=hashlib.md5("\n".join(sorted(current)).encode()).hexdigest()
    seen=set((f.get(F_SEEN) or "").split("\n")) - {""}
    new=sorted(current - seen)
    upd[F_SEEN]="\n".join(sorted(current))[:90000]
    return rec["id"], upd, (new if (seen and new) else None)  # baseline first time, no false alert

def main():
    recs=get_monitored()
    due=[r for r in recs if due_today(r.get("fields",{}).get(F_FREQ))]
    print(f"{len(recs)} monitored links, {len(due)} due today "
          f"({'FORCE_ALL' if FORCE_ALL else TODAY.strftime('%A')}).")
    sess=requests.Session(); updates=[]; changed=failed=done=0

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs=[ex.submit(process,r,sess) for r in due]
        for fut in as_completed(futs):
            rid, upd, new = fut.result()
            if upd.get(F_STATUS) in ("dead","error"): failed+=1
            if new:
                changed+=1
                upd[F_NEW]=f"{TODAY.isoformat()}: " + ", ".join(new)
                upd[F_CHANGE]=TODAY.isoformat()
                upd[F_ALERT]="new"
            updates.append({"id":rid,"fields":upd}); done+=1
            if done%200==0: print(f"  {done}/{len(due)}")

    print(f"Writing {len(updates)} updates back to Airtable ...")
    patch(updates)
    print(f"Done. Pages with NEW document links: {changed}.  Failed to fetch: {failed}.")
    if changed: print('See them: filter the table by alert_status = "new".')

if __name__=="__main__":
    main()
