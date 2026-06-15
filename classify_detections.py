#!/usr/bin/env python3
"""
classify_detections.py — turn raw detections into answerable events.

Fills, for each detection, deterministic fields (free, no AI) that make it readable:
  doc_type     sustainability_report / annual_report / policy / financial_report /
               press_release / other  (from URL + title)
  doc_year     the year the document refers to (title preferred over URL timestamps)
  recent       checkbox: doc_year is this year or last year (current reporting cycle)
  source_kind  PDF document / Web page / Spreadsheet / Presentation / Video / File
  label        a human-readable name: the title if it's informative, otherwise the
               humanized file name from the URL (rescues empty / "PDF" / "Download" titles)

Your questions become saved views, e.g.
  new sustainability report -> doc_type=sustainability_report AND recent AND status=new

Processes any row missing doc_type OR label, so it backfills new fields on existing rows
and is safe to run daily.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_DETECTIONS_TABLE (default: detections)
Detections table fields to add:
  doc_type -> Single select   doc_year -> Single line text   recent -> Checkbox
  source_kind -> Single select   label -> Single line text
"""
import os, re, sys, time, datetime as dt, collections
from urllib.parse import quote, urlsplit, unquote

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
RECENT_SINCE = CUR - 1
F_URL="document_url"; F_TITLE="title"; F_TYPE="doc_type"; F_YEAR="doc_year"
F_RECENT="recent"; F_KIND="source_kind"; F_LABEL="label"

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
GENERIC = {"","download","downloads","download center","sustainability","report","reports","home","pdf",
           "documents","document","more","read more","link","click here","view","details","learn more",
           "esg","media","news","press","overview","here","file"}
EXT_KIND = {"pdf":"PDF document","doc":"Word document","docx":"Word document","rtf":"Word document",
            "xls":"Spreadsheet","xlsx":"Spreadsheet","csv":"Spreadsheet",
            "ppt":"Presentation","pptx":"Presentation","mp4":"Video","webm":"Video","mov":"Video"}

def year_of(title, url):
    for blob in (title, url):
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

def source_kind(url):
    p=urlsplit(url).path.lower()
    m=re.search(r"\.([a-z0-9]{2,5})$", p)
    if not m: return "Web page"
    e=m.group(1)
    if e in EXT_KIND: return EXT_KIND[e]
    if e in ("html","htm","asp","aspx","php","jsp","shtml"): return "Web page"
    return f"File (.{e})"

def poor(t):
    t=(t or "").strip().lower()
    return t in GENERIC or len(t)<5 or len(t.split())<2

def humanize(seg):
    seg=unquote(seg or "")
    seg=re.sub(r"\.(pdf|html?|aspx?|php|jsp|docx?|xlsx?|pptx?|rtf|csv)$","",seg,flags=re.I)
    seg=re.sub(r"[-_%+]+"," ",seg)
    seg=re.sub(r"\s+"," ",seg).strip()
    toks=seg.split()
    while toks and toks[0].isdigit() and len(toks[0])>=5:   # drop leading ID numbers, keep years
        toks.pop(0)
    return " ".join(toks)

def make_label(title, url, doc_type, year):
    t=(title or "").strip()
    if not poor(t): return t[:120]
    seg=[s for s in urlsplit(url).path.split("/") if s]
    h=humanize(seg[-1]) if seg else ""
    if len(h.split())>=2: return h[:120]
    base=doc_type.replace("_"," ")
    return (f"{base} {year}".strip() if year else base)

def get_rows():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=requests.get(url,headers=HEADERS,params=params,timeout=30); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    # process rows missing doc_type OR label (so new fields backfill on existing rows)
    return [r for r in out if not (r.get("fields",{}).get(F_TYPE) and r.get("fields",{}).get(F_LABEL))]

def patch(updates):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(updates),10):
        r=requests.patch(url,headers={**HEADERS,"Content-Type":"application/json"},
                         json={"records":updates[i:i+10],"typecast":True},timeout=30)
        r.raise_for_status(); time.sleep(0.25)

def main():
    recs=get_rows()
    print(f"{len(recs)} detections to (re)classify.")
    updates=[]; bytype=collections.Counter(); bykind=collections.Counter(); hi=0
    for r in recs:
        f=r.get("fields",{}); title=f.get(F_TITLE,""); url=f.get(F_URL,"")
        typ=classify(title,url); yr=year_of(title,url); recent=bool(yr and yr>=RECENT_SINCE)
        fields={F_TYPE:typ, F_RECENT:recent, F_KIND:source_kind(url), F_LABEL:make_label(title,url,typ,yr)}
        if yr: fields[F_YEAR]=str(yr)
        bytype[typ]+=1; bykind[fields[F_KIND]]+=1
        if recent and typ in ("sustainability_report","annual_report","policy"): hi+=1
        updates.append({"id":r["id"],"fields":fields})
    if not updates:
        print("Nothing to classify."); return
    patch(updates)
    print("doc_type:", dict(bytype.most_common()))
    print("source_kind:", dict(bykind.most_common()))
    print(f"High-signal (recent + sustainability/annual/policy): {hi}")

if __name__=="__main__":
    main()
