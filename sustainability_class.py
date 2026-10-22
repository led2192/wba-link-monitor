#!/usr/bin/env python3
"""
sustainability_class.py — where a document's sustainability disclosure lives, what standards it
names, and whether it was assured. Decided by rules from the document's text, no AI field.

Why: since the 2024 reports many companies no longer publish a separate sustainability report; the
disclosure sits inside the annual report (EU CSRD "sustainability statement", India's BRSR, Hong Kong
ESG sections, UK TCFD chapters). Typed as "Annual Report", those documents are invisible to anyone
looking for sustainability reporting. This script gives every report-family row four facts:

    sustainability_class   D dedicated      a standalone sustainability, ESG, CSR or impact report
                           E embedded       an annual or integrated report that holds a substantive
                                            sustainability section
                           S specialized    one topic or framework: climate, CDP, GRI index, policies...
                           F financial only an annual or financial report without such a section
    class_reason           the rule that decided, with the numbers behind it
    sustainability_share   % of the pages with text that are about sustainability (full text only)
    frameworks             the reporting standards the document names: ESRS, GRI, SASB, ISSB, TCFD,
                           TNFD, SDGs, Integrated Reporting Framework, EU Taxonomy, GHG Protocol,
                           SBTi, CDP, UN Global Compact, BRSR, HKEX ESG Guide
    assurance              external assurance of the sustainability information, when the text says:
                           limited / reasonable / limited and reasonable / assured, level not
                           stated / none found

The four classes follow the public taxonomy of SustainabilityReports.com (Dedicated, Embedded,
Specialized, Financial), so the counts can be compared with theirs. Two things are added: the
standards are a second axis instead of report types (an annual report can follow ESRS, a dedicated
report GRI; their "TCFD Report" is a type, here TCFD is a framework any document can name), and the
embedded class is decided from the pages themselves, with the evidence written down, in any language
the lexicon knows (English, Spanish, Portuguese, French, German, Italian, Dutch, Japanese, Chinese).

How the class is decided (decide()):
  by type   Sustainability, ESG, CSR and Impact reports are D; Integrated reports are E; climate,
            environmental, social, CDP, GRI, SASB, engagement reports, certifications and the
            policies are S.
  by text   Annual reports, 10-Ks, financial statements, interim reports, registration and statutory
            documents and presentations are E or F from their pages. A page is "about sustainability"
            when it holds at least 1.5 sustainability terms per 1,000 characters and 3 different
            ones (a risk-factor page names each once; a sustainability page is dense with them). E when
            the longest block of such pages is 5+ pages, or 8+ such pages with a block of 3+, or they
            are 25%+ of the pages with text (3+ pages), or the contents page names a sustainability
            section and 2+ such pages exist. Otherwise F.
  first     Without full text, the first pages (cover_text, six pages) decide only in one direction:
  pages     a contents entry such as "Sustainability report 74", "Non-financial statement",
            "BRSR" gives E (reason "first pages: ..."); otherwise the class stays blank until the full
            text arrives (the queue re-reads first-pages rows once it does).
Calibrated on 22 Oct 2026 against the 2,619 documents the Source Library analysts typed and the
pipeline holds in full text: 252 of 452 annual reports come out E, and the blocks found were
sustainability chapters, TCFD and ESRS sections and BRSRs in every sample checked by hand.

Frameworks count when named twice in the full text (once on the first pages). Assurance is read from
the full text of D, E and S documents: "limited assurance" near sustainability words, "reasonable
assurance" near them and away from the financial audit wording.

Queue: rows with class_reason blank, plus first-pages rows whose full text has since arrived; --all
redoes every row. Dry run by default; --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE, TEXT_STORE_REPO + TEXT_STORE_TOKEN (or TEXT_STORE_DIR)
"""
import argparse, collections, gzip, json, os, re, sys, tempfile, time
from concurrent.futures import ProcessPoolExecutor

API = "https://api.airtable.com/v0"
D, E, S, F = "D dedicated", "E embedded", "S specialized", "F financial only"
BY_TYPE = {
    "Sustainability Report": D, "ESG Report": D, "CSR Report": D, "Impact Report": D,
    "Integrated Report": E,
    "Climate Report": S, "Environmental Reports": S, "Social Reports": S, "CDP Report": S, "GRI": S,
    "SASB Index": S, "Engagement Report": S, "Certifications": S, "Code of Conduct": S,
    "Human Rights Policy": S, "Policy Documents": S,
}
BY_TEXT = {"Annual Report", "10K Form", "Financial Statement", "Interim Reports", "Registration Document",
           "Statutory Documents", "Company Presentations"}
FIELDS = ["library_id", "source_type_check", "source_type", "discard", "sustainability_class", "class_reason",
          "sustainability_share", "frameworks", "assurance"]
MIN_PAGE_CHARS = 200      # a page with less text (a photo, a chart) neither counts nor breaks a block
DENSITY = 1.5             # sustainability terms per 1,000 characters
DISTINCT = 3              # different terms on the page
COVER_CHARS = 10_000      # the first pages, as cover_text holds them

# ---- the lexicon ---------------------------------------------------------------------------------
# Matching runs on lowercased text with single spaces, against one trie-shaped regex (all the phrases
# share prefixes in the pattern, so a 300-phrase lexicon is as fast as a short one). WORDS need a
# boundary after them ("sustainable", not "sustainably"); STEMS take any ending.
WORDS = """
esrs|csrd|gri|tcfd|sasb|tnfd|issb|brsr|esg|csr|sdg|sdgs|ghg|ltifr|trir|sbti|ungc|ifrs s1|ifrs s2|eu taxonomy
greenhouse gas|greenhouse gases|scope 1|scope 2|scope 3|co2|co2e|tco2|tco2e
carbon emission|carbon emissions|carbon footprint|carbon neutral|carbon neutrality|carbon intensity|carbon offset|carbon offsets|carbon credit|carbon credits|carbon pricing
net zero|net-zero|decarbonisation|decarbonization|renewable energy|renewable electricity|renewable source|renewable sources
energy consumption|energy intensity|energy efficiency
climate change|climate risk|climate risks|climate related|climate-related|climate transition|climate action|climate strategy|climate resilience|global warming
biodiversity|nature related|nature-related|deforestation
water consumption|water withdrawal|water stewardship|water use|water usage|water intensity|water stress|wastewater
waste generated|waste management|waste reduction|waste diverted|hazardous waste|recycled|recycling|circular economy|circularity
human rights|modern slavery|child labour|child labor|forced labour|forced labor|living wage|due diligence|responsible sourcing|supplier code
diversity|diversity and inclusion|diversity, equity and inclusion|inclusion|gender pay|gender balance|gender diversity|gender equality|pay gap|women in
health and safety|health & safety|occupational health|occupational safety|occupational injury|occupational injuries|lost time injury|lost-time injury
injury rate|injury frequency|fatality|fatalities
employee engagement|employee wellbeing|employee well-being|employee turnover|training hours
community investment|community engagement|community development|community program|community programs
sustainable|sustainability|sustainable development goal|sustainable development goals|corporate responsibility|corporate social responsibility
materiality|material topic|material topics|material issue|material issues|material matter|material matters|stakeholder engagement
non-financial statement|non-financial information|non-financial report|non-financial reporting|nonfinancial statement|nonfinancial information
anti-corruption|anticorruption|anti-bribery|antibribery|business ethics|code of conduct
science based target|science based targets|science-based target|science-based targets|un global compact|paris agreement|just transition
impact assessment|impact measurement|social impact|environmental impact
sostenibilidad|sostenible|sustentabilidade|sustentavel|sustentável|durabilité|durabilite|développement durable|developpement durable|nachhaltigkeit
sostenibilità|sostenibilita|sostenibile|duurzaamheid|duurzaam|duurzame
emisiones|emissões|emissoes|émissions|emissionen|emissioni|gases de efecto invernadero|gases de efeito estufa|gaz à effet de serre|gas serra
cambio climático|cambio climatico|mudança climática|mudanças climáticas|changement climatique|klimawandel|cambiamento climatico|klimaatverandering
biodiversidad|biodiversidade|biodiversité|biodiversite|biodiversität|biodiversitat|biodiversità|biodiversiteit
derechos humanos|direitos humanos|droits humains|droits de l'homme|menschenrechte|diritti umani|mensenrechten
diversidad|diversidade|diversité|diversite|diversität|diversitat|diversità|diversiteit
materialidad|materialidade|matérialité|materialite|wesentlichkeit|materialità|materialiteit
huella de carbono|pegada de carbono|empreinte carbone|co2-fußabdruck|impronta di carbonio|co2-voetafdruk
responsabilidad social|responsabilidade social|responsabilité sociale|soziale verantwortung|responsabilità sociale
neutralidad de carbono|neutralité carbone|klimaneutralität|klimaneutral|carbono neutro
economía circular|economia circular|économie circulaire|kreislaufwirtschaft|economia circolare|circulaire economie
energía renovable|energías renovables|energia renovável|energias renováveis|énergie renouvelable|énergies renouvelables|erneuerbare energie|erneuerbare energien
energie rinnovabili|hernieuwbare energie
seguridad y salud|saúde e segurança|santé et sécurité|arbeitssicherheit|salute e sicurezza
información no financiera|informacion no financiera|performance extra-financière|performance extra-financiere|nichtfinanzielle|non finanziaria|non finanziario
taxonomía|taxonomia|taxonomie|tassonomia
informe de sostenibilidad|relatório de sustentabilidade|relatorio de sustentabilidade|rapport de durabilité|nachhaltigkeitsbericht|nachhaltigkeitserklärung
bilancio di sostenibilità|duurzaamheidsverslag
"""
STEMS = "volunteer whistleblow nachhaltig treibhausgas broeikasgas emissie".split()
CJK = ("サステナビリティ サステナブル 持続可能 温室効果ガス 脱炭素 カーボンニュートラル 気候変動 生物多様性 人権 ダイバーシティ マテリアリティ "
       "可持续 可持續 温室气体 溫室氣體 碳中和 碳排放 气候变化 氣候變化 生物多样性 生物多樣性 人权 人權 环境、社会及管治 環境、社會及管治 "
       "社会责任 社會責任 可持续发展 可持續發展").split()


def _trie(phrases):
    r"""One regex alternation that shares prefixes: ['carbon neutral', 'carbon credit'] ->
    carbon\ (?:neutral|credit)."""
    root = {}
    for ph in phrases:
        node = root
        for ch in ph:
            node = node.setdefault(ch, {})
        node[""] = {}

    def build(node):
        if "" in node and len(node) == 1:
            return ""
        alts, opt = [], False
        for ch, sub in sorted(node.items()):
            if ch == "":
                opt = True; continue
            alts.append(re.escape(ch) + build(sub))
        if opt and alts:
            return "(?:" + "|".join(alts) + ")?"
        return alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
    return build(root)


_WORDS = [w.strip() for w in re.split(r"[|\n]", WORDS) if w.strip()]
LEX = re.compile(r"(?<![a-z0-9])(?:(?:" + _trie(_WORDS) + r")(?![a-z])|(?:" + _trie(STEMS) + r"))")
LEX_CJK = re.compile("|".join(map(re.escape, CJK)))
_WS = re.compile(r"\s+")

# a contents-page entry for a sustainability section: a heading followed by a page number, or a
# statutory name for the section
SECTION = re.compile(
    r"(?:sustainability|esg|non-?financial|corporate\s(?:social\s)?responsibility|environmental,?\ssocial,?\s(?:and|&)\sgovernance|"
    r"climate-related\sfinancial|tcfd|sustainable\sdevelopment)"
    r"\s*(?:statement|report|review|section|disclosures?|information|performance|highlights)?\s*[.\u2026_ ]{0,40}\s*(?:p\.?\s*)?\d{1,3}(?![\d%.,])"
    r"|business\sresponsibility\s(?:and|&)\ssustainability\sreport|\bbrsr\b|sustainability\sstatement|non-?financial\s(?:statement|information\sstatement)|"
    r"estado\sde\sinformaci[oó]n\sno\sfinanciera|d[eé]claration\sde\sperformance\sextra-?financi[eè]re|nichtfinanzielle\s(?:erkl[aä]rung|berichterstattung)|"
    r"nachhaltigkeitsbericht|nachhaltigkeitserkl[aä]rung|dichiarazione\s(?:consolidata\s)?(?:di\scarattere\s)?non\sfinanziari[ao]|rendicontazione\sdi\ssostenibilit[aà]|"
    r"informe\sde\ssostenibilidad|rapport\sde\sdurabilit[eé]|relat[oó]rio\sde\ssustentabilidade|duurzaamheidsverslag|"
    r"サステナビリティ|可持续发展报告|可持續發展報告|环境、社会及管治|環境、社會及管治", re.I)

# the standards a document can name: phrase -> framework (matched on lowercased text)
FRAMEWORK_PHRASES = {
    "ESRS": ["esrs", "european sustainability reporting standards", "csrd", "corporate sustainability reporting directive"],
    "GRI": ["gri standards", "gri standard", "gri content index", "gri universal", "global reporting initiative",
            "accordance with the gri", "reference with the gri", "gri 2-", "gri 3-", "gri 20", "gri 30", "gri 40"],
    "SASB": ["sasb", "sustainability accounting standards board"],
    "ISSB": ["issb", "ifrs s1", "ifrs s2", "international sustainability standards board"],
    "TCFD": ["tcfd", "task force on climate-related financial disclosures", "taskforce on climate-related financial disclosures"],
    "TNFD": ["tnfd", "taskforce on nature-related"],
    "Integrated Reporting Framework": ["<ir>", "integrated reporting framework", "international integrated reporting council", "iirc",
                                       "value reporting foundation"],
    "SDGs": ["sdg", "sdgs", "sustainable development goals"],
    "UN Global Compact": ["un global compact", "united nations global compact", "ungc"],
    "GHG Protocol": ["ghg protocol", "greenhouse gas protocol"],
    "EU Taxonomy": ["eu taxonomy", "taxonomy regulation", "regulation (eu) 2020/852"],
    "SBTi": ["sbti", "science based targets initiative", "science-based targets initiative", "science based target", "science-based target"],
    "CDP": ["carbon disclosure project", "cdp climate", "cdp water", "cdp forest", "cdp questionnaire", "cdp score", "cdp disclosure",
            "cdp rating", "cdp a list", "cdp a-list"],
    "BRSR": ["brsr", "business responsibility and sustainability report"],
    "HKEX ESG Guide": ["esg reporting guide", "esg reporting code", "appendix 27 to the", "appendix c2 to the", "appendix 27 of the", "appendix c2 of the"],
}
_FW_OF = {ph: name for name, phs in FRAMEWORK_PHRASES.items() for ph in phs}
FW = re.compile(r"(?<![a-z0-9])(?:" + _trie(list(_FW_OF)) + r")(?![a-z])")
LIMITED = re.compile(r"limited assurance")
REASONABLE = re.compile(r"reasonable assurance")
SUS_CONTEXT = re.compile(r"sustainability|esg|non-?financial|greenhouse|emissions|ghg|scope [123]|csrd|esrs|gri|carbon")
FIN_CONTEXT = re.compile(r"financial statements|free from material misstatement|audit opinion|\bisas?\b|auditing standards|"
                         r"internal controls?|provide reasonable assurance that")
# the limited-assurance boilerplate compares itself with a reasonable one ("less in extent than for a
# reasonable assurance engagement"): that mention is not a reasonable assurance
COMPARES = re.compile(r"limited assurance|less in extent|substantially (?:lower|less)|would have been obtained|not (?:a|an) reasonable")
ASSURED = re.compile(r"independent (?:limited )?assurance (?:report|statement)|assurance (?:statement|report) (?:on|over|of|for) "
                     r"(?:the |our |selected )?(?:sustainability|esg|non-?financial|greenhouse|ghg|emissions|climate)|"
                     r"(?:independent |external )?(?:assurance|verification) statement|independently (?:assured|verified)|"
                     r"externally assured|(?:has|have|were|was) been assured|isae 3000|isae 3410|aa1000as")


def norm(text):
    """lowercase, single spaces"""
    return _WS.sub(" ", (text or "").lower())


def page_hits(text):
    """(hits, distinct terms) on one page; text already lowercased."""
    hits = LEX.findall(text) + LEX_CJK.findall(text)
    return len(hits), len(set(hits))


def page_flags(pages):
    """[(page, text)] -> True (about sustainability) / False / None (too little text) per page."""
    out = []
    for p, t in pages:
        if len(t) < MIN_PAGE_CHARS:
            out.append(None); continue
        n, d = page_hits(norm(t))
        out.append(n * 1000 / len(t) >= DENSITY and d >= DISTINCT)
    return out


def profile(pages):
    flags = page_flags(sorted(pages))
    n = sum(1 for f in flags if f is not None)
    k = sum(1 for f in flags if f)
    best = cur = 0
    for f in flags:
        if f:
            cur += 1; best = max(best, cur)
        elif f is not None:
            cur = 0
    return {"text_pages": n, "sus": k, "share": round(100 * k / n) if n else None, "run": best}


def section_named(cover):
    m = SECTION.search(cover or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else None


def frameworks(text, min_hits=2):
    """The standards named at least min_hits times; text already lowercased."""
    c = collections.Counter(_FW_OF[m] for m in FW.findall(text or ""))
    return [n for n in FRAMEWORK_PHRASES if c[n] >= min_hits]


def assurance(text):
    """What the text says about external assurance of the sustainability information (lowercased text)."""
    text = text or ""
    lim = any(SUS_CONTEXT.search(text[max(0, m.start() - 400):m.end() + 400]) for m in LIMITED.finditer(text))
    rea = False
    for m in REASONABLE.finditer(text):
        w = text[max(0, m.start() - 300):m.end() + 300]
        if SUS_CONTEXT.search(w) and not FIN_CONTEXT.search(w) and not COMPARES.search(w):
            rea = True; break
    if lim and rea:
        return "limited and reasonable"
    if lim or rea:
        return "limited" if lim else "reasonable"
    return "assured, level not stated" if ASSURED.search(text) else "none found"


def embedded(pr, section):
    """Is there a substantive sustainability section? (rule name, or None)"""
    if pr["run"] >= 5:
        return f"a block of {pr['run']} sustainability pages"
    if pr["sus"] >= 8 and pr["run"] >= 3:
        return f"{pr['sus']} sustainability pages, a block of {pr['run']}"
    if pr["sus"] >= 3 and pr["share"] >= 25:
        return f"{pr['share']}% of the pages with text are about sustainability"
    if section and pr["sus"] >= 2:
        return f"the contents names \"{section}\" and {pr['sus']} pages are about sustainability"
    return None


def decide(doc_type, pages=None, cover=""):
    """-> {"class", "reason", "share", "frameworks", "assurance"}. pages: [(page, text)] full text, or
    None; cover: the first pages' text when there is no full text."""
    doc_type = (doc_type or "").strip()
    text = norm(" ".join(t for _, t in sorted(pages))) if pages else ""
    first = norm(" ".join(t for _, t in sorted(pages)[:6])[:COVER_CHARS] if pages else (cover or "")[:COVER_CHARS])
    out = {"class": None, "reason": "", "share": None,
           "frameworks": frameworks(text) if pages else frameworks(first, 1), "assurance": ""}
    klass = BY_TYPE.get(doc_type)
    if klass:
        out["class"] = klass
        word = {D: "a dedicated sustainability report", E: "an integrated report", S: "a specialized document"}[klass]
        if pages:
            pr = profile(pages)
            out["share"] = pr["share"]
            out["reason"] = f"{doc_type}: {word}; {pr['sus']} of {pr['text_pages']} pages with text are about sustainability"
            out["assurance"] = assurance(text)
        else:
            out["reason"] = f"first pages: {doc_type}: {word}" if cover else f"{doc_type}: {word}; no text yet"
        return out
    if doc_type not in BY_TEXT:
        out["reason"] = f"{doc_type or 'no type'}: not a report family this classifies"
        return out
    section = section_named(first)
    if pages:
        pr = profile(pages)
        out["share"] = pr["share"]
        if not pr["text_pages"]:
            out["reason"] = "no readable text"
            return out
        why = embedded(pr, section)
        if why:
            out["class"] = E
            out["reason"] = f"{why} ({pr['sus']} of {pr['text_pages']} pages with text)"
            out["assurance"] = assurance(text)
        else:
            out["class"] = F
            out["reason"] = (f"{pr['sus']} of {pr['text_pages']} pages with text are about sustainability, longest block "
                             f"{pr['run']}; " + (f"the contents names \"{section}\" but the pages do not bear it out"
                                                 if section else "no sustainability section in the contents"))
        return out
    if not cover:
        out["reason"] = "no text yet"
        return out
    if section:
        out["class"] = E
        out["reason"] = f"first pages: the contents names \"{section}\""
    else:
        out["reason"] = "first pages: no sustainability section named; waiting for the full text"
    return out


# ---- Airtable and the text store ------------------------------------------------------------------

def type_of(f):
    return (f.get("source_type_check") or "").strip() or (f.get("source_type") or "").strip()


def airtable_rows(token, base, everything):
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    formula = ("NOT({discard})" if everything else
               "AND(NOT({discard}), OR({class_reason}='', LEFT({class_reason}&'', 12)='first pages:', "
               "LEFT({class_reason}&'', 11)='no text yet', RIGHT({class_reason}&'', 11)='no text yet'))")
    params = [("pageSize", "100"), ("filterByFormula", formula)] + [("fields[]", f) for f in FIELDS]
    out, offset = {}, None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
        for r in j.get("records", []):
            lid = r["fields"].get("library_id")
            if lid and (type_of(r["fields"]) in BY_TYPE or type_of(r["fields"]) in BY_TEXT):
                out[lid] = r
        offset = j.get("offset")
        if not offset:
            return out
        time.sleep(0.21)


def cover_rows(token, base, everything):
    """Stream rows with cover text not yet read (100 at a time: cover_text is up to 10,000 characters)."""
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}"}
    formula = ("AND(NOT({discard}), {cover_text}!='')" if everything else
               "AND(NOT({discard}), {cover_text}!='', OR({class_reason}='', "
               "LEFT({class_reason}&'', 11)='no text yet', RIGHT({class_reason}&'', 11)='no text yet'))")
    params = [("pageSize", "100"), ("filterByFormula", formula), ("fields[]", "library_id"), ("fields[]", "cover_text")]
    offset = None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", f"{API}/{base}/report_library", H, params=p).json()
        yield from j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return
        time.sleep(0.21)


def read_part(args):
    """Decide every wanted document whose newest copy is in this pages part. wanted: {id: type}."""
    path, wanted = args
    out, cur, pages = {}, None, []

    def flush():
        if cur is not None and pages:
            out[cur] = decide(wanted[cur], pages)

    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r["id"] not in wanted:
                continue
            if r["id"] != cur:
                flush(); cur, pages = r["id"], []
            pages.append((r["p"], r.get("t") or ""))
    flush()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--all", action="store_true", help="redo every row, not only new ones")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--cover-limit", type=int, default=60000,
                    help="at most this many documents read from their first pages per run (0 = all)")
    a = ap.parse_args()
    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    if not (token and base):
        sys.exit("Set AIRTABLE_TOKEN and AIRTABLE_BASE.")
    import text_core as tc
    rows = airtable_rows(token, base, a.all)
    print(f">>> {len(rows)} library rows in the queue.", flush=True)
    store = tc.open_store()
    folder = tempfile.mkdtemp(prefix="sc-")
    names = [n for n in store.assets() if tc.part_of(n)[0]]
    tc.sync_down(store, [n for n in names if n.startswith("docs-")], folder)
    index = tc.load_docs_index(folder, names)
    want = collections.defaultdict(dict)          # part -> {library_id: type}
    cover_ids = set()
    skipped = collections.Counter()
    for lid, r in rows.items():
        d = index.get(lid)
        reason = r["fields"].get("class_reason") or ""
        if not d or d.get("status") != "ok":
            if reason.startswith("first pages:") and not a.all:
                skipped["first pages already read"] += 1; continue
            cover_ids.add(lid)
            skipped["no full text: first pages"] += 1; continue
        want[d["part"]][lid] = type_of(r["fields"])
        if a.limit and sum(len(v) for v in want.values()) >= a.limit:
            break
    n_docs = sum(len(v) for v in want.values())
    print(f">>> {n_docs} documents to read in {len(want)} parts; skipped {dict(skipped)}.", flush=True)
    tc.sync_down(store, [f"pages-{p}.jsonl.gz" for p in want], folder)
    results = {}
    jobs = [(os.path.join(folder, f"pages-{p}.jsonl.gz"), ids) for p, ids in want.items()]
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, out in enumerate(ex.map(read_part, jobs), 1):
            results.update(out)
            if i % 20 == 0:
                print(f"  {i}/{len(jobs)} parts, {len(results)} documents", flush=True)
    for ids in want.values():                       # in the store but without pages: leave the queue
        for lid, t in ids.items():
            results.setdefault(lid, decide(t, [(1, "")]))
    if cover_ids:
        n_cover = 0
        for r in cover_rows(token, base, a.all):
            lid = r["fields"].get("library_id")
            if lid not in cover_ids or lid in results:
                continue
            results[lid] = decide(type_of(rows[lid]["fields"]), None, r["fields"].get("cover_text") or "")
            n_cover += 1
            if a.cover_limit and n_cover >= a.cover_limit:
                break
        print(f">>> {n_cover} documents read from their first pages.", flush=True)
    classes = collections.Counter(); fw = collections.Counter(); asr = collections.Counter(); ups = []
    for lid, res in results.items():
        classes[res["class"] or "blank"] += 1
        fw.update(res["frameworks"]); asr[res["assurance"] or "blank"] += 1
        f = rows[lid]["fields"]
        new = {"sustainability_class": res["class"], "class_reason": res["reason"],
               "sustainability_share": res["share"], "frameworks": res["frameworks"], "assurance": res["assurance"] or None}
        old = {"sustainability_class": f.get("sustainability_class"), "class_reason": f.get("class_reason"),
               "sustainability_share": f.get("sustainability_share"), "frameworks": sorted(f.get("frameworks") or []),
               "assurance": f.get("assurance")}
        cmp = dict(new, frameworks=sorted(new["frameworks"]))
        if any((old[k] or None) != (cmp[k] or None) for k in new):
            ups.append({"id": rows[lid]["id"], "fields": new})
    lines = [f"## Sustainability class ({'written' if a.commit else 'dry run, nothing written'})", "",
             f"Documents read: {len(results)}. Rows to update: {len(ups)}.", "",
             "Class: " + ", ".join(f"{k} {v}" for k, v in classes.most_common()), "",
             "Frameworks named: " + ", ".join(f"{k} {v}" for k, v in fw.most_common()), "",
             "Assurance: " + ", ".join(f"{k} {v}" for k, v in asr.most_common()), "",
             "| library_id | class | share | frameworks | assurance | reason |", "|---|---|---|---|---|---|"]
    for lid, r in list(results.items())[:40]:
        lines.append(f"| {lid} | {r['class'] or ''} | {r['share'] if r['share'] is not None else ''} | "
                     f"{', '.join(r['frameworks'])} | {r['assurance']} | {r['reason'][:140].replace('|', '/')} |")
    out = "\n".join(lines)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    if not a.commit:
        print("DRY RUN. Re-run with --commit to write.")
        return
    from monitor_core import airtable_request
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for i in range(0, len(ups), 10):
        try:
            airtable_request("PATCH", f"{API}/{base}/report_library", H, {"records": ups[i:i + 10], "typecast": True})
            done += len(ups[i:i + 10])
        except Exception as e:
            print(f">>> WARN: write failed ({e}); those rows stay queued.", flush=True)
        time.sleep(0.22)
        if done and done % 5000 == 0:
            print(f"  {done} written", flush=True)
    print(f">>> wrote {done} rows.")


if __name__ == "__main__":
    main()
