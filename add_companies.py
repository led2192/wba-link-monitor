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
  2. From the page or the browser extension (since 15 Nov 2026). A signed-in person adds a company and it exists
     at once in the evidence database, with its id (public.request_company; the owner: "that I do not have to wait
     for someone to confirm it"). This script gives it its row of `companies` here, at the watch's next side jobs
     (about every 15 minutes), so that its documents have a company to go to. Clear duplicates were refused when
     the person asked; one that only this table can see (a company added to it since last night's copy) is settled
     here: the company made is withdrawn and the files sent for it go to the company we had.
  3. Rejected companies. The owner accepts or rejects in lanterN's admin panel. A rejected company's row here says
     so in intake_note, its documents are discarded with a reason and its pages are no longer read. Nothing is
     deleted.
  4. Requests made before 15 Nov 2026, which waited for the `approve` box of the Airtable table
     `company_requests`: a ticked one still becomes a company.

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
# 26 Nov 2026, the owner: "everything that is not SDG2000 must not be in Airtable". A company a person or the catalogue
# adds exists in the evidence database at once (public.request_company) and gets NO row here: its request stays
# 'new' and the files sent for it wait (browser_uploads.py treats them as waiting, not as orphans) until lantrNa's own
# lane for these companies reads them without Airtable. Companies added before that day keep the rows they have.
LN_IN_AIRTABLE = False

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


def settle(name, website, lei, cid, existing):
    """A company a person added in lanterN, which already has its id there. ('add', cid, note): give it its row;
    ('done', cid, note): the row is there (a rerun); ('merge', other id, note): this table holds the same company
    under another id, so the one made is withdrawn."""
    if cid in existing[3]:
        return "done", cid, f"added as {cid}"
    what, got, note = decide(name, website, lei, existing)
    if what == "add":
        if got != cid:                      # the id the database gave stands (the two are computed the same way)
            existing[3].discard(got); existing[3].add(cid)
            for index in existing[:3]:
                for k, v in list(index.items()):
                    if v == got:
                        index[k] = cid
        return "add", cid, f"added as {cid}" + note[len(f"added as {got}"):]
    m = re.search(r"already a company: (\S+)", note)
    return ("merge", m.group(1), note) if m else ("add", cid, f"added as {cid}; {note}")


REJECT_REASON = "Wrong company / misattributed"


def rejection_patches(cid, library_rows, page_rows):
    """What a rejected company changes in the library: [(table, record id, fields)]. Its documents are discarded
    with a reason (never deleted) and its pages are no longer read. Rows already discarded or not read are left."""
    out = [("report_library", r["id"], {"discard": True, "discard_reason": REJECT_REASON})
           for r in library_rows if (r.get("fields", {}).get("wba_id") or "") == cid and not r["fields"].get("discard")]
    out += [("monitored_links", r["id"], {"monitor": False})
            for r in page_rows if (r.get("fields", {}).get("wba_id") or "") == cid and r["fields"].get("monitor")]
    return out


def restore_patches(cid, library_rows, page_rows):
    """A rejection undone: the documents the rejection discarded (its reason, no other) come back and the company's
    pages are read again."""
    out = [("report_library", r["id"], {"discard": False, "discard_reason": None})
           for r in library_rows if (r.get("fields", {}).get("wba_id") or "") == cid and r["fields"].get("discard")
           and _name(r["fields"].get("discard_reason")) == REJECT_REASON]
    out += [("monitored_links", r["id"], {"monitor": True})
            for r in page_rows if (r.get("fields", {}).get("wba_id") or "") == cid and not r["fields"].get("monitor")]
    return out


def _name(v):
    return v.get("name") if isinstance(v, dict) else v


EDIT_FIELDS = {"name": "company_name", "website": "website", "lei": "lei", "isin": "isin", "industry": "wba_industry",
               "country": "hq_country", "region": "hq_region"}


def edit_fields(fields):
    """The owner's correction of a company (admin panel) as the library's fields. An emptied value empties the field."""
    return {EDIT_FIELDS[k]: (v if v not in ("", None) else None) for k, v in (fields or {}).items() if k in EDIT_FIELDS}


def isin_of(note):
    """The ISIN the extension puts in a request's note ("ISIN ES0109067019"), or ''."""
    m = re.search(r"\bISIN ([A-Z]{2}[A-Z0-9]{9}[0-9])\b", note or "")
    return m.group(1) if m else ""


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
    c = {"hand rows": 0, "added": 0, "not added": 0, "requests copied": 0, "requests approved": 0, "made by people": 0, "withdrawn": 0, "rejected": 0, "restored": 0, "edited": 0, "kept out of Airtable": 0}
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
                cur.execute("select count(*) from information_schema.columns where table_schema = 'public' and table_name = 'company_requests' and column_name = 'wba_id'")
                at_once = cur.fetchone()[0] > 0
                made = []
                if at_once:
                    # 'waiting' too: a watch still running the code of before 15 Nov 2026 copied such a request to the old Airtable table and left it waiting
                    cur.execute("select id, name, website, lei, wba_id, note from public.company_requests where status in ('new', 'waiting') and wba_id is not null "
                                "and review is distinct from 'rejected' order by id limit %s", (a.limit,))
                    made = cur.fetchall()
                for rid, name, website, lei, cid, req_note in made:
                    what, got, note = settle(name, website, lei, cid, existing)
                    c["made by people"] += 1
                    print(f"  company {cid} (request {rid}): {what}{' into ' + got if what == 'merge' else ''}", flush=True)
                    if what == "add" and not LN_IN_AIRTABLE:
                        c["kept out of Airtable"] += 1; continue          # it exists in the evidence database; nothing of it is written here
                    if what == "add":
                        new_ids.append(cid); c["added"] += 1
                    elif what == "merge":
                        c["withdrawn"] += 1
                    if not a.commit:
                        continue
                    if what == "add":
                        airtable_request("POST", f"{API}/{BASE}/{quote('companies')}", headers, payload={"fields": clean({
                            "wba_id": cid, "company_name": (name or "").strip(), "website": website,
                            "lei": norm_lei(lei), "isin": isin_of(req_note), "intake_note": f"{note}, added by a person in lanterN ({today})"})})
                    if what in ("add", "done"):
                        cur.execute("update public.company_requests set status = 'added', result = %s where id = %s", (note, rid))
                        # files sent before the row existed and set aside as having no company: they wait again
                        cur.execute("update public.browser_uploads set status = 'stored', result = null where wba_id = %s and status = 'orphan' "
                                    "and result = 'the company is not in the companies table'", (cid,))
                    else:
                        # the same company under another id: the files sent go to it (unless it has the same file),
                        # the company made leaves the list, the request says why
                        cur.execute("update public.browser_uploads b set wba_id = %s where b.wba_id = %s and not exists "
                                    "(select 1 from public.browser_uploads o where o.wba_id = %s and o.sha256 = b.sha256)", (got, cid, got))
                        cur.execute("delete from public.companies where wba_id = %s and wba_id like 'LN\\_%%'", (cid,))
                        cur.execute("update public.company_requests set status = 'not added', result = %s, review = 'rejected', "
                                    "review_note = %s, reviewed_at = now() where id = %s", (note, note[:300], rid))
                    conn.commit()
                # companies the owner rejected in the admin panel: the library's side
                rejected = []
                if at_once:
                    cur.execute("select id, wba_id, review_note from public.company_requests where review = 'rejected' and status in ('new', 'added') "
                                "and wba_id like 'LN\\_%%' order by id limit %s", (a.limit,))
                    rejected = cur.fetchall()
                for rid, cid, why in rejected:
                    safe = cid.replace("'", "")
                    patches = rejection_patches(cid, list(sweep("report_library", ["wba_id", "discard"], "{wba_id}='%s'" % safe)),
                                                list(sweep("monitored_links", ["wba_id", "monitor"], "{wba_id}='%s'" % safe)))
                    row = next((r for r in records if (r["fields"].get("wba_id") or "").strip() == cid), None)
                    c["rejected"] += 1
                    print(f"  company {cid} (request {rid}) rejected: {sum(1 for p in patches if p[0] == 'report_library')} documents discarded, "
                          f"{sum(1 for p in patches if p[0] == 'monitored_links')} pages no longer read", flush=True)
                    if not a.commit:
                        continue
                    for table, rec_id, fields in patches:
                        airtable_request("PATCH", f"{API}/{BASE}/{quote(table)}/{rec_id}", headers, payload={"fields": fields})
                    if row:
                        airtable_request("PATCH", f"{API}/{BASE}/{quote('companies')}/{row['id']}", headers,
                                         payload={"fields": {"intake_note": f"rejected in lanterN ({today})" + (f": {why}" if why else "")}})
                    cur.execute("update public.company_requests set status = 'rejected' where id = %s", (rid,))
                    conn.commit()
                # a rejection the owner undid: the documents and pages it set aside come back
                restored = []
                if at_once:
                    cur.execute("select id, wba_id from public.company_requests where status = 'restore' and wba_id like 'LN\\_%%' order by id limit %s", (a.limit,))
                    restored = cur.fetchall()
                for rid, cid in restored:
                    safe = cid.replace("'", "")
                    patches = restore_patches(cid, list(sweep("report_library", ["wba_id", "discard", "discard_reason"], "{wba_id}='%s'" % safe)),
                                              list(sweep("monitored_links", ["wba_id", "monitor"], "{wba_id}='%s'" % safe)))
                    row = next((r for r in records if (r["fields"].get("wba_id") or "").strip() == cid), None)
                    c["restored"] += 1
                    print(f"  company {cid} (request {rid}) restored: {len(patches)} rows brought back", flush=True)
                    if not a.commit:
                        continue
                    for table, rec_id, fields in patches:
                        airtable_request("PATCH", f"{API}/{BASE}/{quote(table)}/{rec_id}", headers, payload={"fields": fields})
                    if row:
                        airtable_request("PATCH", f"{API}/{BASE}/{quote('companies')}/{row['id']}", headers,
                                         payload={"fields": {"intake_note": f"added as {cid}, restored after a rejection ({today})"}})
                    cur.execute("update public.company_requests set status = 'added' where id = %s", (rid,))
                    conn.commit()
                # the owner's corrections of a company (admin panel): into the library's companies table
                cur.execute("select to_regclass('public.company_edits') is not null")
                edits = []
                if cur.fetchone()[0]:
                    cur.execute("select id, wba_id, fields from public.company_edits where applied is null order by id limit %s", (a.limit,))
                    edits = cur.fetchall()
                if edits:
                    by_id = {(r["fields"].get("wba_id") or "").strip(): r["id"] for r in sweep("companies", ["wba_id"])}      # read again: rows made above are in it
                for eid, cid, fields in edits:
                    rec_id, patch = by_id.get(cid), edit_fields(fields)
                    print(f"  edit {eid} of {cid}: {', '.join(sorted(patch)) or 'nothing'}{'' if rec_id else ' (no row yet: next run)'}", flush=True)
                    if not rec_id:
                        continue
                    c["edited"] += 1
                    if a.commit:
                        if patch:
                            airtable_request("PATCH", f"{API}/{BASE}/{quote('companies')}/{rec_id}", headers, payload={"fields": patch})
                        cur.execute("update public.company_edits set applied = now() where id = %s", (eid,))
                        conn.commit()
                cur.execute("select id, name, website, lei, note, at::date from public.company_requests where status = 'new' "
                            + ("and wba_id is null " if at_once else "") + "order by id limit %s", (a.limit,))
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
                        if what == "add":
                            # the search's own list of companies (load_companies.py fills it nightly) learns the
                            # company now, so its links and documents can be added at once
                            cur.execute("select to_regclass('public.companies') is not null")
                            if cur.fetchone()[0]:
                                cur.execute("insert into public.companies (wba_id, name, website) values (%s, %s, %s) on conflict (wba_id) do nothing",
                                            (cid, (f.get("company_name") or "").strip(), f.get("website") or None))
                        m = re.match(r"^REQ_(\d+)$", f.get("request_id") or "")
                        if m:
                            cur.execute("update public.company_requests set status = %s, result = %s where id = %s",
                                        ("added" if what == "add" else "not added", note, int(m.group(1))))
                            conn.commit()
    else:
        print("  SUPABASE_DB_URL not set: the requests from the page are not handled this run", flush=True)

    if a.ids_file:
        open(a.ids_file, "w").write(",".join(new_ids))
    print(f"{c['made by people']} companies made by people ({c['kept out of Airtable']} kept out of Airtable, {c['withdrawn']} withdrawn as duplicates), {c['rejected']} rejected by the owner, {c['restored']} restored, {c['edited']} corrected; "
          f"{c['hand rows']} rows added by hand, {c['requests copied']} requests copied, {c['requests approved']} approved: "
          f"{c['added']} companies added, {c['not added']} not added" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=c["hand rows"] + c["requests copied"] + c["requests approved"], rows_out=c["added"] if a.commit else 0,
               counters=c, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
