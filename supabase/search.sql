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
create or replace function public.companies_matching(company_filter text) returns text[]
  language sql stable security definer set search_path = public
  as $$
    select case when company_filter is null or btrim(company_filter) = '' then null
                else coalesce((select array_agg(distinct d.wba_id) from public.documents d
                               where d.wba_id is not null
                                 and (d.wba_id = btrim(company_filter) or d.company ilike '%' || btrim(company_filter) || '%')),
                              array['']::text[]) end
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
-- first, with a highlighted extract (⟦ ⟧ around the matched words). Filters are optional.
create or replace function public.search_pages(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 50)
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
             ts_headline('simple', left(s.body, 200000), tsq,
                         'MaxFragments=2, MaxWords=40, MinWords=15, FragmentDelimiter=" … ", StartSel=⟦, StopSel=⟧')
      from (
        select p.library_id, p.page, p.body, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url
        from public.pages p
        join public.documents d on d.library_id = p.library_id
        where p.words @@ tsq
          and p.year = y
          and p.wba_id <> ''
          and (type_filter is null or p.doc_type = type_filter)
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
          and (type_filter is null or p.doc_type = type_filter)
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
        and (type_filter is null or p.doc_type = type_filter)
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
  n bigint := 0;
  c record;
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
                 and (type_filter is null or p.doc_type = type_filter)) then
      n := n + 1;
    end if;
  end loop;
  return n;
end;
$$;

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

revoke all on function public.search_pages(text, text, int, int, text, int) from public, anon;
revoke all on function public.search_summary(text, text, int, int, text) from public, anon;
revoke all on function public.search_company_count(text, text, int, int, text) from public, anon;
grant execute on function public.search_company_count(text, text, int, int, text) to authenticated;
revoke all on function public.search_passages(text, text, text, int, int, int) from public, anon;
revoke all on function public.can_search() from public, anon;
revoke all on function public.search_companies() from public, anon;
grant execute on function public.search_companies() to authenticated;
revoke all on function public.export_pages(text, text, int, int, text, int) from public, anon;
grant execute on function public.export_pages(text, text, int, int, text, int) to authenticated;
revoke all on function public.email_may_search(text) from public, anon, authenticated;
revoke all on function public.companies_matching(text) from public, anon, authenticated;
grant execute on function public.search_pages(text, text, int, int, text, int) to authenticated;
grant execute on function public.search_summary(text, text, int, int, text) to authenticated;
grant execute on function public.search_passages(text, text, text, int, int, int) to authenticated;
grant execute on function public.can_search() to authenticated;
