# The evidence database

What WBA needs from the pipeline is evidence and data per company: the sources are the proof behind each figure, the text and the extracted values are the product. The evidence database is where the full text of every disclosure lives in a form that can be searched by word, by phrase and, later, by meaning, and where extracted values will be kept with their quote and page. This page says what it holds, how to use it, how it is kept current, and how it is set up. Owner's direction of 24 Oct 2026; built on 26 Oct 2026.

## 1. The layers

| Layer | What it is | Where |
| --- | --- | --- |
| 0. Sources | Company pages watched, reports harvested, files attached | Disclosure Library (Airtable), the text store |
| 1. Corpus | Every page of every attached disclosure, every type, every year | `pages` |
| 2. Groups | Passages of about 120 words tagged climate, nature, social, financial (`evidence_chunks.py`) | `passages` |
| 3. Attributes | A value per company and attribute, with quote, page, period and who validated it | `datapoints` (empty until the extraction pilot) |

The Disclosure Library stays the source of truth for what a document is (company, type, year, discard); the database copies that metadata with the text and can be rebuilt from the text store at any time.

## 2. What is in it

Supabase project `wba-evidence` (managed Postgres, region Frankfurt, Pro plan, Small compute, disk 100 GB). Tables, all in schema `public`:

| Table | Rows (26 Oct 2026) | What |
| --- | --- | --- |
| `documents` | 186,902 | One per document the text store has tried: company, type, year, language, title, address, pages, characters, `text_status` (ok, no_text, not_pdf, no_file ...). Documents with no company are out of the library's scope now (discarded since their text was read) and are left out of searches. |
| `pages` | 6,640,513 | The text of each page (`body`), its words with positions (`words`), and copies of the document's `year`, `doc_type` and `wba_id` for the index. |
| `passages` | 190,987 | The evidence passages with their tags (`dims`, `area`, `topics`, `terms`), and an empty `embedding` column for search by meaning later. |
| `value_versions` | grows with each committed Propose values run | Every figure the model reader found, since 1 Nov 2026: one row per company, attribute, year, document and method (`version_id` = sha1 of those), with value, unit, as printed, scale, column heading, Scope 2 basis, page, the year asked, first and last seen. A 2023 figure printed in the 2023, 2024 and 2025 reports is three rows; attribute_values shows one and lists the others in `other_reports`. Restatements and how a company's figures change from report to report are read here. |
| `datapoints` | 0 | Extracted values: as printed and normalized, unit, period, scope, document, page, quote, `method` (stated or calculated), extractor, confidence, who validated. |
| `load_log`, `load_state` | | Which store files are in, and where a long job (the words backfill) stands. |
| `search_access` | | Addresses outside WBA allowed to search. |

The word index is `pages_search`: a GIN index on the words together with year, type and company (extension `btree_gin`), in the `simple` text configuration (no stemming), so it works in every language and finds figures and acronyms as written. Known gap: Chinese and Japanese text has no spaces, so it cannot be split into words by this index; a different index (PGroonga is available on Supabase) is the option when that matters.

## 3. Searching

### The search page
`site/index.html`, published on GitHub Pages by the Search page workflow. Sign in with the code (or the link) sent by email; the session stays in the browser until you sign out; search all text or the evidence passages; filter by company (one or several, picked from the list of the companies in the database, function `search_companies`, or any part of a name or id typed by hand), year, type (one or several) or group; the all-text results come one line per document by default (`search_documents`: how many of its pages match, the extract of its best page, and "Show pages" for the pages of that document, `search_pages` with `doc_filter`), or page by page with the option off; each result shows company, year, type, title, page, a highlighted extract and a link that opens the PDF at that page; counts of companies, documents and pages (shown when they arrive; the results come first; the counts stop at 20,000 pages and then carry a "+", and the exact number of companies fills in after, from `search_company_count`, which asks company by company); Cancel stops a search; CSV download of up to 5,000 pages (`export_pages`: the same pages without the highlighted extracts, a plain window of text around the first word instead). Words match as written: `"exact phrase"`, `-word` to exclude, `or` between alternatives. Results come newest first.

### Who may search
Every search runs through a database function that first checks the caller: a signed-in address at worldbenchmarkingalliance.org, an address listed in `search_access` (owner, SQL editor: `insert into search_access (email) values ('name@example.com');`), or a direct database session (the loader, the checks, the SQL editor). The tables themselves are closed to the API (row level security, no policy), and the key in the page is the publishable one, which is meant to be public.

### In the SQL editor (owner)
Supabase → SQL Editor → New query; only `select` statements. The functions the page uses:

```sql
select * from search_pages('"living wage"');                       -- newest first, 50 rows
select * from search_pages('climate', 'Unilever', 2024, 2026, 'Annual Report', 100);
select * from search_summary('"net zero"');                        -- companies, documents, pages (counts stop at 20,000 pages)
select search_company_count('revenue');                               -- exact companies, whatever the word (a probe per company)
select * from search_documents('revenue', 'Unilever; Nestlé');       -- one line per document, two companies; types too: 'Annual Report; CDP Report'
select * from search_pages('revenue', doc_filter => 'PT_00123-ab12cd34ef');  -- the pages of one document
select * from search_passages('water withdrawal', 'nature');       -- passages, optionally one group
```

Or the tables directly, for example the pages of one document that hold a phrase:

```sql
select page, left(body, 300) from pages
where library_id = 'PT_00197-358cd67795' and words @@ websearch_to_tsquery('simple', '"net zero"')
order by page;
```

Do not edit the tables by hand: the loader overwrites them, and anything typed there is lost.

### How a search runs
Every condition (words, year, type, company) is answered inside the index, so only pages that match everything are fetched; a phrase is checked against the stored words, not the text; results come year by year from the newest, stopping at the rows asked, so a rare phrase and a common word cost about the same. Measured on a 1-in-11 copy of the corpus (607,081 pages, Small compute's memory settings): "scope 3" count 1.6 s cold, results 0.6 s; filtered searches a few milliseconds. The first design (words without positions, filters after the fetch) took minutes on "scope 3": [pitfalls.md](pitfalls.md), 26 Oct 2026.

## 4. How it is kept current

| Job | When | What |
| --- | --- | --- |
| Build full text | nightly 21:37 | Reads every attached disclosure of every year into the text store (private repository), newest first |
| Evidence passages | after Build full text | Tags passages of 2024+ reports (climate, nature, social, financial) |
| Evidence database load | every 6 hours (41 */6) | Copies what is new from the text store into the database: documents (every run, with the latest metadata), pages and passages (files not loaded yet; a re-extracted document's pages are replaced). Stops at the repository variable `SUPABASE_MAX_GB`. |
| Search check | manual, and on pull requests that touch the search functions | Ten sample searches with counts and timings, recorded in Pipeline QA |
| Supabase check | manual, and on pull requests that touch it | Read-only test that the three secrets reach the project |
| Gold evaluation | manual | Does the search find the page the analyst cited? (section 6) |

The search index `pages_search` is built by the database itself, through the pg_cron job `build-pages-search` that the load hands it to (it takes over three hours, longer than a GitHub job may run); the job removes itself once the index exists. Progress, in the SQL editor: `select phase, blocks_done, blocks_total from pg_stat_progress_create_index;`. Two triggers in the database keep the page copies in step: a page written gets its document's year, type and company; a document whose year, type or company changes (a correction in the library) updates its pages. Scripts: [scripts.md](scripts.md). Schema: `supabase/schema.sql`; search functions: `supabase/search.sql`; both applied by the loader and safe to run again.

## 5. Setting it up (owner)

Done on 26 Oct 2026; kept here for a new project or a new person. Never paste a key or password into chat, documents or the repository: only into GitHub secrets and a password manager.

1. **Supabase.** Organization WBA, plan Pro, spend cap off (a capped project cannot grow its disk). Project `wba-evidence`, region Central EU (Frankfurt), compute Small (2 GB), Data API on, "automatically expose new tables" off, automatic row level security on. Disk 100 GB (Project Settings → Compute and Disk; it can be raised once every 6 hours, never lowered). MFA on the account.
2. **Keys and address.** Project Settings → API Keys → a secret key named `github_actions` (not the publishable one, not the legacy service_role). The database address: Connect → Session pooler → URI, with the database password put in; the session pooler, because GitHub's runners have no IPv6 and the direct connection is IPv6 only. A password with only letters and digits avoids address problems.
3. **GitHub secrets** (repository `wba-link-monitor`, Settings → Secrets and variables → Actions): `SUPABASE_URL` (`https://<project ref>.supabase.co`, nothing after it), `SUPABASE_SERVICE_KEY` (the secret key), `SUPABASE_DB_URL` (the pooler address). **Variables:** `SUPABASE_MAX_GB` (85 with a 100 GB disk), `SUPABASE_PUBLISHABLE_KEY` (for the page). Run the Supabase check workflow: it says which of the three is wrong without printing it.
4. **The search page.** Settings → Pages → Source: GitHub Actions. Supabase → Authentication → URL Configuration: Site URL and a redirect URL `https://led2192.github.io/wba-link-monitor/`. Addresses outside WBA that may search: `search_access` (section 3).
5. **First load.** Actions → Evidence database load → mode commit. About 3 hours for the whole store; later runs load only what is new.

## 6. Measuring the evidence layer: the gold evaluation

The 2025 benchmarks left a validated answer per company and attribute (the golden dataset: Attributes bases with the value, the analyst's quote, the page and the source document; ACT Core was validated at attribute level, the social and non-social bases at indicator level). `gold_eval.py` uses them to measure the evidence layer without any model, for the pilot attributes in `gold_attributes.json` (board climate expertise, climate policies supported, low-carbon capex, Scope 1, Scope 2, revenue):

1. is the cited document in the library and its text in the database (collection);
2. is the analyst's quote in our text, found as a six-word phrase inside the document, which also gives the page in our numbering (extraction; analysts often write the printed folio);
3. with the attribute's search words, is that page among the document's top 1, 3 or 10 pages (retrieval).

The first full run (27 Oct 2026, 5,266 answers): about half the cited documents were not matched in the library; when the text was there, the cited page was in the top 10 for 62% (board climate expertise) to 97% (Scope 1) of answers.

What matters is the right value, not the analyst's document (owner's rule, 28 Oct). For the number attributes, `value_eval.py` measures that ceiling directly: is the validated number on any page of the company's documents, printed at any scale ("513,727" in millions, "9.83" billion, "65 534", "2.561.249"), in that year's documents, and on a page that names the attribute. Extraction is scored the same way, by value.

Whatever model reads the passages later cannot be right about a page that is not among them, so this is the floor. Reads the gold bases with the read-only secret `GOLD_TOKEN`; writes nothing anywhere but `gold_eval.md` and the run's counters in Pipeline QA. Two things the gold data showed: some validated answers are calculated, not copied (a board share worked out from "1 of 12"), which is why `datapoints.method` distinguishes stated from calculated; and of 480 validated low-carbon capex answers only 29 carry a quote, so attributes with many quotes weigh more.

## 7. Where this is going

In order, as of 26 Oct 2026:
1. The first gold evaluation and the search words it suggests per attribute.
2. An attribute specification (type, options, unit, applicability, definition, where the evidence usually is), from the gold bases' attribute tables and the ITA attribute sheet, and an extractor that proposes value, quote, page and confidence per company and attribute, measured against the validated answers. Analysts review proposals; they do not query.
3. Full-page OCR of scanned key reports (about 450,000 pages), so scans enter the corpus beyond their first pages.
4. Search by meaning (embeddings) over the passages, once a provider is chosen.
5. A Claude connector to the same database.

Decisions and what was rejected: [decisions.md](decisions.md), 24 and 26 Oct 2026.
