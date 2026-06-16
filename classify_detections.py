#!/usr/bin/env python3
"""
classify_detections.py — turn raw detections into answerable events.

Fills, for each detection (deterministic, no AI):
  doc_type     sustainability_report / annual_report / policy / financial_report /
               press_release / other. A "report" type is only assigned when there is a
               document word (report/statement/disclosure/...) OR the link is a PDF; a mere
               section page like /sustainability or "Certifications" is "other", not a report.
  doc_year     year the document refers to (title preferred over URL timestamps)
  recent       checkbox: doc_year is this year or last year
  source_kind  PDF document / Web page / Spreadsheet / Presentation / Video / File
  label        human-readable name: the real title if usable, else the humanized file name
               from the URL. NEVER the doc_type (that caused circular "sustainability report").

Set CLASSIFY_FORCE=true to re-process every row (use once after a logic change). Otherwise
it only processes rows missing doc_type or label.

Env: AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_DETECTIONS_TABLE (default: detections)
Fields: doc_type (Single select), doc_year (Single line text), recent (Checkbox),
        source_kind (Single select), label (Single line text)
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
FORCE = os.environ.get("CLASSIFY_FORCE", "").lower() in ("true", "1", "yes")
if not (TOKEN and BASE):
    sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE environment variables.")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

CUR = dt.date.today().year
RECENT_SINCE = CUR - 1
F_URL="document_url"; F_TITLE="title"; F_TYPE="doc_type"; F_YEAR="doc_year"
F_RECENT="recent"; F_KIND="source_kind"; F_LABEL="label"

YEAR = re.compile(r"(20[12]\d)")
STRONG_PRESS = re.compile(r"appoint|announce|\bnames?\b|awarded|\bwins?\b|launch|partners?.with|"
                          r"to.acquire|completes|joins|welcomes|celebrat|presentation|conference", re.I)
DOCWORD = re.compile(r"report|statement|review|disclosure|memoria|rapport|bericht|informe|"
                     r"10-?k|20-?f|prospectus|filing|accounts|factbook|databook", re.I)
SUS = re.compile(r"sustainab|\besg\b|\bcsr\b|climate|environment|carbon|emission|net.?zero|"
                 r"responsib|non.?financial|\bimpact\b|materiality|\btcfd\b|\bcdp\b|carbon.disclos|"
                 r"decarboni|biodiversit|\bghg\b|stewardship", re.I)
ANN = re.compile(r"annual|integrated|jahresbericht|rapport.?annuel|informe.?anual", re.I)
FIN = re.compile(r"financ|\bresults?\b|quarterly|half.?year|interim|earnings|10-?k|20-?f|trading.?statement", re.I)
POLICY = re.compile(r"\bpolicy\b|\bpolicies\b|code.?of.?conduct|\bcharter\b|modern.?slavery", re.I)
NEWS = re.compile(r"press|news|media|/article|story|release|/blog", re.I)

GENERIC_STRICT = {"","download","downloads","download center","pdf","more","read more","link",
                  "click here","view","details","learn more","here","file","document","documents",
                  "open","go","view all","see more","read"}
EXT_KIND = {"pdf":"PDF document","doc":"Word document","docx":"Word document","rtf":"Word document",
            "xls":"Spreadsheet","xlsx":"Spreadsheet","csv":"Spreadsheet",
            "ppt":"Presentation","pptx":"Presentation","mp4":"Video","webm":"Video","mov":"Video"}

def year_of(title, url):
    for blob in (title, url):
        ys=[int(y) for y in YEAR.findall(blob or "") if 2010 <= int(y) <= CUR+1]
        if ys: return max(ys)
    return None

def is_pdf_url(url):
    return urlsplit(url).path.lower().endswith(".pdf")

def classify(title, url):
    blob=f"{title} {url}"
    if STRONG_PRESS.search(blob): return "press_release"
    if POLICY.search(blob):       return "policy"
    has_doc = bool(DOCWORD.search(blob)); pdf = is_pdf_url(url)
    if has_doc or pdf:                       # only call it a report if it's actually a document
        if SUS.search(blob): return "sustainability_report"
        if ANN.search(blob): return "annual_report"
        if FIN.search(blob): return "financial_report"
        if pdf and NEWS.search(blob): return "press_release"
        if pdf: return "other"
    if NEWS.search(blob): return "press_release"
    return "other"

def source_kind(url):
    p=urlsplit(url).path.lower()
    m=re.search(r"\.([a-z0-9]{2,5})$", p)
    if not m: return "Web page"
    e=m.group(1)
    if e in EXT_KIND: return EXT_KIND[e]
    if e in ("html","htm","asp","aspx","php","jsp","shtml"): return "Web page"
    return f"File (.{e})"

def humanize(seg):
    seg=unquote(seg or "")
    seg=re.sub(r"\.(pdf|html?|aspx?|php|jsp|docx?|xlsx?|pptx?|rtf|csv)$","",seg,flags=re.I)
    seg=re.sub(r"[-_%+]+"," ",seg)
    seg=re.sub(r"\s+"," ",seg).strip()
    toks=seg.split()
    while toks and toks[0].isdigit() and len(toks[0])>=5:
        toks.pop(0)
    return " ".join(toks)

def make_label(title, url):
    t=(title or "").strip()
    if t and t.lower() not in GENERIC_STRICT:
        return t[:120]
    seg=[s for s in urlsplit(url).path.split("/") if s]
    h=humanize(seg[-1]) if seg else ""
    if h: return h[:120]
    return (t or (seg[-1] if seg else "(untitled)"))[:120]

def get_rows():
    url=f"{API}/{BASE}/{quote(TABLE)}"
    params={"pageSize":100}; out=[]; offset=None
    while True:
        if offset: params["offset"]=offset
        r=requests.get(url,headers=HEADERS,params=params,timeout=30); r.raise_for_status()
        j=r.json(); out.extend(j.get("records",[])); offset=j.get("offset"); time.sleep(0.25)
        if not offset: break
    if FORCE: return out
    return [r for r in out if not (r.get("fields",{}).get(F_TYPE) and r.get("fields",{}).get(F_LABEL))]

def patch(updates):
    url=f"{API}/{BASE}/{quote(TABLE)}"
    for i in range(0,len(updates),10):
        r=requests.patch(url,headers={**HEADERS,"Content-Type":"application/json"},
                         json={"records":updates[i:i+10],"typecast":True},timeout=30)
        r.raise_for_status(); time.sleep(0.25)

def main():
    recs=get_rows()
    print(f"{len(recs)} detections to (re)classify{' [FORCE]' if FORCE else ''}.")
    updates=[]; bytype=collections.Counter(); hi=0
    for r in recs:
        f=r.get("fields",{}); title=f.get(F_TITLE,""); url=f.get(F_URL,"")
        typ=classify(title,url); yr=year_of(title,url); recent=bool(yr and yr>=RECENT_SINCE)
        fields={F_TYPE:typ, F_RECENT:recent, F_KIND:source_kind(url), F_LABEL:make_label(title,url)}
        if yr: fields[F_YEAR]=str(yr)
        bytype[typ]+=1
        if recent and typ in ("sustainability_report","annual_report","policy"): hi+=1
        updates.append({"id":r["id"],"fields":fields})
    if not updates:
        print("Nothing to classify."); return
    patch(updates)
    print("doc_type:", dict(bytype.most_common()))
    print(f"High-signal (recent + sustainability/annual/policy): {hi}")

if __name__=="__main__":
    main()
