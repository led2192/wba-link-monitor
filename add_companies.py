#!/usr/bin/env python3
"""
add_companies.py: companies that are not in WBA's list become companies of lanterN.

Until 11 Nov 2026 a company existed here only if WBA's SDG2000 list held it, under its WBA id (PT_00001). The
list is updated late (a company chosen for next year's list waits months for its id), and the owner wants to
follow companies WBA never covered. Three ways in, all ending in the same place, a row of the `companies`
table with an id:

  1. By hand. Add rows to `companies` in Airtable with company_name and website (lei and add_link if known)
     and leave wba_id empty; a list is pasted from a spreadsheet the same way. This script gives each row its
     id and discover_pages.py (next step of the workflow) searches its website for the report pages.
  2. From the page. "Missing a company? Ask for it" in lanterN writes a request to the evidence database
     (public.company_requests, status new). This script copies new requests to the Airtable table
     `company_requests`, where a person reads them.
  3. Approved requests. A request whose `approve` box is ticked becomes a row of `companies`, like one added
     by hand, and the request is marked done in both places.

The id. A company with no WBA id gets lanterN's own: "LN_" and the first 8 hex digits of the SHA-1 of its LEI,
or of its name without accents, case, punctuation and legal endings when it has no LEI. The same company
always gets the same id, so a rerun never makes a second row (deterministic ids, idempotent upserts). The WBA
id, when WBA gives one, is a later step (not built: renaming an id touches the documents already harvested).

Duplicates. A row is not given an id, and its `intake_note` says why, when a company already in the table has
the same LEI, the same name (compared as above) or the same website host. Nothing is deleted or merged: the
person decides.

    python add_companies.py [--commit] [--limit 200]

Dry run prints what it would do. Env: AIRTABLE_TOKEN, AIRTABLE_BASE; SUPABASE_DB_URL for the requests (without
it only the hand-added rows are handled); AIRTABLE_QA_TOKEN for the run record (QA_COMMIT=true writes it).
Prints ids and counts, never the requesters.
"""
import argparse, hashlib, os, re, sys, unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "sa", "ag", "nv", "bv", "se",
         "spa", "ab", "asa", "oyj", "as", "gmbh", "llc", "lp", "holdings", "holding", "group", "the", "sab", "de", "cv", "tbk", "pt",
         "bhd", "berhad", "kk", "pjsc", "ojsc", "jsc", "sae", "saog", "psc"}
LEI_RE = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")


def norm_name(name):
    """The name as compared: no accents, lower case, letters and digits only, legal endings dropped.
    "Nestlé S.A." and "NESTLE SA" are the same company; "Target" and "Target Corporation" too."""
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn").lower().replace(".", "")      # "S.A." is "sa"
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]
    kept = [w for w in words if w not in LEGAL]
    return " ".join(kept or words)


def norm_lei(lei):
    """The LEI in capitals when it has the form of one (20 characters, two check digits), else ''."""
    s = re.sub(r"\s+", "", (lei or "")).upper()
    return s if LEI_RE.match(s) else ""


def host_of(url):
    """The website's host without www, '' when there is none. A link without a scheme is read as https."""
    u = (url or "").strip()
    if not u:
        return ""
    h = (urlparse(u if "://" in u else "https://" + u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def company_id(name, lei):
    """lanterN's id for a company with no WBA id: from the LEI when there is one, else from the name."""
    key = norm_lei(lei) or norm_name(name)
    return "LN_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8] if key else ""


def index_existing(records):
    """What the table already holds, to compare a new row with: {lei: id}, {name: id}, {host: id}, {ids}."""
    by_lei, by_name, by_host, ids = {}, {}, {}, set()
    for rec in records:
        f = rec.get("fields", {})
        w = (f.get("wba_id") or "").strip()
        if not w:
            continue
        ids.add(w)
        if norm_lei(f.get("lei")):
            by_lei.setdefault(norm_lei(f.get("lei")), w)
        if norm_name(f.get("company_name")):
            by_name.setdefault(norm_name(f.get("company_name")), w)
        if host_of(f.get("website")):
            by_host.setdefault(host_of(f.get("website")), w)
    return by_lei, by_name, by_host, ids


def decide(name, website, lei, existing):
    """('add', id, note) or ('skip', None, why). existing: index_existing(...); it is updated when a row is added,
    so two rows of the same company in one batch give one company."""
    by_lei, by_name, by_host, ids = existing
    name = (name or "").strip()
    if len(norm_name(name)) < 2:
        return "skip", None, "no company name"
    l, n, h = norm_lei(lei), norm_name(name), host_of(website)
    if (lei or "").strip() and not l:
        return "skip", None, "the LEI does not have the form of one (20 characters): correct it or empty it"
    for found, why in ((by_lei.get(l) if l else None, "the same LEI"), (by_name.get(n), "the same name"), (by_host.get(h) if h else None, "the same website")):
        if found:
            return "skip", None, f"already a company: {found} has {why}"
    cid = company_id(name, l)
    if cid in ids:
        return "skip", None, f"already a company: {cid}"
    ids.add(cid); by_name[n] = cid
    if l:
        by_lei[l] = cid
    if h:
        by_host[h] = cid
    return "add", cid, f"added as {cid}" + ("" if h else "; no website given, so its pages cannot be searched for: fill website or add_link")


def clean(fields):
    """The fields with a value (Airtable is not sent empty ones on a new row)."""
    return {k: v for k, v in fields.items() if v not in (None, "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=200, help="rows and requests this run")
    ap.add_argument("--ids-file", default="", help="write the new ids here, comma-separated (for discover_pages.py --wba)")
    a = ap.parse_args()
    for v in ("AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import qa
    from urllib.parse import quote
    from base_health import sweep, API
    from monitor_core import airtable_request
    BASE = os.environ["AIRTABLE_BASE"]
    headers = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("add_companies.py")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    c = {"hand rows": 0, "added": 0, "not added": 0, "requests copied": 0, "requests approved": 0}
    new_ids = []

    records = list(sweep("companies", ["wba_id", "company_name", "website", "lei", "intake_note"]))
    existing = index_existing(records)

    # 1. rows added by hand: a name, no id, not already answered
    todo = [r for r in records if not (r["fields"].get("wba_id") or "").strip() and (r["fields"].get("company_name") or "").strip()
            and not (r["fields"].get("intake_note") or "").startswith("not added")][:a.limit]
    c["hand rows"] = len(todo)
    for rec in todo:
        f = rec["fields"]
        what, cid, note = decide(f.get("company_name"), f.get("website"), f.get("lei"), existing)
        fields = {"intake_note": f"{note} ({today})"} if what == "add" else {"intake_note": f"not added: {note} ({today})"}
        if what == "add":
            fields["wba_id"] = cid; new_ids.append(cid); c["added"] += 1
        else:
            c["not added"] += 1
        print(f"  hand row: {what}{' ' + cid if cid else ''}", flush=True)
        if a.commit:
            airtable_request("PATCH", f"{API}/{BASE}/{quote('companies')}/{rec['id']}", headers, payload={"fields": fields})

    # 2 and 3. requests from the page
    if os.environ.get("SUPABASE_DB_URL"):
        import evidence_db
        with evidence_db.connect() as conn:
            cur = conn.cursor()
            cur.execute("select to_regclass('public.company_requests') is not null")
            if cur.fetchone()[0]:
                cur.execute("select id, name, website, lei, note, at::date from public.company_requests where status = 'new' order by id limit %s", (a.limit,))
                for rid, name, website, lei, note, at in cur.fetchall():
                    c["requests copied"] += 1
                    print(f"  request {rid}: copied to company_requests", flush=True)
                    if a.commit:
                        airtable_request("POST", f"{API}/{BASE}/{quote('company_requests')}", headers, payload={"fields": clean({
                            "request_id": f"REQ_{rid}", "company_name": name, "website": website, "lei": lei,
                            "note": note, "requested_on": str(at), "status": "waiting"})})
                        cur.execute("update public.company_requests set status = 'waiting' where id = %s and status = 'new'", (rid,))
                        conn.commit()
                for rec in sweep("company_requests", ["request_id", "company_name", "website", "lei", "approve", "status"]):
                    f = rec.get("fields", {})
                    if not f.get("approve") or (f.get("status") or "") != "waiting":
                        continue
                    what, cid, note = decide(f.get("company_name"), f.get("website"), f.get("lei"), existing)
                    c["requests approved"] += 1
                    c["added" if what == "add" else "not added"] += 1
                    print(f"  approved request {f.get('request_id')}: {what}{' ' + cid if cid else ''}", flush=True)
                    if what == "add":
                        new_ids.append(cid)
                    if a.commit:
                        if what == "add":
                            airtable_request("POST", f"{API}/{BASE}/{quote('companies')}", headers, payload={"fields": clean({
                                "wba_id": cid, "company_name": (f.get("company_name") or "").strip(), "website": f.get("website"),
                                "lei": norm_lei(f.get("lei")), "intake_note": f"{note}, asked for in lanterN ({today})"})})
                        airtable_request("PATCH", f"{API}/{BASE}/{quote('company_requests')}/{rec['id']}", headers,
                                         payload={"fields": {"status": "added" if what == "add" else "not added", "result": f"{note} ({today})"}})
                        m = re.match(r"^REQ_(\d+)$", f.get("request_id") or "")
                        if m:
                            cur.execute("update public.company_requests set status = %s, result = %s where id = %s",
                                        ("added" if what == "add" else "not added", note, int(m.group(1))))
                            conn.commit()
    else:
        print("  SUPABASE_DB_URL not set: the requests from the page are not handled this run", flush=True)

    if a.ids_file:
        open(a.ids_file, "w").write(",".join(new_ids))
    print(f"{c['hand rows']} rows added by hand, {c['requests copied']} requests copied, {c['requests approved']} approved: "
          f"{c['added']} companies added, {c['not added']} not added" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=c["hand rows"] + c["requests copied"] + c["requests approved"], rows_out=c["added"] if a.commit else 0,
               counters=c, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
