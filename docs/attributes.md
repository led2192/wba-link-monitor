# Attribute values

How the pipeline finds the values of benchmark attributes (revenue, Scope 1 and 2 emissions, low-carbon capex, and later the others) for every company, how each route is scored against the analysts' validated answers, and where the values go for review. Started 28 Oct 2026.

## The idea in one paragraph

Retrieval and rules first, a model only for what they cannot read, and every route scored against the validated 2025-benchmark answers before anything reaches an analyst. What matters is the right value, not the analyst's own source: a value printed in another report of the same company counts. Everything specific to an attribute lives in one file, `attributes.json`, so a change of methodology or a new attribute is an edit of that file and a rerun of the scoring, not new code (owner's rule, 29 Oct 2026).

## The pieces

| Piece | What it is |
| --- | --- |
| `attributes.json` | The specification: for each attribute its WBA definition, type, unit family and scales, labels (several languages), excluded neighbours, preferred document types, period, scope, XBRL concepts |
| `gold_attributes.json` | Which attributes are scored and where the validated answers are (the 2024 and 2025 gold bases, read-only) |
| `value_eval.py` | The ceiling: is the validated number printed anywhere in the company's documents of that year, in any scale? |
| `sec_facts.py`, `esef_facts.py` | Revenue from machine-readable filings (SEC companyfacts; European ESEF reports on filings.xbrl.org), scored |
| `extract_eval.py` | The generic reader: the year's documents, the pages that name the attribute, then the value read by rules (`--reader rules`) or by a model (`--reader model`, `model_reader.py`), scored stage by stage |
| `label_mining.py` | Learns from the validated answers which words reports print next to a value; suggestions for `labels` in `attributes.json` |
| `propose_values.py` | Writes values to the Disclosure Library table `attribute_values` for analysts to review. First source: SEC and ESEF revenue |

## Routes and how well they do (29 Oct 2026)

| Route | Attributes | Scored on | Right |
| --- | --- | --- | --- |
| SEC XBRL | revenue | 326 validated answers of companies with an SEC CIK | 89 % of those with a fact (288 of 323) |
| ESEF XBRL | revenue | 265 validated answers of European companies | 83 % of those with a fact (48 of 58); filings.xbrl.org has a filing for about 1 in 5 |
| Rules reader | number attributes | 40 answers per attribute | 8 to 12 %; capex 0 % (its labels did not lead to the right pages) |
| Model reader (Haiku 5.5, 3 pages) | number attributes | same 40 answers | revenue 30 %, Scope 1 30 %, Scope 2 location 20 %, market 28 %, capex 0 %; right for 57 to 85 % of its answers; $0.07 per 200 answers ($0.50 per 1,000) |

The model's misses on the first sample: the neighbouring year's column (fiscal-year labels such as FY25 for a year ending March 2025, which is reporting year 2024), a restated figure next to the validated one, and pages that never reached it (the validated number was on its 3 pages for 34 % of the answers, on the 25 ranked pages for 50 to 60 %). The second version gives it 6 pages, half by rule candidates and half by rank, and spells out the fiscal-year labels.

Reading the reader's table: "Number in those documents" is the ceiling (the value is printed somewhere in the year's documents: about 60 to 80 %), "Number on the ranked pages" is what the reader was given (32 to 60 %), and "Right" is what it got. The loss between the last two is reading; the loss between the first two is retrieval, which `label_mining.py` addresses.

Banks: revenue for WBA is income net of interest expense (SEC `RevenuesNetOfInterestExpense`, "gross income" or "total operating income" in European reports), never gross interest income. Interest-income concepts were removed from the rules on 29 Oct 2026 (pitfalls.md).

## The table analysts review: `attribute_values`

One row per company, attribute, year and method (`value_id` = `PT_00001|GMC_00048.1|2024|SEC XBRL`), in the Disclosure Library. Fields are listed in [fields.md](fields.md).

- **status**: `proposed` (written by the robot), `accepted` or `rejected` (set by a person and never touched again), `superseded` (the robot no longer proposes it, for instance after a rule changed; kept, never deleted).
- **matches_validated**: `match`, `differs` or `no validated answer`. The `differs` rows are the ones to look at first: either the validated answer has a slip (a currency label, a factor of 1,000) or the rule picked another line.
- **source_url** opens the filing; **detail** says which concept, form, period end and filing date.
- Two methods that disagree on the same company and year are two rows, so the disagreement is visible.

First full run (29 Oct 2026, 2022 to 2024): 1,324 revenue values; SEC 1,014 (288 match, 34 differ, 692 with no validated answer), ESEF 310 (72 match, 22 differ, 216 with no validated answer). Years now default to 2018 to last year.

## Running it

| To | Do |
| --- | --- |
| Fill or refresh revenue from filings | Actions, Propose values, tick commit (years default to 2018 to last year) |
| Score a route against the validated answers | Actions: SEC revenue, ESEF revenue, Generic reader (reader rules or model), Value evaluation |
| Improve an attribute's labels | Actions, Label mining; copy the phrases worth having into `labels` in `attributes.json` (pull request), then run Generic reader again |
| Change the methodology of an attribute | Edit its entry in `attributes.json` (pull request); the pull request's checks score it again |

The model reader uses the repository secret `ANTHROPIC_API_KEY` (prepaid credit in the Anthropic console; the balance is the spending limit). Each run's report prints calls, tokens and cost.

## What comes next

1. Measure the model reader on the same 200 answers and compare with the rules reader.
2. Score the mined labels (in `attributes.json` since 29 Oct 2026: the EU Taxonomy wording for capex, "gross" scope figures, operating and consolidated revenue) and the wider document types.
3. Write the readers' values to `attribute_values` (methods "rules reader" and "model reader", with the page link).
4. Current-year completeness: check that each company's latest reports are in, using the prior-year figures they repeat.
5. The non-number attributes (board climate expertise, climate policies supported): options from the specification, same scoring.
