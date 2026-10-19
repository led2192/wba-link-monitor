# Text search: which companies mention X in their documents

This layer answers questions of the form "what share of companies refer to X in their reports",
with the passages as evidence. X is a name, an acronym, a benchmark, a phrase. The first question
it was built for: how many of the companies in the library mention WBA, and how many of those
mention it next to "gender".

Three pieces:

| piece | where | what it does |
|---|---|---|
| **Mentions** interface | Airtable, base Company Monitoring | Define a search, read the answer, review the documents found |
| `mention_searches`, `mention_hits` | Airtable tables behind the interface | One row per search; one row per document found |
| **Build full text** and **Mention search** | GitHub Actions in this repository | Read every page of every attached PDF into a text store; run the searches over it every night |

Nothing in the existing tables (`companies`, `monitored_links`, `report_library`, `webpage_library`)
is changed by any of it.

## Asking a question

Open the **Mentions** interface, page **Searches**, and add a row. Only the first field is
required.

| field | what to put in it |
|---|---|
| `find` | The names to look for, one per line. `World Benchmarking Alliance`, and on the next line the misspelling `World Benchmark Alliance`, because companies use it. |
| `short_names` | Acronyms, one per line (`WBA`). An acronym on its own is ambiguous (WBA is also the Wireless Broadband Alliance), so it only counts when the same document shows one of the full names somewhere, or when a confirming word sits within 8 words of it. |
| `confirming_words` | Words that settle what the acronym means when they stand next to it: `benchmark*`, `SDG2000`, `CHRB`, `ACT`. "WBA Social Benchmark" counts; "WBA Wi-Fi certification" does not. |
| `near` | Optional. Words that must appear close to a mention for it to count: `gender*`. Without this, any mention counts. |
| `distance` | How many words apart "close" is. Blank means 25. |
| `doc_types` | Optional. Only documents of these types (the AI type, with the keyword type as fallback). Blank means every type in the store. |
| `year_from`, `year_to` | Optional. Only documents dated in this range. Undated documents are left out as soon as either is set. |
| `companies` | Optional. One `wba_id` per line (commas and semicolons also work). The answer is then counted inside this list: "% of the companies in benchmark B that refer to B" is this field filled with B's companies. |
| `look_in` | **Full text**: every page of every document in the store. The real answer. **First pages**: the first six pages only, read from Airtable's `cover_text`; works before the store exists and gives a lower bound. |
| `active` | Ticked: the search runs every night and stays current as new reports arrive. Unticked: it keeps its last result and is skipped. |

The answer appears on the same row after the next run: `result` is the sentence, the counts are
next to it, and `status` says `ok` or why the search could not run.

To get the answer within minutes instead of overnight, run the **Mention search** workflow by hand
(repository > Actions > Mention search > Run workflow), mode `commit`, and type the search's name
in `search`. Leave `search` blank to run every active search. The workflow also takes an ad hoc
search typed straight into the form (`find`, `short_names`, `near` ...); with mode `commit` and a
`name` it is saved as a search row, so the result and the documents land in Airtable like any
other.

### The terms

- Whole words only: `gender` does not match "engendered"; `WBA` does not match "wbaseline".
- Case does not matter, except that a term typed in capitals (`WBA`, `CHRB`) matches capitals only.
  Lower-case "wba" occurs in URLs and codes; capital WBA is a name.
- Accents matter. Type a term the way it is written in the documents.
- A trailing `*` matches any ending: `benchmark*` finds benchmark, benchmarks, benchmarking.
- Hyphens never hide a word: a word broken across a line ("Bench-" / "marking") is matched as one,
  "co-operation" matches cooperation, and "gender-responsive" still matches gender.
- Several terms on one line separated by `;` are read as several terms. Commas are not separators:
  company and index names contain them.
- Other alphabets work (Korean, Chinese, Japanese, Cyrillic, Arabic). For Chinese and Japanese,
  which have no spaces, "distance in words" is distance in characters.

### Reading the numbers

The result reads like `43 of 1,783 companies with documents on file (2.4%)`.

- The denominator is never the whole universe. In full text it is the companies that have at least
  one document the engine could read in the search's scope; a company whose documents are all
  scanned images, or outside the scope, cannot be said not to mention something, so it is not
  counted either way. In first-pages mode it is every company with a document on file, whatever
  the scope, because Airtable does the reading and the engine does not see which documents were
  in range.
- `first pages only (lower bound)` means exactly that: a reference on page 40 is invisible to that
  run. Treat the figure as "at least".
- With a company list, the result is `X of Y listed companies ...; Z of the N listed have none`:
  Z companies on the list have no searchable document at all, and are outside the denominator.
- `share_after_review` is the figure to quote once the documents have been looked at: it leaves
  out documents marked **Not a mention** and documents the last run no longer found (`stale`).
  Until someone reviews, it equals the raw share.

The passages are matched by words, not by meaning. "Bloomberg Gender-Equality Index ... WBA Nature
Benchmark" (Kering, 8 words apart) satisfies a WBA + gender search and is not a reference to the
Gender Benchmark. That is what the review step is for.

## Reviewing the documents

Page **Review** of the interface lists every document found, one per row, with the passage and the
matched words in bold. Pick a search in the dropdown and go down the list.

- `evidence` shows up to three passages, closest match first. In full text, `p. N` is a link that
  opens the PDF on that page. In first-pages mode the label is `first pages`.
- `open` is the same link for the first passage.
- `review`: **Mention**, **Not a mention** or **Unsure**. Only **Not a mention** changes the
  share; the other two are bookkeeping for the reviewer.
- `notes` is free text. Both fields belong to people: the engine never writes them, and they
  survive every re-run.
- `stale`: the last run of the same kind no longer found this document (the search was tightened,
  the file was replaced, the row was discarded). Stale rows stay in the table with their review
  and come back by themselves if the document matches again. The Review page hides them.

A document found in both the first-pages and the full-text run is one row: full text takes over
the row and first pages never overwrites it.

## One-time set-up

The full-text store is the only new piece of infrastructure. It is a private GitHub repository
used as a file locker: every text file is a release asset. Full text is never stored in this
repository, which is public. Its run logs carry the headline, the company names and one short
passage per company; the run summary carries the headline and the names only.

1. Create a **private** repository under the same account, for instance `led2192/wba-text-store`,
   and initialise it with a README. The README matters: GitHub cannot create a release in a
   repository with no commits.
2. Create a fine-grained personal access token (GitHub > Settings > Developer settings > Personal
   access tokens > Fine-grained tokens): repository access limited to that one repository,
   repository permission **Contents: Read and write**, nothing else. Choose the longest expiry
   you are comfortable with and note the date: when it expires, the nightly build fails with a 401
   whose message names the secret to renew.
3. In this repository, Settings > Secrets and variables > Actions:
   - Variables tab: `TEXT_STORE_REPO` = `led2192/wba-text-store`
   - Secrets tab: `TEXT_STORE_TOKEN` = the token
4. Commit `text_core.py`, `build_fulltext.py`, `mention_search.py`, the two workflow files and this
   document.

Until the variable exists, the nightly **Build full text** run is skipped (grey, not red) and
the nightly search run with it; a search run by hand works in first-pages mode. A search set to
**Full text** before the store holds anything is reported as `Not run` on its row and does not
stop the others.

### First runs, in this order

1. **Mention search**, mode `commit`, `look_in` = `first pages`. Fills the interface with the
   lower bound in about a minute, no store needed.
2. **Build full text**, mode `dry-run`. Sweeps the library, prints the plan (documents and pages in
   scope by type, how many shards), extracts five documents and prints what came out. Writes
   nothing. Check the plan on the run page.
3. **Build full text**, mode `commit`, `limit` = `50`. Fifty documents end up in the store; the
   run page shows the outcome per document. Then **Mention search**, mode `commit`, `look_in` =
   `full text`: the WBA searches answer over those fifty.
4. **Build full text**, mode `commit`, nothing else. The whole default scope. The planning job
   splits the queue into up to eight parallel shards of about four hours; what a shard does not
   finish, the next night picks up. Expect one or two nights.

From then on it runs by itself: **Build full text** every night at 21:37 UTC (after the attachment
recovery and the cover-text run, so the day's PDFs are read the same day), **Mention search**
right after it.

## How it works

**Scope.** The default reads the report and policy families: sustainability, ESG, CSR, integrated,
impact and social reports, annual reports, engagement, climate and environmental reports, policy
documents, codes of conduct, human rights policies, GRI, SASB and CDP documents, and UNKNOWN;
dated 2021 or later, plus undated documents. Left out: interim reports, statutory documents,
presentations, financial statements, 10-K forms, registration documents, certifications. They are
over half of all pages and the least likely home of a benchmark reference. A manual run can widen
this (`types` = `all`, or a comma-separated list; `year_min`); once something has been read it
stays searchable whatever later runs are scoped to. To make a wider scope permanent, change
`NIGHTLY_TYPES` / `NIGHTLY_YEAR_MIN` at the top of `build_fulltext.yml`.

**The store.** Gzipped JSON-lines files, one line per page, grouped in "parts" (one flush of one
extraction job: at most 300 documents, 120 MB of text or 20 minutes). Parts are append-only, so a
job that dies loses only its unflushed tail. A part's text file is uploaded before its index file;
a text file without its index file is an interrupted flush and is ignored, then redone. GitHub
allows 1,000 files per release, so parts go into numbered releases (`text-parts-0001`, `-0002`
...) that fill up to 800 files each; the metadata snapshot and the work queue live in
`text-state`. Nothing in the store is edited by hand.

**What is recorded per document** (the `status` in the store, shown by the dry run):

| status | meaning | retried? |
|---|---|---|
| `ok` | text extracted | no |
| `no_text` | no text layer (scanned); not searchable, and not evidence of absence | no |
| `encrypted` | needs a password (most "encrypted" PDFs open with an empty one and are read) | no |
| `parse_fail` | complete file the PDF library cannot read | only with `retry_failed` |
| `timeout` | over its time allowance (240 s, more for long documents, 30 min at most) | only with `retry_failed` |
| `too_big` | over 150 MB | no |
| `not_pdf` | the attachment is not a PDF | no |
| `crash` | killed its worker process | only with `retry_failed` |
| `download_fail`, `truncated` | the CDN did not deliver the file whole | yes, up to 3 attempts |

A replaced attachment is read again (the newest part wins). A discarded or deleted row drops out
of the metadata snapshot and stops counting at the next search.

**Speed and cost.** Extraction runs at roughly 16 pages per second per core; the planner assumes
less and sizes shards accordingly. The default scope is about 3 million pages. A search downloads
the parts (a few GB compressed) and scans them with a byte-level pre-filter, so one nightly run
over every active search takes a few minutes, however many searches there are. GitHub Actions
minutes are free for a public repository; release storage has no size cap.

**Guards.** A search matching more than 5,000 documents is reported with its numbers but its
documents are not written (`status` says `Too broad`): add nearby words or narrow the scope. In
first-pages mode a term present on the first pages of more than 25,000 documents is refused for
the same reason.

## Limits worth knowing

- Attached PDFs only. Web pages (`webpage_library`) are not read.
- A scanned PDF has no text and cannot be searched. The dry run and the search log say how many
  documents in scope have no searchable text.
- `report_library` holds the same PDF more than once for some companies (the same file under two
  URLs, or a URL that differs only by `&amp;`). Such a document shows up as two or three rows in
  the hits. Company counts are unaffected; document counts are inflated by these duplicates.
- First-pages mode reads `cover_text` as stored, where a word hyphenated across a line break is
  still broken ("bench- marking"); full text repairs that at extraction. One more reason it is a
  lower bound.
- Accented and unaccented spellings are different terms. Translated names (a Japanese rendering of
  "World Benchmarking Alliance") are not found unless typed as a term; none occurred on the first
  pages of the library.
- The matcher counts words, not meaning. The review step is part of the method, not an optional
  extra.

## Removing it

Delete the two tables and the **Mentions** interface in Airtable, the two workflow files and the
three scripts here, the repository variable and secret, and the private repository. Nothing else
references any of it.
