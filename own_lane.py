#!/usr/bin/env python3
"""
own_lane.py: the companies that are not SDG2000 are followed without Airtable.

The owner's rule of 26 Nov 2026: the Disclosure Library (Airtable) is for the SDG2000 list, whose companies have ids
PT_. Every other company (ids LN_: added by a person, or from the catalogue) lives in the evidence database only.
Until now the robot that watches report pages and fetches documents worked on Airtable alone, so such a company
could be added and nothing was read for it. This script is the same work for those companies, written to the
evidence database and nowhere else. Four steps, each repeatable:

  1. take     a company that waits (public.company_requests, status new or waiting, with its id, not rejected) enters
              the lane (public.own_companies). The pages people asked for with the extension (public.page_scans, status
              new) become its pages.
  2. find     for a company with a website and no page yet, the report pages one hop from the home page, as
              discover_pages.py finds them for the list (the same reading: active_search and discover_pages.explore).
              Asked again after RETRY_DAYS when none was found.
  3. scan     each watched page is read once a day; the documents it links (monitor_core.doc_links, the reading
              the robot uses) are registered in public.own_docs under the id the library would give them (ids.doc_id).
  4. read     a registered document is fetched, its text read page by page (build_fulltext.pdf_pages) and written to
              public.documents and public.pages, where every search finds it. Type, year and title come from the
              file name and the first page (classify_library, date_documents), as for a file dropped by hand. The
              files people sent with the extension for these companies (public.browser_uploads, status stored) are
              read from the storage the same way.

The file itself is not kept: the document's address is, and the search links to it. A site that refuses the robot is
not worked around; its documents come in through the extension.

    python own_lane.py [--commit] [--minutes 100] [--docs 600] [--only LN_xxxxxxxx]

Dry run by default: reads and counts, fetches nothing from a company's site and writes nothing. Stops reading
documents when the evidence database reaches MAX_DB_GB. Prints ids and counts only, never a name or an address.
Env: SUPABASE_DB_URL; SUPABASE_URL and SUPABASE_SERVICE_KEY for the files of the extension (without them they wait).
"""
import argparse, collections, io, os, sys, time
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote

MAX_DB_GB = float(os.environ.get("MAX_DB_GB") or 140)
MAX_MB = 80                  # a larger file is registered as too_big and not read
RETRY_DAYS = 7               # a company with no page found is searched again after this
SCAN_HOURS = 20
MAX_DOCS_A_PAGE = 400
PAUSE = 1.0                  # seconds between two requests to company sites
RETRY_STATUS = ("download_fail", "truncated")
WANT = ["reports_hub", "sustainability_page"]


def home_of(website):
    """The address to start from: the website as given, with https when it has no scheme; '' when there is none."""
    w = (website or "").strip()
    if not w or "." not in w:
        return ""
    return w if w.lower().startswith(("http://", "https://")) else "https://" + w


def is_pdf_link(url):
    """A link the lane reads now: a PDF by its path. Other document endpoints are left for a later step."""
    return urlsplit(url or "").path.lower().endswith(".pdf")


def new_docs(wba, links, known):
    """[(library_id, url, text)] for the document links of a page (monitor_core.doc_links: {key: (url, text)}) that
    are PDFs and not registered yet. The id is the library's own (ids.doc_id), so a document can never be two rows."""
    from ids import doc_id
    out, seen = [], set()
    for _, (url, text) in sorted(links.items()):
        if not is_pdf_link(url):
            continue
        lid = doc_id(wba, url)
        if lid in known or lid in seen:
            continue
        seen.add(lid); out.append((lid, url, (text or "").strip()[:300]))
    return out[:MAX_DOCS_A_PAGE]


def describe(url, link_text, first_page, filename=None):
    """(type or None, year or None, label) of a document from its file name, its link's text and its first page."""
    from classify_library import classify_from_file, title_from
    from date_documents import decide
    name = filename or unquote(urlsplit(url or "").path.rsplit("/", 1)[-1])
    cover = first_page or ""
    typ = classify_from_file(name, cover)[0] or (classify_from_file((link_text or "").replace(" ", "-") + ".pdf", "")[0] if link_text else "")
    year = decide(url or "", name, cover)[0]
    label = (link_text or "").strip() or title_from(name, cover) or name
    return (typ or None), (int(year) if str(year).isdigit() else None), label[:300]


def document_row(lid, wba, company, url, typ, year, label, status, n_pages, chars, now):
    """The public.documents row, in load_supabase.upsert_documents' order."""
    return (lid, None, wba, company, typ, year, None, label, url, n_pages, chars, status, "own", now, None, now[:10])


def fetch_doc(sess, url, ua, max_bytes=MAX_MB << 20, timeout=90):
    """(kind, bytes): ok | too_big | not_pdf | truncated | download_fail | refused (the site said no: not tried again soon)."""
    try:
        with sess.get(url, headers={"User-Agent": ua}, timeout=timeout, stream=True, allow_redirects=True) as r:
            if r.status_code in (401, 403, 429):
                return "refused", b""
            if r.status_code >= 400:
                return "download_fail", b""
            clen = r.headers.get("Content-Length") or ""
            if clen.isdigit() and int(clen) > max_bytes:
                return "too_big", b""
            buf = io.BytesIO()
            for chunk in r.iter_content(1 << 18):
                buf.write(chunk)
                if buf.tell() > max_bytes:
                    return "too_big", b""
            data = buf.getvalue()
    except Exception:
        return "download_fail", b""
    if b"%PDF" not in data[:1024]:
        return "not_pdf", b""
    if (clen.isdigit() and len(data) < int(clen)) or b"%%EOF" not in data[-2048:]:
        return "truncated", b""
    return "ok", data


def read_pdf(data):
    """(status, [(page, text)], page count, characters) of a PDF's bytes."""
    from build_fulltext import pdf_pages
    status, pages, n = pdf_pages(data)
    return status, pages, n, sum(len(t) for _, t in pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--minutes", type=float, default=100)
    ap.add_argument("--docs", type=int, default=600)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("own_lane.py")
    c = collections.Counter(); deadline = time.time() + a.minutes * 60
    only = [w.strip() for w in a.only.split(",") if w.strip()]
    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select to_regclass('public.own_companies') is not null")
        if not cur.fetchone()[0]:
            print("public.own_companies is not there yet (supabase/search.sql): nothing to do."); run.finish("ok", rows_in=0, rows_out=0, notes="no table yet"); return 0

        # 1. take
        cur.execute("""select distinct q.wba_id from public.company_requests q join public.companies c on c.wba_id = q.wba_id
                        where q.status in ('new', 'waiting') and q.wba_id is not null and q.wba_id not like 'PT\\_%%' and q.review is distinct from 'rejected'
                          and not exists (select 1 from public.own_companies o where o.wba_id = q.wba_id)""")
        fresh = [r[0] for r in cur.fetchall()]; c["companies taken"] = len(fresh)
        if a.commit and fresh:
            cur.execute("insert into public.own_companies (wba_id) select unnest(%s::text[]) on conflict do nothing", (fresh,))
            cur.execute("update public.company_requests set status = 'added', result = 'followed by lantrNa itself, outside Airtable' where wba_id = any(%s) and status in ('new', 'waiting')", (fresh,))
            conn.commit()
        cur.execute("""select o.wba_id, c.name, c.website, o.searched_at from public.own_companies o join public.companies c on c.wba_id = o.wba_id
                        where not exists (select 1 from public.company_requests q where q.wba_id = o.wba_id and q.review = 'rejected')
                          and (%s::text[] is null or o.wba_id = any(%s)) order by o.since""", (only or None, only or None))
        companies = cur.fetchall(); names = {r[0]: r[1] for r in companies}; mine = list(names)
        cur.execute("""select p.id, p.wba_id, p.url from public.page_scans p where p.status = 'new' and p.wba_id = any(%s)""", (mine,))
        asked = cur.fetchall(); c["pages asked for by people"] = len(asked)
        if a.commit:
            from ids import link_id
            for sid, wba, url in asked:
                cur.execute("insert into public.own_pages (id, wba_id, url, kind, how) values (%s, %s, %s, 'page', 'person') on conflict (id) do update set status = 'watched'", (link_id(wba, url), wba, url))
                cur.execute("update public.page_scans set status = 'done', result = 'watched by lantrNa', done_at = now() where id = %s", (sid,))
            conn.commit()
        print(f"{len(companies):,} companies in the lane ({len(fresh)} new); {len(asked)} pages asked for by people.", flush=True)
        if not a.commit:
            cur.execute("select count(*) filter (where status = 'watched'), (select count(*) from public.own_docs where status = 'new') from public.own_pages")
            w, d = cur.fetchone(); print(f"Dry run: {w:,} pages watched, {d:,} documents registered and not read. Nothing fetched, nothing written.")
            run.finish("ok", rows_in=len(companies), rows_out=0, notes="dry run"); return 0

        import requests, active_search as AS, robot_identity as bot
        from discover_pages import explore
        from monitor_core import doc_links
        from ids import link_id
        from load_supabase import upsert_documents, load_pages
        sess = requests.Session()

        # 2. find
        for wba, name, website, searched in companies:
            if time.time() > deadline:
                break
            cur.execute("select count(*) from public.own_pages where wba_id = %s and status = 'watched'", (wba,))
            has_pages = cur.fetchone()[0]
            if searched and (has_pages or (datetime.now(timezone.utc) - searched).days < RETRY_DAYS):
                continue          # searched once and it has pages, or searched lately and none was found; a page a person gave does not spare the first search
            home = home_of(website); found, why = (None, "no website")
            if home:
                try:
                    found, why = explore(AS, sess, home, WANT, set())
                except Exception as e:
                    found, why = None, type(e).__name__
                time.sleep(PAUSE)
            for typ, url, note, ndocs in (found or []):
                cur.execute("insert into public.own_pages (id, wba_id, url, kind, how) values (%s, %s, %s, %s, 'found') on conflict (id) do nothing", (link_id(wba, url), wba, url, typ))
            c["companies searched"] += 1; c["companies with pages found"] += 1 if found else 0; c["pages found"] += len(found or [])
            cur.execute("update public.own_companies set searched_at = now(), note = %s where wba_id = %s", (None if found else f"no report page found ({why})"[:200], wba))
            conn.commit()

        # 3. scan
        cur.execute("""select p.id, p.wba_id, p.url from public.own_pages p where p.status = 'watched' and p.wba_id = any(%s)
                         and (p.scanned_at is null or p.scanned_at < now() - make_interval(hours => %s)) order by p.scanned_at nulls first""", (mine, SCAN_HOURS))
        for pid, wba, url in cur.fetchall():
            if time.time() > deadline:
                break
            r = AS.fetch(sess, url); time.sleep(PAUSE); c["pages scanned"] += 1
            if r is None or r.status_code >= 400:
                cur.execute("update public.own_pages set scanned_at = now(), last_result = %s, fails = fails + 1 where id = %s", (str(r.status_code if r is not None else "no response"), pid)); conn.commit()
                c["pages not answered"] += 1; continue
            try:
                links = doc_links(r.text, r.url)
            except Exception:
                links = {}
            cur.execute("select library_id from public.own_docs where wba_id = %s", (wba,)); known = {x[0] for x in cur.fetchall()}
            fresh_docs = new_docs(wba, links, known)
            cur.executemany("insert into public.own_docs (library_id, wba_id, url, link_text, found_on) values (%s, %s, %s, %s, %s) on conflict (library_id) do nothing",
                            [(lid, wba, u, t, pid) for lid, u, t in fresh_docs])
            cur.execute("update public.own_pages set scanned_at = now(), last_result = 'ok', fails = 0, docs = %s where id = %s", (sum(1 for _, (u, _) in links.items() if is_pdf_link(u)), pid))
            conn.commit(); c["documents registered"] += len(fresh_docs)

        # 4. read: the documents found, then the files people sent
        cur.execute("select pg_database_size(current_database()) / 1e9"); db_gb = float(cur.fetchone()[0])
        todo = []
        if db_gb < MAX_DB_GB:
            cur.execute("""select d.library_id, d.wba_id, d.url, d.link_text, null::text, null::bigint, null::text from public.own_docs d
                            where d.wba_id = any(%s) and (d.status = 'new' or (d.status = any(%s) and d.tries < 3 and d.tried_at < now() - interval '20 hours'))
                            order by d.first_seen desc limit %s""", (mine, list(RETRY_STATUS), a.docs))
            todo = cur.fetchall()
            storage = None
            if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
                from browser_uploads import Storage, library_id as upload_id
                storage = Storage(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"].strip(), sess)
                cur.execute("select b.id, b.wba_id, b.source_url, b.filename, b.bytes, b.path from public.browser_uploads b where b.status = 'stored' and b.wba_id = any(%s) order by b.id limit 200", (mine,))
                todo = [(upload_id(w, src, fn, size), w, src, None, path, uid, fn) for uid, w, src, fn, size, path in cur.fetchall()] + list(todo)
        else:
            print(f"The evidence database is at {db_gb:.0f} GB, at or over its ceiling of {MAX_DB_GB:.0f}: no document is read.", flush=True)
        batch_docs, batch_pages, done = [], [], []

        def flush():
            if not batch_docs:
                return
            for lid, status, tries_up, uid, n in done:          # a sent file says which document it is, so the year its sender gave is used (load_supabase.sender_years)
                if uid:
                    cur.execute("update public.browser_uploads set library_id = %s where id = %s", (lid, uid))
            upsert_documents(conn, batch_docs, say=lambda *x, **k: None)
            if batch_pages:
                load_pages(conn, f"own-{now()}", batch_pages)
            for lid, status, tries_up, uid, n in done:
                if uid:
                    cur.execute("update public.browser_uploads set status = 'taken', result = %s where id = %s and status = 'stored'", (f"read by lantrNa itself: {status}, {n or 0} pages", uid))
                cur.execute("""insert into public.own_docs (library_id, wba_id, url, status, tries, tried_at, read_at, how)
                               select %s, wba_id, url, %s, 1, now(), now(), 'sent' from public.documents where library_id = %s
                               on conflict (library_id) do update set status = excluded.status, tries = public.own_docs.tries + 1, tried_at = now(),
                                 read_at = case when excluded.status = 'ok' then now() else public.own_docs.read_at end""", (lid, status, lid))
            conn.commit(); batch_docs.clear(); batch_pages.clear(); done.clear()

        for lid, wba, url, text, path, uid, fname in todo:
            if time.time() > deadline:
                break
            kind, data = fetch_doc(sess, storage.signed(path) if path else url, bot.BOT_UA); c["documents fetched"] += 1
            if not path:
                time.sleep(PAUSE)
            status, pages, n, chars = ("ok", [], None, 0)
            if kind == "ok":
                status, pages, n, chars = read_pdf(data)
            else:
                status = kind
            if kind != "ok":          # not fetched: only the lane's own note changes; no document row is made of it, and a sent file stays stored
                if not path:
                    cur.execute("update public.own_docs set status = %s, tries = tries + 1, tried_at = now() where library_id = %s", (status, lid)); conn.commit()
                c[f"not read: {status}"] += 1; continue
            typ, year, label = describe(url, text, pages[0][1] if pages else "", fname)
            batch_docs.append(document_row(lid, wba, names.get(wba), url, typ, year, label, status, n, chars, now()))
            batch_pages.extend((lid, p, t) for p, t in pages); done.append((lid, status, 1, uid, n))
            c["documents read" if status == "ok" else f"no text: {status}"] += 1
            if len(batch_docs) >= 25:
                flush()
        flush()

        # how far each company is covered (the admin list reads company_pages)
        cur.execute("""insert into public.company_pages (wba_id, watched)
                         select o.wba_id, (select count(*) from public.own_pages p where p.wba_id = o.wba_id and p.status = 'watched') from public.own_companies o
                       on conflict (wba_id) do update set watched = excluded.watched, at = now()""")
        conn.commit()
    print("; ".join(f"{k} {v:,}" for k, v in sorted(c.items())) or "nothing to do", flush=True)
    run.finish("ok", rows_in=c["documents fetched"], rows_out=c["documents read"], counters=dict(c), notes="written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
