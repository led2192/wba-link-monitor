#!/usr/bin/env python3
"""
inbox.py — the person's three one-step actions, handled within minutes by the Scan now watcher.

1. Paste a link (companies.add_link, one per line) or drop a file (companies.add_file).
   For each link the robot decides by itself, so nothing else has to be filled in:
     - already watched page or already held document  -> says so (and where), adds nothing
     - a PDF (by its address, or by opening it)          -> new report_library row; the file is
                                                            attached, read and labelled now
     - a web page                                        -> new monitored_links row with a guessed
                                                            type, scanned in the same pass
     - refused by the site                               -> says so: download it and use add_file
   A dropped file becomes a report_library row (a file already held, same name and size, is
   refused). Results go to companies.add_result, newest first; the boxes empty themselves.

2. Approve suggested pages (companies.approve_suggestions): every line still in suggested_links
   (written by discover_pages.py) is added like a pasted page link.

3. Delete a document now (report_library.delete_now): the row is archived
   (state/deleted_by_hand.jsonl.gz), its document is tombstoned (the same list the purge uses,
   state/tombstones_report_library.tsv.gz, read by the harvester, so it never comes back), the
   two files are pushed to the repository, and only then is the row deleted. Verified rows are
   never deleted.

Every addition goes through the same duplicate guards as the nightly harvester, so a paste can
never create a second copy of something we hold. Env: AIRTABLE_TOKEN, AIRTABLE_BASE.
"""
import gzip, hashlib, json, os, re, subprocess, time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import requests

from ids import normalize, doc_id
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
TOKEN = os.environ.get("AIRTABLE_TOKEN"); BASE = os.environ.get("AIRTABLE_BASE")
H = {"Authorization": f"Bearer {TOKEN}"}
LINKS, LIB, COMP = "monitored_links", "report_library", "companies"
TOMBSTONES = "state/tombstones_report_library.tsv.gz"
ARCHIVE = "state/deleted_by_hand.jsonl.gz"
URL_RX = re.compile(r"https?://[^\s<>\"']+|(?:www\.)[^\s<>\"']+", re.I)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9"}
PAGE_TYPES = ("reports_hub", "sustainability_page", "investor_relations", "policies")
LABEL = {"sustainability_page": "Sustainability", "reports_hub": "Reports",
         "investor_relations": "Investor Relations", "policies": "Policies"}
FREQ = {"reports_hub": "mon_wed_fri", "sustainability_page": "mon_wed_fri",
        "investor_relations": "mon_wed_fri", "policies": "mon_fri"}
# same rules as active_search.py, kept here so this module imports without side effects
TYPE_RULES = [
    ("sustainability_page", re.compile(r"sustainab|esg|environment|csr|responsib|climate|impact|"
                                       r"human.?right|modern.?slavery|divers|inclusion|net.?zero", re.I)),
    ("reports_hub", re.compile(r"report|annual|financ|result|filing|disclosur|publication|download|"
                               r"presentations|interim|/documents?(/|$)|librar", re.I)),
    ("investor_relations", re.compile(r"investor|shareholder|/ir/|/ir$|stock|general.?meeting", re.I)),
    ("policies", re.compile(r"policy|policies|code-of-conduct|governance|ethics|compliance|whistleblow", re.I))]


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get(table, params):
    out = []; offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{BASE}/{quote(table)}", H, params=p).json()
        out.extend(j.get("records", [])); offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.12)


def write(table, method, records, typecast=True):
    url = f"{API}/{BASE}/{quote(table)}"
    out = []
    for i in range(0, len(records), 10):
        r = airtable_request(method, url, {**H, "Content-Type": "application/json"},
                             {"records": records[i:i + 10], "typecast": typecast})
        out += r.json().get("records", [])
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------------------------------------
# 1 + 2. links, files and approved suggestions
# ---------------------------------------------------------------------------------------------
def links_in(text):
    out = []
    for m in URL_RX.findall(text or ""):
        u = m.rstrip(").,;]")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


def suggested_in(text):
    """[(type, url)] from suggested_links lines 'type | url | why'."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        urls = links_in(line)
        if not urls:
            continue
        typ = parts[0] if parts and parts[0] in PAGE_TYPES else ""
        out.append((typ, urls[0]))
    return out


def guess_type(url, title=""):
    blob = (urlsplit(url).path + " " + title).lower()
    for typ, rx in TYPE_RULES:
        if rx.search(blob):
            return typ
    return "reports_hub"


def expected(url):
    """What the library will call this document at first, in plain words: the type guessed from the
    address and the year, both checked again once the file is read."""
    import classify_library as CL, harvest_reports as HR
    t, _ = CL.classify(url)
    y = HR.year_of(url)
    what = " ".join(x for x in (t, str(y) if y else "") if x) or "no type yet"
    return f"First guess from the address: {what}. The type and year are checked once the file is read (minutes to a day)."


def what_is(url, sess):
    """('pdf', target) | ('page', title) | ('blocked', code) | ('gone', code) | ('error', '')."""
    import harvest_reports as HR, doc_probe as DP
    if HR.is_pdf_url(url):
        # an address that looks like a PDF can still be refused (CLP, Oct 2026: the row was added,
        # the download then failed and the report sat without a file, untyped, "being read")
        try:
            r = sess.get(url, headers=UA, timeout=25, allow_redirects=True, stream=True)
            code = r.status_code
            r.close()
            if code in (401, 403, 429):
                return "blocked", code
            if code in (404, 410):
                return "gone", code
        except requests.RequestException:
            pass                                   # unreachable now: add it, the attach step retries
        return "pdf", url
    try:
        r = sess.get(url, headers=UA, timeout=25, allow_redirects=True, stream=True)
        code = r.status_code
        ctype = (r.headers.get("Content-Type") or "").lower()
        head = next(r.iter_content(4096), b"")
        if code in (401, 403, 429):
            return "blocked", code
        if code >= 400:
            return "gone", code
        if "pdf" in ctype or head.startswith(b"%PDF"):
            return "pdf", r.url
        body = head + b"".join(c for _, c in zip(range(60), r.iter_content(4096)))
        r.close()
        m = re.search(rb"<title[^>]*>(.*?)</title>", body, re.I | re.S)
        title = m.group(1).decode("utf-8", "ignore").strip()[:120] if m else ""
        # a viewer page on an asset platform (Widen...) hides a PDF: let doc_probe resolve it
        v, target = DP.check(url, sess)
        if v == "pdf":
            return "pdf", target or url
        return "page", title
    except requests.RequestException:
        return "error", ""


def company_state(wba):
    pages = get(LINKS, [("pageSize", "100"), ("filterByFormula", f"{{wba_id}}='{wba}'"),
                        ("fields[]", "url"), ("fields[]", "type"), ("fields[]", "monitor")])
    docs = get(LIB, [("pageSize", "100"), ("filterByFormula", f"{{wba_id}}='{wba}'"),
                     ("fields[]", "document_url"), ("fields[]", "file"), ("fields[]", "discard"),
                     ("fields[]", "library_id")])
    return pages, docs


def lib_row(wba, name, url, found_on=""):
    import harvest_reports as HR
    key = normalize(url) or url.strip().lower()
    return {"library_id": f"{wba}-{hashlib.sha1(key.encode()).hexdigest()[:10]}", "wba_id": wba,
            "company_name": name, "document_url": url, **({"found_on": found_on} if found_on else {}),
            "doc_year": HR.year_of(url), "source_link_id": ""}


def handle_company(rec, sess, tombs):
    """Returns (result lines, created library rows, created page ids)."""
    import harvest_reports as HR
    f = rec["fields"]
    wba = (f.get("wba_id") or "").strip(); name = f.get("company_name") or ""
    lines, created_docs, new_pages = [], [], []
    if not wba:
        return ["This company has no wba_id: nothing could be added."], [], []
    pages, docs = company_state(wba)
    page_keys = {normalize(p["fields"].get("url", "")): p["fields"].get("type") or "page" for p in pages}
    page_ids = {normalize(p["fields"].get("url", "")): p.get("id") for p in pages}
    known = {doc_id(wba, d["fields"].get("document_url", "")) for d in docs if d["fields"].get("document_url")}
    # rows we hold without their PDF (the site refused the download): a file dropped with that link
    # goes onto the existing row instead of making a second one
    fileless = {doc_id(wba, d["fields"].get("document_url", "")): d["id"] for d in docs
                if d["fields"].get("document_url") and not d["fields"].get("file") and not d["fields"].get("discard")}
    fill_rows = {}                                  # link -> existing record id to put the file on
    known_files = {((a.get("filename") or "").lower(), a.get("size")) for d in docs
                   for a in (d["fields"].get("file") or [])[:1]}

    blocked_links = []    # links the site refused; a file dropped in the same go is that document
    todo = [(None, u) for u in links_in(f.get("add_link"))]   # None = decide by opening it
    if f.get("approve_suggestions"):
        todo += suggested_in(f.get("suggested_links"))
    for forced_type, url in todo:
        short = url if len(url) < 90 else url[:87] + "..."
        k = normalize(url)
        if k in page_keys:
            # A page you paste is a page you want harvested. Pages typed news / other / homepage
            # are watched but never harvested for documents (the Woodbois /investors/ and BCEG
            # report-list cases, Oct 2026): give such a page a harvested type and scan it now.
            if page_keys[k] not in HR.ALL_TYPES:
                typ = forced_type or guess_type(url)
                write(LINKS, "PATCH", [{"id": page_ids[k], "fields": {"type": typ, "monitor": True,
                                                                        "scan_now": True}}])
                lines.append(f"Already watched, but as {page_keys[k]} (not harvested): changed to {typ} "
                             f"and being scanned now: {short}")
                page_keys[k] = typ
            else:
                lines.append(f"Already watched as {page_keys[k]}: {short}")
            continue
        did = doc_id(wba, url)
        if did in known:
            if did in fileless and f.get("add_file"):
                blocked_links.append(url); fill_rows[url] = fileless[did]
            elif did in fileless:
                lines.append(f"Already in the library, but without its PDF (the site refuses our robot): "
                             f"download it and drop it in add_file together with this link: {short}")
            else:
                lines.append(f"Already in the library: {short}")
            continue
        if did in tombs:
            lines.append(f"Not added, you deleted this document before: {short}")
            continue
        kind, info = ("page", "") if forced_type else what_is(url, sess)   # '' type: decide by opening
        if kind == "pdf":
            row = lib_row(wba, name, info or url)
            if doc_id(wba, row["document_url"]) in known:
                lines.append(f"Already in the library: {short}"); continue
            created, _, blocked, _ = HR.upsert([row], {}, BASE, TOKEN, LIB,
                                               existing={})   # known docs checked above
            if created:
                created_docs += created; known.add(did)
                lines.append(f"Document added: {short}. {expected(row['document_url'])}")
            else:
                lines.append(f"Already in the library: {short}")
        elif kind == "page":
            typ = forced_type or guess_type(url, info)
            out = write(LINKS, "POST", [{"fields": {
                "link_name": f"{name} — {LABEL.get(typ, 'Reports')}", "wba_id": wba,
                "company_name": name, "url": url, "type": typ, "monitor": True,
                "frequency": FREQ.get(typ, "mon_fri"), "is_pdf": False,
                "source": "approved suggestion" if forced_type is not None else "added by hand",
                "companies": [rec["id"]], "scan_now": True}}])
            new_pages += [o["id"] for o in out]; page_keys[k] = typ
            lines.append(f"Page added as {typ} and being scanned now: {short}")
        elif kind == "blocked":
            if f.get("add_file"):
                blocked_links.append(url)        # paired with a dropped file below
            else:
                lines.append(f"The site refused our robot ({info}), nothing added. Open it yourself, "
                             f"download the PDF and drop it in add_file together with this link: {short}")
        elif kind == "gone":
            lines.append(f"This address does not open ({info}), nothing added: {short}")
        else:
            lines.append(f"Could not reach this address, nothing added (try again later): {short}")

    for att in f.get("add_file") or []:
        fname = att.get("filename") or "file.pdf"
        if (fname.lower(), att.get("size")) in known_files:
            lines.append(f"File already in the library: {fname}")
            continue
        # A file dropped together with a link the site refused IS that document: keep the real
        # address, so the duplicate guards and the harvester recognise it later.
        src = blocked_links.pop(0) if blocked_links else ""
        key = (normalize(src) or src.lower()) if src else f"upload:{wba}:{fname.lower()}:{att.get('size')}"
        row = {"library_id": f"{wba}-{hashlib.sha1(key.encode()).hexdigest()[:10]}", "wba_id": wba,
               "company_name": name, "file": [{"url": att["url"], "filename": fname}],
               "doc_year": HR.year_of(src or fname) or HR.year_of(fname),
               "first_seen": datetime.now(timezone.utc).date().isoformat(),
               "Reviewer notes": f"Uploaded by hand ({fname})" + (f", downloaded from {src}." if src else ".")}
        if src and src in fill_rows:
            # the document is already a row, only its PDF was missing: put the file there
            write(LIB, "PATCH", [{"id": fill_rows[src], "fields": {"file": row["file"],
                                                                   "Reviewer notes": row["Reviewer notes"]}}])
            known_files.add((fname.lower(), att.get("size")))
            lines.append(f"File added to the existing row of {src[:80]}. Its type and year are read from "
                         "the file within the day.")
            continue
        if src:
            row["document_url"] = src
        out = write(LIB, "POST", [{"fields": row}])
        created_docs += out; known_files.add((fname.lower(), att.get("size")))
        lines.append(f"File added: {fname}" + (f" (as the document at {src[:80]})" if src else "")
                     + ". Its type and year are read from the file within the day.")
    for u in blocked_links:
        lines.append(f"The site refused our robot, and no file was left to pair with this link: {u[:90]}")
    return lines, created_docs, new_pages


def run_links():
    formula = "OR({add_link}!='', {add_file}!='', {approve_suggestions})"
    recs = get(COMP, [("pageSize", "100"), ("filterByFormula", formula)] +
               [("fields[]", x) for x in ("wba_id", "company_name", "add_link", "add_file", "add_result",
                                           "approve_suggestions", "suggested_links")])
    if not recs:
        return False
    from purge_discarded import load_tombstones
    tombs = load_tombstones()
    sess = requests.Session()
    created_all, results, touched = [], [], []
    for rec in recs:
        print(f">>> inbox: {rec['fields'].get('company_name')}", flush=True)
        try:
            lines, created, _ = handle_company(rec, sess, tombs)
        except Exception as e:
            lines, created = [f"Something went wrong ({type(e).__name__}); your input is kept, "
                              "it will be retried in a few minutes."], []
            write(COMP, "PATCH", [{"id": rec["id"], "fields": {"add_result": (
                f"{stamp()}: " + lines[0] + "\n" + (rec['fields'].get('add_result') or ''))[:90000]}}])
            continue
        created_all += created
        f = rec["fields"]
        upd = {"add_link": "", "add_file": [], "approve_suggestions": False,
               "add_result": (f"{stamp()}:\n" + "\n".join("- " + l for l in lines) + "\n\n"
                              + (f.get("add_result") or ""))[:90000]}
        if f.get("approve_suggestions"):
            upd["suggested_links"] = ""
        results.append({"id": rec["id"], "fields": upd})
        if f.get("wba_id"):
            touched.append(f["wba_id"])
    write(COMP, "PATCH", results)
    if created_all:
        import scan_now
        scan_now.finish_docs(created_all)
    if touched:
        subprocess.run(["python", "company_coverage.py", "--commit", "--wba", ",".join(touched)], check=False)
    return True


# ---------------------------------------------------------------------------------------------
# 3. delete now
# ---------------------------------------------------------------------------------------------
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


PAGE_TOMBSTONES = "state/tombstones_monitored_links.tsv.gz"
PAGE_ARCHIVE = "state/deleted_pages.jsonl.gz"


def page_key(wba, url):
    return f"{wba}|{normalize(url) or url.strip().lower()}"


def load_page_tombstones(path=PAGE_TOMBSTONES):
    """{wba|normalized url: url} of every page deleted by hand. discover_pages.py and
    active_search.py never propose or add these again."""
    if not os.path.exists(path):
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return {p[0]: (p[1] if len(p) > 1 else "") for p in (l.rstrip("\n").split("\t") for l in fh if l.strip())}


def save_state(tombs, archives, msg):
    """tombs: {tombstone file: new lines (key first)}, archives: {archive file: json lines}.
    Appends them on top of the latest main and pushes. True when pushed: only then may rows be
    deleted. Retries when another job pushed in between. Only inside GitHub Actions (it resets the
    checkout to the latest main)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        print("    save_state runs only inside GitHub Actions; nothing saved.", flush=True)
        return False
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    for attempt in range(4):
        git("fetch", "-q", "origin", "main")
        git("reset", "-q", "--hard", "origin/main")
        os.makedirs("state", exist_ok=True)
        for path, new_lines in tombs.items():
            old = []
            if os.path.exists(path):
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    old = [l.rstrip("\n") for l in fh if l.strip()]
            have = {l.split("\t", 1)[0] for l in old}
            add = [l for l in new_lines if l.split("\t", 1)[0] not in have]
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write("\n".join(old + add) + "\n")
        for path, lines in archives.items():
            with gzip.open(path, "at", encoding="utf-8") as fh:   # gzip members append safely
                for a in lines:
                    fh.write(a + "\n")
        git("add", *tombs.keys(), *archives.keys())
        c = git("commit", "-q", "-m", msg)
        if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
            print(f"    commit failed: {c.stderr.strip()}", flush=True)
        p = git("push", "-q", "origin", "HEAD:main")
        if p.returncode == 0:
            return True
        print(f"    push rejected (attempt {attempt + 1}): {p.stderr.strip()[:200]}", flush=True)
        time.sleep(5 + 5 * attempt)
    return False


def run_page_deletes():
    """monitored_links.delete_now: archive, tombstone (pushed first), then delete. The documents
    found on the page stay. Certified pages are refused."""
    recs = get(LINKS, [("pageSize", "100"), ("filterByFormula", "{delete_now}")] +
               [("fields[]", x) for x in ("link_id", "wba_id", "company_name", "url", "type", "certified",
                                           "notes", "monitor", "source", "status")])
    if not recs:
        return False
    keep = [r for r in recs if r["fields"].get("certified")]
    go = [r for r in recs if not r["fields"].get("certified")]
    if keep:
        write(LINKS, "PATCH", [{"id": r["id"], "fields": {"delete_now": False, "notes": (
            "Not deleted: this page is certified. Untick certified first if it really should go.\n"
            + (r["fields"].get("notes") or ""))}} for r in keep])
    if not go:
        return True
    today = datetime.now(timezone.utc).date().isoformat()
    tomb = [f"{page_key(r['fields'].get('wba_id', ''), r['fields'].get('url', ''))}\t{r['fields'].get('url', '')}"
            f"\t{today}\tDeleted by hand" for r in go if r["fields"].get("url")]
    arch = [json.dumps({"record_id": r["id"], "deleted_on": today, "fields": r["fields"]}, ensure_ascii=False)
            for r in go]
    if not save_state({PAGE_TOMBSTONES: tomb}, {PAGE_ARCHIVE: arch},
                      f"Pages deleted by hand: {len(go)} monitored_links row(s) ({today})"):
        print(">>> could not save the page tombstones; nothing deleted, will retry.", flush=True)
        return True
    url = f"{API}/{BASE}/{quote(LINKS)}"
    for i in range(0, len(go), 10):
        airtable_request("DELETE", url, H, params=[("records[]", r["id"]) for r in go[i:i + 10]])
        time.sleep(0.22)
    print(f">>> deleted {len(go)} page(s) by hand.", flush=True)
    wbas = sorted({r["fields"].get("wba_id") for r in go if r["fields"].get("wba_id")})
    if wbas:
        subprocess.run(["python", "company_coverage.py", "--commit", "--wba", ",".join(wbas[:40])], check=False)
    return True


def run_deletes():
    recs = get(LIB, [("pageSize", "100"), ("filterByFormula", "{delete_now}")])
    if not recs:
        return False
    keep = [r for r in recs if r["fields"].get("verified")]
    go = [r for r in recs if not r["fields"].get("verified")]
    if keep:
        write(LIB, "PATCH", [{"id": r["id"], "fields": {"delete_now": False, "Reviewer notes": (
            "Not deleted: this row is verified. Untick verified first if it really should go.\n"
            + (r["fields"].get("Reviewer notes") or ""))}} for r in keep])
    if not go:
        return True
    today = datetime.now(timezone.utc).date().isoformat()
    tomb, arch = [], []
    for r in go:
        f = dict(r["fields"])
        if f.get("file"):
            f["file"] = [{"filename": a.get("filename"), "size": a.get("size")} for a in f["file"]]
        f.pop("cover_text", None)
        arch.append(json.dumps({"record_id": r["id"], "deleted_on": today, "fields": f}, ensure_ascii=False))
        url = r["fields"].get("document_url") or ""
        wba = r["fields"].get("wba_id") or ""
        if url and wba:
            tomb.append(f"{doc_id(wba, url)}\t{r['fields'].get('library_id', '')}\t{today}\tDeleted by hand")
    if not save_state({TOMBSTONES: tomb}, {ARCHIVE: arch},
                      f"Deleted by hand: {len(go)} report_library row(s) ({today})"):
        print(">>> could not save the tombstones; nothing deleted, will retry.", flush=True)
        return True
    url = f"{API}/{BASE}/{quote(LIB)}"
    for i in range(0, len(go), 10):
        airtable_request("DELETE", url, H, params=[("records[]", r["id"]) for r in go[i:i + 10]])
        time.sleep(0.22)
    print(f">>> deleted {len(go)} row(s) by hand, tombstoned {len(tomb)} document(s).", flush=True)
    wbas = sorted({r["fields"].get("wba_id") for r in go if r["fields"].get("wba_id")})
    if wbas:
        subprocess.run(["python", "company_coverage.py", "--commit", "--wba", ",".join(wbas[:40])], check=False)
    return True


def run_new_companies():
    """A company whose website was just filled in and never searched: look for its report pages
    now (suggestions only, see discover_pages.py) instead of waiting for the night."""
    recs = get(COMP, [("pageSize", "100"), ("fields[]", "wba_id"),
                      ("filterByFormula", "AND({website}!='', {suggestions_checked}=BLANK(), "
                                          "OR({urls_total}=BLANK(), {urls_total}=0))")])
    wbas = [r["fields"]["wba_id"] for r in recs if r["fields"].get("wba_id")][:10]
    if not wbas:
        return False
    print(f">>> new companies: searching {len(wbas)} website(s) for report pages ...", flush=True)
    subprocess.run(["python", "discover_pages.py", "--commit", "--wba", ",".join(wbas)], check=False)
    return True


def run_searches():
    """mention_searches: a new search (never run, words filled in, untouched for 2 minutes so it is
    not caught half-typed) or one with run_now ticked starts the Mention search workflow at once
    for that search only. The answer lands on the row in ~10 minutes."""
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (tok and repo):
        return False
    formula = ("AND({search}!='', {find}!='', "
               "IS_BEFORE(LAST_MODIFIED_TIME(), DATEADD(NOW(), -2, 'minutes')), "
               "OR({run_now}, AND({last_run}=BLANK(), {status}='')))")
    recs = get("mention_searches", [("pageSize", "100"), ("filterByFormula", formula),
                                    ("fields[]", "search")])
    if not recs:
        return False
    done = []
    for r in recs[:10]:
        name = r["fields"].get("search", "")
        resp = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/mention_search.yml/dispatches",
                             headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                             json={"ref": "main", "inputs": {"mode": "commit", "search": name}}, timeout=30)
        ok = resp.status_code in (200, 204)
        print(f">>> search '{name}': {'started' if ok else 'could not start: ' + resp.text[:200]}", flush=True)
        done.append({"id": r["id"], "fields": {"run_now": False, "status": (
            f"Running since {stamp()}: the answer arrives here in about 10 minutes." if ok else
            "Could not start the search; tick run_now to try again.")}})
    write("mention_searches", "PATCH", done)
    return True


# ---------------------------------------------------------------------------------------------
# live coverage: recompute a company's coverage within minutes of any change that affects it
# ---------------------------------------------------------------------------------------------
_LAST_COVERAGE_CHECK = None
WATCH = [   # (table, fields whose change matters for coverage / the queue)
    (LIB, ["source_type_check", "source_type", "doc_year", "curated_doc_title", "discard", "file", "wba_id"]),
    (LINKS, ["type", "monitor", "status", "wba_id"]),
    (COMP, ["queue_snooze_until", "website", "suggested_links"]),
]
LIVE_MAX = 200     # more companies than this at once = a bulk job; the nightly run covers those


def run_coverage_refresh():
    """Companies whose documents, pages or queue inputs changed since the last check (by a person
    or a robot) get their coverage, gaps and queue recomputed now. Only the fields that matter
    are watched, so coverage writing to companies does not trigger itself."""
    global _LAST_COVERAGE_CHECK
    now = datetime.now(timezone.utc)
    since = _LAST_COVERAGE_CHECK or (now - timedelta(minutes=15))
    stamp_iso = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    wbas = set()
    for table, fields in WATCH:
        watched = ", ".join("{%s}" % x for x in fields if x != "wba_id")
        formula = f"IS_AFTER(LAST_MODIFIED_TIME({watched}), '{stamp_iso}')"
        try:
            rows = get(table, [("pageSize", "100"), ("fields[]", "wba_id"), ("filterByFormula", formula)])
        except Exception as e:
            print(f">>> live coverage: could not read {table} ({e}).", flush=True)
            return False
        wbas |= {(r["fields"].get("wba_id") or "").strip() for r in rows}
        if len(wbas) > LIVE_MAX:
            break
    _LAST_COVERAGE_CHECK = now
    wbas.discard("")
    if not wbas:
        return False
    if len(wbas) > LIVE_MAX:
        print(f">>> live coverage: {len(wbas)}+ companies changed at once (a bulk job); "
              "left to the nightly coverage run.", flush=True)
        return False
    print(f">>> live coverage: refreshing {len(wbas)} company(ies).", flush=True)
    ws = sorted(wbas)
    for i in range(0, len(ws), 40):
        subprocess.run(["python", "company_coverage.py", "--commit", "--wba", ",".join(ws[i:i + 40])], check=False)
    return True


def run():
    a = run_deletes() | run_page_deletes()
    b = run_links()
    c = run_new_companies()
    try:
        d = run_searches()
    except Exception as e:
        print(f">>> search start failed: {e}", flush=True); d = False
    try:
        run_coverage_refresh()      # never counts as "work done": it must not shorten the watch pause
    except Exception as e:
        print(f">>> live coverage failed: {e}", flush=True)
    return a or b or c or d


if __name__ == "__main__":
    print("did work" if run() else "inbox empty")
