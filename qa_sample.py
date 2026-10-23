#!/usr/bin/env python3
"""
qa_sample.py — puts the day's review cases in Pipeline QA (labels, in_review ticked), so a person
spends ten minutes where a label buys the most information.

One card per document, judging three facts at once: type, year and title. The batch (default 15):
  high_stakes_disagreement  5  open TYPE_DISAGREE failures; the URL/AI type pair with the fewest
                               labels so far goes first, so every confusion pair gets measured
  non_english               3  open TYPE_MISSING failures with detail non_english
  agreement_control         3  documents whose two opinions do not conflict, drawn at random from
                               report-family 2023+ rows; shown BLIND (no robot or AI type on the card;
                               the pipeline's values go to hidden_values) so accuracy is not anchored
  new_arrival               2  typed documents first seen in the last 7 days
  unnamed                   2  reserved for cases the scripts cannot code; filled with extra
                               disagreements while there are none
A document already labelled is never picked again. The draw is seeded by the date, so a re-run on
the same day picks the same cases (and the label_id upsert makes it a no-op).

Housekeeping on every run settles each card into a final label (settle()):
  - you answered your_call: Agree takes Claude's answer (suggested_value, suggested_year, suggested_title)
    as the truth; Disagree keeps what you corrected (true_type, true_year, title_wrong) and takes the rest
    from Claude's answer; Can't tell does the same but marks the label not sure. labelled_by = Edi.
  - you filled true_type, true_year or title_wrong without your_call (the old way): kept as yours.
  - the daily pre-read (Opus) left the card out of Today's cases (in_review unticked) because it was
    confident, or the card went 3 days without an answer: Claude's answer becomes the label,
    labelled_by = Claude, sure only when claude_confidence is high.
  Settled cards get reviewed ticked and leave Today's cases. Labels by Claude score the address rules and
  the year rules; anything that scores the Airtable AI type must leave them out (model graded by model).

Each new card gets a split: 70% eval, 15% fewshot, 15% regression (by hash of the label_id).
Modes: dry run by default (prints the batch); --commit writes.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE (Disclosure Library, read only here), AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE.
"""
import argparse, collections, datetime as dt, hashlib, json, os, random, re, sys, time
from urllib.parse import quote

import qa
from monitor_core import airtable_request

API = "https://api.airtable.com/v0"
LIB = "report_library"
REPORT_FAMILY = ("Sustainability Report", "ESG Report", "CSR Report", "Integrated Report", "Impact Report",
                 "Annual Report", "10K Form")
QUOTA = (("high_stakes_disagreement", 5), ("non_english", 3), ("agreement_control", 3),
         ("new_arrival", 2), ("unnamed", 2))
DOC_FIELDS = ["library_id", "wba_id", "company_name", "doc_label", "document_url", "found_on", "cover_text",
              "source_type", "source_type_check", "doc_language", "year", "year_source", "year_score",
              "year_evidence", "source_title", "auto_title", "discard"]
STALE_DAYS = 3          # a card unanswered this long is settled with Claude's answer


# ---- pure helpers ----------------------------------------------------------------------------------
def split_for(label_id):
    h = int(hashlib.sha1(label_id.encode()).hexdigest()[:8], 16) % 100
    return "eval" if h < 70 else ("fewshot" if h < 85 else "regression")


def pair_of(message):
    """'URL says A; AI says B' -> (A, B)."""
    try:
        a, b = message.split(";", 1)
        return a.replace("URL says", "").strip(), b.replace("AI says", "").strip()
    except Exception:
        return ("", "")


def pick_disagreements(failures, labelled_pairs, n, rng, exclude):
    """Round-robin over pairs, least-labelled pair first, so every confusion gets measured."""
    by_pair = collections.defaultdict(list)
    for f in failures:
        if f.get("library_id") in exclude:
            continue
        by_pair[pair_of(f.get("message") or "")].append(f)
    for rows in by_pair.values():
        rng.shuffle(rows)
    order = sorted(by_pair, key=lambda p: (labelled_pairs.get(p, 0), -len(by_pair[p]), p))
    out = []
    while len(out) < n and any(by_pair[p] for p in order):
        for p in order:
            if by_pair[p] and len(out) < n:
                out.append(by_pair[p].pop())
    return out


def _name(v):
    return (v.get("name") if isinstance(v, dict) else v) or ""


def settle(l, today, stale_cut):
    """The updates that turn one card into a final label ({} when nothing to do). Pure."""
    if l.get("reviewed"):
        return {"in_review": False} if l.get("in_review") else {}
    call = _name(l.get("your_call"))
    s_type, s_year, s_title = (l.get("suggested_value") or "").strip(), l.get("suggested_year"), \
        (l.get("suggested_title") or "").strip()
    conf = _name(l.get("claude_confidence"))
    old_way = bool(l.get("verdict") or l.get("true_type") or l.get("true_year") or l.get("title_wrong"))
    upd = {}
    if call or old_way:
        upd = {"reviewed": True, "labelled_on": today, "labelled_by": "Edi", "in_review": False}
        if call:
            if not l.get("true_type") and s_type:
                upd["true_type"] = s_type
            if not l.get("true_year") and s_year:
                upd["true_year"] = s_year
            if call == "Agree" and s_title and not l.get("title_wrong"):
                upd["title_wrong"] = True
            if call == "Can't tell":
                upd["sure"] = False
        return upd
    left_out = not l.get("in_review")
    stale = (l.get("batch_date") or today) < stale_cut
    if s_type and conf and (left_out or stale):
        upd = {"reviewed": True, "labelled_on": today, "labelled_by": "Claude", "in_review": False,
               "true_type": s_type, "sure": conf == "high"}
        if s_year:
            upd["true_year"] = s_year
        if s_title:
            upd["title_wrong"] = True
        return upd
    if l.get("in_review") and (l.get("batch_date") or today) < (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat():
        return {"in_review": False}   # never pre-read and a week old: leave the queue unlabelled
    return {}


REFUSAL = re.compile(r"^\W*(i don.t|i do not|i cannot|i can.t|i.m unable|i am unable|unable to|sorry|there is no|"
                     r"there are no|the text provided|the extracted text|no title|no clear|i need|please provide|based on the)",
                     re.I)


def ai_text(v):
    """The AI field's answer; '' when it answered with a sentence instead of a title (same rule as the
    report_library.ai_title formula)."""
    t = (v.get("value") or "").strip() if isinstance(v, dict) else ((v or "").strip() if isinstance(v, str) else "")
    return "" if (REFUSAL.match(t[:80]) or len(t) > 200) else t


def card(doc, stratum, today):
    """A labels row for one Disclosure Library document."""
    lid = doc.get("library_id") or doc["_id"]
    label_id = f"{lid}|document|{today}"
    kw, ai = (doc.get("source_type") or "").strip(), (doc.get("source_type_check") or "").strip()
    y = doc.get("year")
    ys = doc.get("year_source")
    sc = doc.get("year_score")
    year_value = ""
    if isinstance(y, (int, float)):
        year_value = str(int(y)) + (f" (source {ys}" + (f", score {int(sc)}" if isinstance(sc, (int, float)) else "") + ")"
                                    if ys not in (None, "") else "")
    row = {"target": "document", "field": "document", "stratum": stratum, "company_name": doc.get("company_name"),
           "wba_id": doc.get("wba_id"), "doc_label": (doc.get("doc_label") or "")[:250],
           "document_url": doc.get("document_url"), "page_url": doc.get("found_on"), "library_id": lid,
           "record_id": doc["_id"], "cover_snippet": (doc.get("cover_text") or "")[:400],
           "doc_language": doc.get("doc_language"), "year_value": year_value,
           "year_evidence": (doc.get("year_evidence") or "")[:1000],
           "title_value": (ai_text(doc.get("source_title")) or doc.get("auto_title") or "")[:250],
           "batch_date": today, "in_review": True, "sure": True, "split": split_for(label_id)}
    if stratum == "agreement_control":
        row["hidden_values"] = json.dumps({"source_type": kw, "source_type_check": ai, "effective": ai or kw})
    else:
        row["robot_value"] = kw
        row["ai_value"] = ai
    return label_id, row


# ---- Airtable -----------------------------------------------------------------------------------------
def get(token, base, table, fields, formula=None, max_rows=None, sort=None):
    url = f"{API}/{base}/{quote(table)}"
    headers = {"Authorization": f"Bearer {token}"}
    params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
    if formula:
        params.append(("filterByFormula", formula))
    if sort:
        params += [("sort[0][field]", sort[0]), ("sort[0][direction]", sort[1])]
    out, offset = [], None
    while True:
        p = list(params) + ([("offset", offset)] if offset else [])
        j = airtable_request("GET", url, headers, params=p).json()
        for rec in j.get("records", []):
            f = rec.get("fields", {}); f["_id"] = rec["id"]; out.append(f)
        offset = j.get("offset")
        if not offset or (max_rows and len(out) >= max_rows):
            break
        time.sleep(0.12)
    return out[:max_rows] if max_rows else out


def get_docs(token, base, record_ids):
    if not record_ids:
        return []
    out = []
    for i in range(0, len(record_ids), 40):
        chunk = record_ids[i:i + 40]
        formula = "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in chunk) + ")"
        out += get(token, base, LIB, DOC_FIELDS, formula)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="batch date (seeds the draw)")
    args = ap.parse_args()
    tok, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    qtok = os.environ.get("AIRTABLE_QA_TOKEN"); qbase = os.environ.get("AIRTABLE_QA_BASE", qa.QA_BASE_DEFAULT)
    if not (tok and base and qtok):
        sys.exit("Set AIRTABLE_TOKEN, AIRTABLE_BASE and AIRTABLE_QA_TOKEN.")
    if args.commit:
        os.environ["QA_COMMIT"] = "true"
    today = args.date
    run = qa.Run("qa_sample.py", token=qtok, base=qbase)

    # what is already labelled, and how often each pair has been judged
    labels = get(qtok, qbase, qa.T_LABELS, ["label_id", "library_id", "robot_value", "ai_value", "verdict",
                                            "true_type", "true_year", "title_wrong", "reviewed", "in_review",
                                            "batch_date", "your_call", "suggested_value", "suggested_year",
                                            "suggested_title", "claude_confidence"])
    exclude = {l.get("library_id") for l in labels if l.get("library_id")}
    labelled_pairs = collections.Counter((l.get("robot_value") or "", l.get("ai_value") or "")
                                         for l in labels if l.get("reviewed"))

    # housekeeping: settle every card that can be settled
    stale_cut = (dt.date.fromisoformat(today) - dt.timedelta(days=STALE_DAYS)).isoformat()
    house, settled = [], collections.Counter()
    for l in labels:
        upd = settle(l, today, stale_cut)
        if upd:
            house.append({"id": l["_id"], "fields": upd})
            settled[upd.get("labelled_by") or "left_queue"] += 1

    rng = random.Random(today)
    picked = []   # (stratum, record_id)
    # 1. disagreements (+ the unnamed slots while no unnamed cases exist)
    dis = get(qtok, qbase, qa.T_FAILURES, ["library_id", "record_id", "message"],
              "AND({code}='TYPE_DISAGREE', {status}='open', {severity}='High')")
    n_dis = dict(QUOTA)["high_stakes_disagreement"] + dict(QUOTA)["unnamed"]
    for f in pick_disagreements(dis, labelled_pairs, n_dis, rng, exclude):
        picked.append(("high_stakes_disagreement", f.get("record_id")))
    # 2. non-English untyped
    ne = get(qtok, qbase, qa.T_FAILURES, ["library_id", "record_id"],
             "AND({code}='TYPE_MISSING', {detail}='non_english', {status}='open')")
    ne = [f for f in ne if f.get("library_id") not in exclude]
    rng.shuffle(ne)
    picked += [("non_english", f.get("record_id")) for f in ne[:dict(QUOTA)["non_english"]]]
    # 3. agreement controls: a seeded slice of report-family rows that do not conflict
    hexch = "0123456789abcdef"[rng.randrange(16)]
    fam = "OR(" + ",".join(f"{{source_type_check}}='{t}'" for t in REPORT_FAMILY) + ")"
    ctrl = get(tok, base, LIB, ["library_id"],
               f"AND(NOT({{discard}}), {{type_mismatch}}='', {fam}, {{year}}>=2023, {{cover_status}}='ok', "
               f"RIGHT({{library_id}},1)='{hexch}')", max_rows=300)
    ctrl = [c for c in ctrl if c.get("library_id") not in exclude]
    rng.shuffle(ctrl)
    picked += [("agreement_control", c["_id"]) for c in ctrl[:dict(QUOTA)["agreement_control"]]]
    # 4. new arrivals
    new = get(tok, base, LIB, ["library_id"],
              "AND(NOT({discard}), {source_type_check}!='', IS_AFTER({first_seen}, DATEADD(TODAY(), -7, 'days')))",
              max_rows=300)
    new = [c for c in new if c.get("library_id") not in exclude]
    rng.shuffle(new)
    picked += [("new_arrival", c["_id"]) for c in new[:dict(QUOTA)["new_arrival"]]]

    picked = [(s, r) for s, r in picked if r]
    docs = {d["_id"]: d for d in get_docs(tok, base, [r for _, r in picked])}
    seen, n = set(), collections.Counter()
    for stratum, rid in picked:
        d = docs.get(rid)
        if not d or d.get("discard") or rid in seen:
            continue
        seen.add(rid)
        label_id, row = card(d, stratum, today)
        run.label(label_id, **row)
        n[stratum] += 1
        print(f"  [{stratum:24}] {(row.get('doc_label') or '')[:80]}", flush=True)

    counters = {"by_stratum": dict(n), "housekeeping": len(house), "settled": dict(settled), "labels_total": len(labels),
                "reviewed_total": sum(1 for l in labels if l.get("reviewed"))}
    if args.commit and house:
        run._write(qa.T_LABELS, house, "PATCH")
    run.finish("ok", rows_in=len(picked), rows_out=sum(n.values()), counters=counters)
    run.write_step_summary("ok", counters, title="QA sample")


if __name__ == "__main__":
    main()
