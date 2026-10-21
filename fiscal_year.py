#!/usr/bin/env python3
"""
fiscal_year.py — the reporting year of a document, from what the document itself says.

Convention (the Source Library's, used by analysts in 97% of their records): a document's
fiscal_year is the year in which the reporting period it covers ENDS. April 2024 - March 2025 is
2025; January - December 2024 is 2024.

How it decides (decide()): read the full text, page by page, and collect every statement that
names a reporting period, each as a piece of evidence with a weight:

  period   "for the year ended 31 December 2021", "six months ended 30 June 2011",
           "ejercicio terminado el 31 de diciembre de 2024", "截至2024年12月31日止年度"   weight 3
  scope    "this report covers 1 January to 31 December 2021", "reporting period: FY2021",
           "the reporting period of this report is ..."                                   weight 5
  balance  "as at 31 December 2021" (the date of a balance sheet)                         weight 1
  label    "FY2021", "fiscal year 2021", "financial year 2021/22", "2024年3月期"            weight 1
  title    "Annual Report 2021", "2021 Sustainability Report" on the first two pages        weight 2

Each piece points to a period END year. Statements of a period that ends after the document was
published (the upload folder in the link, a date in the file name) are dropped, and so are
maturity dates, targets and laws (they never match these patterns to begin with). The year with
the most weight wins.

CONFIDENCE SCORE (0-100), from four things that each make a wrong answer less likely:
  dominance   the winner's share of all the weight (comparative columns "2021 vs 2020" make
              the previous year a strong runner-up in every annual report, so this is the share,
              not a yes/no)                                                        up to 45 points
  volume      how much evidence: a full annual report repeats "year ended ..." on every page of
              its financial statements; a two-page policy says it once         up to 25 points
  kinds       how many DIFFERENT kinds of evidence agree (a period statement AND the title AND a
              scope sentence agreeing is much stronger than one kind repeated)  up to 20 points
  cover       the winner also appears on the first two pages                       10 points
The score is calibrated against the analysts' years in the Source Library (evaluate mode): the
threshold for "high" is set where the high-scoring years are at least 95% right.

Nothing here talks to Airtable: pages in, (year, period_end, score, evidence) out.
"""
import collections, re, unicodedata

_MON_NAMES = {
    # English, Spanish, Portuguese, French, German, Italian, Dutch, Indonesian
    "january": 1, "jan": 1, "enero": 1, "janeiro": 1, "janvier": 1, "januar": 1, "gennaio": 1, "januari": 1,
    "february": 2, "feb": 2, "febrero": 2, "fevereiro": 2, "février": 2, "fevrier": 2, "februar": 2, "febbraio": 2, "februari": 2, "pebruari": 2,
    "march": 3, "mar": 3, "marzo": 3, "março": 3, "marco": 3, "mars": 3, "märz": 3, "maerz": 3, "maart": 3, "maret": 3,
    "april": 4, "apr": 4, "abril": 4, "avril": 4, "aprile": 4,
    "may": 5, "mayo": 5, "maio": 5, "mai": 5, "maggio": 5, "mei": 5,
    "june": 6, "jun": 6, "junio": 6, "junho": 6, "juin": 6, "juni": 6, "giugno": 6,
    "july": 7, "jul": 7, "julio": 7, "julho": 7, "juillet": 7, "juli": 7, "luglio": 7,
    "august": 8, "aug": 8, "agosto": 8, "août": 8, "aout": 8, "augustus": 8, "agustus": 8,
    "september": 9, "sep": 9, "sept": 9, "septiembre": 9, "setembro": 9, "septembre": 9, "settembre": 9,
    "october": 10, "oct": 10, "octubre": 10, "outubro": 10, "octobre": 10, "oktober": 10, "ottobre": 10,
    "november": 11, "nov": 11, "noviembre": 11, "novembro": 11, "novembre": 11,
    "december": 12, "dec": 12, "diciembre": 12, "dezembro": 12, "décembre": 12, "decembre": 12, "dezember": 12, "dicembre": 12, "desember": 12,
}
_MON = "(?:" + "|".join(sorted((re.escape(m) for m in _MON_NAMES), key=len, reverse=True)) + r")\.?"
_Y = r"(20[0-3]\d)"
_D = r"(?:[0-3]?\d)(?:st|nd|rd|th|er|º)?"
# a date, capturing (month word or number) and the year
DATE = (r"(?:" + _D + r"\s+(?:de\s+|of\s+)?(" + _MON + r")\s+(?:de\s+)?" + _Y +           # 31 December 2021 / 31 de diciembre de 2021
        r"|(" + _MON + r")\s+" + _D + r",?\s+" + _Y +                                       # December 31, 2021
        r"|" + _D + r"[./-](0?[1-9]|1[0-2])[./-]" + _Y +                                     # 31.12.2021 / 31/12/2021
        r"|" + _Y + r"[./-](0?[1-9]|1[0-2])[./-][0-3]?\d" +                                  # 2021-12-31
        r"|" + _Y + r"\s*年\s*(1[0-2]|0?[1-9])\s*月(?:\s*[0-3]?\d\s*日)?" +                  # 2021年12月31日
        r"|(" + _MON + r")\s+" + _Y + r")")                                                  # December 2021


def _date_parts(m, start):
    """(year, month) from the groups of a DATE match beginning at group index `start`."""
    g = m.groups()[start:start + 13]
    # pairs in DATE order: (mon, y), (mon, y), (num, y), (y, num), (y, num), (mon, y)
    for mon, y in ((g[0], g[1]), (g[2], g[3]), (g[4], g[5]), (g[7], g[6]), (g[9], g[8]), (g[10], g[11])):
        if y:
            if mon and not mon.isdigit():
                mm = _MON_NAMES.get(mon.lower().rstrip("."))
            else:
                mm = int(mon) if mon else None
            return int(y), mm
    return None, None


_ENDED = r"(?:ended|ending|end(?:ed)?\s+on|terminad[oa]|finalizad[oa]|cerrad[oa]|findo|encerrad[oa]|terminé|clos|clôturé|clôturée|abgeschlossen|chiuso|eindigend|berakhir)"
_PERIOD_WORD = (r"(?:year|period|twelve\s+months|12\s+months|six\s+months|6\s+months|three\s+months|nine\s+months|"
                r"quarter|half[- ]year|ejercicio|período|periodo|exercício|exercice|année|geschäftsjahr|esercizio|boekjaar|tahun\s+buku)")
PATTERNS = [
    # scope: what the report covers
    ("scope", 5, re.compile(r"(?:report(?:ing)?\s+period|(?:this|the)\s+report\s+(?:covers|relates\s+to|refers\s+to)|"
                            r"(?:information|data)\s+in\s+this\s+report\s+(?:refers|relates)\s+to|"
                            r"(?:reported|presented)\s+[^.;]{0,60}?for\s+the\s+period|"
                            r"período\s+(?:de\s+)?(?:reporte|informe)|periodo\s+(?:de\s+)?(?:reporte|informe)|"
                            r"período\s+(?:do\s+)?relat[oó]rio|报告期|報告期間|보고\s*기간)"
                            r"[^.;。]{0,120}?(?:to|until|through|–|—|-|a|al|até|au|bis|至|~|～)\s*" + DATE, re.I)),
    ("scope", 5, re.compile(r"(?:report(?:ing)?\s+period|this\s+report\s+(?:covers|relates\s+to))[^.;]{0,60}?"
                            r"(?:financial|fiscal|calendar)?\s*(?:year|fy)\s*" + _Y, re.I)),
    # period: "year ended 31 December 2021"
    ("period", 3, re.compile(_PERIOD_WORD + r"\s+(?:that\s+|which\s+)?" + _ENDED + r"\s+(?:on\s+|el\s+|em\s+|le\s+|am\s+|il\s+|op\s+|pada\s+)?" + DATE, re.I)),
    ("period", 3, re.compile(r"截至\s*" + _Y + r"\s*年\s*(1[0-2]|0?[1-9])\s*月\s*[0-3]?\d\s*日\s*止")),
    # balance sheet date
    ("balance", 1, re.compile(r"(?:as\s+(?:at|of)|al|em|au|zum|per)\s+" + DATE, re.I)),
    # fiscal labels
    ("label", 1, re.compile(r"(?<![A-Za-z])(?:FY|fiscal\s+year|financial\s+year|ejercicio|exercício|geschäftsjahr|esercizio)\s*"
                            + _Y + r"(?:\s*[/-]\s*(\d{2,4}))?(?!\d)", re.I)),
    ("label", 1, re.compile(r"(?<![A-Za-z])FY\s*'?(\d{2})(?:\s*[/-]\s*(\d{2}))?(?!\d)")),
    ("label", 1, re.compile(_Y + r"\s*年\s*(1[0-2]|0?[1-9])\s*月期")),                        # 2025年3月期 (Japan)
    # title
    ("title", 2, re.compile(r"(?:annual|integrated|sustainability|esg|csr|responsibility|impact|climate|tcfd|"
                            r"non-financial|stewardship|human\s+rights|modern\s+slavery|environmental|social)\s+"
                            r"(?:and\s+\w+\s+)?(?:report|review|statement|disclosure)s?\s*(?:for\s+)?(?:fy\s*)?" + _Y
                            + r"(?:\s*[/-]\s*(\d{2,4}))?(?!\d)", re.I)),
    ("title", 2, re.compile(r"(?<!\d)" + _Y + r"(?:\s*[/-]\s*(\d{2,4}))?\s+(?:annual|integrated|sustainability|esg|csr|"
                            r"impact|climate|tcfd|environmental|social)\s+(?:report|review)", re.I)),
    ("title", 2, re.compile(_Y + r"\s*年?\s*(?:年度报告|年度報告|社会责任报告|可持续发展报告|ESG报告|环境、社会及管治报告|統合報告書|サステナビリティ)")),
]
DATE_GROUPS = 12


def _end_year(kind, rx_i, m):
    """The period end year (and month when known) a match points to."""
    pat = PATTERNS[rx_i][2]
    g = m.groups()
    if pat.pattern.endswith(DATE) and len(g) >= DATE_GROUPS:
        y, mm = _date_parts(m, len(g) - DATE_GROUPS)
        return y, mm
    if kind == "period" and "截至" in pat.pattern:
        return int(g[0]), int(g[1])
    if kind == "label" and "月期" in pat.pattern:
        return int(g[0]), int(g[1])
    if kind in ("label", "title", "scope"):
        y, tail = g[0], (g[1] if len(g) > 1 else None)
        if len(y) == 2:                       # FY21
            y = "20" + y
        y = int(y)
        if tail:                              # 2024/25 or 2024-2025: the period ends in the second year
            t = int(tail[-2:]) + 2000
            if t == y + 1:
                return t, None
        return y, None
    return None, None


def evidence(pages, max_pages=400):
    """[(kind, weight, end_year, end_month, page, quote)] from a list of (page_no, text)."""
    out = []
    for pno, text in pages[:max_pages]:
        if not text:
            continue
        t = unicodedata.normalize("NFKC", text)
        t = re.sub(r"\s+", " ", t)
        for i, (kind, w, rx) in enumerate(PATTERNS):
            for m in rx.finditer(t):
                y, mm = _end_year(kind, i, m)
                if not y or not (2000 <= y <= 2035):
                    continue
                if kind == "title" and pno > 2:
                    continue                  # a report title quoted on page 40 is a reference
                q = t[max(0, m.start() - 40): m.end() + 20].strip()
                out.append((kind, w, y, mm, pno, q))
    return out


TIERS = [("scope", "what the report says it covers"), ("period", "'year ended <date>' statements"),
         ("title", "the report title on the first pages"), ("label", "FY labels and balance-sheet dates")]
TIER_OF = {"scope": "scope", "period": "period", "title": "title", "label": "label", "balance": "label"}
TIER_BASE = {"scope": 60, "period": 45, "title": 30, "label": 20}


def decide(pages, latest_possible=None):
    """(fiscal_year, period_end 'YYYY-MM' or 'YYYY', score 0-100, evidence quote, detail dict).

    The evidence is ranked in tiers, strongest first: what the report says it covers (scope, on its
    first ten pages) > 'year ended <date>' statements > the title > FY labels and balance dates.
    The year is decided by the strongest tier that has evidence; weaker tiers only move the score.
    Score = tier base (scope 60, period 45, title 30, labels 20) + up to 20 for how clearly one year
    dominates inside that tier + up to 20 for the other tiers that agree (minus 10 for each one
    that points elsewhere). latest_possible: the latest (year, month) the period can end in, i.e.
    the month the document was published or first seen, or a bare year, or None."""
    ev = evidence(pages)
    if latest_possible:
        cy, cm = latest_possible if isinstance(latest_possible, tuple) else (latest_possible, 12)
        ev = [e for e in ev if e[2] < cy or (e[2] == cy and (e[3] or 1) <= cm)]
    ev = [e for e in ev if not (e[0] == "scope" and e[4] > 10)]
    if not ev:
        return None, None, 0, "", {"n": 0}
    by_tier = collections.defaultdict(collections.Counter)
    months = collections.defaultdict(collections.Counter)
    for kind, w, y, mm, pno, q in ev:
        tier = TIER_OF[kind]
        early = 2 if pno <= 10 else 1             # the opening pages describe the report itself
        by_tier[tier][y] += w * early
        if mm and kind in ("scope", "period"):
            months[y][mm] += w
    winners = {}
    for tier, c in by_tier.items():
        top = c.most_common()
        best_w = top[0][1]
        winners[tier] = (max(y for y, w in top if w == best_w), best_w / sum(c.values()))
    chosen_tier = next(t for t, _ in TIERS if t in winners)
    best, share = winners[chosen_tier]
    score = TIER_BASE[chosen_tier] + 20 * share
    for t, (y, _) in winners.items():
        if t != chosen_tier:
            score += 10 if y == best else -10
    score = int(max(0, min(100, round(score))))
    mm = months[best].most_common(1)[0][0] if months[best] else None
    end = f"{best}-{mm:02d}" if mm else str(best)
    order = {"scope": 0, "period": 1, "title": 2, "label": 3, "balance": 4}
    quote = sorted([e for e in ev if e[2] == best and TIER_OF[e[0]] == chosen_tier],
                   key=lambda e: (order[e[0]], e[4]))[0]
    return best, end, score, f"p.{quote[4]}: {quote[5][:200]}", {
        "n": len(ev), "tier": chosen_tier, "share": round(share, 2),
        "winners": {t: y for t, (y, _) in winners.items()}}
