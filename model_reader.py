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

Model: READER_MODEL (default claude-haiku-5-5), through the Messages API with ANTHROPIC_API_KEY, or through the
Gemini API with GEMINI_API_KEY when the name starts with "gemini" (9 Nov 2026, to measure other readers). Token use
is added to the `usage` counter the caller passes (calls, input_tokens, output_tokens, quote_not_on_page,
model_errors). No document text is printed.
"""
import json, os, re, time

API = "https://api.anthropic.com/v1/messages"


class ModelRefused(RuntimeError):
    """The API refused the call for a reason a retry won't fix (key, credit, model name): stop the run."""
DEFAULT_MODEL = "claude-haiku-5-5"
PAGE_CHARS = 8000                       # per page; a long page is cut, tables sit near the top in practice
LONG_RUN = 16                           # more pages than this in one call (--select whole) and the adjacency notes are per run, not per pair
MAX_TOKENS = 2500                   # 300 cut replies off before their JSON (average reply 236 tokens, 29 Oct 2026); with one
                                    # entry per year the replies averaged 785 tokens and 1,500 cut 1 in 10 (31 Oct 2026)
SCALES = {"units": 1, "thousands": 1e3, "ten thousands": 1e4, "lakhs": 1e5, "millions": 1e6, "crores": 1e7, "hundred millions": 1e8,
          "billions": 1e9}

# The unit of a value after its scale is applied (12 Nov 2026). The value kept is the printed number times the
# page's scale, but the unit kept was the unit as printed: Hitachi's "0.39 Mt-CO2e" became 390,000 with the unit
# "Mt-CO2e", which reads as 390,000 megatonnes. When a scale of a thousand, a million or a billion was applied and
# the unit itself names that scale (a word: "million tonnes", "RMB'000", "USD bn"; or a prefix on tonnes: kt, Mt,
# Gt), the scale is taken out of the unit: "t-CO2e", "tonnes", "RMB". A unit that does not name the scale is left
# as it is (MWh stays MWh). The same rule is in supabase/search.sql (unit_shown) for the rows already stored.
_UNIT_WORDS = {1e3: r"thousands?(\s+of)?|'?000s?|\bk\b", 1e6: r"millions?(\s+of)?|\bmn\b|\bmio\b|\bmm\b", 1e9: r"billions?(\s+of)?|\bbn\b"}
_UNIT_PREFIX = {1e3: "k", 1e6: "m", 1e9: "g"}


def unit_after_scale(unit, scale):
    u = str(unit or "").strip()
    if not u or scale not in _UNIT_WORDS:
        return u
    out = re.sub(_UNIT_WORDS[scale], " ", u, flags=re.I)
    if out == u:
        out = re.sub(r"^\s*" + _UNIT_PREFIX[scale] + r"(?=t)", "", u, flags=re.I)
    if out == u and scale == 1e6:
        out = re.sub(r"(?<=[^A-Za-z])m\.?$", "", u)                 # "€m", "US$ m": never the m of "RMB" or "sqm"
    out = re.sub(r"\s+", " ", out).strip(" -,;:()[]")
    return out or u


SYSTEM = ("You read pages of company reports and find one reported figure, for every year the pages print it. Answer "
          "with one JSON object and nothing else: no explanation before or after it. Copy each number exactly as printed "
          "on the page. Never compute, convert or round.")
FIRST_YEAR, LAST_YEAR = 2010, time.gmtime().tm_year


def adjacent_notes(pages):
    """Lines telling the model which pages follow each other in the same document, so a table that runs
    from one page to the next is read as one (1 Nov 2026). pages: [(library_id, page, body)]."""
    out = []
    if len(pages) > LONG_RUN:          # --select whole (10 Nov 2026): one line per run of consecutive pages, not one per pair
        runs = []
        for i, (lid, page, _) in enumerate(pages, 1):
            if runs and runs[-1][0] == lid and runs[-1][1] == page - 1:
                runs[-1][1], runs[-1][3] = page, i
            else:
                runs.append([lid, page, i, i])
        return [f"P{i0} to P{i1} are consecutive pages of one document, in order: a table may run from one to the next."
                for _, _, i0, i1 in runs if i1 > i0]
    where = {(lid, page): i for i, (lid, page, _) in enumerate(pages, 1)}
    for (lid, page), i in sorted(where.items(), key=lambda x: x[1]):
        j = where.get((lid, page + 1))
        if j:
            out.append(f"P{j} is the page right after P{i} in the same document: a table may run from one to the other.")
    return out


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def fiscal_note(g):
    """The sentence that says when this company's fiscal year ends and which period is the reporting year
    asked, when the year end is known (g["fy_month"], from the company's documents). For a year ending
    January to May the reporting year is the period ending in the following calendar year (2 Nov 2026: ORIX,
    year end March, had the column "2024", the year ended March 31, 2024, read as reporting year 2024)."""
    m = g.get("fy_month")
    y = g["year"]
    if not m or not 1 <= int(m) <= 12:
        return ""
    m = int(m)
    from reporting_year import shifted
    end_year = y + 1 if shifted(m) else y
    if m == 12:
        return (f"This company's fiscal year is the calendar year: reporting year {y} is the year ended December 31, {y}. "
                f"A column headed {y} is that year.")
    start = f"{MONTHS[m % 12]} {end_year - 1}"
    return (f"This company's fiscal year ends in {MONTHS[m - 1]}: reporting year {y} is the year from {start} to "
            f"{MONTHS[m - 1]} {end_year}, printed as \"year ended {MONTHS[m - 1]} {end_year}\", FY{end_year}, "
            f"FY{str(end_year - 1)[2:]}/{str(end_year)[2:]}" + (f" or (in Japan) FY{end_year - 1}" if shifted(m) else "")
            + f". Take that column" + (f", not the one for the year ended {MONTHS[m - 1]} {end_year - 1}" if shifted(m) else "")
            + ". For every figure give \"period_end\": the month the column's period ends, as YYYY-MM.")


RULES_IN_PROMPT = True   # False with extract_eval --guidance check: the rules only drop figures, the prompt is today's
RULES = None    # decision_rules.json (decision_mining.py --save) when extract_eval --guidance learned; None = today


MD_GUIDANCE = {}   # gmc -> WBA's own assessment guidance (Pipeline QA attribute_guidance), with extract_eval --guidance md


def md_note(code):
    """WBA's assessment guidance and prompt for the attribute, as one block of the prompt, or ''. The text
    is WBA methodology: it is read at run time from the private Pipeline QA base and never logged."""
    m = MD_GUIDANCE.get(code or "")
    if not m:
        return ""
    return ("WBA's assessment guidance for this attribute (follow it to choose the figure):\n"
            + "\n\n".join(x for x in (m.get("guidance"), m.get("md_prompt")) if x)
            + "\n(Whatever the guidance says about output, units or conversion: reply only in the format asked below, "
              "with the figure as printed and its scale.)")


def rules_note(code):
    """The learned choice rules of an attribute as one instruction, or ''."""
    r = ((RULES or {}).get("attributes") or {}).get(code or "")
    if not r or not RULES_IN_PROMPT:
        return ""
    parts = []
    if r.get("follow"):
        parts.append("the line it is printed on usually says " + ", ".join(f'"{w}"' for w, *_ in r["follow"][:8]))
    if r.get("avoid"):
        parts.append("never take a figure from a line that says " + ", ".join(f'"{w}"' for w, *_ in r["avoid"][:8]))
    if r.get("take_largest"):
        parts.append(f"when several lines of the page qualify, take the largest figure (the analysts did in {r['largest']} of {r['competing']} such pages)")
    return ("Learned from the analysts' validated answers: " + "; ".join(parts) + ".") if parts else ""


def line_of(body, printed):
    """The lines of a page that print a number, by its printed digits."""
    from label_mining import number_spans
    d = 0 if float(printed).is_integer() else min(2, len(repr(float(printed)).split(".")[1]))
    return [ln for ln in (body or "").split("\n") if number_spans(ln, float(printed), d)]


def rule_ok(code, entry, pages):
    """False when the figure's line says an avoided word and none of the followed ones (learned rules)."""
    r = ((RULES or {}).get("attributes") or {}).get(code or "")
    if not r or not r.get("avoid"):
        return True
    body = next((b for lid, p, b in pages if lid == entry.get("library_id") and str(p) == str(entry.get("page"))), None)
    lines = line_of(body, entry.get("printed")) if body is not None and entry.get("printed") is not None else []
    if not lines:
        return True
    avoid = {w for w, *_ in r["avoid"]}; follow = {w for w, *_ in r.get("follow") or []}
    from label_mining import words
    for ln in lines:
        ws = set(words(ln))
        if not (ws & avoid) or (ws & follow):
            return True                  # one clean printing is enough
    return False


def prompt(g, attr, pages):
    """The user message: what to find, then the pages, each tagged P1, P2, ..."""
    labels = ", ".join(attr.get("labels", [])[:12])
    lines = [f"Find: {attr['name']}.",
             f"Definition: {attr.get('definition', '').strip()}" if attr.get("definition") else "",
             f"Notes: {attr['reading_notes'].strip()}" if attr.get("reading_notes") else "",
             f"Labels it may appear under: {labels}." if labels else "",
             f"Reporting year: {g['year']}. A fiscal year ending between January and May of {g['year'] + 1} is also "
             f"reporting year {g['year']}: a column headed FY{g['year'] + 1}, FY{str(g['year'] + 1)[2:]} or {g['year']}/{str(g['year'] + 1)[2:]} "
             f"for such a year is the one to take, not the column headed {g['year'] - 1} or FY{g['year']} of a company "
             f"whose year ends in March. When several year columns are printed, take the one for this reporting year, "
             f"even if a later year is also printed.",
             fiscal_note(g),
             f"Period: {attr['period']}." if attr.get("period") else "",
             f"Scope: {attr['scope']}." if attr.get("scope") else "",
             "Take the figure for that year, for the whole company or group, not a segment, a region or a target.",
             g.get("hint") or "",
             rules_note(g.get("attribute")),
             md_note(g.get("attribute")),
             (f"The figure wanted is the {attr['variant']['wanted']} one. Say in \"basis\" what the page calls the figure: "
              f"\"{attr['variant']['wanted']}\", \"{attr['variant']['other']}\", or \"not stated\" when the page gives one Scope 2 "
              f"figure without saying which. Never give a figure the page calls {attr['variant']['other']}.") if attr.get("variant") else "",
             "The labels above are examples: if a page prints the figure under another name (\"Gross Scope 1\", "
             "\"Net sales\", \"Direct emissions\"), give it. A figure in a table whose year columns are headed further up "
             "the page counts. Reply with no figure only when none of the pages prints it.",
             "", "Pages:"]
    for i, (_, _, body) in enumerate(pages, 1):
        lines += [f"<page id=\"P{i}\">", (body or "")[:PAGE_CHARS], "</page>"]
    lines += adjacent_notes(pages)
    y = g["year"]
    lines += ["", "Reply with JSON: {\"values\": [{\"page\": \"P1\", \"printed\": \"12,345.6\", \"scale\": one of "
              + ", ".join(f'"{s}"' for s in SCALES) + " (the unit the page states for that number, e.g. "
              "\"in millions\", \"€m\", \"'000\", \"ktCO2e\"; 万 is ten thousands, 億 or 亿 hundred millions), \"unit\": \"EUR\" or \"tCO2e\" etc., "
              f"\"year\": {y}, \"column\": the heading of the column or line the figure sits under, exactly as printed, "
              "e.g. \"FY25\", \"2024\", \"2024/25\", \"Year ended 31 March 2025\", \"period_end\": the month the column's "
              "period ends, as YYYY-MM, when the page says it (\"for the years ended March 31, 2024\" above a column headed 2024 "
              "is 2024-03)}, ...]}. "
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


def period_year(period_end):
    """(reporting year, month) from a period end "YYYY-MM": the year the period ends, or the year before when
    it ends January to May (reporting_year.py); (None, None) when there is no month."""
    m = re.fullmatch(r"\s*((?:19|20)\d\d)-(\d\d?)\s*", str(period_end or ""))
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None, None
    y, mo = int(m.group(1)), int(m.group(2))
    from reporting_year import from_end_month
    return from_end_month(y, mo), mo


PERIOD_CHECK = "all"   # "currency-only" (extract_eval --period-check currency-only): the check that drops an entry whose
                       # period ends in another month than the company's financial year applies to money only, since
                       # emissions and headcounts are often reported by calendar year or at a date (7 Nov 2026: 5 of
                       # 22 declined answers whose pages printed the figure, all emissions or employees)


def check(reply, pages, to_number, numbers_on, fy_month=None, why=None, period_check=True):
    """{'value', 'printed', 'scale', 'library_id', 'page', 'unit', 'year', 'column'} when the entry names a page
    that prints the number, else None (nothing usable) or False (a number the page does not print).
    to_number and numbers_on are the rule reader's own parsers. The year must be a reporting year the column
    heading allows: the heading's year or the one before it (a fiscal year ending January to May). When the
    entry gives the period's end month, the reporting year is taken from it (2 Nov 2026: the model called the
    year ended March 31, 2024 reporting year 2024; it is 2023), and when the company's year end is known
    (fy_month) a period ending in another month is a misread column and the entry is dropped."""
    def drop(reason):                   # why an entry was not kept (7 Nov 2026: the model "gave no figure" for 71 of 308
        if why is not None:             # answers whose pages print the analyst's figure; most said nothing)
            why.append(reason)
        return None
    if not reply or not reply.get("page") or not reply.get("printed"):
        return drop("no page or no number")
    m = re.fullmatch(r"P(\d+)", str(reply["page"]).strip())
    if not m or not 1 <= int(m.group(1)) <= len(pages):
        return drop("no page or no number")
    lid, page, body = pages[int(m.group(1)) - 1]
    try:
        printed = printed_number(reply["printed"], body, to_number)
    except (TypeError, ValueError):
        return drop("number not readable")
    if not any(abs(v - printed) < 1e-9 for v in numbers_on(body or "")):
        return False                    # named a number the page does not print
    scale = SCALES.get(str(reply.get("scale") or "units").strip().lower(), 1)
    try:
        year = int(reply.get("year")) if reply.get("year") not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    column = str(reply.get("column") or "").strip()[:60]
    cy = column_year(column)
    py, pm = period_year(reply.get("period_end"))
    if period_check and pm and fy_month and abs(pm - int(fy_month)) > 1 and abs(pm - int(fy_month)) < 11:
        return drop("period ends in another month than the company's year")   # a misread column
    if py is not None and (not cy or py in (cy, cy - 1)):
        year = py                       # the period end says which reporting year the column is
    if year is not None and (not FIRST_YEAR <= year <= LAST_YEAR + 1 or (cy and year not in (cy, cy - 1))):
        return drop("year not allowed by the column heading")   # the model mixed its columns
    if year is not None and cy and fy_month and int(fy_month) > 6 and year != cy:
        return drop("year differs from the column (July-December year end)")   # a column of 2025 is 2025, never 2024 (2 Nov 2026:
                                        # CVS Health's 2025 verification opinion, "January 1 to December 31, 2025", read as 2024)
    return {"value": printed * scale, "printed": printed, "scale": scale, "library_id": lid, "page": page,
            "unit": unit_after_scale(reply.get("unit"), scale), "unit_printed": reply.get("unit") or "", "column": column, "year": year,
            "basis": str(reply.get("basis") or "").strip().lower()[:20]}


def check_all(reply, pages, to_number, numbers_on, usage, fy_month=None, why=None, period_check=True):
    """Every usable entry of a reply, one per year (the first wins), in the reply's order. Accepts the old
    single-object shape too."""
    entries = reply.get("values") if isinstance(reply, dict) and isinstance(reply.get("values"), list) else \
        ([reply] if isinstance(reply, dict) and reply.get("page") else [])
    out, seen = [], set()
    for e in entries:
        got = check(e if isinstance(e, dict) else None, pages, to_number, numbers_on, fy_month, why, period_check)
        if got is False:
            usage["quote_not_on_page"] += 1
            if why is not None:
                why.append("number not on the page named")
            continue
        if not got or got["year"] in seen:
            continue
        seen.add(got["year"]); out.append(got)
    return out


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def is_gemini(model):
    return (model or "").lower().startswith("gemini")


def gemini_body(messages, system, max_tokens, thinking=True):
    """The Gemini generateContent request for the same messages: roles user and model, the system text as
    system_instruction, temperature 0, and the lowest thinking level (a figure read from a table needs none;
    thinking tokens are billed as output)."""
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]} for m in messages],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0}}
    if thinking:
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
    return body


def call_gemini(messages, usage, sess, model, system, max_tokens):
    """The model's text reply from the Gemini API (GEMINI_API_KEY), with the same retries and usage counting as
    the Messages API path; a 400 that names the thinking setting is retried without it (older models)."""
    url = GEMINI_API.format(model=model)
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"].strip(), "content-type": "application/json"}
    thinking = True
    for attempt in range(6):
        body = gemini_body(messages, system, max_tokens, thinking)
        long_call = sum(len(m.get("content") or "") for m in messages) > 400_000      # --select whole: a document in one call
        r = sess.post(url, headers=headers, json=body, timeout=600 if long_call else 180)
        if r.status_code in (429, 500, 503) and attempt < 5:
            try:
                wait = float((getattr(r, "headers", None) or {}).get("retry-after") or 0)
            except (TypeError, ValueError):
                wait = 0
            time.sleep(min(max(wait, 10 * 2 ** attempt), 120)); continue
        if r.status_code == 400 and thinking and "think" in (r.text or "").lower():
            thinking = False; continue
        if r.status_code != 200:
            usage["model_errors"] += 1
            if r.status_code in (400, 401, 402, 403, 404):                                # 402: the key's project has no working billing (10 Nov 2026)
                raise ModelRefused(f"Gemini API refused the call: HTTP {r.status_code} {' '.join((r.text or '').split())[:300]}")
            if usage["model_errors"] <= 3 or usage["model_errors"] % 50 == 0:          # the first run failed 237 times in silence (9 Nov 2026)
                print(f">>> Gemini API error {r.status_code} (failure {usage['model_errors']}): {' '.join((r.text or '').split())[:200]}", flush=True)
            return ""
        j = r.json()
        if not j.get("candidates"):                                                      # a blocked or empty reply: no parts to read
            usage["model_errors"] += 1
            if usage["model_errors"] <= 3:
                print(f">>> Gemini API reply without candidates: {str(j)[:200]}", flush=True)
            return ""
        usage["calls"] += 1
        meta = j.get("usageMetadata", {})
        usage["input_tokens"] += meta.get("promptTokenCount", 0)
        usage["output_tokens"] += meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0)
        cands = j.get("candidates") or [{}]
        if cands[0].get("finishReason") == "MAX_TOKENS":
            usage["cut_at_token_limit"] += 1
        return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))
    return ""


def call(messages, usage, sess=None, model=None, system=None, max_tokens=None):
    """The model's text reply (Messages API, or the Gemini API when the model name starts with "gemini", 9 Nov
    2026), retrying four times on overload or rate limits (parallel shards share the account's limits, 31 Oct
    2026). system and max_tokens default to the reader's."""
    import requests
    sess = sess or requests
    model = model or (os.environ.get("READER_MODEL") or DEFAULT_MODEL)
    if is_gemini(model):
        return call_gemini(messages, usage, sess, model, system or SYSTEM, max_tokens or MAX_TOKENS)
    body = {"model": model, "max_tokens": max_tokens or MAX_TOKENS,
            "system": system or SYSTEM, "messages": messages}
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
        text = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        if not text:                                             # 10 Nov 2026: a third of short-answer calls came back with tokens spent and no text
            usage["replies_without_text"] += 1
            usage["reply blocks: " + ",".join(sorted({c.get("type", "?") for c in j.get("content", [])}) or ["none"]) + f"; stop {j.get('stop_reason')}"] += 1
        return text
    return ""


UNSTATED_BASIS = "keep"   # a Scope 2 figure whose basis the page does not state is kept for the market-based
                          # attribute too, with its basis_note (default since 7 Nov 2026, owner's decision: on 181
                          # market answers 57% to 69% right, +11.6 points, interval +5.5 to +18.2, McNemar p 0.001;
                          # right among proposals 85% and 83%). "default-only" (--unstated-basis default-only) is
                          # the earlier rule: such a figure goes to location-based only (30 Oct 2026).


def variant_filter(attr, entries, usage=None, why=None):
    """The entries an attribute with a "variant" (Scope 2 location- or market-based) may keep: the ones the
    page calls by the wanted basis; the ones with no basis stated, marked basis_note (for the non-default
    variant, market-based, only while UNSTATED_BASIS is "keep", the default since 7 Nov 2026); never the other
    basis. A report that gives one Scope 2 figure without naming a basis therefore yields a row under each
    basis, each saying the page names none (30 Oct 2026 it yielded one, after a reviewer rejected the
    duplicate; the analysts' own market-based answers showed otherwise, 7 Nov 2026)."""
    v = attr.get("variant")
    if not v:
        return entries
    out = []
    for e in entries:
        b = e.get("basis") or ""
        if v["other"] in b:
            if usage is not None:
                usage["dropped_other_basis"] += 1
            if why is not None:
                why.append(f"basis {b or '?'}, not {v['wanted']}")
            continue
        if v["wanted"] not in b:                          # not stated, or something else
            if not v.get("default") and UNSTATED_BASIS != "keep":
                if usage is not None:
                    usage["dropped_basis_not_stated"] += 1
                if why is not None:
                    why.append("basis not stated")
                continue
            e["basis_note"] = (f"The report gives one Scope 2 figure without saying location- or market-based; "
                               f"it is filed under {v['wanted']}.")
        out.append(e)
    return out


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
    dropped = note.setdefault("dropped", []) if note is not None else None
    got = check_all(reply, pages, to_number, numbers_on, usage, g.get("fy_month"), dropped,
                    PERIOD_CHECK == "all" or attr.get("unit_family") == "currency")
    if RULES:
        kept = [e for e in got if rule_ok(g.get("attribute"), e, pages)]
        usage["dropped by the learned rules"] += len(got) - len(kept)
        got = kept
    for e in got:
        if e["year"] is None:           # the old single-object shape, or a missing year: the year asked
            e["year"] = g["year"]
    got.sort(key=lambda e: e["year"] != g["year"])
    got = variant_filter(attr, got, usage, dropped)
    usage["years_returned"] += len(got)
    return got


def slots_prompt(g, attr, pages):
    """The slot read (9 Nov 2026): the attribute's variants (attr["slots"]) asked one by one for the reporting year,
    each with its page, printed number, scale, unit and column, so the choice between accounting variants is
    made afterwards in code, with the analysts' rule, not by the model. The header is the usual prompt's."""
    head = prompt(g, attr, pages).split("\nReply with JSON:")[0]
    y = g["year"]
    slots = "\n".join(f'  "{k}": {v}' for k, v in attr["slots"].items())
    tail = ["", f"Reports print several figures that could be called {attr['name']}. Give every one of these variants that the pages "
            f"print for reporting year {y}, as its own entry, and leave out the variants the pages do not print:", slots, "",
            "Reply with JSON: {\"values\": [{\"slot\": the variant's key above, \"page\": \"P1\", \"printed\": \"12,345.6\", "
            "\"scale\": one of " + ", ".join(f'"{s}"' for s in SCALES) + " (the unit the page states; a percentage is \"units\"), "
            f"\"unit\": \"EUR\", \"tCO2e\" or \"%\", \"year\": {y}, \"column\": the column or line heading as printed, "
            "\"period_end\": YYYY-MM when the page says it}, ...]}. One entry per variant at most, the figure for reporting year "
            f"{y} only, never a figure the pages do not print, no text before or after the JSON. If the pages print none, reply "
            "{\"values\": [], \"why\": \"...\"}."]
    return head + "\n".join(tail)


def read_slots(g, attr, pages, usage, sess=None, note=None):
    """One call: the attribute's variants the pages print for the year asked, each checked against its page
    (check), as entries carrying "slot". Order: attr["slots"] order (the first present is the default proposal).
    Entries for other years, or whose slot is unknown, are dropped. note["why"] as read_years."""
    if not pages or not attr.get("slots"):
        return []
    from extract_eval import to_number
    from value_eval import numbers_on
    text = call([{"role": "user", "content": slots_prompt(g, attr, pages)}], usage, sess, max_tokens=1200)
    reply = parse_reply(text)
    entries = reply.get("values") if isinstance(reply, dict) and isinstance(reply.get("values"), list) else []
    if note is not None and not entries:
        note["why"] = (str(reply.get("why") or "")[:120] if isinstance(reply, dict) else ("(no JSON in the reply)" if text else "(empty reply)"))
    dropped = note.setdefault("dropped", []) if note is not None else None
    by_slot = {}
    for e in entries:
        if not isinstance(e, dict) or str(e.get("slot") or "") not in attr["slots"] or str(e.get("slot")) in by_slot:
            continue
        got = check(e, pages, to_number, numbers_on, g.get("fy_month"), dropped, PERIOD_CHECK == "all" or attr.get("unit_family") == "currency")
        if got is False:
            usage["quote_not_on_page"] += 1
            continue
        if not got or (got["year"] is not None and got["year"] != g["year"]):
            continue
        got["year"] = g["year"]; got["slot"] = str(e["slot"])
        by_slot[got["slot"]] = got
    usage["slots_returned"] += len(by_slot)
    return [by_slot[k] for k in attr["slots"] if k in by_slot]


SELECTOR = "off"   # "candidates" (extract_eval --selector candidates, 8 Nov 2026): a generous extractor proposes up to
                   # CANDIDATES figures for the year asked, with row, column and footnote, and a second call picks one
                   # of them against the attribute's definition and notes (or none). The attribute's conditions help
                   # choose between printed figures instead of making the extractor decline (7 Nov 2026: rules in the
                   # prompt cost market 16 points, rules as a filter 9).
CANDIDATES = 3
SELECT_SYSTEM = ("You choose which of several figures found in a company's report is the one an attribute asks for. "
                 "Answer with one JSON object and nothing else.")


def candidates_prompt(g, attr, pages):
    """The extractor: the attribute's name, definition and labels (no conditions), the pages, and a request for up
    to CANDIDATES different figures for the year asked, best first, each with its row, column and footnote."""
    labels = ", ".join(attr.get("labels", [])[:12])
    y = g["year"]
    lines = [f"Find: {attr['name']}.",
             f"Definition: {attr.get('definition', '').strip()}" if attr.get("definition") else "",
             f"Labels it may appear under: {labels}." if labels else "",
             f"Reporting year: {y}. A fiscal year ending between January and May of {y + 1} is also reporting year {y}.",
             fiscal_note(g),
             g.get("hint") or "",
             (f"Say in \"basis\" what the page calls each figure: \"{attr['variant']['wanted']}\", \"{attr['variant']['other']}\" "
              "or \"not stated\".") if attr.get("variant") else "",
             f"List up to {CANDIDATES} DIFFERENT figures the pages print for reporting year {y} that could be this attribute, "
             "most likely first: for example a total and a part of it, two bases, two tables that disagree, or the same "
             "line in two units. Give a figure even when you are unsure it fits; another step decides. Copy each number "
             "exactly as printed. Never compute.", "", "Pages:"]
    for i, (_, _, body) in enumerate(pages, 1):
        lines += [f"<page id=\"P{i}\">", (body or "")[:PAGE_CHARS], "</page>"]
    lines += adjacent_notes(pages)
    lines += ["", "Reply with JSON: {\"candidates\": [{\"page\": \"P1\", \"printed\": \"12,345.6\", \"scale\": one of "
              + ", ".join(f'"{s}"' for s in SCALES) + ", \"unit\": \"EUR\" or \"tCO2e\" etc., "
              f"\"year\": {y}, \"column\": the column heading as printed, \"period_end\": YYYY-MM when the page says it, "
              "\"row\": the row or line label as printed, \"note\": the footnote or nearby words that qualify the figure, ten "
              "words or fewer" + (", \"basis\": as above" if attr.get("variant") else "") + "}, ...]}. "
              "If the pages print no such figure for that year, reply {\"candidates\": [], \"why\": \"...\"}."]
    return "\n".join(l for l in lines if l is not None)


def select_prompt(g, attr, cands):
    """The selector: the attribute's definition, notes and conditions, and the checked candidates side by side."""
    lines = [f"Attribute: {attr['name']}.",
             f"Definition: {attr.get('definition', '').strip()}" if attr.get("definition") else "",
             f"Notes: {attr['reading_notes'].strip()}" if attr.get("reading_notes") else "",
             f"Reporting year: {g['year']}. Whole company or group, not a segment, a region or a target.",
             (f"The attribute wants the {attr['variant']['wanted']} figure; a figure whose basis the report does not state "
              f"counts too, one it calls {attr['variant']['other']} does not.") if attr.get("variant") else "",
             rules_note(g.get("attribute")), md_note(g.get("attribute")),
             "", "Figures found in the report:"]
    for i, c in enumerate(cands, 1):
        lines.append(f"{i}. {c.get('printed_text') or c['printed']} ({c.get('scale_word') or 'units'}, {c.get('unit') or '?'}); "
                     f"row: {c.get('row') or '?'}; column: {c.get('column') or '?'}; note: {c.get('note') or '-'}"
                     + (f"; basis: {c.get('basis') or 'not stated'}" if attr.get("variant") else "")
                     + (f"; line on the page: {c['line'][:200]}" if c.get("line") else ""))
    lines += ["", "Compare them: which one is the attribute for the reporting year? A total and its part, or two bases, "
              "are told apart by the definition. Choose the one that fits best; the figures were already found under "
              "this attribute's labels, so always choose one. "
              "Reply with JSON only, no text before it: {\"choice\": the number of the figure, \"why\": ten words or fewer}"]
    return "\n".join(l for l in lines if l is not None)


def read_candidates(g, attr, pages, usage, sess=None, note=None):
    """SELECTOR "candidates": the extractor's checked candidates for the year asked, then the selector's choice.
    [] or [the chosen entry]. note receives "candidates" (their values, in the extractor's order) and "choice"
    (the index chosen, None)."""
    if not pages:
        return []
    from extract_eval import to_number
    from value_eval import numbers_on
    reply = parse_reply(call([{"role": "user", "content": candidates_prompt(g, attr, pages)}], usage, sess))
    raw = reply.get("candidates") if isinstance(reply, dict) and isinstance(reply.get("candidates"), list) else []
    dropped = note.setdefault("dropped", []) if note is not None else None
    cands = []
    for e in raw[:CANDIDATES]:
        if not isinstance(e, dict):
            continue
        e = dict(e, year=g["year"]) if e.get("year") in (None, "") else e
        got = check(e, pages, to_number, numbers_on, g.get("fy_month"), dropped,
                    PERIOD_CHECK == "all" or attr.get("unit_family") == "currency")
        if got is False:
            usage["quote_not_on_page"] += 1
            if dropped is not None:
                dropped.append("number not on the page named")
            continue
        if not got or got["year"] != g["year"] or any(abs(got["value"] - c["value"]) < 1e-9 for c in cands):
            continue
        body = next((b for l, p, b in pages if (l, p) == (got["library_id"], got["page"])), "")
        got.update(row=str(e.get("row") or "")[:80], note=str(e.get("note") or "")[:120], printed_text=str(e.get("printed") or ""),
                   scale_word=str(e.get("scale") or "units"), line=(line_of(body, got["printed"]) or [""])[0].strip())
        cands.append(got)
    usage["candidates"] += len(cands)
    if note is not None:
        note["candidates"] = [c["value"] for c in cands]
        note["choice"] = None
        if not cands:
            note["why"] = str((reply or {}).get("why") or "(no candidate)")[:120]
    if not cands:
        return []
    if len(cands) == 1:                   # nothing to choose between (8 Nov 2026: the first run let the selector
        k = 0                             # refuse a lone right candidate 1 time in 3)
    else:
        rep = parse_reply(call([{"role": "user", "content": select_prompt(g, attr, cands)}], usage, sess,
                               system=SELECT_SYSTEM, max_tokens=600))
        try:
            k = int((rep or {}).get("choice")) - 1
        except (TypeError, ValueError):
            k = -1
        if not 0 <= k < len(cands):       # an unreadable or cut reply keeps the extractor's first candidate (8 Nov 2026:
            usage["selector unreadable, first kept"] += 1      # 119 replies cut at 200 tokens read as "none")
            k = 0
    if note is not None:
        note["choice"] = k
    chosen = cands[k]
    for key in ("row", "note", "printed_text", "scale_word", "line"):
        chosen.pop(key, None)
    return variant_filter(attr, [chosen], usage, dropped)


def read_with_model(g, attr, pages, usage, sess=None, note=None):
    """The model's proposal for the year asked, checked against the pages, or None; the figures for the other
    years the pages print come with it under "others"."""
    got = (read_candidates if SELECTOR == "candidates" else read_years)(g, attr, pages, usage, sess, note)
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
    both = (os.environ.get("READER_PRICE") or "").split("/")     # "in/out", from a workflow's price input
    pin = float(both[0]) if len(both) == 2 and both[0] else float(os.environ.get("READER_PRICE_IN") or "0.10")
    pout = float(both[1]) if len(both) == 2 and both[1] else float(os.environ.get("READER_PRICE_OUT") or "0.50")
    return usage["input_tokens"] / 1e6 * pin + usage["output_tokens"] / 1e6 * pout


def cost_note():
    """A warning for the cost line when the reader is not the default model and no price was given: the cost is
    then at the Haiku list price and understates a larger model's (a Sonnet run of 8 Nov 2026 printed $0.47)."""
    model = os.environ.get("READER_MODEL") or DEFAULT_MODEL
    priced = os.environ.get("READER_PRICE") or os.environ.get("READER_PRICE_IN") or os.environ.get("READER_PRICE_OUT")
    if model == DEFAULT_MODEL or priced:
        return ""
    return f" (at the {DEFAULT_MODEL} list price, not {model}'s: set READER_PRICE to \"in/out\" dollars per million tokens)"
