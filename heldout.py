#!/usr/bin/env python3
"""
heldout.py: attributes kept out of development, to measure how the readers do on attributes nobody tuned them for.

Every reader experiment until 8 Nov 2026 used 8 number attributes and 5 narrative ones, and several choices
(reading notes, exclusions, the Scope 2 basis rule) were tuned on them. The owner asked whether the system is
being built around a handful of attributes. The 2026 benchmarks bring hundreds of attributes without validated
answers, so the honest estimate is how the readers do on an attribute they have never seen, with only its
definition from the gold bases' Metadata Codes table.

Which attributes are held out is fixed by a hash of the code, not by a list: an attribute is held out when
sha1(SALT + code) falls in the first quarter. The rule needs no file, cannot be edited by looking at results,
and assigns any attribute added later. Attributes already tuned (in attributes.json or gold_attributes.json)
are development attributes whatever their hash.

Rules for development (docs/decisions.md, 8 Nov 2026):
- never write reading notes, labels or exclusions for a held-out attribute, and never look at its wrong answers
  to tune anything; only its shares and the answers fixed and broken by an option are read;
- an option is adopted only if no attribute of the development set and no kind of the held-out set gets worse
  by more than 3 points.

The spec of a held-out number attribute is built at run time from Metadata Codes (auto_number_spec): its
description is its name and its first label, its content words (core_terms) its other labels and search terms,
its definition the model's definition, the unit family from the units of its validated answers; no reading
notes, no exclusions, no siblings. The same builder runs on development attributes with extract_eval
--auto-spec, which is where the builder is checked and improved (never on held-out answers).
"""
import collections, hashlib, json, os, re

SALT = "wba-heldout-2026-11|"
SHARE = 4                                   # one attribute in SHARE is held out
HERE = os.path.dirname(os.path.abspath(__file__))
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CNY", "RMB", "CHF", "CAD", "AUD", "INR", "BRL", "KRW", "TWD", "HKD", "SGD",
              "SEK", "NOK", "DKK", "ZAR", "MXN", "ARS", "CLP", "COP", "PEN", "IDR", "MYR", "THB", "PHP", "VND", "TRY",
              "RUB", "PLN", "CZK", "HUF", "ILS", "SAR", "AED", "QAR", "KWD", "EGP", "NGN", "KES", "MAD", "NZD", "PKR"}


def development():
    """The codes already tuned on: every attribute of attributes.json and gold_attributes.json."""
    out = set()
    for name in ("attributes.json", "gold_attributes.json"):
        try:
            out |= set(json.load(open(os.path.join(HERE, name), encoding="utf-8"))["attributes"])
        except (OSError, KeyError, ValueError):
            pass
    return out


def is_heldout(code, dev=None):
    code = (code or "").strip()
    if not code or code in (development() if dev is None else dev):
        return False
    return int(hashlib.sha1((SALT + code).encode()).hexdigest(), 16) % SHARE == 0


def unit_family(units):
    """The unit family of a number attribute from the units of its validated answers (most common first):
    currency, tco2e, percent (left out by the number reader), or count for anything else."""
    for u, _ in collections.Counter(u.strip() for u in units if (u or "").strip()).most_common():
        if u.startswith("%") or u.lower().startswith("percent"):
            return "percent"
        if "co2" in u.lower():
            return "tco2e"
        if u.upper() in CURRENCIES:
            return "currency"
        return "count"
    return "count"


def auto_labels(description):
    """Search labels from a description alone: the description, then shorter forms without a question's
    opening, a leading 'total' and anything in brackets. No hand-written words."""
    d = re.sub(r"<[^>]+>", " ", description or "").strip().rstrip("?.").strip()
    d = re.sub(r"\s+", " ", d)
    out = [d.lower()]
    s = re.sub(r"(?i)^(what|how much|how many)\s+(is|are|was|were)\s+(the\s+)?(company'?s\s+)?", "", d)
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    out.append(s.lower())
    out.append(re.sub(r"(?i)^total\s+", "", s).strip().lower())
    return [x for x in dict.fromkeys(out) if len(x) >= 3]


GENERIC = {"number", "total", "amount", "value", "quantity", "annual", "annually", "per", "year", "years", "the",
           "of", "in", "on", "for", "and", "a", "an", "to", "company", "company's", "companys", "its", "reported", "level",
           "does", "do", "is", "are", "has", "have", "what", "which", "how", "whether", "there", "any", "this", "that",
           "disclose", "discloses", "disclosed", "report", "reports", "provide", "provides", "with", "by", "or", "it"}


def core_terms(description):
    """The content words of a description, without words any number attribute could have ('number of',
    'total', 'amount', 'per year'): 'Number of passengers per year' -> 'passengers'. Reports print the
    content words, rarely the description, so these find the pages; generic, never written per attribute."""
    out = []
    for l in auto_labels(description):
        ws = [w for w in re.findall(r"[a-z0-9][a-z0-9+\-']*", l) if w not in GENERIC]
        if ws:
            out.append(" ".join(ws))
    return list(dict.fromkeys(out))


def auto_queries(description):
    """Search queries for a judgement attribute from its description alone: its content words (core_terms),
    then every pair of neighbouring content words. A page is found when it has both words of a pair, in any
    order, so a long question still finds the pages that discuss it (all its words together rarely appear).
    At most 12 queries."""
    core = core_terms(description)
    words = core[-1].split() if core else []
    pairs = [f"{a} {b}" for a, b in zip(words, words[1:])]
    return list(dict.fromkeys(core[-1:] + pairs + (words if len(words) == 1 else [])))[:12]


def auto_number_spec(meta, units):
    """An attributes.json entry for a held-out number attribute, from its Metadata Codes row and the units of its
    validated answers. None when it is a percentage or has no description."""
    desc = re.sub(r"\s+", " ", (meta.get("Description") or "")).strip()
    family = unit_family(units)
    if not desc or family == "percent":
        return None
    definition = re.sub(r"<[^>]+>", " ", meta.get("Definition") or "")
    definition = re.sub(r"\s+", " ", definition).strip()[:800]
    labels = list(dict.fromkeys(auto_labels(desc) + core_terms(desc)))
    return {"name": desc[:120], "definition": definition or desc, "type": "number", "unit_family": family,
            "labels": labels, "more_terms": core_terms(desc), "exclude": [], "index": {"codes": [], "terms": labels[1:3]},
            "doc_types": ["Annual Report", "Integrated Report", "Sustainability Report", "ESG Report"],
            "period": "fiscal_year", "scope": "group", "memory": True, "status": "heldout"}


def metadata_codes(token, bases):
    """{code: Metadata Codes fields} from the first gold base that answers (the list is the same in each)."""
    from narrative_eval import read_base
    for b in bases:
        meta = {}
        for r in read_base(token, b, "Metadata Codes", ["Global Metadata Code (GMC)", "Description", "Definition", "Data Type", "Select Options", "Units"]):
            f = r["fields"]
            meta[(f.get("Global Metadata Code (GMC)") or "").strip()] = f
        if meta:
            return meta
    return {}


def type_of(f):
    dt = f.get("Data Type")
    return (dt.get("name") if isinstance(dt, dict) else dt) or ""


def number_codes(meta, n, dev=None):
    """The held-out codes of number attributes, in hash order (a fixed, arbitrary order), all of them: the caller
    keeps the first n that have validated answers that are not percentages. (Until 9 Nov 2026 only n*3 were
    returned, and of the first 60 only 8 had such answers.)"""
    dev = development() if dev is None else dev
    codes = [c for c, f in meta.items() if type_of(f) == "Number" and is_heldout(c, dev)]
    codes.sort(key=lambda c: hashlib.sha1((SALT + c).encode()).hexdigest())
    return codes


if __name__ == "__main__":                 # list the held-out codes of a code list given on stdin, for a quick check
    import sys
    for line in sys.stdin:
        c = line.strip()
        if c:
            print(c, "heldout" if is_heldout(c) else "development")
