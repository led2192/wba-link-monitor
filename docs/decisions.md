# Decisions

One entry per choice between options: what was decided, why, and what was rejected. Newest first. Earlier entries are reconstructed from code comments and project notes; dates are when the choice took effect.

### 2026-10-22: A file named "financial statements" can be the whole annual report
In Italy ("Relazione finanziaria annuale"), Germany ("Jahresabschluss und Lagebericht") and the Nordics the statutory annual report is published under the name of its financial statements, but holds the management report, the corporate governance report and, from 2025, the sustainability statement (Acea 2025: 452 pages, file "2025-consolidated-financial-statements.pdf"). `doc_scope.py` now keeps such a file Full when its first pages list both a management report and the financial statements and the contents page numbers reach 70 to 105% of the file's length. Tested on the 487 "Part of a report" rows of 60+ pages: 7 change, all whole reports. Rejected: counting the parts named on the first pages without the page test (46 changes, most of them wrong: the sections of split UK reports carry the whole report's navigation bar); a page-count floor such as 150 pages (standalone financial statements volumes of banks run to 400 pages and are real parts).

### 2026-10-22: Document scope by rules, not the AI field
`scope` (Full document / Part of a report / Summary or short document) is decided by `doc_scope.py` from the file name (page ranges, chapter numbers, section words), the title, the page count and the first page, with the rule written in `scope_reason`. On 60 random report documents judged by hand the rules were right on 50 of 54 and the AI field `doc_scope` on 48 of 52; the AI field also leaves every non-English document blank and gives no reason. Rejected: keeping the AI field (no reason to check, English only, its 41+ page "sections" were often whole reports); a table-of-contents rule (figures on contents pages read as page numbers, tested and dropped).

### 2026-10-22: Years from the first pages when there is no full text
The full-text store holds about 42,000 documents (the report families); the other ~230,000 rows got their year only from the title or the link. The same reader now runs on `cover_text` (the first six pages) for those rows. Checked on 1,351 documents with an analyst fiscal-year-end date, using the first pages alone: score 60-79 was 96% right and 80+ was 99%, the same calibration as the full text, so the same threshold (60) applies. Evidence from the first pages is prefixed "first pages:" and the document is read again from its full text once that exists. Rejected: extending the full-text store to every document first (weeks of extraction and gigabytes for documents whose first pages already state the period).

### 2026-10-22: The Source Library is the first ground truth, with doubt flags
The analysts' reviewed 2025 base gives type and year for thousands of documents the pipeline also holds; it is used as evaluation labels before any daily label exists. Analyst labels are not assumed perfect: where the pipeline has strong contrary evidence the label is kept out of the evaluation and shown to a person. Rejected: importing more Source Library documents (webpage captures and secondary types do not move completeness; the measurement does).

### 2026-10-22: Claude reviews a daily sample; Sonnet daily, Opus weekly
Claude pre-labels the 15 daily cases (type, year, title, with a reason) and the owner adjudicates; a weekly Opus review turns patterns into pull requests. Both run on the owner's Claude plan, not the API. Rejected: Claude reviewing every new document (cost, and it would replace measurement with a second unmeasured classifier); no human labels at all (nothing to measure against). Agreement-control cases are shown without a suggestion so the accuracy measurement is not anchored.

### 2026-10-22: Separate Pipeline QA base
Failures, runs, labels and evals live in their own base, keyed so a recurring failure is one row. Rejected: logging inside the Disclosure Library (500,000-record cap, and the log would grow with every run); GitHub logs (expire after 90 days, cannot be compared across commits).

### 2026-10-22: Disagreements matter only when they move a completeness check
URL type and AI type disagree on half the English corpus. A disagreement is a failure only when choosing one type over the other changes a completeness check (sustainability family, annual family, Code of Conduct, Human Rights Policy), and a written row only for report-shaped 2023+ documents. Rejected: treating every disagreement as a failure (40,000 low-stakes ones, such as Financial Statement vs Interim Reports, change nothing).

### 2026-10-21: One year field, fiscal-year convention
`year` is the year in which the reporting period ends (the Source Library convention, 97% of analyst records), read from the full text with a confidence score; the AI year counts only when its quoted evidence is found in the document. Rejected: the year in the file name or upload folder (systematically off by one for reports published the following year).

### 2026-10-21: Import from the Source Library, never write to it
Analyst-curated reports 2023+ and missing policies are copied into the Disclosure Library with duplicate guards; the Source Library is read with a separate read-only token. Rejected: syncing both ways.

### 2026-10-20: The owner adds, the robot files
`add_link`, `add_file` and `approve_suggestions` need no other field and check duplicates themselves. Robot-found pages are proposed, not added. Rejected: forms with required fields; auto-adding discovered pages (the 37,000-junk-row sitemap experience).

### 2026-10-19: Never delete without archive and tombstone
Rows are discarded with a reason; deletion is a separate, archived step, and a deleted document is blocked from coming back. Rejected: deleting discarded rows directly.

### 2026-07-17: Cross-domain PDFs admitted by default
A PDF linked from the company's own monitored page is the company's document wherever it is hosted (Q4, Sitecore, mziq, HKEX, group domains); only standard-setter and rating hosts are excluded, and documents linked by 5+ companies are dropped at harvest. Rejected: blocking cross-domain PDFs (silently lost whole document estates) and an allowlist of CDNs (whack-a-mole).

### 2026-07-15: Browser lane as a rotating queue
Pages that need a browser are rendered oldest first, a fixed number a night, inside a time budget. Rejected: rendering everything each run (killed by GitHub's 6-hour limit, coverage became a lottery).

### 2026-07-01: Queues key off text status fields
Every queue selects on a text status (`cover_status`, `file_status`), never a number. Airtable treats 0 as blank, which kept failed rows in the queue forever.

### 2026-06: Documents first, GPT-4o mini for classification
About 88% of analyst citations come from documents, so the pipeline is built around documents, with web pages captured only when they are disclosures themselves. GPT-4o mini runs the Airtable AI fields for cost; Claude Haiku was considered.
