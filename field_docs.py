#!/usr/bin/env python3
"""
field_docs.py: writes docs/fields.md, the field-by-field reference of the Disclosure Library and the
Pipeline QA bases, from their Airtable schema.

The descriptions themselves live in Airtable (every field description is kept current with the code);
this script only lays them out as one page that people who never open Airtable's field editor can read,
and adds two things Airtable does not show in one place: who writes each field (you, the robot, or a
formula) and the allowed values of each single or multiple select.

Inputs, either:
  --from-json DL.json QA.json   schema files (the shape of Airtable's meta API: {"tables": [...]}, each
                                field with name, type, description and, for selects, options.choices or
                                config.choices); a Claude session can save them with the Airtable tools.
  (no flag)                     reads the meta API with AIRTABLE_TOKEN (needs the schema.bases:read scope)
                                for AIRTABLE_BASE and AIRTABLE_QA_BASE.
Writes docs/fields.md only with --commit; otherwise prints it. Reads only; never writes to Airtable.
"""
import argparse, datetime as dt, json, os, sys, urllib.request

DL_BASE = "appXS0v6SnWdzAsY3"
QA_BASE = "appJQ2mRtjHjzLq9E"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "fields.md")

# Fields a person fills or changes. Everything else is written by the robot (scripts, Airtable AI)
# or computed by a formula. Keep in step with docs/guide.md.
HUMAN = {
    "monitored_links": {"url", "type", "frequency", "notes", "monitor", "certified", "certified_by",
                        "capture_page", "scan_now", "scan_concern", "delete_now", "assigned_analyst"},
    "report_library": {"source_type_check", "source_subtypes", "discard", "discard_reason", "Reviewer notes",
                       "curated_doc_title", "verified", "delete_now", "doc_scope", "analyst_flag",
                       "analyst_flag_note", "ai_year_test", "entity_check"},
    "companies": {"analyst", "adopted_on", "Notes", "add_link", "add_file", "website", "suggested_links",
                  "approve_suggestions", "queue_snooze_until", "key_figures_checked", "sec_cik", "logo"},
    "mention_searches": {"search", "find", "short_names", "confirming_words", "near", "distance", "doc_types",
                         "year_from", "year_to", "companies", "look_in", "active", "run_now"},
    "mention_hits": {"review", "notes"},
    "failures": {"status", "review_note"},
    "labels": {"verdict", "true_type", "true_value", "true_year", "title_wrong", "sure", "reason", "your_call"},
}
# Short notes for fields that carry no description in Airtable.
EXTRA = {
    ("monitored_links", "link_id"): "Stable id of the page (written by assign_ids.py). The join key for documents found on it.",
    ("monitored_links", "wba_id"): "The company this page belongs to.",
    ("monitored_links", "frequency"): "How often the plain monitor reads the page.",
    ("monitored_links", "http_status"): "HTTP code of the last read (200 ok, 403 refused, 404 gone).",
    ("monitored_links", "final_url"): "Where the address ended up after redirects.",
    ("monitored_links", "is_pdf"): "The watched address is itself a PDF, not a page.",
    ("monitored_links", "source"): "How the page entered the base (company list, discovery, added by hand, approved suggestion...).",
    ("monitored_links", "alert_status"): "new = new document links since the last check.",
    ("report_library", "library_id"): "Stable id of the document (from its normalized address). Unique.",
    ("report_library", "page_count"): "Pages in the PDF (read with the cover).",
    ("report_library", "doc_year"): "Year from the rules (file name, cover, filing, address). One input to `year`; doc_year_from says where it came from.",
    ("report_library", "match_confidence"): "How sure the address classifier was about source_type.",
    ("report_library", "cover_text"): "Text of the first six pages, read by build_cover_text.py. What the AI fields and most rules read.",
    ("report_library", "cover_status"): "Result of reading the cover: ok, or why not (no_text, parse_fail, truncated, file too big...).",
    ("report_library", "page_type"): "Type of the page the document was found on.",
    ("webpage_library", "library_id"): "Web pages printed to PDF every Sunday (capture_page ticked on the page). One row per capture with change detection.",
}
SELECTS = ("singleSelect", "multipleSelects")
TYPE_WORDS = {"singleLineText": "text", "multilineText": "long text", "url": "link", "number": "number",
              "percent": "percent", "checkbox": "checkbox", "date": "date", "dateTime": "date and time",
              "singleSelect": "pick one", "multipleSelects": "pick several", "multipleRecordLinks": "link to rows",
              "multipleAttachments": "file", "formula": "formula", "aiText": "Airtable AI", "count": "count",
              "rollup": "rollup", "multipleLookupValues": "lookup", "createdTime": "created time",
              "lastModifiedTime": "last modified", "singleCollaborator": "person", "richText": "rich text"}


def choices(f):
    for key in ("options", "config"):
        c = (f.get(key) or {}).get("choices")
        if c:
            return [x["name"] for x in c if x.get("name")]
    return []


def who(table, f):
    if f["name"] in HUMAN.get(table, ()):
        return "you"
    if f["type"] in ("formula", "count", "rollup", "multipleLookupValues", "createdTime", "lastModifiedTime"):
        return "formula"
    if f["type"] == "aiText":
        return "Airtable AI"
    return "robot"


def cell(s):
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def render_table(t):
    lines = [f"### {t['name']}", ""]
    if t.get("description"):
        lines += [t["description"], ""]
    lines += ["| Field | Kind | Who writes it | What it means |", "| --- | --- | --- | --- |"]
    for f in t["fields"]:
        desc = f.get("description") or EXTRA.get((t["name"], f["name"]), "")
        ch = choices(f) if f["type"] in SELECTS else []
        if ch:
            shown = ch if len(ch) <= 30 else ch[:30] + [f"... ({len(ch)} in all)"]
            desc = (desc + " " if desc else "") + "Values: " + ", ".join(shown) + "."
        lines.append(f"| `{f['name']}` | {TYPE_WORDS.get(f['type'], f['type'])} | {who(t['name'], f)} | {cell(desc)} |")
    return lines + [""]


def render(dl, qa, today):
    out = ["# Field reference", "",
           f"Every field of the two Airtable bases the pipeline uses, generated by `field_docs.py` on {today} from the "
           "live schema. The text comes from the field descriptions in Airtable, which are the source: change a "
           "description there (or ask for it in a pull request) and regenerate this page.", "",
           "\"Who writes it\": **you** = a person fills or changes it (the owner or an analyst); **robot** = a script "
           "writes it, do not edit by hand; **formula** = computed by Airtable from other fields; **Airtable AI** = an "
           "Airtable AI field that reads the cover text.", "",
           "How to use the fields day to day is in [guide.md](guide.md).", "",
           f"## Disclosure Library (`{DL_BASE}`)", ""]
    for t in dl["tables"]:
        out += render_table(t)
    out += [f"## Pipeline QA (`{QA_BASE}`)", ""]
    for t in qa["tables"]:
        out += render_table(t)
    return "\n".join(out).rstrip() + "\n"


def fetch(base):
    req = urllib.request.Request(f"https://api.airtable.com/v0/meta/bases/{base}/tables",
                                 headers={"Authorization": "Bearer " + os.environ["AIRTABLE_TOKEN"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", nargs=2, metavar=("DL_JSON", "QA_JSON"))
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    a = ap.parse_args(argv)
    if a.from_json:
        dl, qa = (json.load(open(p)) for p in a.from_json)
    else:
        dl = fetch(os.environ.get("AIRTABLE_BASE", DL_BASE))
        qa = fetch(os.environ.get("AIRTABLE_QA_BASE", QA_BASE))
    text = render(dl, qa, a.date)
    if a.commit:
        with open(OUT, "w") as fh:
            fh.write(text)
        print(f"wrote {OUT} ({text.count(chr(10))} lines)")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
