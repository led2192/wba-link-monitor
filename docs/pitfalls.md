# Pitfalls

Lessons that cost time to learn. Read before changing the area they name.

## Airtable
- **0 equals blank in formulas.** `{page_count} = BLANK()` is true for 0, so a failure marked with 0 stays in the queue forever. Queue on a text status field.
- **`typecast` fabricates.** A string sent to a link field with `typecast: true` creates an empty row in the linked table when nothing matches. Resolve record ids first; never send strings to link fields.
- **One record twice in one batch is a 422** that fails the whole call (the 21 Oct harvest failure, PR #27).
- **Patching rows while paging a filtered query shifts the offset**; some rows are skipped that night. Harmless when the queue is re-read the next night, wrong for one-shot jobs.
- **The 500,000-record cap counts every table.** Bulk imports and new logging tables come out of the same budget.

## Fetching and documents
- **`cryptography` is a silent pypdf dependency.** Without it AES-encrypted PDFs, most corporate PDFs, fail to parse.
- **A 200 is not a page.** Consent walls, Cloudflare challenges and JavaScript shells answer 200 with no links. A page that suddenly shows no links is suspect, not empty.
- **Extensionless documents are invisible to shape tests.** Q4 `/static-files/<uuid>`, Kentico `/getattachment/`, Liferay `.pdf/<uuid>`, `/files/<slug>`, DAM hosts (Widen, Bynder): add the pattern to `monitor_core` when a site shows documents but no links are kept.
- **Truncated downloads look like PDFs.** Check Content-Length and the `%%EOF` tail before parsing; a cut body sends pypdf into a slow recovery.
- **One transient failure must not become a permanent verdict.** A 403 from a challenge, an OpenFIGI 429, a site down for a night: retry with an attempt counter before marking blocked, none or not_pdf.

## Identity and duplicates
- **Idempotency first.** A re-insert bug combined with a scope expansion once flooded the review queue. Upsert by id before widening anything.
- **Rows without a URL are invisible to the harvester's guard.** Uploads and Source Library imports can get a URL-keyed twin; dedupe's hash signal is what catches it.
- **doc_key strips short query parameters and 32-hex path segments.** Sites that select documents with them collapse distinct documents into one.
- **A purge tombstone is forever.** A wrong duplicate decision, once purged, blocks the real document.

## Classification and years
- **Two classifiers disagree on half the corpus.** Never pick one silently; measure on labels.
- **Non-English documents are not AI-typed.** The classify formula runs on English only; 30,000+ rows rely on the URL guess or nothing.
- **Years in file names are publication years.** "annual-report-2025.pdf" is usually FY2024. Use the reporting-period evidence.
- **A cited 10-K is usually the right year.** US sustainability reports, CDP answers and proxy statements cite "our Annual Report on Form 10-K for the year ended ..." for the very period they report on. Ignoring such citations broke more years than it fixed (measured 23 Oct 2026). Measure a rule before believing an anecdote.
- **The top of a cover can be a table.** "Ten Year Summary 2015 2016 ... 2024" starts with the oldest year. Three or more years in the title line are a header, not the document's year.
- **Bulk rewrites move the completeness numbers.** Run a bulk year or type rewrite only after its pull request shows the measured effect (`year_eval.py`), and read the next morning's `completeness_change` on the companies that lost a check.
- **AI cost scales with input length.** The cover_text cap (`COVER_CHARS`) is the lever; whitespace collapsing barely matters.

- **The sections of a split report carry the whole report's contents.** UK and South African report sections repeat the full navigation bar ("Strategic report | Governance | Financial statements") on their first pages, so the parts named there say nothing about the file in hand. Compare the contents page numbers with the file's own page count.
- **"Reasonable assurance" appears in every limited-assurance report.** The ISAE 3000 boilerplate says the procedures are "less in extent than for a reasonable assurance engagement", and every financial audit opinion obtains "reasonable assurance" about the financial statements. Read the level only near sustainability words, away from the audit and internal-control wording, and never from a sentence that compares the two levels.
- **Some PDFs give text shifted by one character.** Tokio Marine's integrated report reads "DPNQBSFEXJUI" for "compared with" (a font with an off-by-one encoding). Such pages carry no terms and no years; a fix belongs in the text extraction, not in the readers.

- **A reader that waits for the full text must only wait for types the full-text build reads.** The first sustainability_class run left about 60,000 interim reports, statutory documents and presentations blank "waiting for the full text", which `build_fulltext.py` never reads (CORE_TYPES). Check the reader's scope against CORE_TYPES whenever either changes.

## Strategy
- **Sitemaps add junk, not reports** (37,000 bad rows in one run). Page monitoring plus search is the reliable pair.
- **The goal is events, not URLs.** "Company published a report" is the signal; filtering belongs in the pipeline, not in a review queue.
