"""Which documents held under a company are a subsidiary's own: told by the subsidiary's name in the address or the label.

24 Nov 2026: a question about a bank's challenges was answered first from the annual report of its UK subsidiary,
which the library holds under the group. Its label said only "Annual Report 2025"; its address named the subsidiary.
The register of legal entities gives each group's entities by name (public.register_names, kind 'a subsidiary'). A
document of company W whose address or label holds the name of one of W's subsidiaries, and not just W's own name,
is taken as that subsidiary's.

How a name is looked for: without its legal form (public.name_key did that), of two words or more; in the label as
whole words, and in the address with the spaces taken out ("santanderuk" in ".../SantanderUKGroupHoldingsplc2025...").
The longest name wins. A name that is the start of the company's own name, or that the company's own name holds, is
no evidence and is skipped.

Dry run by default: counts only. --commit writes public.document_entities (one row a document: the entity's LEI is
not known from the name alone, so the name, the kind and how it was told). Prints counts, never a title or a name.
"""
import argparse, collections, os, re, sys, unicodedata

MIN_SQUASHED = 10            # a name looked for in an address with no spaces must be this long, or it matches by chance


def fold(s):
    s = unicodedata.normalize("NFD", s or "")
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c)).lower()).strip()


def told_by(label, url, names, own):
    """(name, how) of the longest subsidiary name the label or the address holds, or None. names: [(key, name)] of one
    company's subsidiaries, own: the company's own name folded."""
    lab, squashed = " " + fold(label) + " ", re.sub(r"[^a-z0-9]", "", fold(url.split("//", 1)[-1].split("/", 1)[-1] if url else ""))
    best = None
    for key, name in names:
        if not key or " " not in key or key in own or own.startswith(key):
            continue
        how = "label" if (" " + key + " ") in lab else "address" if len(key.replace(" ", "")) >= MIN_SQUASHED and key.replace(" ", "") in squashed else None
        if how and (best is None or len(key) > len(best[0])):
            best = (key, name, how)
    return (best[1], best[2]) if best else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("subsidiary_documents.py")
    c, found = collections.Counter(), []
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select n.wba_id, n.key, n.name from public.register_names n where n.kind = 'a subsidiary'")
            names = collections.defaultdict(list)
            for w, k, n in cur.fetchall():
                names[w].append((k, n))
            cur.execute("select wba_id, name from public.companies where wba_id = any(%s)", (list(names),)); own = {w: fold(n) for w, n in cur.fetchall()}
            cur.execute("select library_id, wba_id, label, url, coalesce(doc_type, '') from public.documents where wba_id = any(%s) and coalesce(text_status, '') = 'ok'", (list(names),))
            docs = cur.fetchall()
        conn.rollback()
        firms = set()
        for lid, w, label, url, kind in docs:
            got = told_by(label, url, names[w], own.get(w, ""))
            c["documents_looked_at"] += 1
            if got:
                found.append((lid, got[0], got[1])); firms.add(w); c["told_by_" + got[1]] += 1; c["type_" + (kind or "untyped").lower().replace(" ", "_")] += 1
        c["companies_with_subsidiary_names"] = len(names); c["companies_with_such_documents"] = len(firms)
        print(f"{len(docs):,} read documents of {len(names):,} companies with named subsidiaries: {len(found):,} are told as a subsidiary's own "
              f"({c['told_by_label']:,} by the label, {c['told_by_address']:,} by the address), in {len(firms):,} companies" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
        print("by type: " + ", ".join(f"{k[5:]} {n:,}" for k, n in c.most_common() if k.startswith("type_")), flush=True)
        if a.commit:
            with conn.cursor() as cur:
                cur.execute("delete from public.document_entities where how in ('label', 'address')")
                cur.executemany("insert into public.document_entities (library_id, name, kind, how) values (%s, %s, 'a subsidiary', %s) on conflict (library_id) do nothing", found)
            conn.commit()
    run.finish("ok", rows_in=len(docs), rows_out=len(found) if a.commit else 0, counters=dict(c), notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
