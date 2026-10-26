-- Search functions of the evidence database, for the search page and for Claude.
-- Applied by load_supabase.py after schema.sql, only where Supabase's auth schema exists.
-- The tables stay closed (row level security, no policy): the page reaches the data only through these
-- functions, which check who is asking first. Safe to run again.

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

-- Pages that contain the words (websearch syntax: "exact phrase", -excluded, or), newest documents first,
-- with a highlighted extract. Filters are optional. At most 2,000 matching pages are considered before
-- sorting, so a very common word answers quickly; the summary says when there are more.
create or replace function public.search_pages(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null, max_rows int default 50)
returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text,
               page int, excerpt text)
language plpgsql stable security definer set search_path = public, extensions
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  if numnode(tsq) = 0 then
    return;
  end if;
  return query
    with hits as (
      select p.library_id, p.page, d.year as y
      from public.pages p
      join public.documents d on d.library_id = p.library_id
      where public.page_words(p.body) @@ tsq
        and (company_filter is null or d.company ilike '%' || company_filter || '%' or d.wba_id = company_filter)
        and (year_from is null or d.year >= year_from)
        and (year_to is null or d.year <= year_to)
        and (type_filter is null or d.doc_type = type_filter)
      limit 2000
    ), top as (
      select * from hits order by y desc nulls last, library_id, page limit least(greatest(max_rows, 1), 200)
    )
    select d.library_id, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url, t.page,
           ts_headline('simple', left(p.body, 200000), tsq,
                       'MaxFragments=2, MaxWords=40, MinWords=15, FragmentDelimiter=" … ", StartSel=<mark>, StopSel=</mark>')
    from top t
    join public.documents d on d.library_id = t.library_id
    join public.pages p on p.library_id = t.library_id and p.page = t.page
    order by t.y desc nulls last, t.library_id, t.page;
end;
$$;

-- How many companies, documents and pages contain the words, with the same filters. Counts stop at
-- 100,000 pages (capped = true) so a common word does not run for minutes.
create or replace function public.search_summary(
  q text, company_filter text default null, year_from int default null, year_to int default null,
  type_filter text default null)
returns table (companies bigint, documents bigint, pages bigint, capped boolean)
language plpgsql stable security definer set search_path = public, extensions
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
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
      select d.wba_id, p.library_id
      from public.pages p
      join public.documents d on d.library_id = p.library_id
      where public.page_words(p.body) @@ tsq
        and (company_filter is null or d.company ilike '%' || company_filter || '%' or d.wba_id = company_filter)
        and (year_from is null or d.year >= year_from)
        and (year_to is null or d.year <= year_to)
        and (type_filter is null or d.doc_type = type_filter)
      limit 100001
    )
    select count(distinct h.wba_id), count(distinct h.library_id), least(count(*), 100000), count(*) > 100000
    from hits h;
end;
$$;

-- Evidence passages (climate, nature, social, financial) that contain the words, optionally one group.
create or replace function public.search_passages(
  q text, dim text default null, company_filter text default null, year_from int default null,
  year_to int default null, max_rows int default 50)
returns table (library_id text, company text, wba_id text, year int, doc_type text, label text, url text,
               page int, dims text[], area text, excerpt text)
language plpgsql stable security definer set search_path = public, extensions
as $$
declare
  tsq tsquery := websearch_to_tsquery('simple', coalesce(q, ''));
begin
  if not public.can_search() then
    raise exception 'not allowed to search' using errcode = '42501';
  end if;
  return query
    select d.library_id, d.company, d.wba_id, d.year, d.doc_type, d.label, d.url, ps.page, ps.dims, ps.area,
           case when numnode(tsq) = 0 then left(ps.body, 600)
                else ts_headline('simple', ps.body, tsq, 'MaxFragments=1, MaxWords=60, MinWords=25, StartSel=<mark>, StopSel=</mark>') end
    from public.passages ps
    join public.documents d on d.library_id = ps.library_id
    where (numnode(tsq) = 0 or public.page_words(ps.body) @@ tsq)
      and (dim is null or dim = any(ps.dims))
      and (company_filter is null or d.company ilike '%' || company_filter || '%' or d.wba_id = company_filter)
      and (year_from is null or d.year >= year_from)
      and (year_to is null or d.year <= year_to)
    order by d.year desc nulls last, d.library_id, ps.page, ps.n
    limit least(greatest(max_rows, 1), 200);
end;
$$;

revoke all on function public.search_pages(text, text, int, int, text, int) from public, anon;
revoke all on function public.search_summary(text, text, int, int, text) from public, anon;
revoke all on function public.search_passages(text, text, text, int, int, int) from public, anon;
revoke all on function public.can_search() from public, anon;
revoke all on function public.email_may_search(text) from public, anon, authenticated;
grant execute on function public.search_pages(text, text, int, int, text, int) to authenticated;
grant execute on function public.search_summary(text, text, int, int, text) to authenticated;
grant execute on function public.search_passages(text, text, text, int, int, int) to authenticated;
grant execute on function public.can_search() to authenticated;
