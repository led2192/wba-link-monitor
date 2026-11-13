#!/usr/bin/env python3
"""
search_timing.py: where the time of a lanterN search goes, stage by stage. Read-only.

find_pages (supabase/search.sql) took 3 to 10 seconds a search on 10 Nov 2026 and cut by the clock, so the
same search could answer differently under load. Before redesigning it, this script times its stages one by
one on a few sample searches, with the owner's connection: the fixed costs (the key, the company names, the
list of years), the candidate pool gathered year by year as the function does (with and without copying the
page's word vector), the same pool in one statement, the ranking of the pool, and the excerpts at several
lengths. It prints counts and seconds, never page text (the logs are public).

    python search_timing.py

Run by hand through the Search check workflow (input mode = timing). Env: SUPABASE_DB_URL.
"""
import os, sys, time

CASES = [
    # (label, words, phrase or None, company or None, year or None)
    ("Nike sustainability", ["sustainability"], None, "Nike", None),
    ("Unilever scope 1 2024", ["scope", "1"], None, "Unilever", 2024),
    ("climate", ["climate"], None, None, None),
    ("grievance mechanism suppliers", ["grievance", "mechanism", "suppliers"], None, None, None),
    ('"just transition"', [], "just transition", None, None),
    ("Nike \"code of conduct\"", [], "code of conduct", "Nike", None),
]
# Questions written in full, as people type them to Ask (13 Nov 2026: a long question about one company ran into the
# timeout while short searches took 2 s). These are our own sentences, not anyone's: the log is public.
LONG = [
    # (label, question, company or None)
    ("3 words, one company", "scope 1 emissions", "Nike"),
    ("question of 8 kept words, one company", "What are the main climate transition risks and how does the board oversee progress against sustainability targets?", "Nike"),
    ("question of 8 kept words, a large company", "What are the main climate transition risks and how does the board oversee progress against sustainability targets?", "Unilever"),
    ("question of 8 kept words, no company", "What are the main climate transition risks and how does the board oversee progress against sustainability targets?", None),
    ("question of 5 kept words, one company", "How many employees received human rights training last year?", "Nike"),
]


def level_queries(cur, words):
    """(every word, every word but one) as tsquery text, built as find_pages builds its levels 0 and 1."""
    cur.execute("select " + ", ".join(["public.find_word_query(%s)::text"] * len(words)), words)
    tq = ["(" + t + ")" for t in cur.fetchone()]
    every = " & ".join(tq)
    but_one = " | ".join("(" + " & ".join(t for j, t in enumerate(tq) if j != i) + ")" for i in range(len(tq))) if len(tq) > 1 else every
    return every, but_one


def usual_pages(cur, question, company):
    """The pages find_pages gives today (option off), sorted, to compare an option's list with."""
    cur.execute("select set_config('lantern.company_first', 'off', false)")
    cur.execute("select coalesce(array_agg(library_id || '|' || page order by library_id, page), '{}') from public.find_pages('timing', %s, %s, null, null, null, 50, 'timing')", (question, company))
    got = cur.fetchone()[0]
    cur.execute("select set_config('lantern.company_first', 'on', false)")
    return got


def whole_function(cur, conn, md):
    """The searches as the page asks them, on questions written in full: the two levels of the word index by
    themselves, then find_pages, find_meaning and find_blend whole. Seconds and row counts only."""
    md += ["", "### Questions written in full", "",
           "| Question | Stage | Rows | Seconds |", "| --- | --- | --: | --: |"]
    for label, question, company in LONG:
        def row(stage, sql, args, count=True):
            try:
                t0 = time.time()
                cur.execute(sql, args)
                n = cur.fetchone()[0] if count else len(cur.fetchall())
                md.append(f"| {label} | {stage} | {n:,} | {time.time() - t0:.2f} |")
                conn.commit()
            except Exception as e:
                conn.rollback()
                cur.execute("set statement_timeout = '90s'")
                md.append(f"| {label} | {stage}: failed after {time.time() - t0:.1f} s ({type(e).__name__}: {str(e).splitlines()[0][:80]}) | | |")
        try:
            cur.execute("select words from public.find_terms(%s)", (question,))
            words = cur.fetchone()[0]
            ids = None
            if company:
                cur.execute("select public.companies_matching(%s)", (company,))
                ids = cur.fetchone()[0]
            every, but_one = level_queries(cur, words)
            a = {"ids": ids}
            md.append(f"| {label} | words kept by find_terms | {len(words)} | |")
            if ids:
                row("pages of the company (no words)", "select count(*) from (select 1 from public.pages p where p.wba_id = any(%(ids)s::text[]) limit 200000) x", a)
            row("level 0: every word, 800 at most", f"select count(*) from (select p.library_id from public.pages p where {WHERE} limit 800) x", {**a, "q": every})
            row("level 1: every word but one, 800 at most", f"select count(*) from (select p.library_id from public.pages p where {WHERE} limit 800) x", {**a, "q": but_one})
            row("level 1 with the word vectors copied, as the function does", f"select count(*) from (select p.library_id, p.page, length(p.words::text) from public.pages p where {WHERE} limit 800) x", {**a, "q": but_one})
            row("find_pages, whole", "select count(*) from public.find_pages('timing', %s, %s, null, null, null, 50, 'timing')", (question, company))
            row("find_pages again (pages now in memory)", "select count(*) from public.find_pages('timing', %s, %s, null, null, null, 50, 'timing')", (question, company))
            if company:
                cur.execute("select set_config('lantern.company_first', 'on', false)")
                row("find_pages with the option company_first", "select count(*) from public.find_pages('timing', %s, %s, null, null, null, 50, 'timing')", (question, company))
                row("the same pages as without it (1 = yes)", "select ((select coalesce(array_agg(library_id || '|' || page order by library_id, page), '{}') "
                    "from public.find_pages('timing', %s, %s, null, null, null, 50, 'timing')) = %s::text[])::int", (question, company, usual_pages(cur, question, company)))
                cur.execute("select set_config('lantern.company_first', 'off', false)")
                conn.commit()
            t0 = time.time()
            cur.execute("select public.query_vector_made(%s)", (question,))
            vec = (cur.fetchone()[0] or {}).get("vec")
            conn.commit()
            md.append(f"| {label} | the question's vector | {1 if vec else 0} | {time.time() - t0:.2f} |")
            if vec:
                row("find_meaning, whole", "select count(*) from public.find_meaning('timing', %s::real[], %s, null, null, null, 50)", (vec, company))
                row("find_blend, whole (what Ask reads from)", "select count(*) from public.find_blend('timing', %s, %s::real[], %s, null, null, null, 24, 'timing')", (question, vec, company))
            if company and label.startswith("question of 8 kept words, one"):
                cur.execute(f"explain (analyze, buffers) select p.library_id, p.page, p.words from public.pages p where {WHERE} limit 800", {**a, "q": but_one})
                PLANS.append((label, [r[0][:180] for r in cur.fetchall()][:40]))
                conn.commit()
        except Exception as e:
            conn.rollback()
            cur.execute("set statement_timeout = '90s'")
            md.append(f"| {label} | failed: {type(e).__name__}: {str(e).splitlines()[0][:120]} | | |")
    for label, plan in PLANS:
        md += ["", f"Plan of level 1 ({label}):", "", "```"] + plan + ["```"]
    # what the log says of the last seven days: how long searches took, by the number of words and whether a company narrowed them
    try:
        cur.execute("""select case when coalesce(btrim(terms), '') = '' then 0 else array_length(regexp_split_to_array(btrim(terms), '[[:space:]]+'), 1) end as nw,
                              coalesce(company, '') <> '' as narrowed, count(*), percentile_cont(0.5) within group (order by ms),
                              percentile_cont(0.9) within group (order by ms), max(ms)
                       from public.search_log where at > now() - interval '7 days' and coalesce(client, '') not in ('timing', 'eval') group by 1, 2 order by 1, 2""")
        md += ["", "Searches of the last seven days in the log (a search cut by the timeout leaves no row):", "",
               "| Words | A company | Searches | Median s | 90% under s | Longest s |", "| --: | --- | --: | --: | --: | --: |"]
        md += [f"| {nw} | {'yes' if nar else 'no'} | {n:,} | {float(p50 or 0) / 1000:.1f} | {float(p90 or 0) / 1000:.1f} | {float(mx or 0) / 1000:.1f} |" for nw, nar, n, p50, p90, mx in cur.fetchall()]
    except Exception as e:
        conn.rollback()
        md.append(f"log summary failed: {type(e).__name__}")


PLANS = []
WHERE = "p.words @@ %(q)s::tsquery and p.wba_id <> '' and (%(ids)s::text[] is null or p.wba_id = any(%(ids)s::text[]))"


def timed(cur, sql, args=None):
    """(first row, seconds)."""
    t0 = time.time()
    cur.execute(sql, args)
    row = cur.fetchone() if cur.description else None
    return row, time.time() - t0


def main():
    if not os.environ.get("SUPABASE_DB_URL"):
        sys.exit("Set SUPABASE_DB_URL.")
    import evidence_db
    md = ["### Search timing", "", "Read-only. Seconds from GitHub's servers, network included (a round trip is the first line).", ""]
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("set statement_timeout = '90s'")
        cur.execute("set work_mem = '128MB'")
        _, s = timed(cur, "select 1")
        md.append(f"- a round trip: {s:.3f} s")
        (n,), s = timed(cur, "select count(*) from (select distinct coalesce(year, 0) from public.documents) x")
        md.append(f"- the list of years (asked once per pass by find_pages): {n} years, {s:.3f} s")
        (n,), s = timed(cur, "select coalesce(array_length(public.companies_matching('Nike'), 1), 0)")
        md.append(f"- companies_matching('Nike'): {n} ids, {s:.3f} s")
        (n,), s = timed(cur, "select count(*) from public.search_log l where l.key_name = 'team' and l.at > now() - interval '1 minute'")
        md.append(f"- the searches-per-minute count: {s:.3f} s")
        (n, pages), s = timed(cur, "select count(*), sum(coalesce(pages, 0)) from public.documents where wba_id <> ''")
        md.append(f"- documents: {n:,} with {pages or 0:,} pages, {s:.3f} s")
        cur.execute("select indexname, pg_size_pretty(pg_relation_size(indexname::regclass)), indexdef from pg_indexes where schemaname = 'public' and tablename = 'pages'")
        for name, size, d in cur.fetchall():
            md.append(f"- index {name} ({size}): `{d.split(' USING ')[-1][:160]}`")
        cur.execute("select pg_size_pretty(pg_table_size('public.pages')), (select reltuples::bigint from pg_class where oid = 'public.pages'::regclass)")
        size, rows = cur.fetchone()
        md.append(f"- pages: about {rows:,} rows, {size} with its long values")
        cur.execute("select coalesce(year, 0), count(*), sum(coalesce(pages, 0)) from public.documents where wba_id <> '' group by 1 order by 1 desc")
        md.append("- documents and pages by year: " + ", ".join(f"{y}: {d:,}/{p or 0:,}" for y, d, p in cur.fetchall()))
        cur.execute("select distinct coalesce(year, 0) from public.documents order by 1 desc")
        years = [r[0] for r in cur.fetchall()]
        md += ["", "| Search | Stage | Rows | Seconds |", "| --- | --- | --: | --: |"]
        for label, words, phrase, company, year in CASES:
            try:
                parts = ["public.find_word_query(%s)"] * len(words) + (["phraseto_tsquery('simple', %s)"] if phrase else [])
                cur.execute("select (" + " && ".join(parts) + ")::text", words + ([phrase] if phrase else []))
                q = cur.fetchone()[0]
                ids = None
                if company:
                    cur.execute("select public.companies_matching(%s)", (company,))
                    ids = cur.fetchone()[0]
                a = {"q": q, "ids": ids}
                ys = [year] if year else years
                # 1. as the function does: one statement per year, 400 pages each, up to 1,600
                for what, cols in (("ids only", "p.library_id, p.page"), ("with the word vector", "p.library_id, p.page, length(p.words)")):
                    got, t0 = 0, time.time()
                    for y in ys:
                        cur.execute(f"select count(*) from (select {cols} from public.pages p where {WHERE} and p.year = %(y)s limit 400) x", {**a, "y": y})
                        got += cur.fetchone()[0]
                        if got >= 1600:
                            break
                    md.append(f"| {label} | year by year, {what} | {got:,} | {time.time() - t0:.2f} |")
                # 2. one statement
                yf = " and p.year = %(y)s" if year else ""
                (n,), s = timed(cur, f"select count(*) from (select p.library_id, p.page from public.pages p where {WHERE}{yf} limit 1600) x", {**a, "y": year})
                md.append(f"| {label} | one statement, 1,600 at most, ids only | {n:,} | {s:.2f} |")
                (n,), s = timed(cur, f"select count(*) from (select 1 from public.pages p where {WHERE}{yf} limit 50000) x", {**a, "y": year})
                md.append(f"| {label} | every matching page, counted (50,000 at most) | {n:,} | {s:.2f} |")
                # 3. the pool in a temporary table (ids), then the ranking and the excerpts on it
                cur.execute("create temp table if not exists t_pool (library_id text, page int)")
                cur.execute("truncate t_pool")
                _, s = timed(cur, f"insert into t_pool select p.library_id, p.page from public.pages p where {WHERE}{yf} limit 1600", {**a, "y": year})
                (n, m), s2 = timed(cur, "select count(*), max(ts_rank_cd(p.words, %(q)s::tsquery, 5)) from t_pool t join public.pages p using (library_id, page)", a)
                md.append(f"| {label} | ranking the pool (ts_rank_cd, word vectors read) | {n:,} | {s2:.2f} |")
                (n, m), s2 = timed(cur, "select count(*), max(ts_rank(p.words, %(q)s::tsquery)) from t_pool t join public.pages p using (library_id, page)", a)
                md.append(f"| {label} | ranking the pool again (ts_rank, vectors now in memory) | {n:,} | {s2:.2f} |")
                for chars in (20000, 6000, 2500):
                    (n, _), s3 = timed(cur, "select count(*), sum(length(ts_headline('simple', left(p.body, %(c)s), %(q)s::tsquery, 'MaxFragments=2, MaxWords=40, MinWords=15'))) "
                                            "from (select * from t_pool limit 50) t join public.pages p using (library_id, page)", {**a, "c": chars})
                    md.append(f"| {label} | 50 excerpts from the first {chars:,} characters | {n} | {s3:.2f} |")
                (n, _), s3 = timed(cur, "select count(*), sum(length(p.body)) from (select * from t_pool limit 50) t join public.pages p using (library_id, page)")
                md.append(f"| {label} | 50 page texts read, no excerpt | {n} | {s3:.2f} |")
                conn.commit()
            except Exception as e:
                conn.rollback()
                cur.execute("set statement_timeout = '90s'")
                md.append(f"| {label} | failed: {type(e).__name__}: {str(e).splitlines()[0][:120]} | | |")
        if os.environ.get("TIMING_LONG", "true") != "false":
            whole_function(cur, conn, md)
        # one plan, to see how the word index is used with a year
        try:
            cur.execute("select (public.find_word_query('sustainability'))::text")
            q = cur.fetchone()[0]
            cur.execute("explain (analyze, buffers) select p.library_id, p.page from public.pages p where p.words @@ %s::tsquery and p.wba_id <> '' and p.year = 2025 limit 400", (q,))
            md += ["", "Plan of one year's statement (sustainability, 2025):", "", "```"] + [r[0][:200] for r in cur.fetchall()] + ["```"]
        except Exception as e:
            conn.rollback()
            md.append(f"plan failed: {type(e).__name__}")
    out = "\n".join(md) + "\n"
    open("search_timing.md", "w").write(out)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
