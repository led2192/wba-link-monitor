"""
page_ranker.py: BM25 page ranking for the readers (proposal 1 of the information retrieval review).

The ranking experiment (rank_eval.py, 4 Nov 2026, 2,314 validated answers) put the analyst's cited page in
the top 3 for 63% of answers with BM25 against 45% with the database's ts_rank_cd. BM25 weighs a word by
how rare it is across the corpus (tCO2e above emissions) and does not favour long pages.

Pages are found with ANY of the query words (the readers used to require all the words of one wording, which
left a third of the narrative answers with no page at all), then scored in Python. Document frequencies come
from a 1% repeatable sample of public.pages, read once per run for the words asked.
"""
import collections

import rank_eval as R

MAX_CANDIDATES = 3000           # pages fetched per company before scoring


class Ranker:
    def __init__(self):
        self.idfs, self.n, self.avg_len = {}, 0, 0.0

    def _ensure(self, cur, terms):
        new = [t for t in terms if t not in self.idfs]
        if not new:
            return
        n, dfs, avg = R.collection_stats(cur, new)
        self.n = self.n or n
        self.avg_len = self.avg_len or avg
        for t in new:
            self.idfs[t] = R.idf(dfs.get(t, 0), self.n or n)

    def rank(self, cur, library_ids, queries, top):
        """[(library_id, page, body, score)] best first: the pages of these documents holding any query word,
        scored by BM25 over all the queries' words."""
        terms = list(dict.fromkeys(t for q in queries for t in R.query_terms(q)))
        if not terms or not library_ids:
            return []
        self._ensure(cur, terms)
        cur.execute("select library_id, page, body from public.pages where library_id = any(%s) "
                    "and words @@ to_tsquery('simple', %s) limit %s",
                    (list(library_ids), " | ".join(terms), MAX_CANDIDATES))
        scored = []
        for lid, page, body in cur.fetchall():
            tf = collections.Counter(R.tokens(body))
            s = R.bm25(tf, len(tf), self.avg_len, terms, self.idfs)
            if s > 0:
                scored.append((lid, page, body or "", s))
        scored.sort(key=lambda r: (-r[3], r[0], r[1]))
        return scored[:top]


RANKER = Ranker()
