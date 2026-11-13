### Search timing

Read-only. Seconds from GitHub's servers, network included (a round trip is the first line).

- a round trip: 0.000 s
- the list of years (asked once per pass by find_pages): 6 years, 0.001 s
- companies_matching('Nike'): 1 ids, 0.001 s
- the searches-per-minute count: 0.000 s
- documents: 122 with 1,442 pages, 0.000 s
- index pages_pkey (72 kB): `btree (library_id, page)`
- index pages_search (1544 kB): `gin (words, year, doc_type, wba_id)`
- pages: about 1,442 rows, 2912 kB with its long values
- documents and pages by year: 2026: 24/266, 2025: 17/204, 2024: 28/336, 2023: 18/216, 2022: 24/288, 2021: 11/132

| Search | Stage | Rows | Seconds |
| --- | --- | --: | --: |
| Nike sustainability | year by year, ids only | 0 | 0.00 |
| Nike sustainability | year by year, with the word vector | 0 | 0.00 |
| Nike sustainability | one statement, 1,600 at most, ids only | 0 | 0.00 |
| Nike sustainability | every matching page, counted (50,000 at most) | 0 | 0.00 |
| Nike sustainability | ranking the pool (ts_rank_cd, word vectors read) | 0 | 0.00 |
| Nike sustainability | ranking the pool again (ts_rank, vectors now in memory) | 0 | 0.00 |
| Nike sustainability | 50 excerpts from the first 20,000 characters | 0 | 0.00 |
| Nike sustainability | 50 excerpts from the first 6,000 characters | 0 | 0.00 |
| Nike sustainability | 50 excerpts from the first 2,500 characters | 0 | 0.00 |
| Nike sustainability | 50 page texts read, no excerpt | 0 | 0.00 |
| Unilever scope 1 2024 | year by year, ids only | 0 | 0.00 |
| Unilever scope 1 2024 | year by year, with the word vector | 0 | 0.00 |
| Unilever scope 1 2024 | one statement, 1,600 at most, ids only | 0 | 0.00 |
| Unilever scope 1 2024 | every matching page, counted (50,000 at most) | 0 | 0.00 |
| Unilever scope 1 2024 | ranking the pool (ts_rank_cd, word vectors read) | 0 | 0.00 |
| Unilever scope 1 2024 | ranking the pool again (ts_rank, vectors now in memory) | 0 | 0.00 |
| Unilever scope 1 2024 | 50 excerpts from the first 20,000 characters | 0 | 0.00 |
| Unilever scope 1 2024 | 50 excerpts from the first 6,000 characters | 0 | 0.00 |
| Unilever scope 1 2024 | 50 excerpts from the first 2,500 characters | 0 | 0.00 |
| Unilever scope 1 2024 | 50 page texts read, no excerpt | 0 | 0.00 |
| climate | year by year, ids only | 1,380 | 0.00 |
| climate | year by year, with the word vector | 1,380 | 0.00 |
| climate | one statement, 1,600 at most, ids only | 1,380 | 0.00 |
| climate | every matching page, counted (50,000 at most) | 1,380 | 0.00 |
| climate | ranking the pool (ts_rank_cd, word vectors read) | 1,380 | 0.00 |
| climate | ranking the pool again (ts_rank, vectors now in memory) | 1,380 | 0.00 |
| climate | 50 excerpts from the first 20,000 characters | 50 | 0.00 |
| climate | 50 excerpts from the first 6,000 characters | 50 | 0.00 |
| climate | 50 excerpts from the first 2,500 characters | 50 | 0.00 |
| climate | 50 page texts read, no excerpt | 50 | 0.00 |
| grievance mechanism suppliers | year by year, ids only | 0 | 0.01 |
| grievance mechanism suppliers | year by year, with the word vector | 0 | 0.00 |
| grievance mechanism suppliers | one statement, 1,600 at most, ids only | 0 | 0.00 |
| grievance mechanism suppliers | every matching page, counted (50,000 at most) | 0 | 0.00 |
| grievance mechanism suppliers | ranking the pool (ts_rank_cd, word vectors read) | 0 | 0.00 |
| grievance mechanism suppliers | ranking the pool again (ts_rank, vectors now in memory) | 0 | 0.00 |
| grievance mechanism suppliers | 50 excerpts from the first 20,000 characters | 0 | 0.00 |
| grievance mechanism suppliers | 50 excerpts from the first 6,000 characters | 0 | 0.00 |
| grievance mechanism suppliers | 50 excerpts from the first 2,500 characters | 0 | 0.00 |
| grievance mechanism suppliers | 50 page texts read, no excerpt | 0 | 0.00 |
| "just transition" | year by year, ids only | 123 | 0.00 |
| "just transition" | year by year, with the word vector | 123 | 0.00 |
| "just transition" | one statement, 1,600 at most, ids only | 123 | 0.00 |
| "just transition" | every matching page, counted (50,000 at most) | 123 | 0.00 |
| "just transition" | ranking the pool (ts_rank_cd, word vectors read) | 123 | 0.00 |
| "just transition" | ranking the pool again (ts_rank, vectors now in memory) | 123 | 0.00 |
| "just transition" | 50 excerpts from the first 20,000 characters | 50 | 0.00 |
| "just transition" | 50 excerpts from the first 6,000 characters | 50 | 0.00 |
| "just transition" | 50 excerpts from the first 2,500 characters | 50 | 0.00 |
| "just transition" | 50 page texts read, no excerpt | 50 | 0.00 |
| Nike "code of conduct" | year by year, ids only | 0 | 0.00 |
| Nike "code of conduct" | year by year, with the word vector | 0 | 0.00 |
| Nike "code of conduct" | one statement, 1,600 at most, ids only | 0 | 0.00 |
| Nike "code of conduct" | every matching page, counted (50,000 at most) | 0 | 0.00 |
| Nike "code of conduct" | ranking the pool (ts_rank_cd, word vectors read) | 0 | 0.00 |
| Nike "code of conduct" | ranking the pool again (ts_rank, vectors now in memory) | 0 | 0.00 |
| Nike "code of conduct" | 50 excerpts from the first 20,000 characters | 0 | 0.00 |
| Nike "code of conduct" | 50 excerpts from the first 6,000 characters | 0 | 0.00 |
| Nike "code of conduct" | 50 excerpts from the first 2,500 characters | 0 | 0.00 |
| Nike "code of conduct" | 50 page texts read, no excerpt | 0 | 0.00 |

### Questions written in full

| Question | Stage | Rows | Seconds |
| --- | --- | --: | --: |
| 3 words, one company | words kept by find_terms | 3 | |
| 3 words, one company | pages of the company (no words) | 0 | 0.00 |
| 3 words, one company | level 0: every word, 800 at most | 0 | 0.00 |
| 3 words, one company | level 1: every word but one, 800 at most | 0 | 0.00 |
| 3 words, one company | level 1 with the word vectors copied, as the function does | 0 | 0.00 |
| 3 words, one company | find_pages, whole | 0 | 0.01 |
| 3 words, one company | find_pages again (pages now in memory) | 0 | 0.01 |
| 3 words, one company | find_pages with the option company_first | 0 | 0.01 |
| 3 words, one company | the same pages as without it (1 = yes) | 1 | 0.01 |
| 3 words, one company | the question's vector | 0 | 0.00 |
| question of 8 kept words, one company | words kept by find_terms | 8 | |
| question of 8 kept words, one company | pages of the company (no words) | 0 | 0.00 |
| question of 8 kept words, one company | level 0: every word, 800 at most | 0 | 0.00 |
| question of 8 kept words, one company | level 1: every word but one, 800 at most | 0 | 0.00 |
| question of 8 kept words, one company | level 1 with the word vectors copied, as the function does | 0 | 0.00 |
| question of 8 kept words, one company | find_pages, whole | 0 | 0.01 |
| question of 8 kept words, one company | find_pages again (pages now in memory) | 0 | 0.01 |
| question of 8 kept words, one company | find_pages with the option company_first | 0 | 0.01 |
| question of 8 kept words, one company | the same pages as without it (1 = yes) | 1 | 0.01 |
| question of 8 kept words, one company | the question's vector | 0 | 0.00 |
| question of 8 kept words, a large company | words kept by find_terms | 8 | |
| question of 8 kept words, a large company | pages of the company (no words) | 38 | 0.00 |
| question of 8 kept words, a large company | level 0: every word, 800 at most | 0 | 0.00 |
| question of 8 kept words, a large company | level 1: every word but one, 800 at most | 0 | 0.00 |
| question of 8 kept words, a large company | level 1 with the word vectors copied, as the function does | 0 | 0.00 |
| question of 8 kept words, a large company | find_pages, whole | 0 | 0.01 |
| question of 8 kept words, a large company | find_pages again (pages now in memory) | 0 | 0.01 |
| question of 8 kept words, a large company | find_pages with the option company_first | 0 | 0.01 |
| question of 8 kept words, a large company | the same pages as without it (1 = yes) | 1 | 0.01 |
| question of 8 kept words, a large company | the question's vector | 0 | 0.00 |
| question of 8 kept words, no company | words kept by find_terms | 8 | |
| question of 8 kept words, no company | level 0: every word, 800 at most | 0 | 0.00 |
| question of 8 kept words, no company | level 1: every word but one, 800 at most | 0 | 0.00 |
| question of 8 kept words, no company | level 1 with the word vectors copied, as the function does | 0 | 0.00 |
| question of 8 kept words, no company | find_pages, whole | 0 | 0.01 |
| question of 8 kept words, no company | find_pages again (pages now in memory) | 0 | 0.01 |
| question of 8 kept words, no company | the question's vector | 0 | 0.00 |
| question of 5 kept words, one company | words kept by find_terms | 7 | |
| question of 5 kept words, one company | pages of the company (no words) | 0 | 0.00 |
| question of 5 kept words, one company | level 0: every word, 800 at most | 0 | 0.00 |
| question of 5 kept words, one company | level 1: every word but one, 800 at most | 0 | 0.00 |
| question of 5 kept words, one company | level 1 with the word vectors copied, as the function does | 0 | 0.00 |
| question of 5 kept words, one company | find_pages, whole | 0 | 0.01 |
| question of 5 kept words, one company | find_pages again (pages now in memory) | 0 | 0.01 |
| question of 5 kept words, one company | find_pages with the option company_first | 0 | 0.01 |
| question of 5 kept words, one company | the same pages as without it (1 = yes) | 1 | 0.01 |
| question of 5 kept words, one company | the question's vector | 0 | 0.00 |

Plan of level 1 (question of 8 kept words, one company):

```
Limit  (cost=0.00..383.43 rows=1 width=735) (actual time=0.313..0.313 rows=0 loops=1)
  Buffers: shared hit=360
  ->  Seq Scan on pages p  (cost=0.00..383.43 rows=1 width=735) (actual time=0.312..0.312 rows=0 loops=1)
        Filter: ((wba_id = ANY ('{""}'::text[])) AND (words @@ '''climate'':* & ''transition'':* & ''risks'':* & ''board'':* & ''oversee'':* & ''progress'':* & ''against'':* | ''ma
        Rows Removed by Filter: 1442
        Buffers: shared hit=360
Planning:
  Buffers: shared hit=2
Planning Time: 0.055 ms
Execution Time: 0.321 ms
```

Searches of the last seven days in the log (a search cut by the timeout leaves no row):

| Words | A company | Searches | Median s | 90% under s | Longest s |
| --: | --- | --: | --: | --: | --: |
| 1 | no | 4 | 0.0 | 0.0 | 0.0 |
| 1 | yes | 4 | 0.0 | 0.0 | 0.0 |
| 2 | no | 3 | 0.4 | 0.5 | 0.5 |
| 2 | yes | 3 | 0.0 | 0.0 | 0.0 |
| 3 | no | 2 | 0.0 | 0.0 | 0.0 |
| 3 | yes | 2 | 0.0 | 0.0 | 0.0 |
| 4 | no | 1 | 0.3 | 0.3 | 0.3 |
| 5 | yes | 3 | 0.0 | 0.0 | 0.0 |
| 6 | no | 7 | 0.0 | 0.2 | 0.2 |
| 6 | yes | 6 | 0.0 | 0.0 | 0.0 |
| 8 | no | 4 | 0.0 | 0.0 | 0.0 |
| 8 | yes | 4 | 0.0 | 0.0 | 0.0 |

Plan of one year's statement (sustainability, 2025):

```
Limit  (cost=0.00..385.24 rows=4 width=10) (actual time=0.617..0.618 rows=0 loops=1)
  Buffers: shared hit=360
  ->  Seq Scan on pages p  (cost=0.00..385.24 rows=4 width=10) (actual time=0.616..0.617 rows=0 loops=1)
        Filter: ((words @@ '''sustainability'':*'::tsquery) AND (wba_id <> ''::text) AND (year = 2025))
        Rows Removed by Filter: 1442
        Buffers: shared hit=360
Planning:
  Buffers: shared hit=7
Planning Time: 0.066 ms
Execution Time: 0.624 ms
```
