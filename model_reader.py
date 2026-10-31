#!/usr/bin/env python3
"""
model_reader.py: a language model reads the few pages that retrieval chose, and says which number on them
is the attribute's value. Used by extract_eval.py --reader model.

Retrieval stays where it was (documents of the year, pages ranked by the attribute's labels, the pages whose
rule candidates scored best); only the last step, reading a flattened table, goes to the model. What the
model gets comes from attributes.json (name, definition, labels, period, scope), so a change of methodology
is an edit of that file, as for the rule reader.

The model is asked to copy the number exactly as printed and to say the page's scale; the value is then
computed here (printed number × scale), never by the model, and the printed number must be on the page it
named, or the proposal is dropped (counted as quote_not_on_page). A model that invents a number therefore
cannot score.

Every year in one call (31 Oct 2026): a report's table prints the figure for two to five years side by
side, so the model is asked for the reporting year wanted AND every other reporting year the same figure is
printed for. Each entry is checked on its own page; read_years returns them all, read_with_model the one for
the year asked (with the others under "others"). One call therefore gives the history that separate calls
per year used to cost, and the value a later report repeats for an earlier year checks the earlier read.

Model: READER_MODEL (default claude-haiku-5-5), through the Messages API with ANTHROPIC_API_KEY. Token use
is added to the `usage` counter the caller passes (calls, input_tokens, output_tokens, quote_not_on_page,
model_errors). No document text is printed.
"""
import json, os, re, time

API = "https://api.anthropic.com/v1/messages"


class ModelRefused(RuntimeError):
    """The API refused the call for a reason a retry won't fix (key, credit, model name): stop the run."""
DEFAULT_MODEL = "claude-haiku-5-5"
PAGE_CHARS = 8000                       # per page; a long page is cut, tables sit near the top in practice
MAX_TOKENS = 2500                   # 300 cut replies off before their JSON (average reply 236 tokens, 29 Oct 2026); with one
                                    # entry per year the replies averaged 785 tokens and 1,500 cut 1 in 10 (31 Oct 2026)
SCALES = {"units": 1, "thousands": 1e3, "ten thousands": 1e4, "lakhs": 1e5, "millions": 1e6, "crores": 1e7, "hundred millions": 1e8,
          "billions": 1e9}

SYSTEM = ("You read pages of company reports and find one reported figure, for every year the pages print it. Answer "
          "with one JSON object and nothing else: no explanation before or after it. Copy each number exactly as printed "
          "on the page. Never compute, convert or round.")
FIRST_YEAR, LAST_YEAR = 2010, time.gmtime().tm_year


def prompt(g, attr, pages):
    """The user message: what to find, then the pages, each tagged P1, P2, ..."""
    labels = ", ".join(attr.get("labels", [])[:12])
    lines = [f"Find: {attr['name']}.",
             f"Definition: {attr.get('definition', '').strip()}" if attr.get("definition") else "",
             f"Labels it may appear under: {labels}." if labels else "",
             f"Reporting year: {g['year']}. A fiscal year ending between January and June of {g['year'] + 1} is also "
             f"reporting year {g['year']}: a column headed FY{g['year'] + 1}, FY{str(g['year'] + 1)[2:]} or {g['year']}/{str(g['year'] + 1)[2:]} "
             f"for such a year is the one to take, not the column headed {g['year'] - 1} or FY{g['year']} of a company "
             f"whose year ends in March. When several year columns are printed, take the one for this reporting year, "
             f"even if a later year is also printed.",
             f"Period: {attr['period']}." if attr.get("period") else "",
             f"Scope: {attr['scope']}." if attr.get("scope") else "",
             "Take the figure for that year, for the whole company or group, not a segment, a region or a target.",
             "The labels above are examples: if a page prints the figure under another name (\"Gross Scope 1\", "
             "\"Net sales\", \"Direct emissions\"), give it. A figure in a table whose year columns are headed further up "
             "the page counts. Reply with no figure only when none of the pages prints it.",
             "", "Pages:"]
    for i, (_, _, body) in enumerate(pages, 1):
        lines += [f"<page id=\"P{i}\">", (body or "")[:PAGE_CHARS], "</page>"]
    y = g["year"]
    lines += ["", "Reply with JSON: {\"values\": [{\"page\": \"P1\", \"printed\": \"12,345.6\", \"scale\": one of "
              + ", ".join(f'"{s}"' for s in SCALES) + " (the unit the page states for that number, e.g. "
              "\"in millions\", \"€m\", \"'000\", \"ktCO2e\"; 万 is ten thousands, 億 or 亿 hundred millions), \"unit\": \"EUR\" or \"tCO2e\" etc., "
              f"\"year\": {y}, \"column\": the heading of the column or line the figure sits under, exactly as printed, "
              "e.g. \"FY25\", \"2024\", \"2024/25\", \"Year ended 31 March 2025\"}, ...]}. "
              f"The first entry is the figure for reporting year {y}. Then one entry for each other reporting year the "
              "same row prints (its other year columns), newest first, at most six entries in all and one per year, each "
              "with its page, printed number and column heading; \"year\" is the reporting year of that column under the "
              "rule above, not the heading. Only years the pages print: never estimate. No text before or after the JSON. "
              "If none of the pages prints the figure for any year, reply {\"values\": [], \"why\": \"...\"} "
              "with the reason in ten words or fewer."]
    return "\n".join(l for l in lines if l is not None)


def parse_reply(text):
    """The first JSON object in the model's text, or None. A reply cut before its closing brackets (the token
    limit) still yields the complete entries of its "values" list, the year asked first among them."""
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        pass
    entries = []
    for part in re.findall(r"\{[^{}]*\}", m.group(0)):
        try:
            e = json.loads(part)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("page"):
            entries.append(e)
    return {"values": entries, "cut": True} if entries else None


def page_convention(body):
    """"," when the page groups thousands with commas (1,701 / 293,316), "." when with points (1.701), else
    None. Decides what a printed "14.408" means: 14.408 on a page of 1,701s, 14408 on a page of 1.701s."""
    comma = len(re.findall(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])", body or ""))
    point = len(re.findall(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?:,\d+)?(?![\d.])", body or ""))
    if comma > point:
        return ","
    if point > comma:
        return "."
    return None


def printed_number(text, body, to_number):
    """The printed number read with the page's own separators. "14.408" on a page that writes 1,701 is
    14.408, not 14,408: to_number alone cannot tell, and read CNOOC's 14.408 million tonnes as 14,408
    million (30 Oct 2026)."""
    t = str(text).strip()
    if re.fullmatch(r"\d{1,3}\.\d{3}", t) and page_convention(body) == ",":
        return float(t)
    if re.fullmatch(r"\d{1,3},\d{3}", t) and page_convention(body) == ".":
        return float(t.replace(",", "."))
    return to_number(t)


def column_year(column):
    """The calendar year a column heading names, or None: "2024" -> 2024, "FY25" -> 2025, "2024/25" -> 2024,
    "Year ended 31 March 2025" -> 2025 (the first four-digit year, else a two-digit FY)."""
    m = re.search(r"(?<!\d)(20\d\d)(?!\d)", column or "")
    if m:
        return int(m.group(1))
    m = re.search(r"FY\s?'?(\d{2})(?!\d)", column or "", flags=re.I)
    return 2000 + int(m.group(1)) if m else None


def check(reply, pages, to_number, numbers_on):
    """{'value', 'printed', 'scale', 'library_id', 'page', 'unit', 'year', 'column'} when the entry names a page
    that prints the number, else None (nothing usable) or False (a number the page does not print).
    to_number and numbers_on are the rule reader's own parsers. The year must be a reporting year the column
    heading allows: the heading's year or the one before it (a fiscal year ending January to June)."""
    if not reply or not reply.get("page") or not reply.get("printed"):
        return None
    m = re.fullmatch(r"P(\d+)", str(reply["page"]).strip())
    if not m or not 1 <= int(m.group(1)) <= len(pages):
        return None
    lid, page, body = pages[int(m.group(1)) - 1]
    try:
        printed = printed_number(reply["printed"], body, to_number)
    except (TypeError, ValueError):
        return None
    if not any(abs(v - printed) < 1e-9 for v in numbers_on(body or "")):
        return False                    # named a number the page does not print
    scale = SCALES.get(str(reply.get("scale") or "units").strip().lower(), 1)
    try:
        year = int(reply.get("year")) if reply.get("year") not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    column = str(reply.get("column") or "").strip()[:60]
    cy = column_year(column)
    if year is not None and (not FIRST_YEAR <= year <= LAST_YEAR + 1 or (cy and year not in (cy, cy - 1))):
        return None                     # a year the heading does not allow: the model mixed its columns
    return {"value": printed * scale, "printed": printed, "scale": scale, "library_id": lid, "page": page,
            "unit": reply.get("unit") or "", "column": column, "year": year}


def check_all(reply, pages, to_number, numbers_on, usage):
    """Every usable entry of a reply, one per year (the first wins), in the reply's order. Accepts the old
    single-object shape too."""
    entries = reply.get("values") if isinstance(reply, dict) and isinstance(reply.get("values"), list) else \
        ([reply] if isinstance(reply, dict) and reply.get("page") else [])
    out, seen = [], set()
    for e in entries:
        got = check(e if isinstance(e, dict) else None, pages, to_number, numbers_on)
        if got is False:
            usage["quote_not_on_page"] += 1
            continue
        if not got or got["year"] in seen:
            continue
        seen.add(got["year"]); out.append(got)
    return out


def call(messages, usage, sess=None, model=None):
    """The model's text reply (Messages API), retrying four times on overload or rate limits (parallel shards
    share the account's limits, 31 Oct 2026)."""
    import requests
    sess = sess or requests
    body = {"model": model or os.environ.get("READER_MODEL", DEFAULT_MODEL), "max_tokens": MAX_TOKENS,
            "system": SYSTEM, "messages": messages}
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(), "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for attempt in range(5):
        r = sess.post(API, headers=headers, json=body, timeout=120)
        if r.status_code in (429, 500, 529) and attempt < 4:   # rate limit or overload: wait as told, else 10, 20, 40, 80 s
            try:
                wait = float((getattr(r, "headers", None) or {}).get("retry-after") or 0)
            except (TypeError, ValueError):
                wait = 0
            time.sleep(min(max(wait, 10 * 2 ** attempt), 120)); continue
        if r.status_code != 200:
            usage["model_errors"] += 1
            if r.status_code in (400, 401, 403, 404):  # a bad key, no credit, a wrong model name: no point going on
                raise ModelRefused(f"Messages API refused the call: HTTP {r.status_code} {r.text[:300]}")
            return ""
        j = r.json()
        usage["calls"] += 1
        usage["input_tokens"] += j.get("usage", {}).get("input_tokens", 0)
        usage["output_tokens"] += j.get("usage", {}).get("output_tokens", 0)
        if j.get("stop_reason") == "max_tokens":
            usage["cut_at_token_limit"] += 1
        return "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
    return ""


def read_years(g, attr, pages, usage, sess=None, note=None):
    """One call: the model's checked figures for every reporting year the pages print, the year asked
    (g["year"]) first when it is there. [] when none. note (a dict), when given, receives the model's reason
    when it gives no figure ("why"). usage["years_returned"] counts the entries."""
    if not pages:
        return []
    from extract_eval import to_number
    from value_eval import numbers_on
    text = call([{"role": "user", "content": prompt(g, attr, pages)}], usage, sess)
    reply = parse_reply(text)
    entries = reply.get("values") if isinstance(reply, dict) and isinstance(reply.get("values"), list) else None
    if note is not None:
        if reply is None:
            note["why"] = "(no JSON in the reply)" if text else "(empty reply)"
        elif entries == [] or (entries is None and not reply.get("page")):
            note["why"] = str(reply.get("why") or "")[:120]
    got = check_all(reply, pages, to_number, numbers_on, usage)
    for e in got:
        if e["year"] is None:           # the old single-object shape, or a missing year: the year asked
            e["year"] = g["year"]
    got.sort(key=lambda e: e["year"] != g["year"])
    usage["years_returned"] += len(got)
    return got


def read_with_model(g, attr, pages, usage, sess=None, note=None):
    """The model's proposal for the year asked, checked against the pages, or None; the figures for the other
    years the pages print come with it under "others"."""
    got = read_years(g, attr, pages, usage, sess, note)
    mine = next((e for e in got if e["year"] == g["year"]), None)
    if mine is None:
        if got and note is not None and not note.get("why"):
            note["why"] = "(only other years: " + ", ".join(str(e["year"]) for e in got) + ")"
        return None
    mine["others"] = [e for e in got if e is not mine]
    return mine


def cost(usage, model=None):
    """Dollars at the list price per million tokens (Haiku 5.5, prompts up to 100,000 tokens, 29 Oct 2026);
    READER_PRICE_IN / READER_PRICE_OUT override it for another model."""
    pin = float(os.environ.get("READER_PRICE_IN", "0.10"))
    pout = float(os.environ.get("READER_PRICE_OUT", "0.50"))
    return usage["input_tokens"] / 1e6 * pin + usage["output_tokens"] / 1e6 * pout
