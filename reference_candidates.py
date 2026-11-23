"""What the reports name that the reference list does not hold: candidates for the list, found in the corpus.

The owner's wish (23 Nov 2026): watch the corpus to know what the list is missing, terms and organisations alike.
Reports introduce what they will go on to cite in one fixed way: the full name, then its acronym in brackets,
"Task Force on Climate-related Financial Disclosures (TCFD)", "greenhouse gas (GHG)". This script reads a random
sample of recent pages, takes every such pair whose acronym can be read off the name, counts the companies and
documents that write it, and keeps the pairs the list does not hold (neither the name nor the acronym) and that at
least MIN_COMPANIES companies use. No model, no cost.

Left out: a company's own name ("Volkswagen AG (VW)": a legal suffix, or a name of the company list); a pair seen
in one company only (its own programme or unit). The kind is a guess from the name's last words (Act, Directive:
regulation; Standard, Protocol, Framework: norm; Council, Institute, Association: organisation; otherwise term)
and the owner sets it when adding the entry.

Rows the owner set aside or added are never touched; the others are replaced by this run's counts. Writes
public.reference_candidates only with --commit. Prints counts only, never the names.
"""
import argparse, collections, os, re, sys

FIRST_YEAR = 2023
SAMPLE_PERCENT = 0.4
MIN_COMPANIES = 4
KEEP = 600
SMALL = {"of", "for", "on", "and", "the", "in", "to", "de", "la", "du", "des", "der", "und", "y", "e", "&", "a", "an", "with", "by"}
LEGAL = re.compile(r"\b(inc|incorporated|ltd|limited|plc|ag|sa|s\.a|nv|n\.v|corp|corporation|gmbh|co|company|llc|lp|se|spa|s\.p\.a|ab|asa|oyj|bhd|tbk|pte|kk|holdings?)\.?$", re.I)
PAIR = re.compile(r"\(([A-Z][A-Za-z]{1,7})\)")
WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*|&")
GUESS = (("regulation", r"\b(act|directive|regulation|law|ordinance|code|decree|rule)$"),
         ("norm", r"\b(standards?|protocol|framework|principles|guidelines?|guidance|initiative|index|taxonomy|convention|agreement|goals?|targets?|methodology|scheme|certification|charter|compact|recommendations)$"),
         ("organisation", r"\b(association|council|institute|institution|organi[sz]ation|board|alliance|foundation|agency|commission|committee|authority|forum|federation|network|coalition|panel|bank|fund|programme|program|society|union|office|department|ministry|administration|partnership|project|task ?force|group)$"))
SAMPLE_SQL = """
select p.library_id, p.wba_id, p.body from public.pages p tablesample system (%s)
where p.year >= %s and p.wba_id <> ''"""


def key_of(text):
    """The same key as public.reference_key: lower case, letters and digits, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def fits(took, acr):
    """True when the acronym can be read off the words: each letter in order from a word's first letter, from the
    first letter after a hyphen, or (only when letters outnumber the words left) from inside a word ("greenhouse gas",
    GHG); one word may give nothing ("Task Force on Climate-related Financial Disclosures", TCFD)."""
    letters, words = acr.upper(), [w for w in took if w.lower() not in SMALL]
    j = skipped = firsts = 0
    for n, w in enumerate(words):
        parts = [x for x in w.split("-") if x]
        if not parts:
            continue
        if j < len(letters) and parts[0][0].upper() == letters[j]:
            j += 1; firsts += 1
            for x in parts[1:]:
                if j < len(letters) and x[0].upper() == letters[j] and len(letters) - j > len(words) - n - 1:
                    j += 1
            if j < len(letters) and len(letters) - j > len(words) - n - 1 and letters[j] in parts[0][1:].upper():
                j += 1
        else:
            skipped += 1
    return j == len(letters) and skipped <= 1 and firsts >= max(2, (len(letters) + 1) // 2) and bool(words) and words[0][0].upper() == letters[0]


def pairs(body):
    """[(name, acronym)] for each "full name (ACRONYM)" of the page whose acronym can be read off the name."""
    out, text = [], " ".join((body or "").split())
    for m in PAIR.finditer(text):
        acr = m.group(1)
        if sum(c.isupper() for c in acr) < 2:
            continue
        words = WORD.findall(text[max(0, m.start() - 160):m.start()])[-(len(acr) + 4):]
        for k in range(2, len(words) + 1):
            took = words[-k:]
            if took[0].lower() in SMALL or took[-1].lower() in SMALL or not fits(took, acr):
                continue
            name = " ".join(took)
            if 6 <= len(name) <= 120 and not name.isupper():
                out.append((name, acr))
            break
    return out


def guess(name):
    for kind, pattern in GUESS:
        if re.search(pattern, name.strip(), re.I):
            return kind
    return "term"


def candidates(rows, held, company_keys, min_companies=MIN_COMPANIES, keep=KEEP):
    """[(key, name, acronym, kind, companies, documents, library_id)] from (library_id, wba_id, body) rows: the pairs
    not held by the list (held: the keys of its names and acronyms), most companies first."""
    firms, docs, spelt, where = collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(collections.Counter), {}
    for lib, wba, body in rows:
        for name, acr in set(pairs(body)):
            k = key_of(name)
            if not k or k in held or key_of(acr) in held or k in company_keys or LEGAL.search(name):
                continue
            firms[(k, acr)].add(wba); docs[(k, acr)].add(lib); spelt[(k, acr)][name] += 1; where.setdefault((k, acr), lib)
    out = []
    for (k, acr), f in firms.items():
        if len(f) >= min_companies:
            name = spelt[(k, acr)].most_common(1)[0][0]
            out.append((k, name, acr, guess(name), len(f), len(docs[(k, acr)]), where[(k, acr)]))
    best = {}
    for row in sorted(out, key=lambda r: (-r[4], -r[5], r[0])):       # one row a key: its most used acronym
        best.setdefault(row[0], row)
    return list(best.values())[:keep]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--percent", type=float, default=SAMPLE_PERCENT, help="the share of the pages sampled")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("reference_candidates.py")
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select name_key, alias_keys from public.reference_entries where kind <> 'about'")
            held = set()
            for nk, aks in cur.fetchall():
                held.add(nk); held.update(aks or [])
            cur.execute("select name from public.companies where name is not null"); company_keys = {key_of(r[0]) for r in cur.fetchall()}
            cur.execute("set statement_timeout = '20min'")
            cur.execute(SAMPLE_SQL, (a.percent, FIRST_YEAR)); rows = cur.fetchall()
            got = candidates(rows, held, company_keys)
            kinds = collections.Counter(g[3] for g in got)
            print(f"{len(rows):,} pages sampled; {len(got):,} names with their acronym that the list does not hold and {MIN_COMPANIES} companies or more write "
                  f"({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))})" + ("" if a.commit else " (dry run: nothing written)"), flush=True)
            if a.commit:
                cur.execute("delete from public.reference_candidates where status = 'found'")
                cur.executemany("insert into public.reference_candidates (key, name, acronym, kind, companies, documents, library_id) values (%s, %s, %s, %s, %s, %s, %s) "
                                "on conflict (key) do update set companies = excluded.companies, documents = excluded.documents, found_at = now()", got)
                conn.commit()
            else:
                conn.rollback()
    run.finish("ok", rows_in=len(rows), rows_out=len(got) if a.commit else 0, counters={"pages": len(rows), "candidates": len(got), **{f"kind_{k}": n for k, n in kinds.items()}},
               notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
