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
MAX_TOKENS = 300
SCALES = {"units": 1, "thousands": 1e3, "lakhs": 1e5, "millions": 1e6, "crores": 1e7, "hundred millions": 1e8,
          "billions": 1e9}

SYSTEM = ("You read pages of company reports and find one reported figure. Answer with one JSON object and "
          "nothing else. Copy the number exactly as printed on the page. Never compute, convert or round.")


def prompt(g, attr, pages):
    """The user message: what to find, then the pages, each tagged P1, P2, ..."""
    labels = ", ".join(attr.get("labels", [])[:12])
    lines = [f"Find: {attr['name']}.",
             f"Definition: {attr.get('definition', '').strip()}" if attr.get("definition") else "",
             f"Labels it may appear under: {labels}." if labels else "",
             f"Reporting year: {g['year']} (a fiscal year ending January to June of {g['year'] + 1} also counts as {g['year']}).",
             f"Period: {attr['period']}." if attr.get("period") else "",
             f"Scope: {attr['scope']}." if attr.get("scope") else "",
             "Take the figure for that year, for the whole company or group, not a segment, a region or a target.",
             "", "Pages:"]
    for i, (_, _, body) in enumerate(pages, 1):
        lines += [f"<page id=\"P{i}\">", (body or "")[:PAGE_CHARS], "</page>"]
    lines += ["", "Reply with JSON: {\"page\": \"P1\", \"printed\": \"12,345.6\", \"scale\": one of "
              + ", ".join(f'"{s}"' for s in SCALES) + " (the unit the page states for that number, e.g. "
              "\"in millions\", \"€m\", \"'000\", \"ktCO2e\"), \"unit\": \"EUR\" or \"tCO2e\" etc., "
              "\"year\": 2024}. If the pages do not give the figure for that year, reply {\"page\": null}."]
    return "\n".join(l for l in lines if l is not None)


def parse_reply(text):
    """The first JSON object in the model's text, or None."""
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def check(reply, pages, to_number, numbers_on):
    """{'value', 'printed', 'scale', 'library_id', 'page', 'unit'} when the reply names a page that prints the
    number, else None. to_number and numbers_on are the rule reader's own parsers."""
    if not reply or not reply.get("page") or not reply.get("printed"):
        return None
    m = re.fullmatch(r"P(\d+)", str(reply["page"]).strip())
    if not m or not 1 <= int(m.group(1)) <= len(pages):
        return None
    lid, page, body = pages[int(m.group(1)) - 1]
    try:
        printed = to_number(str(reply["printed"]).strip())
    except (TypeError, ValueError):
        return None
    if not any(abs(v - printed) < 1e-9 for v in numbers_on(body or "")):
        return False                    # named a number the page does not print
    scale = SCALES.get(str(reply.get("scale") or "units").strip().lower(), 1)
    return {"value": printed * scale, "printed": printed, "scale": scale, "library_id": lid, "page": page,
            "unit": reply.get("unit") or ""}


def call(messages, usage, sess=None, model=None):
    """The model's text reply (Messages API), retrying twice on overload or rate limits."""
    import requests
    sess = sess or requests
    body = {"model": model or os.environ.get("READER_MODEL", DEFAULT_MODEL), "max_tokens": MAX_TOKENS,
            "temperature": 0, "system": SYSTEM, "messages": messages}
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(), "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    for attempt in range(3):
        r = sess.post(API, headers=headers, json=body, timeout=120)
        if r.status_code in (429, 500, 529) and attempt < 2:
            time.sleep(10 * (attempt + 1)); continue
        if r.status_code != 200:
            usage["model_errors"] += 1
            if r.status_code in (400, 401, 403, 404):  # a bad key, no credit, a wrong model name: no point going on
                raise ModelRefused(f"Messages API refused the call: HTTP {r.status_code} {r.text[:300]}")
            return ""
        j = r.json()
        usage["calls"] += 1
        usage["input_tokens"] += j.get("usage", {}).get("input_tokens", 0)
        usage["output_tokens"] += j.get("usage", {}).get("output_tokens", 0)
        return "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
    return ""


def read_with_model(g, attr, pages, usage, sess=None):
    """The model's proposal for one answer, checked against the pages, or None."""
    if not pages:
        return None
    from extract_eval import to_number
    from value_eval import numbers_on
    text = call([{"role": "user", "content": prompt(g, attr, pages)}], usage, sess)
    got = check(parse_reply(text), pages, to_number, numbers_on)
    if got is False:
        usage["quote_not_on_page"] += 1
        return None
    return got


def cost(usage, model=None):
    """Dollars at the list price per million tokens (Haiku 5.5, prompts up to 100,000 tokens, 29 Oct 2026);
    READER_PRICE_IN / READER_PRICE_OUT override it for another model."""
    pin = float(os.environ.get("READER_PRICE_IN", "0.10"))
    pout = float(os.environ.get("READER_PRICE_OUT", "0.50"))
    return usage["input_tokens"] / 1e6 * pin + usage["output_tokens"] / 1e6 * pout
