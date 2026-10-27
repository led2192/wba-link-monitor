#!/usr/bin/env python3
"""
contacts.py: the email addresses companies publish in their own key reports.

Finding someone to write to at a company is a recurring struggle; the reports themselves usually
print an investor relations, sustainability or general mailbox, and sometimes a named person (owner's
idea, 27 Oct 2026). This script reads the full text (the text store) of every key report (report_library
rows with a key_role: the latest and previous wave's financial and sustainability reports), finds every
email address, and keeps the ones on the company's own domains:
  - the registered domains of its website (companies.website) and of its watched pages, minus hosts
    watched for more than MAX_COMPANIES_PER_DOMAIN companies (a group site, a government domain, a
    third-party database), and the domain the report itself was downloaded from when it is not shared.
Addresses on other domains (auditors, agencies, printers, banks) are counted, never kept.

For each company and address, one row in company_contacts:
  kind         from the mailbox name (KINDS): investor relations, sustainability, media, general,
               ethics and compliance, privacy, people, personal (a person's name), other;
  personal     the address names a person (first.last@, f.last@): personal data the company published
               for contact; the role mailboxes are preferred;
  evidence     "p.<page>: <the sentence around it>" from the newest report that prints it;
  source_document, source_year, reports_with_it, first_seen, last_seen.
Rows are never deleted: an address the newer reports no longer print keeps its row and an older
last_seen.

Does the domain accept mail? For each distinct domain, its MX records are read through DNS over HTTPS
(Google's resolver; no email is sent): domain_mail = accepts (mail servers published), none (no mail
servers, a null MX or no such domain: the address cannot receive anything) or unknown (no answer), with
domain_checked. That is as far as a check without sending goes: whether the mailbox itself exists cannot
be told from outside, because most companies' mail servers accept every address and refuse the probe.

Dry run by default (prints counts and a sample, writes contacts.md); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, TEXT_STORE_REPO, TEXT_STORE_TOKEN. Options: --wba ids, --limit N.
"""
import argparse, collections, gzip, json, os, re, sys, tempfile, time
from datetime import date
from urllib.parse import quote, urlsplit

API = "https://api.airtable.com/v0"
TABLE = "company_contacts"
MAX_COMPANIES_PER_DOMAIN = 2
EMAIL = re.compile(r"(?<![\w.+%-])([a-z0-9][a-z0-9._%+-]{0,63})@((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])",
                   re.I)
NOT_TLD = {"png", "jpg", "jpeg", "gif", "svg", "webp", "pdf", "js", "css", "html", "htm"}
# the mailbox name decides the kind; the first matching kind wins
KINDS = [
    ("investor relations", r"(^|[._-])(ir|irs|investors?|investor[._-]?relations?|investorrelations|shareholders?|"
                           r"aktionaer\w*|actionnaires?|accionistas?|ri|relacoes|ird)([._-]|$)|^ir\w{0,3}$|investor"),
    ("sustainability", r"sustainab|sostenib|sustentab|durab|nachhaltig|duurza|responsab|responsib|verantwortung|"
                       r"climate|environment|umwelt|impact|ethical[._-]?sourcing|"
                       r"(^|[._-])(esg|csr|cr|tcfd|gri|ehs|hse)([._-]|$)|^(esg|csr)"),
    ("media", r"press|media|presse|prensa|imprensa|news|communication|kommunikation|comunica|pr$|^pr[._-]|^humas|"
              r"(^|[._-])humas([._-]|$)|public[._-]?relations|^corpcomm|corp[._-]?comm"),
    ("ethics and compliance", r"compliance|ethic|ethik|whistle|speak[._-]?up|integrity|integrit|ombuds|"
                              r"hotline|concerns?$|reporting[._-]?line|denuncia|governance"),
    ("privacy", r"privacy|dpo|data[._-]?protection|datenschutz|gdpr|privacidad|privacidade"),
    ("people", r"^hr$|^hr[._-]|human[._-]?resources|careers?|recruit|jobs?$|talent|(^|[._-])sdm([._-]|$)"),
    ("general", r"^(info|contact|contacts?us|enquir\w*|inquir\w*|hello|mail|office|general|"
                r"kontakt|contato|contacto|informacion|service|customer\w*|webmaster|reception)$|^info|"
                r"cosec|secretar|admin|office|desk|team$|support|service|(^|[._-])group([._-]|$)|"
                r"call|care|halo|helpdesk|help|cs$|^cs[._-]|customer|client|feedback|complain|"
                r"legal|procure|purchas|supplier|vendor|tender|sales|marketing|finance|treasury|tax|audit|"
                r"security|cyber|shareholder[._-]?services|registr|board|governance|corporate|doc$|docs|"
                r"(^|[._-])(ca|cfo|ceo|coo)([._-]|$)"),
]
KINDS = [(k, re.compile(rx, re.I)) for k, rx in KINDS]


def kind_of(local):
    """(kind, personal) from the part before the @."""
    l = local.lower()
    if not re.search(r"[a-z]{2}", l):                                   # 2645.7085@: a number, not a mailbox
        return "other", False
    for k, rx in KINDS:
        if rx.search(l):
            return k, False
    if re.fullmatch(r"[a-z]{2,}([._-][a-z]{2,}){1,2}", l):            # first.last, first_last, first.middle.last
        return "personal", True
    return "other", False


def emails_in(pages):
    """{email: (page, sentence)} of a document's pages [(page, text)], first occurrence of each."""
    out = {}
    for p, text in sorted(pages):
        if not text or "@" not in text:
            continue
        for m in EMAIL.finditer(text):
            local, dom = m.group(1).strip("._-"), m.group(2).lower().strip(".")
            if not local or dom.rsplit(".", 1)[-1] in NOT_TLD:
                continue
            if not re.search(r"[a-z]{2}", local.lower()):
                continue
            email = f"{local.lower()}@{dom}"
            if email not in out:
                lo, hi = max(0, m.start() - 160), min(len(text), m.end() + 120)
                out[email] = (p, re.sub(r"\s+", " ", text[lo:hi]).strip())
    return out


# ---------------------------------------------------------------------------------------------- Airtable
def at_get(table, fields, formula=None):
    from monitor_core import airtable_request
    h = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields] + \
             ([("filterByFormula", formula)] if formula else [])
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{os.environ['AIRTABLE_BASE']}/{quote(table)}", h, params=p).json()
        out += j.get("records", []); offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.12)


def at_write(table, method, records):
    from monitor_core import airtable_request
    h = {"Authorization": f"Bearer {os.environ['AIRTABLE_TOKEN']}", "Content-Type": "application/json"}
    for i in range(0, len(records), 10):
        airtable_request(method, f"{API}/{os.environ['AIRTABLE_BASE']}/{quote(table)}", h, {"records": records[i:i + 10]})
        time.sleep(0.22)


DNS_API = "https://dns.google/resolve"


def domain_accepts_mail(domain, sess):
    """'accepts', 'none' or 'unknown' from the domain's MX records (DNS over HTTPS; nothing is sent)."""
    try:
        r = sess.get(DNS_API, params={"name": domain, "type": "MX"}, timeout=20)
        if r.status_code != 200:
            return "unknown"
        j = r.json()
    except Exception:
        return "unknown"
    status = j.get("Status")
    if status == 3:
        return "none"                                           # no such domain
    if status != 0:
        return "unknown"
    hosts = [a["data"].split()[-1].rstrip(".") for a in j.get("Answer", [])
             if a.get("type") == 15 and isinstance(a.get("data"), str) and a["data"].split()]
    if hosts and all(h == "" for h in hosts):
        return "none"                                           # "null MX" (RFC 7505): mail refused on purpose
    return "accepts" if hosts else "none"                       # no mail servers published for the domain


def check_domains(domains, sess, pause=0.1):
    """{domain: verdict} for every domain, one DNS query each."""
    out = {}
    for d in sorted(domains):
        out[d] = domain_accepts_mail(d, sess)
        time.sleep(pause)
    return out


def company_domains(companies, pages):
    """({wba_id: set(registered domains that are the company's own)}, {domain: set(wba_ids watching it)})."""
    from monitor_core import reg_domain
    owners = collections.defaultdict(set)
    per = collections.defaultdict(set)
    for r in pages:
        f = r["fields"]; w = (f.get("wba_id") or "").strip(); u = f.get("url") or ""
        if w and u:
            d = reg_domain(u); owners[d].add(w); per[w].add(d)
    out = {}
    for w, rec in companies.items():
        site = (rec["fields"].get("website") or "").strip()
        ds = {d for d in per.get(w, ()) if d and len(owners[d]) <= MAX_COMPANIES_PER_DOMAIN}
        if site:
            ds.add(reg_domain(site if site.startswith("http") else "https://" + site))
        out[w] = {d for d in ds if d}
    return out, owners


def own_domains(w, mine, owners, document_url):
    """The company's own domains, plus the domain its report was downloaded from when no other company's
    pages use it."""
    from monitor_core import reg_domain
    own = set(mine.get(w, ()))
    d = reg_domain(document_url) if document_url else ""
    if d and owners.get(d, set()) <= {w}:
        own.add(d)
    return own


def merge(found, today, existing, domain_mail=None):
    """Rows to write from found {(wba, email): [(year, record id of the document, page, sentence)]} and the
    rows already in the table {contact_key: (record id, fields)}; domain_mail {domain: verdict} when the
    domains were checked this run. Returns (creates, updates)."""
    creates, updates = [], []
    domain_mail = domain_mail or {}
    for (w, email), hits in sorted(found.items()):
        hits.sort(key=lambda h: (-(h[0] or 0), h[1]))
        year, doc_rec, page, sentence = hits[0]
        kind, personal = kind_of(email.split("@", 1)[0])
        fields = {"email": email, "wba_id": w, "kind": kind, "personal": personal,
                  "evidence": f"p.{page}: {sentence}"[:1000], "source_document": [doc_rec],
                  "source_year": year or None, "reports_with_it": len({h[1] for h in hits}), "last_seen": today}
        verdict = domain_mail.get(email.split("@", 1)[1])
        if verdict:
            fields["domain_mail"] = verdict
            fields["domain_checked"] = today
        key = f"{w}|{email}"
        if key in existing:
            rid, old = existing[key]
            if any(old.get(k) != v for k, v in fields.items() if k != "last_seen") or old.get("last_seen") != today:
                updates.append({"id": rid, "fields": fields})
        else:
            creates.append({"fields": dict(fields, contact_key=key, first_seen=today)})
    return creates, updates


# ---------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--wba", default="", help="comma-separated wba_ids")
    ap.add_argument("--limit", type=int, default=0, help="at most this many documents (0 = all)")
    ap.add_argument("--no-dns", action="store_true", help="skip the domain mail check")
    a = ap.parse_args()
    if not (os.environ.get("AIRTABLE_TOKEN") and os.environ.get("AIRTABLE_BASE")):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import qa, text_core as tc
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("contacts.py")
    wbas = {w.strip() for w in a.wba.split(",") if w.strip()}
    comps = {r["fields"]["wba_id"]: r for r in at_get("companies", ["wba_id", "company_name", "website"])
             if r["fields"].get("wba_id") and (not wbas or r["fields"]["wba_id"] in wbas)}
    mine, owners = company_domains(comps, at_get("monitored_links", ["wba_id", "url"]))
    docs = {}
    for r in at_get("report_library", ["library_id", "wba_id", "year", "document_url", "key_role"], "{key_role}!=''"):
        f = r["fields"]; w = (f.get("wba_id") or "").strip()
        if f.get("library_id") and w in comps:
            docs[f["library_id"]] = (r["id"], w, f.get("year") if isinstance(f.get("year"), (int, float)) else None,
                                     f.get("document_url") or "")
    print(f">>> {len(comps):,} companies, {len(docs):,} key reports", flush=True)

    store = tc.open_store()
    folder = tempfile.mkdtemp(prefix="ct-")
    names = [n for n in store.assets() if tc.part_of(n)[0]]
    docs_files, _ = tc.complete_parts(names)
    tc.sync_down(store, docs_files, folder)
    index = tc.load_docs_index(folder, docs_files)
    want = collections.defaultdict(set)
    for lid in docs:
        d = index.get(lid)
        if d and d.get("status") == "ok":
            want[d["part"]].add(lid)
    if a.limit:
        keep, n = collections.defaultdict(set), 0
        for p, ids in want.items():
            for lid in ids:
                if n < a.limit:
                    keep[p].add(lid); n += 1
        want = keep
    print(f">>> {sum(len(v) for v in want.values()):,} with full text in {len(want):,} parts", flush=True)

    from monitor_core import reg_domain
    stats = collections.Counter()
    found = collections.defaultdict(list)
    for i, (part, ids) in enumerate(sorted(want.items()), 1):
        name = f"pages-{part}.jsonl.gz"
        tc.sync_down(store, [name], folder)
        pages = collections.defaultdict(list)
        with gzip.open(os.path.join(folder, name), "rt", encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r["id"] in ids:
                    pages[r["id"]].append((r["p"], r.get("t") or ""))
        os.remove(os.path.join(folder, name))
        for lid, pg in pages.items():
            rec, w, year, url = docs[lid]
            own = own_domains(w, mine, owners, url)
            stats["documents read"] += 1
            got = emails_in(pg)
            if got:
                stats["documents with an address"] += 1
            for email, (p, sentence) in got.items():
                if reg_domain("https://" + email.split("@", 1)[1]) in own:
                    found[(w, email)].append((year, rec, p, sentence))
                    stats["own-domain mentions"] += 1
                else:
                    stats["other-domain addresses left out"] += 1
        if i % 25 == 0:
            print(f"  {i}/{len(want)} parts, {len(found):,} addresses so far", flush=True)

    existing = {r["fields"]["contact_key"]: (r["id"], r["fields"]) for r in
                at_get(TABLE, ["contact_key", "email", "wba_id", "kind", "personal", "evidence", "source_document",
                               "source_year", "reports_with_it", "last_seen", "domain_mail", "domain_checked"])
                if r["fields"].get("contact_key")}
    domain_mail = {}
    if not a.no_dns:
        import requests
        domains = {e.split("@", 1)[1] for _, e in found}
        domain_mail = check_domains(domains, requests.Session())
        verdicts = collections.Counter(domain_mail.values())
        stats.update({f"domains {k}": v for k, v in verdicts.items()})
        print(f">>> {len(domains):,} domains checked in DNS: {dict(verdicts)}", flush=True)
    creates, updates = merge(found, date.today().isoformat(), existing, domain_mail)
    kinds = collections.Counter(kind_of(e.split("@", 1)[0])[0] for _, e in found)
    n_comp = len({w for w, _ in found})
    md = [f"### Company contacts from key reports{'' if a.commit else ' (dry run)'}", "",
          f"{stats['documents read']:,} key reports read ({stats['documents with an address']:,} print an address). "
          f"{len(found):,} own-domain addresses for {n_comp:,} of {len(comps):,} companies; "
          f"{stats['other-domain addresses left out']:,} addresses on other domains left out.", "",
          "By kind: " + ", ".join(f"{k} {v:,}" for k, v in kinds.most_common()), "",
          f"Rows to create: {len(creates):,}; to update: {len(updates):,}.", ""]
    if domain_mail:
        v = collections.Counter(domain_mail.values())
        md += [f"Domains checked in DNS: {len(domain_mail):,}; accept mail {v['accepts']:,}, none {v['none']:,}, "
               f"unknown {v['unknown']:,}.", ""]
    sample = [c["fields"] for c in creates[:25]]
    if sample:
        md += ["| Company | Address | Kind | Year |", "| --- | --- | --- | --- |"]
        md += [f"| {f['wba_id']} | {f['email']} | {f['kind']} | {f.get('source_year') or ''} |" for f in sample]
    open("contacts.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)
    if a.commit:
        for c in creates:
            c["fields"]["company"] = [comps[c["fields"]["wba_id"]]["id"]]
        at_write(TABLE, "POST", creates)
        at_write(TABLE, "PATCH", updates)
        print(f">>> created {len(creates):,}, updated {len(updates):,}", flush=True)
    else:
        print(">>> dry run, nothing written.", flush=True)
    run.finish("ok", rows_in=stats["documents read"], rows_out=len(creates) + len(updates),
               counters={**dict(stats), "addresses": len(found), "companies": n_comp, "kinds": dict(kinds)})


if __name__ == "__main__":
    main()
