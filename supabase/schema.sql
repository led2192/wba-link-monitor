-- Schema of the evidence database (Supabase project wba-evidence).
-- Applied by load_supabase.py on every committed run; every statement is safe to run again.
-- Nothing here is readable from a browser: row level security is on and no policy grants access yet.
-- The search page will add policies for signed-in WBA users in its own pull request.

create extension if not exists vector with schema extensions;
create extension if not exists pg_trgm with schema extensions;

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

-- Full text, one row per page. The word index (pages_words) is built by the loader after the
-- first load, because building it once is much faster than updating it row by row.
create table if not exists public.pages (
  library_id  text not null references public.documents (library_id),
  page        int  not null,
  body        text not null,
  primary key (library_id, page)
);

-- The text the word index reads: the first 200,000 characters of a page in the 'simple'
-- configuration (no stemming, so it works in every language and finds figures and acronyms).
-- Queries must use the same function to hit the index.
create or replace function public.page_words(t text) returns tsvector
  language sql immutable parallel safe
  as $$ select to_tsvector('simple'::regconfig, left(t, 200000)) $$;

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

-- Which store files are already in the database, so a run only loads what is new.
create table if not exists public.load_log (
  name       text primary key,
  kind       text not null,                -- pages, evidence
  documents  int,
  rows       int,
  loaded_at  timestamptz not null default now()
);

alter table public.documents  enable row level security;
alter table public.pages      enable row level security;
alter table public.passages   enable row level security;
alter table public.datapoints enable row level security;
alter table public.load_log   enable row level security;
