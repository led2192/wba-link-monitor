#!/usr/bin/env python3
"""
type_eval.py: can our own reader replace the Airtable AI field that types documents? Measured against the types
people gave, before anything changes.

Why (12 Nov 2026): the document type (`source_type_check`) of English documents comes from an Airtable AI field,
and so does the title (`source_title`). Leaving Airtable means typing every document ourselves. `type_covers.py`
already does it for the other languages with the model reader's model, and its only measure so far is its
agreement with the AI field (76% of 300 on 10 Nov), which says how alike the two are, not which one is right.
The Pipeline QA base holds the answer key: the labels with a sure true type given by a person (Source Library
analysts, cards answered by the owner; not the 'correction' labels, which repeat the stored type). Labels settled by a model are left out, so that no
model is graded by a model (qa_sample.py).

What it does: takes N of those documents (the same N every run: ordered by a hash of the record id), reads their
first pages with `type_covers.read_type`, and reports, on the same documents, how often each of three is right:
the type stored today (`source_type_check`: the AI field on English documents), the address and cover rules
(`source_type`), and our reader. Exact, and at check level (the type falls in the same completeness check:
sustainability family, annual family, code of conduct, human rights policy). English and other languages apart,
because the stored type of a non-English document may already be our reader's own (written since 10 Nov) and
that comparison says nothing. Also: how the two disagree (only one right), the commonest mistakes of our reader
by true type, and how often our title equals the AI title.

Writes nothing to Airtable. Prints types and library ids only, never the text of a document.
Env: AIRTABLE_TOKEN, AIRTABLE_BASE (read), AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE (read; the run record with
QA_COMMIT=true), ANTHROPIC_API_KEY (READER_MODEL for another model).
"""
import argparse, collections, hashlib, os, re, sys, time

API = "https://api.airtable.com/v0"
PEOPLE = ("source_library", "Edi")                              # labelled_by values that are a person's own answer
# Not "correction" (12 Nov 2026): qa_sync writes that label whenever source_type_check differs from the first type
# it saw on the row, and takes the new value as the truth. Most of those changes are the AI field answering after
# the address rule, not a person: about 15,000 of the 22,244 sure labels. Graded on them the stored type is right
# by construction (93% in the first run, which measured nothing).
DEPENDENT = ("correction",)
FIELDS = ("library_id", "cover_text", "doc_language", "source_type_check", "source_type", "source_title", "document_url", "company_name")


def value(v):
    """A plain field, a pick-one ({'name'}) or an Airtable AI field ({'state', 'value'}) as text."""
    if isinstance(v, dict):
        v = v.get("value") if "value" in v or "state" in v else v.get("name")
        if isinstance(v, dict):
            v = v.get("name")
    return v.strip() if isinstance(v, str) else ""


def pick(truth, n, offset=0):
    """n record ids of the truth, the same ones every run: ordered by a hash of the id (not by the id, which is
    ordered by creation time and would take the oldest documents only). offset skips the first ones: the documents
    a prompt is written on (to read its mistakes) must not be the ones it is measured on."""
    ids = sorted(truth, key=lambda r: hashlib.sha1(r.encode()).hexdigest())[offset:]
    return ids[:n] if n else ids


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def same_title(a, b):
    """Titles count as the same when one holds the other after dropping case and punctuation (the AI field adds
    the company or the year more often than not)."""
    a, b = norm_title(a), norm_title(b)
    return bool(a and b) and (a == b or a in b or b in a)


def score(docs, checks_of):
    """docs: [{truth, stored, rule, ours, english}]. Returns {(group, who, measure): count} with groups
    'English', 'Other', 'All'; who 'stored', 'rule', 'ours'; measures n, typed, exact, check; and for the pair
    stored/ours: both, only_stored, only_ours, neither (exact)."""
    c = collections.Counter()
    for d in docs:
        for g in ("All", "English" if d["english"] else "Other"):
            c[g, "n"] += 1
            for who in ("stored", "rule", "ours"):
                t = d[who]
                c[g, who, "typed"] += bool(t)
                c[g, who, "exact"] += bool(t) and t == d["truth"]
                c[g, who, "check"] += bool(t) and checks_of(t) == checks_of(d["truth"])
            s, o = d["stored"] == d["truth"], d["ours"] == d["truth"]
            c[g, "both" if s and o else "only_stored" if s else "only_ours" if o else "neither"] += 1
    return c


def report(docs, c, left_out, usage_line):
    md = ["# Document types: the stored type, the rules and our reader against the types people gave", "",
          f"{len(docs):,} documents with a sure true type given by a person and their first pages read. {usage_line}", ""]
    if left_out:
        md += ["Left out: " + ", ".join(f"{k} {v:,}" for k, v in collections.Counter(left_out).most_common()) + ".", ""]
    md += ["'Right' is of all the documents of the group, typed or not. Check level: the type falls in the same completeness check.", "",
           "| Documents | Who | Typed | Right (exact) | Right (check level) |", "| --- | --- | --: | --: | --: |"]
    names = {"stored": "stored type (the AI field on English)", "rule": "address and cover rules", "ours": "our reader"}
    for g in ("English", "Other", "All"):
        n = c[g, "n"]
        if not n:
            continue
        for who in ("stored", "rule", "ours"):
            md.append(f"| {g} ({n:,}) | {names[who]} | {c[g, who, 'typed'] / n:.1%} | {c[g, who, 'exact'] / n:.1%} | {c[g, who, 'check'] / n:.1%} |")
    md += ["", "Stored type and our reader on the same documents (exact):", "",
           "| Documents | Both right | Only the stored type | Only our reader | Neither |", "| --- | --: | --: | --: | --: |"]
    for g in ("English", "Other", "All"):
        if c[g, "n"]:
            md.append(f"| {g} | {c[g, 'both']:,} | {c[g, 'only_stored']:,} | {c[g, 'only_ours']:,} | {c[g, 'neither']:,} |")
    by = collections.defaultdict(lambda: [0, 0, 0])
    wrong = collections.Counter()
    for d in docs:
        v = by[d["truth"]]
        v[0] += 1; v[1] += d["stored"] == d["truth"]; v[2] += d["ours"] == d["truth"]
        if d["ours"] != d["truth"]:
            wrong[(d["truth"], d["ours"] or "(no reply)")] += 1
    md += ["", "By true type (exact):", "", "| True type | Documents | Stored type right | Our reader right |", "| --- | --: | --: | --: |"]
    md += [f"| {t} | {v[0]:,} | {v[1] / v[0]:.0%} | {v[2] / v[0]:.0%} |" for t, v in sorted(by.items(), key=lambda x: -x[1][0])]
    md += ["", "Our reader's commonest mistakes (true type > ours): " + ", ".join(f"{a} > {b} {n}" for (a, b), n in wrong.most_common(15)) + ".", ""]
    both = [d for d in docs if d.get("our_title") and d.get("ai_title")]
    if both:
        md += [f"Titles: of {len(both):,} documents with both an AI title and ours, {sum(same_title(d['our_title'], d['ai_title']) for d in both) / len(both):.0%} "
               "are the same title (one holds the other, case and punctuation aside).", ""]
    lost = [d for d in docs if d["stored"] == d["truth"] and d["ours"] != d["truth"]]
    md += ["Where the stored type is right and ours is not, the first 40 (library_id | true | ours):", ""]
    md += [f"- {d['library_id']} | {d['truth']} | {d['ours'] or '(no reply)'}" for d in lost[:40]]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1500, help="documents to read (0 = every one with a true type)")
    ap.add_argument("--offset", type=int, default=0, help="skip this many documents of the fixed order (a second, unseen set)")
    ap.add_argument("--reader", type=int, default=1, choices=[1, 2], help="the reader's prompt: 1 the first pages alone; 2 more rules, more text, the address and the company")
    ap.add_argument("--max-usd", type=float, default=3.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="type_eval")
    a = ap.parse_args()
    tok, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("AIRTABLE_BASE")
    qtok, qbase = os.environ.get("AIRTABLE_QA_TOKEN"), os.environ.get("AIRTABLE_QA_BASE")
    if not (tok and base and qtok and qbase):
        sys.exit("Set AIRTABLE_TOKEN, AIRTABLE_BASE, AIRTABLE_QA_TOKEN and AIRTABLE_QA_BASE.")
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip() and not os.environ.get("GEMINI_API_KEY", "").strip():
        sys.exit("Set ANTHROPIC_API_KEY (or GEMINI_API_KEY with READER_MODEL=gemini-...).")
    import qa, model_reader, type_covers as TC
    from qa_sync import checks_of
    from year_eval import get
    from concurrent.futures import ThreadPoolExecutor
    run = qa.Run("type_eval.py")
    t0 = time.time()
    labels = get(qtok, qbase, qa.T_LABELS, ["true_type", "record_id", "sure", "labelled_by"], "AND({true_type}!='', {record_id}!='', {sure})")
    left_out = collections.Counter()
    truth = {}
    for l in labels:
        tt = value(l.get("true_type"))
        if value(l.get("labelled_by")) in DEPENDENT:
            left_out["labels that are a change of the stored type (the stored type is right on them by construction)"] += 1
        elif value(l.get("labelled_by")) not in PEOPLE:
            left_out["labels not given by a person"] += 1
        elif tt not in TC.TYPES and tt != TC.UNKNOWN:
            left_out["true types outside the list of 23"] += 1
        else:
            truth[l["record_id"]] = tt
    ids = pick(truth, a.n, a.offset)
    print(f">>> {len(truth):,} documents with a sure true type given by a person; reading {len(ids):,}.", flush=True)
    rows = []
    for i in range(0, len(ids), 40):
        rows += get(tok, base, "report_library", list(FIELDS), "OR(" + ",".join(f"RECORD_ID()='{r}'" for r in ids[i:i + 40]) + ")")
    left_out["documents no longer in the library"] += len(ids) - len(rows)
    left_out["documents with no first pages read"] += sum(1 for r in rows if not value(r.get("cover_text")))
    rows = [r for r in rows if value(r.get("cover_text"))]
    usage = collections.Counter()

    def read_one(r):
        u = collections.Counter()
        t, _conf, title = TC.read_type(value(r.get("cover_text")), u, address=value(r.get("document_url")), company=value(r.get("company_name")), version=a.reader)
        return r, t, title, u

    docs, stopped = [], None
    pool = ThreadPoolExecutor(max_workers=max(1, a.workers))
    try:
        for i in range(0, len(rows), 100):
            for r, t, title, u in pool.map(read_one, rows[i:i + 100]):
                usage.update(u)
                docs.append({"library_id": value(r.get("library_id")), "truth": truth[r["_id"]], "stored": value(r.get("source_type_check")),
                             "rule": value(r.get("source_type")), "ours": t or "", "english": value(r.get("doc_language")) == "English",
                             "our_title": title or "", "ai_title": value(r.get("source_title"))})
            print(f"  {len(docs):,} read, ${model_reader.cost(usage):.2f}, {(time.time() - t0) / 60:.0f} min", flush=True)
            if model_reader.cost(usage) >= a.max_usd:
                stopped = f"stopped at the cost cap of ${a.max_usd:.2f}"
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    left_out = collections.Counter({k: v for k, v in left_out.items() if v})
    line = (f"Reader prompt {a.reader}, model {os.environ.get('READER_MODEL') or 'the default'}, documents {a.offset:,} to {a.offset + len(ids):,} of the fixed order. {usage['calls']:,} calls, ${model_reader.cost(usage):.2f}{model_reader.cost_note()}, {(time.time() - t0) / 60:.0f} min."
            + (f" {stopped}." if stopped else ""))
    c = score(docs, checks_of)
    out = report(docs, c, left_out, line)
    open(a.out + ".md", "w", encoding="utf-8").write(out)
    print(out, flush=True)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(out + "\n")
    n = c["English", "n"] or 1
    run.finish("ok", rows_in=len(docs), rows_out=0,
               counters={"read": len(docs), "usd": round(model_reader.cost(usage), 3), "english": c["English", "n"],
                         "english_stored_exact": round(c["English", "stored", "exact"] / n, 3), "english_ours_exact": round(c["English", "ours", "exact"] / n, 3)},
               notes=stopped or "measured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
