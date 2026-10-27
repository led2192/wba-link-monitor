#!/usr/bin/env python3
"""
key_reports.py: for every company, the long annual reports that carry its full-year financial data and
its full-year sustainability data, for the two most recent publication waves (owner's request, 24 Oct 2026).

Waves. Reports are grouped by the year they are published in, read from the end of the period they cover:
    wave P   periods ending July of P-1 to June of P (published during P, as a rule)
    in 2026: the 2026 wave is calendar-2025 reports and fiscal years such as April 2025 to March 2026;
             the 2025 wave is calendar-2024 reports and fiscal years ending July 2024 to June 2025.
The period end is `period_end` (YYYY-MM, read from the full text) when it has a month; otherwise the
document's `year` with the company's usual fiscal-year-end month (the most common month among its own
documents' period_end, December when none is known).

Which documents count (a "long annual report"):
    financial data      Annual Report, Integrated Report, 10K Form, a universal registration document
                        (is_urd), or as a last resort a Financial Statement
    sustainability data Sustainability, ESG, CSR, Integrated or Impact Report, or a whole annual report /
                        10-K / URD with a substantial embedded sustainability section (is_embedded)
    never               discarded rows; parts of a report or summaries (scope); documents shorter than
                        MIN_PAGES when the page count is known; documents whose cover names a
                        subsidiary rather than the group (entity_check = subsidiary, below)

Whose report is it? (entity_check, written on the candidate documents)
    Ode's developer confirmed (24 Oct 2026) that their pipeline reads the cover only to name a document,
    not to check the entity, and a BMW financing subsidiary's annual report was counted as BMW's. Here
    the first 1,200 characters of the cover are searched for a legal entity name that contains one of the
    company's distinctive name words and ends in a legal form (Limited, plc, B.V., AG, Inc....). When the
    first such name also carries a subsidiary marker (Finance, Funding, Capital, Treasury, Investment,
    Leasing, Insurance, a country name...) that the company's own name does not, the document is a
    `subsidiary` report; when the first name is the group's, `group`; when no such name is found, `unclear`
    (unclear documents still count: the check only removes what it can see).

Writes (with --commit):
    report_library  entity_check, entity_name           (candidates only, when changed)
    companies       fy_end_month (the usual fiscal-year-end month, December when no document states one),
                    kr_latest_status, kr_prev_status    (Both in one report / Financial and sustainability,
                                                         separate / Financial only / Sustainability only / None)
                    kr_latest_fin, kr_latest_sust, kr_prev_fin, kr_prev_sust   (links to report_library)
                    kr_note                             (what was chosen and what was left out, in words)
Dry run by default: prints the counts and writes key_reports.md.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE; QA: AIRTABLE_QA_TOKEN (optional). Args: --wave 2026 --wba PT_00019,...
"""
import argparse, collections, datetime as dt, os, re, time, unicodedata
from urllib.parse import quote

import qa
import company_coverage as CC
from monitor_core import airtable_request

MIN_PAGES = {"fin": 40, "fin_fs": 15, "sust": 15}
FIN_TYPES = {"Annual Report", "Integrated Report", "10K Form"}
FIN_RANK = {"Integrated Report": 4, "Annual Report": 3, "10K Form": 3, "Registration Document": 3, "Financial Statement": 1}
SUST_RANK = {"Integrated Report": 4, "Sustainability Report": 3, "ESG Report": 3, "CSR Report": 3, "Impact Report": 2,
             "Annual Report": 1, "10K Form": 1, "Registration Document": 1}
STATUS = {"both_one": "Both in one report", "both_sep": "Financial and sustainability, separate",
          "fin": "Financial only", "sust": "Sustainability only", "none": "None"}

LIB_FIELDS = [CC.F_WBA, CC.F_TYPE_AI, CC.F_TYPE_KW, CC.F_YEAR, CC.F_FILE, CC.F_DISCARD, "doc_label", "library_id",
              "page_count", "sustainability_class", "scope", "class_reason", "period_end", "file_status",
              "entity_check", "entity_name"]


# ---------------------------------------------------------------- waves

def wave_bounds(p):
    """((year, month) first, (year, month) last) of wave p."""
    return (p - 1, 7), (p, 6)


def wave_of(end, p):
    """'latest' when end (year, month) is in wave p, 'previous' when in wave p-1, else None."""
    if not end:
        return None
    for name, w in (("latest", p), ("previous", p - 1)):
        lo, hi = wave_bounds(w)
        if lo <= end <= hi:
            return name
    return None


PE = re.compile(r"^(20\d\d)(?:-(\d\d))?")


def end_month(period_end):
    """(year, month or None) from '2025-03' / '2025'."""
    m = PE.match(str(period_end or "").strip())
    if not m:
        return None, None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def doc_end(f, fye_month):
    y, mo = end_month(f.get("period_end"))
    if y and mo and 1 <= mo <= 12:
        return (y, mo)
    year = f.get(CC.F_YEAR)
    if isinstance(year, (int, float)) and CC.YEAR_OK.match(str(int(year))):
        return (int(year), fye_month)
    if y:
        return (y, fye_month)
    return None


def company_fye(months):
    """The company's usual fiscal-year-end month: the most common month among its documents, else December."""
    c = collections.Counter(m for m in months if m)
    return c.most_common(1)[0][0] if c else 12


# ---------------------------------------------------------------- roles

def roles(f, t):
    """Which data the document carries as a long annual report: a set of 'fin' / 'sust' (empty = none)."""
    if CC._name(f.get("scope")) in CC.PIECES:
        return set()
    pages = f.get("page_count") or 0
    out = set()
    if t in FIN_TYPES or CC.is_urd(f, t):
        if not pages or pages >= MIN_PAGES["fin"]:
            out.add("fin")
    elif t == "Financial Statement" and (not pages or pages >= MIN_PAGES["fin_fs"]):
        out.add("fin")
    if t in CC.SUST_FAMILY and (not pages or pages >= MIN_PAGES["sust"]):
        out.add("sust")
    elif CC.is_embedded(f, t):
        out.add("sust")
    return out


def rank(c, role):
    """Sort key, best first: covers both, type, has text to read, pages."""
    table = FIN_RANK if role == "fin" else SUST_RANK
    return (len(c["roles"]) == 2, table.get(c["type"], 0), c["readable"], c["pages"], c["library_id"])


def choose(cands):
    """{role: candidate} and the status key for one wave's candidates of one company."""
    best = {}
    for role in ("fin", "sust"):
        pool = [c for c in cands if role in c["roles"]]
        if pool:
            best[role] = max(pool, key=lambda c: rank(c, role))
    # one document carrying both is preferred over two halves when it exists
    both = [c for c in cands if len(c["roles"]) == 2]
    if both and not (best.get("fin") is best.get("sust")):
        one = max(both, key=lambda c: rank(c, "fin"))
        if FIN_RANK.get(one["type"], 0) >= FIN_RANK.get(best["fin"]["type"], 0):
            best = {"fin": one, "sust": one}
    if "fin" in best and "sust" in best:
        key = "both_one" if best["fin"] is best["sust"] else "both_sep"
    else:
        key = "fin" if "fin" in best else "sust" if "sust" in best else "none"
    return best, key


# ---------------------------------------------------------------- entity

GENERIC = set("""
group groups holding holdings company companies co corp corporation inc incorporated limited ltd plc llc lp sa s a ag
se nv n v bv b v spa s p a ab asa oyj as a s kk k k tbk bhd berhad pte public the and of de del la le les des et y e
international global industries industrial industry enterprises enterprise worldwide sociedad anonima aktiengesellschaft
""".split())
LEGAL = (r"(?:B\.?\s?V\.?|N\.?\s?V\.?|S\.?\s?A\.?(?:\s?B\.?)?|S\.?\s?p\.?\s?A\.?|AG|SE|GmbH|plc|PLC|Plc|Limited|LIMITED|Ltd\.?|"
         r"LTD\.?|Inc\.?|INC\.?|LLC|L\.?P\.?|Corporation|CORPORATION|Corp\.?|Pte\.?\s?Ltd\.?|Berhad|Bhd\.?|Tbk\.?|"
         r"ASA|AB|Oyj|A/S|K\.K\.|Co\.,?\s?Ltd\.?|S\.A\.U\.|SAS|S\.A\.S\.|DAC|Pty\.?\s?Ltd\.?|Holdings?)")
MARKERS = set("""
finance financing financial funding capital treasury investment investments invest credit leasing lease insurance
assurance reinsurance bank banking securities issuer issuance trust receivables mortgage properties property
asset boligkreditt livsforsikring forsikring kredit hypotek pensjon pension pensions
""".split())
COUNTRIES = set("""
uganda kenya nigeria ghana tanzania zambia malawi rwanda congo madagascar seychelles chad niger gabon egypt morocco
tunisia algeria india pakistan bangladesh sri lanka nepal indonesia malaysia singapore philippines thailand vietnam
cambodia myanmar china hong kong taiwan japan korea australia zealand canada mexico brazil argentina chile colombia
peru ecuador venezuela uruguay paraguay bolivia panama guatemala uk ireland france germany italy spain portugal
netherlands belgium luxembourg switzerland austria sweden norway denmark finland poland czech slovakia hungary romania
bulgaria greece turkey russia ukraine israel saudi arabia emirates uae qatar kuwait oman bahrain jordan lebanon
america usa us deutschland espana españa italia nederland belgie belgique suisse schweiz osterreich polska
""".split())


TITLE_MARKERS = (MARKERS | COUNTRIES) - {"finance", "financial", "financing", "capital", "investment", "investments",
                                        "invest", "credit", "trust", "property", "properties"}


def fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def name_words(company_name):
    """The distinctive words of a company's name ('Bayerische Motoren Werke AG' -> bayerische, motoren, werke)."""
    words = re.findall(r"[a-z0-9&]+", fold(company_name))
    return [w for w in words if w not in GENERIC and len(w) >= 2]


ENTITY = re.compile(r"((?:[A-Z0-9&][\w&.'’-]*\s+){0,7}?(?:[A-Z0-9&][\w&.'’-]*),?\s+(?:\([A-Za-z ]+\)\s+)?" + LEGAL + r")(?![\w])")


GROUP_WORDS = {"group", "holding", "holdings", "konsern", "konsernet", "konzern", "groupe", "grupo", "gruppo", "groep"}


def _trim(ent, words):
    """The entity from its first company-name word: 'Annual Financial Statements 2025 PUMA SE' -> 'PUMA SE'."""
    toks = ent.split()
    for i, t in enumerate(toks):
        if any(w in re.findall(r"[a-z0-9&]+", fold(t)) for w in words):
            return " ".join(toks[i:])
    return ent


def _subsidiary_words(tokens, own):
    return [w for w in tokens if w not in own and (w in MARKERS or w in COUNTRIES)]


# In a title, "Financial", "Finance", "Capital"... after the company name are usually the kind of document
# ("UPM Financial Statement Release", "EBRD Financial Report", "UPM Green Finance Report"), not a subsidiary.
def title_check(title, company_name):
    """'subsidiary' when the title names the company followed by a subsidiary word ('DNB Boligkreditt Annual
    Report', 'DNB Asset Management', 'Va Tech Wabag Deutschland GmbH'), 'group' when it names the company's
    group ('DNB Group Annual report', 'DNB konsernet'), else ''. title may be a doc_label ('year | company |
    title'): only the title part is read."""
    parts = [x.strip() for x in (title or "").split(" | ")]
    title = parts[-1] if len(parts) >= 3 else (title or "")
    words = name_words(company_name)
    own = set(re.findall(r"[a-z0-9]+", fold(company_name)))
    toks = re.findall(r"[a-z0-9&]+", fold(title))
    for i, t in enumerate(toks):
        if t in words:
            after = toks[i + 1:i + 3]
            if any(a in GROUP_WORDS for a in after):
                return "group"
            if [w for w in _subsidiary_words(after, own) if w in TITLE_MARKERS]:
                return "subsidiary"
    return ""


def entity_check(cover, company_name, head=1200, title=""):
    """('group' | 'subsidiary' | 'unclear', the entity name found). The title decides first when it is plain
    about it (a group report, or a named subsidiary); else the first legal name on the cover."""
    words = name_words(company_name)
    if not words:
        return "unclear", ""
    by_title = title_check(title, company_name) if title else ""
    if by_title == "group":
        return "group", "title: " + title[:100]
    own = set(re.findall(r"[a-z0-9]+", fold(company_name)))
    text = re.sub(r"\s+", " ", (cover or "")[:head])
    for m in ENTITY.finditer(text):
        ent = m.group(1).strip(" ,.;:")
        if not any(w in re.findall(r"[a-z0-9&]+", fold(ent)) for w in words):
            continue
        ent = _trim(ent, words)
        ew = re.findall(r"[a-z0-9&]+", fold(ent))
        if any(w in GROUP_WORDS for w in ew):
            return "group", ent[:120]           # "Mizuho Financial Group, Inc.", "HSBC Holdings plc": the parent
        return ("subsidiary" if _subsidiary_words(ew, own) else "group"), ent[:120]
    if by_title == "subsidiary":
        return "subsidiary", "title: " + title[:100]
    return "unclear", ""


# ---------------------------------------------------------------- data

def companies(wbas=None):
    out = {}
    for rec in CC.sweep(CC.COMP, [CC.C_WBA, "company_name", "kr_latest_status", "kr_prev_status", "kr_latest_fin",
                                  "kr_latest_sust", "kr_prev_fin", "kr_prev_sust", "kr_note", "fy_end_month"],
                        "companies", CC.wba_formula(wbas)):
        w = (rec["fields"].get(CC.C_WBA) or "").strip()
        if w:
            out[w] = rec
    return out


def covers(ids):
    """{record id: cover_text} for these rows, 50 at a time."""
    out, ids = {}, list(ids)
    url = f"{CC.API}/{CC.BASE}/{quote(CC.LIB)}"
    for i in range(0, len(ids), 50):
        part = ids[i:i + 50]
        formula = "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in part) + ")"
        params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", "cover_text")]
        offset = None
        while True:
            p = params + ([("offset", offset)] if offset else [])
            j = airtable_request("GET", url, CC.H, params=p).json()
            for r in j.get("records", []):
                out[r["id"]] = r.get("fields", {}).get("cover_text") or ""
            offset = j.get("offset")
            if not offset:
                break
        time.sleep(0.12)
    return out


def candidate(rec, t, rl):
    f = rec["fields"]
    return {"id": rec["id"], "type": t, "roles": rl, "pages": f.get("page_count") or 0,
            "readable": bool(f.get(CC.F_FILE)) or CC._name(f.get("file_status")) == "too_big",
            "label": f.get("doc_label") if isinstance(f.get("doc_label"), str) else "",
            "library_id": f.get("library_id") or "", "entity": f.get("entity_check"), "entity_name": f.get("entity_name") or ""}


def describe(c):
    return f"{c['label'] or c['library_id']} ({c['type']}{', ' + str(c['pages']) + ' pages' if c['pages'] else ''})"


def note_for(wave, best, key, left_out):
    lines = []
    for name, p in (("latest", wave), ("previous", wave - 1)):
        b, k = best[name], key[name]
        lo, hi = wave_bounds(p)
        head = f"{p} wave (periods ending {lo[1]:02d}/{lo[0]} to {hi[1]:02d}/{hi[0]}): {STATUS[k]}"
        if k == "both_one":
            head += f". {describe(b['fin'])}"
        else:
            parts = [f"financial: {describe(b['fin'])}" if "fin" in b else "", f"sustainability: {describe(b['sust'])}" if "sust" in b else ""]
            head += (". " + "; ".join(x for x in parts if x)) if any(parts) else ""
        lines.append(head)
    if left_out:
        lines.append("Left out, cover names a subsidiary: " + "; ".join(left_out[:5]) + (f" and {len(left_out) - 5} more" if len(left_out) > 5 else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=int, default=dt.date.today().year, help="publication year of the latest wave")
    ap.add_argument("--wba", default="", help="comma-separated wba_ids (default: all)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()
    if a.commit:
        os.environ["QA_COMMIT"] = "true"
    run = qa.Run("key_reports.py")
    wbas = [w.strip() for w in a.wba.split(",") if w.strip()] or None
    comp = companies(wbas)
    names = {w: (r["fields"].get("company_name") or "") for w, r in comp.items()}

    rows = collections.defaultdict(list)
    for rec in CC.sweep(CC.LIB, LIB_FIELDS, "report_library", CC.wba_formula(wbas)):
        f = rec["fields"]
        w = (f.get(CC.F_WBA) or "").strip()
        if w and not f.get(CC.F_DISCARD):
            rows[w].append(rec)

    # candidates of the two waves, before the entity check
    cand, fyes = {}, {}
    for w, recs in rows.items():
        # the usual year-end month from the long annual reports only: half-year and quarterly reports end in
        # June or September every year and would outvote a December year-end (ACCIONA, 27 Oct 2026)
        annual = [r for r in recs if roles(r["fields"], CC.eff_type(r["fields"]))]
        fye = fyes[w] = company_fye([end_month(r["fields"].get("period_end"))[1] for r in (annual or recs)])
        for rec in recs:
            f = rec["fields"]
            t = CC.eff_type(f)
            rl = roles(f, t)
            if not rl:
                continue
            wv = wave_of(doc_end(f, fye), a.wave)
            if wv:
                cand.setdefault(w, {"latest": [], "previous": []})[wv].append(candidate(rec, t, rl))
    ids = [c["id"] for w in cand.values() for lst in w.values() for c in lst]
    print(f">>> {len(ids):,} candidate long reports in the {a.wave} and {a.wave - 1} waves; reading their covers ...", flush=True)
    cov = covers(ids)

    ent_counts, ent_updates, samples = collections.Counter(), [], collections.defaultdict(list)
    for w, waves in cand.items():
        for lst in waves.values():
            for c in lst:
                kind, ent = entity_check(cov.get(c["id"], ""), names.get(w, ""), title=c["label"])
                if len(samples[kind]) < (40 if kind == "subsidiary" else 12):
                    samples[kind].append(f"{names.get(w, w)}: \"{ent}\" in {c['label'][:90]}")
                cur = CC._name(c["entity"]) or ""
                if cur and c["entity_name"] == ent:
                    kind = cur                    # same reading as before: a person's correction stands
                elif cur != kind or c["entity_name"] != ent:
                    ent_updates.append({"id": c["id"], "fields": {"entity_check": kind, "entity_name": ent}})
                ent_counts[kind] += 1
                c["entity"] = kind

    status_counts = {"latest": collections.Counter(), "previous": collections.Counter()}
    overview, comp_updates = collections.Counter(), []
    for w, rec in comp.items():
        waves = cand.get(w, {"latest": [], "previous": []})
        best, key, left = {}, {}, []
        for name in ("latest", "previous"):
            ok = [c for c in waves[name] if c["entity"] != "subsidiary"]
            left += [describe(c) for c in waves[name] if c["entity"] == "subsidiary"]
            best[name], key[name] = choose(ok)
            status_counts[name][key[name]] += 1
        lk, pk = key["latest"], key["previous"]
        overview["latest complete" if lk in ("both_one", "both_sep") else
                 "latest partial" if lk != "none" else
                 "only previous wave" if pk != "none" else "neither wave"] += 1
        target = {"kr_latest_status": STATUS[lk], "kr_prev_status": STATUS[pk],
                  "kr_latest_fin": [best["latest"]["fin"]["id"]] if "fin" in best["latest"] else [],
                  "kr_latest_sust": [best["latest"]["sust"]["id"]] if "sust" in best["latest"] else [],
                  "kr_prev_fin": [best["previous"]["fin"]["id"]] if "fin" in best["previous"] else [],
                  "kr_prev_sust": [best["previous"]["sust"]["id"]] if "sust" in best["previous"] else [],
                  "kr_note": note_for(a.wave, best, key, left),
                  # the usual year-end month, for report_library.fiscal_year_wba on rows without a month
                  "fy_end_month": fyes.get(w, 12)}
        cur = rec["fields"]
        if any(CC._norm(cur.get(k)) != CC._norm(v) for k, v in target.items()):
            comp_updates.append({"id": rec["id"], "fields": target})

    n = len(comp) or 1
    md = [f"### Key reports, {a.wave} and {a.wave - 1} waves" + ("" if a.commit else " (dry run)"), "",
          f"Companies: {len(comp):,}. Candidate long reports: {len(ids):,}.", "",
          f"| | {a.wave} wave (latest) | {a.wave - 1} wave |", "| --- | --- | --- |"]
    for k, label in STATUS.items():
        md.append(f"| {label} | {status_counts['latest'][k]:,} ({status_counts['latest'][k] / n:.0%}) | "
                  f"{status_counts['previous'][k]:,} ({status_counts['previous'][k] / n:.0%}) |")
    md += ["", "Overview: " + "; ".join(f"{k} {v:,}" for k, v in overview.most_common()) + ".", "",
           "Whose report (entity_check) on the candidates: " + ", ".join(f"{k} {v:,}" for k, v in ent_counts.most_common()) + ".", ""]
    for kind in ("subsidiary", "group", "unclear"):
        if samples[kind]:
            md += [f"Samples, {kind}:", ""] + [f"- {s}" for s in samples[kind]] + [""]
    md += [f"Writes: {len(ent_updates):,} documents (entity_check), {len(comp_updates):,} companies.", ""]
    open("key_reports.md", "w").write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)

    if a.commit:
        lib = f"{CC.API}/{CC.BASE}/{quote(CC.LIB)}"
        com = f"{CC.API}/{CC.BASE}/{quote(CC.COMP)}"
        for url, ups in ((lib, ent_updates), (com, comp_updates)):
            for i in range(0, len(ups), 10):
                airtable_request("PATCH", url, CC.H, {"records": ups[i:i + 10]})   # no typecast: link fields get ids only
                time.sleep(0.22)
    run.finish("ok", rows_in=len(ids), rows_out=len(comp_updates),
               counters={"latest": dict(status_counts["latest"]), "previous": dict(status_counts["previous"]),
                         "entity": dict(ent_counts), "overview": dict(overview)})
    run.write_step_summary("ok", dict(overview), title="Key reports")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        open("key_reports.md", "w").write("### Key reports: the run failed\n\n```\n" + traceback.format_exc()[-3000:] + "\n```\n")
        raise
