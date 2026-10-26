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
- **The biggest file is usually the most important one.** Full annual and integrated reports run to 10-60 MB; Airtable's upload endpoint stops at 5 MB. Anything that needs the attachment silently skips them; read from the document's own address instead.
- **Address words shared by several types decide nothing.** "sustainability report" was listed under both Sustainability Report and Integrated Report, and a tie went to whichever type came first in the file (CLP's sustainability reports typed Integrated Report, Oct 2026). Shared tokens now weigh slightly less than exclusive ones; "form 10" matched 10-Q quarterly reports as 10-K.
- **A row is not a document until it has its file.** add_link added PDF rows the site would not let us download; they sat untyped with "being read" in add_result. Open the link first and say plainly when the file is missing.
- **AI cost scales with input length.** The cover_text cap (`COVER_CHARS`) is the lever; whitespace collapsing barely matters.

- **The sections of a split report carry the whole report's contents.** UK and South African report sections repeat the full navigation bar ("Strategic report | Governance | Financial statements") on their first pages, so the parts named there say nothing about the file in hand. Compare the contents page numbers with the file's own page count.
- **"Reasonable assurance" appears in every limited-assurance report.** The ISAE 3000 boilerplate says the procedures are "less in extent than for a reasonable assurance engagement", and every financial audit opinion obtains "reasonable assurance" about the financial statements. Read the level only near sustainability words, away from the audit and internal-control wording, and never from a sentence that compares the two levels.
- **Some PDFs give text shifted by one character.** Tokio Marine's integrated report reads "DPNQBSFEXJUI" for "compared with" (a font with an off-by-one encoding). Such pages carry no terms and no years; a fix belongs in the text extraction, not in the readers.

- **A reader that waits for the full text must only wait for types the full-text build reads.** The first sustainability_class run left about 60,000 interim reports, statutory documents and presentations blank "waiting for the full text", which `build_fulltext.py` never reads (CORE_TYPES). Check the reader's scope against CORE_TYPES whenever either changes. (Since 24 Oct 2026 the nightly build reads every type, so these documents get their full text too; a reader that decided them from first pages may read them again.)

## Strategy
- **Sitemaps add junk, not reports** (37,000 bad rows in one run). Page monitoring plus search is the reliable pair.
- **The goal is events, not URLs.** "Company published a report" is the signal; filtering belongs in the pipeline, not in a review queue.
- **A formula field can return an object, not text.** When a formula fails on a row, Airtable's API returns `{"error": "#ERROR!"}` (or `{"specialValue": "NaN"}`) instead of a string. Changing `doc_label` to read a new formula (`ai_title`, 23 Oct 2026) made some rows error and `company_coverage.py` crashed on `.split`. Reads now go through `airtable_request`, which turns such values into None; formulas that read AI fields guard with `ISERROR`. Test a formula change on the PR comparison before the nightly run.
- **An empty Airtable AI field breaks formulas that read it.** An AI field never generated (state empty) makes `{field} & ''` an error inside REGEX and LEFT, and every formula built on it shows #ERROR! (doc_label, 23 Oct 2026, visible in the analysts' synced view). Guard with `IF(ISERROR(LEN({field} & '')), '', ...)` and check the rows with an empty AI field before switching a visible formula.

## One time budget for two queues starves the second
The office-network runner had one 90-minute budget for refused files, then refused pages. On 24 Oct 2026 the 300 files took 92 minutes and no page was read, although pages unlock whole sites. Give each queue its own slice (`pc_fetch.py --page-minutes`), and check every queue in the first real run, not only the first one.

## Airtable attachment links expire after about two hours
A job that lists rows first and downloads their attachments later fails on everything it reaches after the links expire. `ocr_covers.py` read 600 rows at the start of a 172-minute run on 23 Oct 2026 and 492 downloads failed. Re-read the rows in small batches just before downloading (`fresh()` in `ocr_covers.py`), or download within minutes of listing.

## Tesseract in parallel needs OMP_THREAD_LIMIT=1
Tesseract starts several threads per page by default. With 4 OCR workers on a 4-core runner they fight for the cores: on 24 Oct 2026, 22 of 25 pages hit the 120-second timeout. `ocr_covers.py` runs each tesseract with `OMP_THREAD_LIMIT=1`, so parallelism comes from the workers only.

## GitHub skips scheduled runs when it is busy
On 24 Oct 2026 the office-network workflow (cron every 4 hours) got only two of its four runs: GitHub never created the others. A job that must run often on a self-hosted runner should repeat inside one long job (`pc_fetch.py --repeat-hours 22 --every-minutes 120`) and keep the schedule only as the fallback. Cloud jobs in the nightly chain are safe because they are chained on each other (workflow_run), not scheduled one by one.

## A 200 answer is not a file
Some sites answer a request for a PDF with their home page and status 200. Airtable's own URL fetch then stores the page under the PDF's name, and the row looks attached: on 25 Oct 2026, 79 Asian Development Bank documents held the same 215 KB copy of adb.org's home page (adb-financial-report-2025.pdf, adb-sustainability-report-2024.pdf...). Build cover text marked them not_pdf, but no retry job looked at rows that already had a file. Check the attachment's type, not only its presence; `fix_wrong_attachments.py` now sends such rows back to be fetched.

## A page type is too coarse to say a gone page is covered
`sustainability_page` covers both a company's report library and a topic page about cocoa. On 25 Oct 2026 the gone-page repair unticked 307 of 317 gone pages without searching for a replacement, because the company had some other live page of the same type: Hanwha's sustainability report list (`/sustainability/reports.do`) was "covered" by an ESG topic page, FrieslandCampina's financial and sustainability reports page by whatever else was watched. A gone page that lists reports is now covered only by another live page that also lists reports (`discover_pages.is_listing`). Judge coverage by what the page holds, not by its type.

## A thread that never returns keeps the whole job alive
Every Browser monitor run from at least 16 Oct to 25 Oct 2026 ended cancelled at GitHub's 350-minute job limit, although the script stops starting pages after 180 minutes: one browser page or a Playwright thread that never returned kept the thread pool (and the Python exit) waiting. Progress was saved every 100 pages, so most of the work survived, but the runs never ended cleanly and the last pages of each were lost. Never wait on a pool without a time limit: `playwright_monitor.py` now gives each chunk at most 20 minutes, abandons hung pages to fresh workers, and ends the process with `os._exit` after the last write.

## Rolling 24-hour counts double count when a job runs twice
base_health ran at 16:41 on 25 Oct 2026 (the 12:17 schedule, started late) and again at 00:50 on 26 Oct (chained after the nightly coverage). Both counted documents created in the 24 hours before, so the 2,880 documents harvested on the morning of the 25th showed as new on both days, and the second day looked like growth that never reached the base. Count per calendar day.

## OCR with the wrong language gives noise, and noise gives wrong years
The English Tesseract model read Chinese scans as "WENS iEt imECBS {it AER GS)". The text had enough letters to count as read, the AI made a title of it, and a year rule found 2016 in it: Wen's Foodstuff lost its recent sustainability report on 26 Oct 2026. `ocr_covers.py` now adds the company's own language model and rejects output that does not read as words; covers read before are re-read with `--redo`.

## Every pull request that adds a changelog line conflicts with the next
Each pull request adds its line at the top of docs/CHANGELOG.md, so merging one leaves every other open pull request with a conflict on the same lines, and the owner waited for someone to resolve it before the next merge (26 Oct 2026). `resolve_doc_conflicts.py` now merges main into open pull requests after every merge and keeps both sides when only documentation collides. Do not resolve those by hand from a shared working folder while another job uses it: a background resolver using `git checkout -f` in the same folder discarded uncommitted work the same morning; it runs in GitHub now.
