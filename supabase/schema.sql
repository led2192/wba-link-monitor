-- Schema of the evidence database (Supabase project wba-evidence).
-- Applied by load_supabase.py on every committed run; every statement is safe to run again.
-- Nothing here is readable from a browser: row level security is on and no policy grants access yet.
-- The search page will add policies for signed-in WBA users in its own pull request.

create extension if not exists vector with schema extensions;
create extension if not exists pg_trgm with schema extensions;
create extension if not exists btree_gin with schema extensions;   -- year, type and company inside the word index

-- One row per document in the full-text store. Metadata is the store's latest snapshot of the
-- Disclosure Library (report_library); the library stays the source of truth.
create table if not exists public.documents (
  library_id    text primary key,          -- report_library.library_id
  record_id     text,                      -- report_library record id
  wba_id        text,
  company       text,
  doc_type      text,                      -- effective type at the time of the snapshot
  year          int,
  lang          text,
  label         text,
  url           text,
  pages         int,
  chars         bigint,
  text_status   text,                      -- ok, no_text, encrypted, parse_fail, not_pdf ...
  text_part     text,                      -- store part the text came from (newest wins)
  extracted_at  text,
  loaded_at     timestamptz not null default now()
);
create index if not exists documents_wba_id on public.documents (wba_id);
create index if not exists documents_year on public.documents (year);
create index if not exists documents_doc_type on public.documents (doc_type);

-- Full text, one row per page.
--   words: the page's words with their positions (page_words), stored so that a phrase search can check
--          candidates without re-reading the text (26 Oct 2026: re-reading made "scope 3" take minutes).
--   year, doc_type, wba_id: copies of the document's values, kept in step by the trigger below, so the
--          word index (pages_search) can answer "newest first", a type filter or a company filter
--          without fetching pages that will be thrown away.
-- The index pages_search is built by the loader once every row has its words (building it once is
-- much faster than updating it row by row); until then searches use the older pages_words index.
create table if not exists public.pages (
  library_id  text not null references public.documents (library_id),
  page        int  not null,
  body        text not null,
  primary key (library_id, page)
);
alter table public.pages add column if not exists words tsvector;
alter table public.pages add column if not exists year int;        -- 0 = undated
alter table public.pages add column if not exists doc_type text;   -- '' = untyped
alter table public.pages add column if not exists wba_id text;     -- '' = no company (out of the library's scope)

-- The text the word index reads: the first 200,000 characters of a page in the 'simple'
-- configuration (no stemming, so it works in every language and finds figures and acronyms).
create or replace function public.page_words(t text) returns tsvector
  language sql immutable parallel safe
  as $$ select to_tsvector('simple'::regconfig, left(t, 200000)) $$;

-- Every page written gets its words and its document's year, type and company.
create or replace function public.pages_before_write() returns trigger
  language plpgsql
  as $$
  begin
    new.words := public.page_words(new.body);
    select coalesce(d.year, 0), coalesce(d.doc_type, ''), coalesce(d.wba_id, '')
      into new.year, new.doc_type, new.wba_id
      from public.documents d where d.library_id = new.library_id;
    new.year := coalesce(new.year, 0); new.doc_type := coalesce(new.doc_type, ''); new.wba_id := coalesce(new.wba_id, '');
    return new;
  end $$;
drop trigger if exists pages_before_write on public.pages;
create trigger pages_before_write before insert or update of body on public.pages
  for each row execute function public.pages_before_write();

-- When a document's year, type or company changes (a correction in the library), its pages follow.
create or replace function public.documents_after_change() returns trigger
  language plpgsql
  as $$
  begin
    update public.pages set year = coalesce(new.year, 0), doc_type = coalesce(new.doc_type, ''), wba_id = coalesce(new.wba_id, '')
     where library_id = new.library_id
       and (year, doc_type, wba_id) is distinct from (coalesce(new.year, 0), coalesce(new.doc_type, ''), coalesce(new.wba_id, ''));
    return null;
  end $$;
drop trigger if exists documents_after_change on public.documents;
create trigger documents_after_change after update of year, doc_type, wba_id on public.documents
  for each row
  when (old.year is distinct from new.year or old.doc_type is distinct from new.doc_type or old.wba_id is distinct from new.wba_id)
  execute function public.documents_after_change();

-- Evidence passages (evidence_chunks.py): about 120 words each, tagged climate, nature, social,
-- financial. embedding is filled later, when the embeddings provider is chosen.
create table if not exists public.passages (
  library_id   text not null references public.documents (library_id),
  page         int  not null,
  n            int  not null,              -- passage number on the page
  dims         text[] not null,
  topics       jsonb,
  terms        jsonb,
  area         text,                       -- planning, implementation, performance, unclear
  integration  text[],
  has_number   boolean,
  body         text not null,
  lexicon      text,                       -- lexicon version that tagged it
  embedding    extensions.vector(1024),
  primary key (library_id, page, n)
);
create index if not exists passages_dims on public.passages using gin (dims);
create index if not exists passages_words on public.passages using gin (public.page_words(body));

-- Extracted values, one per company x attribute x period. Empty until the extraction pilot.
create table if not exists public.datapoints (
  id            bigint generated always as identity primary key,
  wba_id        text not null,
  attribute     text not null,             -- GMC code, or the key of a new attribute
  value_raw     text,                      -- as printed
  value_num     numeric,                   -- normalized number
  value_bool    boolean,
  value_text    text,
  unit          text,
  period_start  date,
  period_end    date,
  scope         text,                      -- group, or the part it covers
  library_id    text references public.documents (library_id),
  page          int,
  quote         text,
  method        text check (method in ('stated', 'calculated')),
  extractor     text,                      -- model or person and version
  confidence    real,
  validated_by  text,
  validated_at  timestamptz,
  created_at    timestamptz not null default now()
);
create index if not exists datapoints_company_attribute on public.datapoints (wba_id, attribute);

-- Every figure a reader found, one row per company x attribute x year x document x method (propose_values.py,
-- since 1 Nov 2026). A 2023 figure printed in the 2023, 2024 and 2025 reports is three rows: attribute_values
-- shows one of them (the year's own read first) and says in other_reports how the others differ, so the
-- analysts see one value and the restatements stay here. version_id is deterministic, so a re-run updates.
create table if not exists public.value_versions (
  version_id   text primary key,           -- sha1 of wba_id|attribute|year|library_id|method
  wba_id       text not null,
  attribute    text not null,              -- GMC code
  year         int  not null,              -- reporting year of the figure
  value        double precision not null,  -- printed number x scale
  unit         text,
  printed      text,                       -- as printed
  scale        double precision,
  column_label text,                       -- the column heading it was read under
  basis        text,                       -- Scope 2: location-based, market-based, not stated
  library_id   text not null,
  page         int,
  read_for     int,                        -- the year asked when it was read (its documents are that year's or the next)
  method       text not null,              -- model reader
  first_seen   timestamptz not null default now(),
  last_seen    timestamptz not null default now()
);
create index if not exists value_versions_company on public.value_versions (wba_id, attribute, year);
alter table public.value_versions enable row level security;

-- Which store files are already in the database, so a run only loads what is new.
create table if not exists public.load_log (
  name       text primary key,
  kind       text not null,                -- pages, evidence
  documents  int,
  rows       int,
  loaded_at  timestamptz not null default now()
);

-- Where a long job stands (the words backfill, the index build), so a run continues where the last stopped.
create table if not exists public.load_state (
  key        text primary key,
  value      text,
  updated_at timestamptz not null default now()
);
alter table public.load_state enable row level security;

alter table public.documents  enable row level security;
alter table public.pages      enable row level security;
alter table public.passages   enable row level security;
alter table public.datapoints enable row level security;
alter table public.load_log   enable row level security;
