"""How large would a list of companies of lantrNa's own be? A count from the register's files, before anything is built.

The owner wants a catalogue of companies wider than the 2,000 lantrNa started from (24 Nov 2026): known by name, with
no documents yet, the largest of each country to be given documents first. Agreed skeleton: the entities of GLEIF's
register that have issued securities (the register's own ISIN-to-LEI mapping), are companies and not funds, are
active, and are nobody's subsidiary (no active ultimate parent in the relationship file). This script only counts
them, so that the next step is designed on numbers:

  1. the ISIN-to-LEI mapping: which LEIs have at least one ISIN
  2. the relationship file: which LEIs have an active ultimate parent (they are subsidiaries)
  3. the register's full file of entities, read in a stream: category, status, country, for the LEIs of step 1

Three downloads from GLEIF (open data, CC0), as lanterN-bot, the largest several hundred megabytes; nothing is kept
and nothing is written. Prints counts, keys and column names, never an entity.
"""
import collections, csv, io, os, sys, tempfile, zipfile

from gleif_relations import INDEX as RR_INDEX, csv_link, open_rows, START, END, TYPE, STATUS, ULTIMATE

LEI_INDEX = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest"
ISIN_INDEX = "https://mapping.gleif.org/api/v2/isin-lei/latest"


def any_link(node):
    """The first address of a file in a small JSON answer, whatever its key: for the mapping's index, whose shape is
    not the golden copy's."""
    if isinstance(node, str):
        return node if node.startswith("http") and ("download" in node.lower() or node.lower().endswith((".zip", ".csv"))) else None
    for v in (node.values() if isinstance(node, dict) else node if isinstance(node, list) else []):
        got = any_link(v)
        if got:
            return got
    return None


def column(cols, *wanted):
    """The first column whose name ends with one of the wanted endings (the files' headers are long dotted paths)."""
    for w in wanted:
        for c in cols:
            if c == w or c.endswith("." + w) or c.lower() == w.lower():
                return c
    return None


def count(entities, cols, with_isin, subsidiaries):
    """Counters for the catalogue from the rows of the entities file. entities: dicts; with_isin, subsidiaries: sets of LEIs."""
    lei_c, cat_c, st_c, cty_c = column(cols, "LEI"), column(cols, "Entity.EntityCategory", "EntityCategory"), column(cols, "Entity.EntityStatus", "EntityStatus"), column(cols, "Entity.LegalAddress.Country", "LegalAddress.Country")
    c, by_country = collections.Counter(), collections.Counter()
    for r in entities:
        c["entities"] += 1
        lei = (r.get(lei_c) or "").strip().upper()
        if lei not in with_isin:
            continue
        c["with_an_isin"] += 1
        cat, st = (r.get(cat_c) or "").strip().upper() if cat_c else "", (r.get(st_c) or "").strip().upper() if st_c else ""
        c["category_" + (cat or "none").lower()] += 1
        if cat in ("FUND", "BRANCH", "SOLE_PROPRIETOR") or st not in ("ACTIVE", ""):
            continue
        c["companies_active_with_an_isin"] += 1
        if lei in subsidiaries:
            c["of_them_subsidiaries"] += 1; continue
        c["catalogue"] += 1; by_country[(r.get(cty_c) or "??").strip().upper() if cty_c else "??"] += 1
    return c, by_country, {"lei": lei_c, "category": cat_c, "status": st_c, "country": cty_c}


def fetch(requests, url, ua, suffix=".zip"):
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        size = 0
        with requests.get(url, headers={"User-Agent": ua}, stream=True, timeout=600) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1 << 20):
                tmp.write(chunk); size += len(chunk)
    return tmp.name, size


def main():
    import requests, qa, robot_identity as bot
    run = qa.Run("catalogue_count.py")
    ua, head = bot.BOT_UA, {"User-Agent": bot.BOT_UA, "Accept": "application/json"}
    def keys(node, pre="", depth=0):
        return [] if not isinstance(node, dict) or depth > 3 else sum(([pre + k] + keys(v, pre + k + ".", depth + 1) for k, v in node.items()), [])
    # 1. ISINs
    i = requests.get(ISIN_INDEX, headers=head, timeout=60); idx = i.json() if i.status_code == 200 else {}
    print(f"isin index: HTTP {i.status_code}; keys: " + ", ".join(sorted(set(keys(idx)))[:30]), flush=True)
    link = any_link(idx); with_isin = set(); n_isin = 0
    if link:
        path, size = fetch(requests, link, ua); rows, cols = open_rows(path)
        lei_c = column(cols, "LEI")
        print(f"isin file: {size / 1048576:.0f} MB; columns: " + ", ".join(cols[:10]), flush=True)
        for r in rows:
            n_isin += 1; with_isin.add((r.get(lei_c) or "").strip().upper())
        os.unlink(path)
    print(f"{n_isin:,} ISINs of {len(with_isin):,} entities", flush=True)
    # 2. subsidiaries
    idx = requests.get(RR_INDEX, headers=head, timeout=60).json(); path, size = fetch(requests, csv_link(idx), ua); rows, cols = open_rows(path)
    subs = {(r.get(START) or "").strip().upper() for r in rows if (r.get(TYPE) or "").strip() == ULTIMATE and (r.get(STATUS) or "").strip().upper() == "ACTIVE" and (r.get(START) or "").strip().upper() != (r.get(END) or "").strip().upper()}
    os.unlink(path)
    print(f"{len(subs):,} entities have an active ultimate parent", flush=True)
    # 3. entities
    i = requests.get(LEI_INDEX, headers=head, timeout=60); idx = i.json() if i.status_code == 200 else {}
    link = csv_link(idx)
    if not link or not with_isin:
        print("the entities file or the ISIN mapping could not be found: no count", flush=True)
        run.finish("ok", rows_in=0, rows_out=0, counters={"isins": n_isin}, notes="no count"); return 0
    path, size = fetch(requests, link, ua); rows, cols = open_rows(path)
    print(f"entities file: {size / 1048576:.0f} MB; {len(cols)} columns, the first: " + ", ".join(cols[:12]), flush=True)
    c, by_country, used = count(rows, cols, with_isin, subs)
    os.unlink(path)
    print("columns used: " + ", ".join(f"{k}={v}" for k, v in used.items()), flush=True)
    print(", ".join(f"{k} {n:,}" for k, n in sorted(c.items())), flush=True)
    print(f"the catalogue would hold {c['catalogue']:,} companies in {len(by_country):,} countries; the largest: " + ", ".join(f"{k} {n:,}" for k, n in by_country.most_common(25)), flush=True)
    run.finish("ok", rows_in=c["entities"], rows_out=0, counters={k: v for k, v in c.items()}, notes="count only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
