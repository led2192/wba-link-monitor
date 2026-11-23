"""The name a company is called by: its listed name without the legal form, when that is safe to read as the company.

23 Nov 2026: the owner typed "main difficulties porsche in 2025". The company is listed as "Porsche AG", the page
matched whole listed names only, and the question was searched across every company. People do not type "AG",
"Inc." or "Corporation". This script works out, for each company, the name without its legal form ("Porsche",
"General Motors", "Bank of China") and says whether the page may read it as the company:

  - it differs from the listed name, has 4 characters or more, and no other company has the same short or listed name;
  - two words or more: safe;
  - one word: safe only when the word is the company's rather than everyday language. Counted on the pages: the
    word is on at most COMMON pages of the library, and either on few pages at all (RARE or fewer) or a good share
    of the pages that hold it are the company's own (OWN_SHARE or more). "Porsche" passes; "Progressive", "Carrier"
    or "Target" do not, and stay recognised only as today (the full name, or the rules for names that are words).

Writes public.company_short_names only with --commit (the whole table is replaced in one transaction; a company not
in this run's list loses its row). Prints counts only, never names.
"""
import argparse, os, re, sys, unicodedata

COMMON = 60000
RARE = 2500
OWN_SHARE = 0.10
TAIL = {"ag", "sa", "se", "nv", "plc", "inc", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "lp", "spa", "ab", "asa", "oyj", "as", "bhd",
        "tbk", "kk", "gmbh", "kgaa", "sab", "cv", "sae", "saog", "pjsc", "jsc", "ojsc", "pao", "psc", "bv", "oy", "aps", "sarl", "srl", "sas", "pcl", "berhad",
        "incorporated", "holdings", "holding", "group", "the", "de", "and", "public"}
HEAD = {"the", "pt", "pjsc", "jsc", "ojsc", "pao"}
COUNT_SQL = "select count(*) from (select 1 from public.pages p where p.words @@ plainto_tsquery('simple', %s) limit %s) x"
OWN_SQL = "select count(*) from public.pages p where p.words @@ plainto_tsquery('simple', %s) and p.wba_id = %s"


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFD", text or "") if not unicodedata.combining(c))


def key_of(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(text).lower()).split())


def short_of(name):
    """The name without its legal form, as written ("Porsche AG" -> "Porsche"), or None when nothing is taken off."""
    words = (name or "").replace(",", " ").split()
    strip = lambda w: re.sub(r"[^a-z0-9&]", "", fold(w).lower())
    took = False
    while len(words) > 1 and (strip(words[-1]) in TAIL or words[-1] in ("&", "-")):
        words.pop(); took = True
    while len(words) > 1 and strip(words[0]) in HEAD:
        words.pop(0); took = True
    short = " ".join(words).strip(" -&.")
    return short if took and len(short) >= 4 and key_of(short) != key_of(name) else None


def plan(companies):
    """[(wba_id, short, words)] for the companies whose short name no other company shares, as a short or a listed name."""
    listed = {}
    for wba, name in companies:
        listed.setdefault(key_of(name), set()).add(wba)
    shorts = [(wba, short_of(name)) for wba, name in companies]
    count = {}
    for _, s in shorts:
        if s:
            count[key_of(s)] = count.get(key_of(s), 0) + 1
    return [(wba, s, len(key_of(s).split())) for wba, s in shorts if s and count[key_of(s)] == 1 and key_of(s) not in listed]


def one_word_safe(pages, own, common=COMMON, rare=RARE, own_share=OWN_SHARE):
    """True when a one-word short name is the company's word: not everywhere, and rare or mostly on its own pages."""
    return pages <= common and (pages <= rare or (pages > 0 and own / pages >= own_share))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import psycopg, qa, evidence_db
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("company_short_names.py")
    rows, c = [], {"companies": 0, "with_short": 0, "many_words": 0, "one_word_safe": 0, "one_word_everyday": 0, "slow": 0}
    with evidence_db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select wba_id, name from public.companies where name is not null"); companies = cur.fetchall(); conn.commit()
            c["companies"] = len(companies)
            for wba, short, words in plan(companies):
                c["with_short"] += 1
                if words > 1:
                    rows.append((wba, short, None, None, True)); c["many_words"] += 1; continue
                try:
                    cur.execute("select set_config('statement_timeout', '40s', true)")
                    cur.execute(COUNT_SQL, (short, COMMON + 1)); pages = cur.fetchone()[0]
                    own = 0
                    if pages <= COMMON:
                        cur.execute(OWN_SQL, (short, wba)); own = cur.fetchone()[0]
                    conn.commit()
                except psycopg.errors.QueryCanceled:
                    conn.rollback(); c["slow"] += 1; continue
                safe = one_word_safe(pages, own)
                rows.append((wba, short, pages, own, safe)); c["one_word_safe" if safe else "one_word_everyday"] += 1
            if a.commit:
                cur.execute("delete from public.company_short_names")
                cur.executemany("insert into public.company_short_names (wba_id, short, pages, own_pages, safe) values (%s, %s, %s, %s, %s)", rows)
                conn.commit()
    print(f"{c['companies']:,} companies; {c['with_short']:,} have a name without the legal form that is theirs alone: {c['many_words']:,} of two words or more, "
          f"{c['one_word_safe']:,} of one word that is the company's, {c['one_word_everyday']:,} of one everyday word (not read as the company), {c['slow']:,} not counted in time"
          + ("" if a.commit else " (dry run: nothing written)"), flush=True)
    run.finish("ok", rows_in=c["companies"], rows_out=len(rows) if a.commit else 0, counters=c, notes="written" if a.commit else "dry run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
