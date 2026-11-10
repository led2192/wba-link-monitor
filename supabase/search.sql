-- Search functions of the evidence database, for the search page and for Claude.
-- Applied by load_supabase.py once the pages_search index exists (see schema.sql), only where
-- Supabase's auth schema exists. The tables stay closed (row level security, no policy): the page reaches
-- the data only through these functions, which check who is asking first. Safe to run again.
--
-- How a search runs (26 Oct 2026, measured on a 1-in-11 copy of the corpus):
--   every condition (words, year, type, company) is answered inside the index pages_search, so only
--   pages that match everything are fetched; a phrase is checked against the stored words, not the text;
--   results come year by year, newest first, stopping as soon as max_rows are found, so a rare phrase
--   and a common word cost about the same. Documents with no company (out of the library's scope)
--   are left out: their pages carry wba_id ''.

-- Who may search: a signed-in user with a WBA address, or an address listed in search_access (added by
-- the owner in the SQL editor: insert into search_access (email) values ('name@example.com');).
-- A direct database session (the loader, the checks) may too.
create table if not exists public.search_access (
  email     text primary key,
  added_at  timestamptz not null default now()
);
alter table public.search_access enable row level security;

create or replace function public.email_may_search(email text) returns boolean
  language sql stable security definer set search_path = public
  as $$
    select coalesce(lower(email) like '%@worldbenchmarkingalliance.org', false)
        or exists (select 1 from public.search_access a where lower(a.email) = lower(email))
  $$;

create or replace function public.can_search() returns boolean
  language sql stable security definer set search_path = public
  as $$
    select session_user = 'postgres' or public.email_may_search(auth.jwt() ->> 'email')
  $$;

-- The companies a company filter means: a WBA id, or part of a name. Null when no filter.
-- Several companies at once, separated by ";" (the page sends the picked companies' ids this way); each
-- part is a WBA id or part of a name. An unknown company gives [''] so that nothing matches.
create or replace function public.companies_matching(company_filter text) returns text[]
  language sql stable security definer set search_path = public
  as $$
    select case when company_filter is null or btrim(company_filter) = '' then null
                else coalesce((select array_agg(distinct d.wba_id) from public.documents d
                               where d.wba_id is not null
                                 and exists (select 1 from unnest(string_to_array(company_filter, ';')) part
                                             where btrim(part) <> ''
                                               and (d.wba_id = btrim(part) or d.company ilike '%' || btrim(part) || '%'))),
                              array['']::text[]) end
  $$;

-- Several document types at once, separated by ";"; null when none given.
create or replace function public.types_matching(type_filter text) returns text[]
  language sql immutable
  as $$
    select nullif(array(select btrim(t) from unnest(string_to_array(coalesce(type_filter, ''), ';')) t where btrim(t) <> ''), '{}')
  $$;

-- The companies that can be searched, for the company picker on the search page: WBA id, name and how many
-- documents with text the database holds for each.
create or replace function public.search_companies()
returns table (wba_id text, company text, documents bigint)
language plpgsql stable security definer
set search_path = public
as $$
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  return query
    select d.wba_id, max(d.company), count(*)
    from public.documents d
    where d.wba_id is not null and d.wba_id <> '' and d.company is not null
    group by d.wba_id
    order by max(d.company);
end;
$$;

-- Pages that contain the words (websearch syntax: "exact phrase", -excluded, or), newest documents
-- first, with a highlighted extract (⟦ ⟧ around the matched words). Filters are optional; doc_filter
-- (a library_id) gives the pages of that one document in page order, which the page uses to open a
-- document from the documents view.
drop function if exists public.search_pages(text, text, int, int, text, int);
create or replace function public.search_pages(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 50, doc_filter text default null)
returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text,
               page int, excerpt text)
language plpgsql stable security definer
set search_path = public, extensions
set work_mem = '128MB'
set statement_timeout = '30s'
set plan_cache_mode = 'force_custom_plan'
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
  need int := least(greatest(coalesce(max_rows, 50), 1), 200);
  got int := 0;
  n int;
  y int;
  ids text[] := public.companies_matching(company_filter);
  types text[] := public.types_matching(type_filter);
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if numnode(tsq) = 0 then
    return;
  end if;
  if doc_filter is not null and btrim(doc_filter) <> '' then
    return query
      select p.library_id, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url, p.page,
             ts_headline('simple', left(p.body, 200000), tsq,
                         'MaxFragments=2, MaxWords=40, MinWords=15, FragmentDelimiter=" … ", StartSel=⟦, StopSel=⟧')
      from public.pages p
      join public.documents d on d.library_id = p.library_id
      where p.library_id = btrim(doc_filter) and p.words @@ tsq
      order by p.page
      limit need;
    return;
  end if;
  for y in select distinct coalesce(d.year, 0) from public.documents d
            where (year_from is null or d.year >= year_from) and (year_to is null or d.year <= year_to)
            order by 1 desc
  loop
    return query
      select s.library_id, s.company, s.wba_id, s.year, s.doc_type, s.label, s.url, s.page,
             ts_headline('simple', left(s.body, 200000), tsq,
                         'MaxFragments=2, MaxWords=40, MinWords=15, FragmentDelimiter=" … ", StartSel=⟦, StopSel=⟧')
      from (
        select p.library_id, p.page, p.body, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url
        from public.pages p
        join public.documents d on d.library_id = p.library_id
        where p.words @@ tsq
          and p.year = y
          and p.wba_id <> ''
          and (types is null or p.doc_type = any(types))
          and (ids is null or p.wba_id = any(ids))
        limit need - got
      ) s
      order by s.company, s.library_id, s.page;
    get diagnostics n = row_count;
    got := got + n;
    exit when got >= need;
  end loop;
end;
$$;

-- One line per document: the documents that contain the words, newest first, each with how many of
-- its pages match and the highlighted extract of its best page (ts_rank). A common word fills the
-- pages view with one long questionnaire (27 Oct 2026: "revenue" showed 100 pages of one CDP answer);
-- this view shows the documents and opens the pages of one on request (search_pages with doc_filter).
-- Per year, the first `window_rows` matching pages are grouped; when a year has more, its counts are
-- lower bounds (pages_capped).
create or replace function public.search_documents(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 50)
returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text,
               pages int, pages_capped boolean, best_page int, excerpt text)
language plpgsql stable security definer
set search_path = public, extensions
set work_mem = '128MB'
set statement_timeout = '30s'
set plan_cache_mode = 'force_custom_plan'
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
  need int := least(greatest(coalesce(max_rows, 50), 1), 200);
  got int := 0;
  n int;
  y int;
  ids text[] := public.companies_matching(company_filter);
  types text[] := public.types_matching(type_filter);
  window_rows constant int := 3000;
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if numnode(tsq) = 0 then
    return;
  end if;
  for y in select distinct coalesce(d.year, 0) from public.documents d
            where (year_from is null or d.year >= year_from) and (year_to is null or d.year <= year_to)
            order by 1 desc
  loop
    return query
      with hits as (
        select p.library_id, p.page, ts_rank(p.words, tsq) as r
        from public.pages p
        where p.words @@ tsq
          and p.year = y
          and p.wba_id <> ''
          and (types is null or p.doc_type = any(types))
          and (ids is null or p.wba_id = any(ids))
        limit window_rows
      ),
      grouped as (
        select h.library_id, count(*)::int as hits, (array_agg(h.page order by h.r desc, h.page))[1] as best
        from hits h
        group by h.library_id
      ),
      capped as (select count(*) >= window_rows as c from hits)
      select d.library_id, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url,
             g.hits, (select c.c from capped c), g.best,
             ts_headline('simple', left(p.body, 200000), tsq,
                         'MaxFragments=2, MaxWords=40, MinWords=15, FragmentDelimiter=" … ", StartSel=⟦, StopSel=⟧')
      from grouped g
      join public.documents d on d.library_id = g.library_id
      join public.pages p on p.library_id = g.library_id and p.page = g.best
      order by d.company, d.library_id
      limit need - got;
    get diagnostics n = row_count;
    got := got + n;
    exit when got >= need;
  end loop;
end;
$$;

-- The same pages, without the highlighted extract and up to 5,000 rows, for the CSV download: the
-- extract is what makes search_pages slow per row (it re-reads the page's text), so the export gives a
-- plain window of the text around the first query word instead.
create or replace function public.export_pages(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 5000)
returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text,
               page int, excerpt text)
language plpgsql stable security definer
set search_path = public, extensions
set work_mem = '128MB'
set statement_timeout = '120s'
set plan_cache_mode = 'force_custom_plan'
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
  need int := least(greatest(coalesce(max_rows, 5000), 1), 5000);
  got int := 0;
  n int;
  y int;
  ids text[] := public.companies_matching(company_filter);
  types text[] := public.types_matching(type_filter);
  first_word text := lower((regexp_match(coalesce(q, ''), '[[:alnum:]][[:alnum:]''’-]*'))[1]);
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if numnode(tsq) = 0 then
    return;
  end if;
  for y in select distinct coalesce(d.year, 0) from public.documents d
            where (year_from is null or d.year >= year_from) and (year_to is null or d.year <= year_to)
            order by 1 desc
  loop
    return query
      select s.library_id, s.company, s.wba_id, s.year, s.doc_type, s.label, s.url, s.page,
             regexp_replace(substring(s.body from greatest(1, coalesce(nullif(strpos(lower(s.body), first_word), 0), 1) - 120) for 300),
                            '\s+', ' ', 'g')
      from (
        select p.library_id, p.page, p.body, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url
        from public.pages p
        join public.documents d on d.library_id = p.library_id
        where p.words @@ tsq
          and p.year = y
          and p.wba_id <> ''
          and (types is null or p.doc_type = any(types))
          and (ids is null or p.wba_id = any(ids))
        limit need - got
      ) s
      order by s.company, s.library_id, s.page;
    get diagnostics n = row_count;
    got := got + n;
    exit when got >= need;
  end loop;
end;
$$;

-- How many companies, documents and pages contain the words, with the same filters. Counting stops
-- at 20,000 pages (capped = true), so a common word answers in seconds.
create or replace function public.search_summary(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null)
returns table (companies bigint, documents bigint, pages bigint, capped boolean)
language plpgsql stable security definer
set search_path = public, extensions
set work_mem = '128MB'
set statement_timeout = '30s'
set plan_cache_mode = 'force_custom_plan'
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
  ids text[] := public.companies_matching(company_filter);
  types text[] := public.types_matching(type_filter);
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if numnode(tsq) = 0 then
    return query select 0::bigint, 0::bigint, 0::bigint, false;
    return;
  end if;
  return query
    with hits as (
      select p.wba_id, p.library_id
      from public.pages p
      where p.words @@ tsq
        and p.wba_id <> ''
        and (year_from is null or p.year >= year_from)
        and (year_to is null or p.year <= year_to)
        and (types is null or p.doc_type = any(types))
        and (ids is null or p.wba_id = any(ids))
      limit 20001
    )
    select count(distinct h.wba_id), count(distinct h.library_id), least(count(*), 20000), count(*) > 20000
    from hits h;
end;
$$;

-- The exact number of companies with at least one page that contains the words. search_summary counts
-- inside its first 20,000 pages, which for a common word ("revenue": over 300,000 pages) is a few hundred
-- companies out of the real figure (27 Oct 2026: the page said 363 where the Disclosure Library's
-- mention search said 1,385). This asks the question company by company: each probe is an index
-- intersection of the word with the company (a handful of index pages, one heap page), and stops at
-- the first page found. Measured on a 300,000-page corpus with the word on 30% of pages: 2,000 probes
-- in 0.1 s. A plpgsql loop on purpose: written as one EXISTS join, the planner scans every matching
-- page once instead (the slow path this avoids).
create or replace function public.search_company_count(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null)
returns bigint
language plpgsql stable security definer
set search_path = public, extensions
set work_mem = '64MB'
set statement_timeout = '60s'
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
  ids text[] := public.companies_matching(company_filter);
  types text[] := public.types_matching(type_filter);
  n bigint := 0;
  c record;
  t0 timestamptz := clock_timestamp();
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if numnode(tsq) = 0 then
    return 0;
  end if;
  for c in select distinct d.wba_id from public.documents d
           where d.wba_id is not null and d.wba_id <> '' and (ids is null or d.wba_id = any(ids)) loop
    if exists (select 1 from public.pages p
               where p.wba_id = c.wba_id and p.words @@ tsq
                 and (year_from is null or p.year >= year_from)
                 and (year_to is null or p.year <= year_to)
                 and (types is null or p.doc_type = any(types))) then
      n := n + 1;
    end if;
    -- On the full database each probe of a very common word reads its long index entry again: "climate"
    -- and "revenue" passed 60 s (28 Oct 2026). After 25 s the count so far is returned as a lower
    -- bound, written as a negative number (-1200 means "at least 1,200"); the page shows "1,200+".
    if clock_timestamp() - t0 > interval '25 seconds' then
      return -n;
    end if;
  end loop;
  return n;
end;
$$;

-- The web interface (PostgREST) returns at most 1,000 rows of any set-returning function, silently.
-- The company list stopped at 1,000 of the companies, alphabetically, so QAFCO and Syngenta were
-- not offered (28 Oct 2026), and the CSV stopped at 1,000 pages instead of 5,000. These two return
-- the same rows as one JSON value, which the cap does not touch.
create or replace function public.search_companies_json() returns jsonb
language sql stable security definer set search_path = public
as $$ select coalesce(jsonb_agg(to_jsonb(c) order by c.company), '[]'::jsonb) from public.search_companies() c $$;

create or replace function public.export_pages_json(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 5000)
returns jsonb
language sql stable security definer set search_path = public
set statement_timeout = '120s'
as $$ select coalesce(jsonb_agg(to_jsonb(e)), '[]'::jsonb)
      from public.export_pages(q, company_filter, year_from, year_to, type_filter, max_rows) e $$;

-- Evidence passages (climate, nature, social, financial) that contain the words, optionally one group.
create or replace function public.search_passages(
  q text, dim text default null, company_filter text default null, year_from int default null,
  year_to int default null, max_rows int default 50)
returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text,
               page int, dims text[], area text, excerpt text)
language plpgsql stable security definer
set search_path = public, extensions
set work_mem = '128MB'
set statement_timeout = '30s'
set plan_cache_mode = 'force_custom_plan'
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
  ids text[] := public.companies_matching(company_filter);
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  return query
    select d.library_id, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url, ps.page, ps.dims, ps.area,
           case when numnode(tsq) = 0 then left(ps.body, 600)
                else ts_headline('simple', ps.body, tsq, 'MaxFragments=1, MaxWords=60, MinWords=25, StartSel=⟦, StopSel=⟧') end
    from public.passages ps
    join public.documents d on d.library_id = ps.library_id
    where (numnode(tsq) = 0 or public.page_words(ps.body) @@ tsq)
      and (dim is null or dim = any(ps.dims))
      and d.company is not null
      and (ids is null or d.wba_id = any(ids))
      and (year_from is null or d.year >= year_from)
      and (year_to is null or d.year <= year_to)
    order by d.year desc nulls last, d.library_id, ps.page, ps.n
    limit least(greatest(coalesce(max_rows, 50), 1), 200);
end;
$$;

revoke all on function public.search_pages(text, text, int, int, text, int, text) from public, anon;
revoke all on function public.search_summary(text, text, int, int, text) from public, anon;
revoke all on function public.search_company_count(text, text, int, int, text) from public, anon;
grant execute on function public.search_company_count(text, text, int, int, text) to authenticated;
revoke all on function public.search_passages(text, text, text, int, int, int) from public, anon;
revoke all on function public.can_search() from public, anon;
revoke all on function public.search_companies() from public, anon;
grant execute on function public.search_companies() to authenticated;
revoke all on function public.search_companies_json() from public, anon;
grant execute on function public.search_companies_json() to authenticated;
revoke all on function public.export_pages_json(text, text, int, int, text, int) from public, anon;
grant execute on function public.export_pages_json(text, text, int, int, text, int) to authenticated;
revoke all on function public.export_pages(text, text, int, int, text, int) from public, anon;
grant execute on function public.export_pages(text, text, int, int, text, int) to authenticated;
revoke all on function public.email_may_search(text) from public, anon, authenticated;
revoke all on function public.companies_matching(text) from public, anon, authenticated;
grant execute on function public.search_pages(text, text, int, int, text, int, text) to authenticated;
revoke all on function public.search_documents(text, text, int, int, text, int) from public, anon;
grant execute on function public.search_documents(text, text, int, int, text, int) to authenticated;
revoke all on function public.types_matching(text) from public, anon, authenticated;
grant execute on function public.search_summary(text, text, int, int, text) to authenticated;
grant execute on function public.search_passages(text, text, text, int, int, int) to authenticated;
grant execute on function public.can_search() to authenticated;


-- ==============================================================================================
-- Find (10 Nov 2026): the one-box search page, site/search.html. The analysts' side of the search
-- engine the pipeline already is for the robot. No sign-in: the link people receive carries a key
-- (#k=...), the page keeps it in the browser and sends it with every call, and the functions below check
-- it against search_keys (sha256 of the key; the key itself is never stored). Every query and every
-- click is logged (search_log, search_clicks): the log is the training data the robot's page ranking
-- lacks for the attributes without validated answers, and the record of what the organisation looks for.
-- Pages are ranked by relevance, not newest first: ts_rank_cd over the stored words (cover density,
-- normalised by page length), weighted by document type and discounted by age, at most 3 pages of one
-- document, over at most 600 candidate pages per year, newest years first (a common word still answers
-- in seconds; the ranking then orders what was fetched, so a one-word query leans to recent years).
-- The key's name is logged, not the key. A key may run 60 searches a minute.
create extension if not exists pgcrypto with schema extensions;

create table if not exists public.search_keys (
  name      text primary key,
  key_hash  text not null,                    -- sha256, hex
  created   timestamptz not null default now(),
  active    boolean not null default true
);
alter table public.search_keys enable row level security;
-- The team's link (10 Nov 2026). To revoke: update search_keys set active = false where name = 'team';
-- a new key: insert a new row with encode(digest('<key>', 'sha256'), 'hex') and send the new link.
insert into public.search_keys (name, key_hash) values ('team', 'a6018fc9ce54a921d2e1540c77d6abfa2c912daa2103f56ad792f3acc01be207') on conflict (name) do nothing;

create table if not exists public.search_log (
  id         bigserial primary key,
  at         timestamptz not null default now(),
  key_name   text,
  q          text,
  company    text,
  year_from  int,
  year_to    int,
  doc_type   text,
  n          int,                             -- candidate pages fetched
  ms         int,
  client     text                             -- what the page says about itself (version), never the person
);
alter table public.search_log enable row level security;
create index if not exists search_log_at on public.search_log (at);

create table if not exists public.search_clicks (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  log_id      bigint,
  library_id  text,
  page        int,
  rank        int
);
alter table public.search_clicks enable row level security;
create index if not exists search_clicks_log on public.search_clicks (log_id);

-- The key's name when it is an active key; null otherwise. The database owner (postgres) counts as the key
-- 'owner', so the check script and Claude can call the functions from the connection string.
create or replace function public.search_key_name(key text) returns text
  language sql stable security definer set search_path = public, extensions
  as $$
    select case when session_user = 'postgres' then 'owner'
                else (select k.name from public.search_keys k
                      where k.active and k.key_hash = encode(extensions.digest(coalesce(key, ''), 'sha256'), 'hex') limit 1) end
  $$;

-- How much a page of each document type weighs in the ranking: the reports people cite first, then the
-- filings and thematic reports, then policies, then slides and interim statements, then the rest.
create or replace function public.type_weight(doc_type text) returns real
  language sql immutable
  as $$
    select case doc_type
      when 'Annual Report' then 1.0 when 'Integrated Report' then 1.0 when 'Sustainability Report' then 1.0
      when 'ESG Report' then 1.0 when 'CSR Report' then 1.0
      when '10K Form' then 0.9 when 'Registration Document' then 0.9 when 'Financial Statement' then 0.9
      when 'Climate Report' then 0.9 when 'Environmental Reports' then 0.9 when 'Social Reports' then 0.9
      when 'Impact Report' then 0.9 when 'CDP Report' then 0.9 when 'GRI' then 0.9 when 'SASB Index' then 0.9
      when 'Policy Documents' then 0.8 when 'Code of Conduct' then 0.8 when 'Human Rights Policy' then 0.8
      when 'Engagement Report' then 0.8
      when 'Company Presentations' then 0.6 when 'Interim Reports' then 0.6
      else 0.5 end::real
  $$;

-- The words and phrases of a query, the way a web search engine reads them (10 Nov 2026): quoted phrases
-- are kept whole; the rest is split into words, lower case, without the little words of the main
-- languages (the, of, la, de, der, van...), at most 8 (a lone digit stays: scope 1); a word of 4 letters or
-- more also matches its longer forms (emission matches emissions; prefix match), a shorter one matches
-- exactly (CO2, GRI). A page that prints the words together, in order, ranks above one that scatters them.
create or replace function public.find_terms(q text, out phrases text[], out words text[])
language plpgsql immutable
as $$
declare
  rest text;
  stop text[] := array['the','a','an','of','in','on','at','to','for','and','or','is','are','was','were','be','by','with','from',
    'as','that','this','these','those','it','its','their','our','we','you','they','he','she','do','does','did','has','have','had',
    'not','but','if','than','then','so','what','which','who','how','when','where','about','into','over','under','per','vs','any',
    'all','also','more','most','some','such','each','other','only','can','could','will','would','should','may','might','there',
    'el','la','los','las','un','una','unos','unas','de','del','al','y','o','u','en','con','por','para','que','es','son','se','su',
    'sus','lo','le','les','des','du','et','ou','à','au','aux','dans','sur','pour','par','est','sont','ce','cette','ces','une',
    'der','die','das','den','dem','ein','eine','einer','eines','und','oder','im','am','auf','für','mit','von','zu','zur','zum',
    'ist','sind','nicht','os','um','uma','do','da','dos','das','nos','nas','com','il','gli','di','della','dei','delle','ed','che',
    'è','het','een','van','op','voor','met','zijn','te','bij','om','niet'];
begin
  phrases := coalesce(array(select btrim(m[1]) from regexp_matches(coalesce(q, ''), '"([^"]+)"', 'g') m where btrim(m[1]) <> ''), '{}');
  rest := regexp_replace(coalesce(q, ''), '"[^"]*"', ' ', 'g');
  words := coalesce(array(
    select w from (
      select distinct on (w) w, ord from (
        select lower(regexp_replace(t, '[^[:alnum:]]', '', 'g')) as w, ordinality as ord
        from regexp_split_to_table(rest, '[[:space:],;:()\[\]{}<>|/\\]+') with ordinality t) x
      where (length(w) >= 2 or w ~ '^[0-9]$') and w <> all(stop)
      order by w, ord) y
    order by ord limit 8), '{}');
end;
$$;

-- One word as a tsquery: a prefix match from 4 letters, exact below (to_tsquery lower-cases).
create or replace function public.find_word_query(w text) returns tsquery
  language sql immutable
  as $$ select to_tsquery('simple', case when length(w) >= 4 then w || ':*' else w end) $$;

alter table public.search_log add column if not exists relaxed int;        -- words dropped to find results (0 = all words)
alter table public.search_log add column if not exists terms text;          -- the words and phrases the engine read

drop function if exists public.find_pages(text, text, text, int, int, text, int, text);
create or replace function public.find_pages(
  key text, q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 50, client text default null)
returns table (log_id bigint, rank int, library_id text, company text, wba_id text, year int, doc_type text,
               label text, url text, page int, score real, excerpt text)
language plpgsql security definer
set search_path = public, extensions
set work_mem = '128MB'
set statement_timeout = '25s'
set plan_cache_mode = 'force_custom_plan'
as $$
declare
  k text := public.search_key_name(key);
  t record;
  phrases text[];
  words text[];
  nw int;
  tqs tsquery[] := '{}';            -- one tsquery per word
  phrase_q tsquery := null;         -- every phrase, required always
  or_q tsquery := null;             -- every word or phrase, for the ranking
  together_q tsquery := null;       -- the words as one phrase, in order: a page printing them together ranks higher
  sub_q tsquery;
  need int := least(greatest(coalesce(max_rows, 50), 1), 200);
  per_year int := 600;
  cap int := 2400;
  got int := 0;
  n int;
  y int;
  i int;
  mask int;
  drop_n int := 0;
  this_year int := extract(year from now())::int;
  t0 timestamptz := clock_timestamp();
  lid bigint;
  ids text[] := public.companies_matching(company_filter);
  types text[] := public.types_matching(type_filter);
begin
  if k is null then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if k <> 'owner' and (select count(*) from public.search_log l where l.key_name = k and l.at > now() - interval '1 minute') >= 60 then
    raise exception 'too many searches in a minute' using errcode = '53400';
  end if;
  select * into t from public.find_terms(q);
  phrases := t.phrases; words := t.words;
  nw := coalesce(array_length(words, 1), 0);
  if nw = 0 and coalesce(array_length(phrases, 1), 0) = 0 then   -- only little words: take them as typed
    words := coalesce(array(select lower(regexp_replace(w, '[^[:alnum:]]', '', 'g')) from regexp_split_to_table(coalesce(q, ''), '\s+') w
                            where length(regexp_replace(w, '[^[:alnum:]]', '', 'g')) >= 1 limit 8), '{}');
    nw := coalesce(array_length(words, 1), 0);
    if nw = 0 then
      return;
    end if;
  end if;
  for i in 1..coalesce(array_length(phrases, 1), 0) loop
    phrase_q := coalesce(phrase_q && phraseto_tsquery('simple', phrases[i]), phraseto_tsquery('simple', phrases[i]));
    or_q := coalesce(or_q || phraseto_tsquery('simple', phrases[i]), phraseto_tsquery('simple', phrases[i]));
  end loop;
  for i in 1..nw loop
    tqs := tqs || public.find_word_query(words[i]);
    or_q := coalesce(or_q || tqs[i], tqs[i]);
  end loop;
  if nw >= 2 then
    together_q := phraseto_tsquery('simple', array_to_string(words, ' '));
  end if;
  create temp table if not exists find_cand (library_id text, page int, words tsvector, year int, doc_type text, primary key (library_id, page)) on commit drop;
  truncate find_cand;
  -- level 0: every word; then every subset missing one word, then missing two, until enough pages are found
  <<levels>>
  for drop_n in 0..least(2, greatest(nw - 1, 0)) loop
    for mask in 0..(case when nw = 0 then 0 else (1 << nw) - 1 end) loop
      -- the subset is the words whose bit is set; it must drop exactly drop_n words
      continue when nw > 0 and (nw - (select count(*) from generate_series(0, nw - 1) b where (mask >> b) & 1 = 1)) <> drop_n;
      sub_q := phrase_q;
      for i in 1..nw loop
        if (mask >> (i - 1)) & 1 = 1 then
          sub_q := coalesce(sub_q && tqs[i], tqs[i]);
        end if;
      end loop;
      continue when sub_q is null;
      for y in select distinct coalesce(d.year, 0) from public.documents d
                where (year_from is null or d.year >= year_from) and (year_to is null or d.year <= year_to)
                order by 1 desc
      loop
        insert into find_cand
          select p.library_id, p.page, p.words, p.year, p.doc_type
          from public.pages p
          where p.words @@ sub_q
            and p.year = y
            and p.wba_id <> ''
            and (types is null or p.doc_type = any(types))
            and (ids is null or p.wba_id = any(ids))
          limit least(per_year, cap - got)
        on conflict do nothing;
        get diagnostics n = row_count;
        got := got + n;
        exit when got >= cap;
      end loop;
      exit levels when got >= cap;
    end loop;
    exit levels when got >= need;
  end loop;
  insert into public.search_log (key_name, q, company, year_from, year_to, doc_type, n, ms, client, relaxed, terms)
    values (k, left(q, 500), left(company_filter, 200), year_from, year_to, left(type_filter, 200), got,
            extract(milliseconds from clock_timestamp() - t0)::int, left(client, 60), drop_n,
            left(array_to_string(words, ' ') || case when coalesce(array_length(phrases, 1), 0) > 0 then ' "' || array_to_string(phrases, '" "') || '"' else '' end, 300))
    returning id into lid;
  return query
    with scored as (
      select c.library_id, c.page, c.year, c.doc_type,
             (select count(*) from unnest(tqs) tq where c.words @@ tq)::int as matched,
             ts_rank_cd(c.words, or_q, 5) as rel,
             (together_q is not null and c.words @@ together_q) as together
      from find_cand c),
    weighted as (
      select s.*, (s.rel * public.type_weight(s.doc_type)
                   / (1 + 0.15 * greatest(this_year - coalesce(nullif(s.year, 0), this_year), 0))
                   * power((s.matched + 0.5) / (greatest(nw, 1) + 0.5), 2)
                   * case when s.together then 1.5 else 1.0 end)::real as score
      from scored s),
    ranked as (
      select w.*, row_number() over (partition by w.library_id order by w.score desc, w.page) as in_doc
      from weighted w),
    top as (
      select r.library_id, r.page, r.year, r.doc_type, r.score,
             row_number() over (order by r.score desc, r.year desc, r.library_id, r.page) as rk
      from ranked r
      where r.in_doc <= 3
      order by r.score desc, r.year desc, r.library_id, r.page
      limit need)
    select lid, t2.rk::int, t2.library_id, d.company, d.wba_id, t2.year, t2.doc_type, d.label, d.url, t2.page, t2.score,
           ts_headline('simple', left(p.body, 200000), or_q,
                       'MaxFragments=2, MaxWords=40, MinWords=15, FragmentDelimiter=" … ", StartSel=⟦, StopSel=⟧')
    from top t2
    join public.documents d on d.library_id = t2.library_id
    join public.pages p on p.library_id = t2.library_id and p.page = t2.page
    order by t2.rk;
end;
$$;

-- A click on a result: which page of which document, at which rank, of which search.
create or replace function public.find_click(key text, log_id bigint, library_id text, page int, rank int default null)
returns void
language plpgsql security definer set search_path = public, extensions
as $$
begin
  if public.search_key_name(key) is null then
    raise exception 'not allowed' using errcode = '42501';
  end if;
  insert into public.search_clicks (log_id, library_id, page, rank) values (log_id, left(library_id, 40), page, rank);
end;
$$;

-- The companies, for the page to recognise a company name typed in the box: one JSON value (the web
-- interface caps set-returning functions at 1,000 rows).
create or replace function public.find_companies(key text) returns jsonb
language plpgsql stable security definer set search_path = public, extensions
set statement_timeout = '20s'
as $$
begin
  if public.search_key_name(key) is null then
    raise exception 'not allowed' using errcode = '42501';
  end if;
  if to_regclass('public.companies') is not null and (select count(*) from public.companies) > 0 then
    -- the profiles (load_companies.py): 2,000 rows, instant; industry, country and region for the box's filters
    return (select coalesce(jsonb_agg(jsonb_build_object('id', c.wba_id, 'name', c.name, 'industry', c.industry, 'country', c.country, 'region', c.region) order by c.name), '[]'::jsonb)
            from public.companies c where c.name is not null);
  end if;
  return (select coalesce(jsonb_agg(jsonb_build_object('id', c.wba_id, 'name', c.company, 'documents', c.documents) order by c.company), '[]'::jsonb)
          from (select d.wba_id, max(d.company) as company, count(*) as documents
                from public.documents d
                where d.wba_id is not null and d.wba_id <> '' and d.company is not null
                group by d.wba_id) c);
end;
$$;

revoke all on function public.search_key_name(text) from public, anon, authenticated;
revoke all on function public.type_weight(text) from public, anon, authenticated;
revoke all on function public.find_terms(text) from public, anon, authenticated;
revoke all on function public.find_word_query(text) from public, anon, authenticated;
revoke all on function public.find_pages(text, text, text, int, int, text, int, text) from public;
grant execute on function public.find_pages(text, text, text, int, int, text, int, text) to anon, authenticated;
revoke all on function public.find_click(text, bigint, text, int, int) from public;
grant execute on function public.find_click(text, bigint, text, int, int) to anon, authenticated;
revoke all on function public.find_companies(text) from public;
grant execute on function public.find_companies(text) to anon, authenticated;
