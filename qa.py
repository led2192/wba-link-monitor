#!/usr/bin/env python3
"""
qa.py — the pipeline's quality log, shared by every script.

Two things every script can record in the Pipeline QA base (a separate Airtable base, never the
Disclosure Library): one `runs` row per execution, and one `failures` row per DISTINCT failure
it can name (code + deterministic key), never per occurrence. A failure seen every night is one
row whose occurrences, last_seen and last_commit move; a key not seen for 30 days is closed as
resolved_silently by close_silent() (qa_sync.py calls it nightly).

Usage inside a script:

    import qa
    run = qa.Run("airtable_monitor.py")          # workflow, run id, commit come from GITHUB_* env
    ...
    run.failure("PAGE_UNREACHABLE", "timeout", key=url, wba_id=wba, page_url=url,
                message="read timeout after 20s", stage="monitor")
    ...
    run.finish("ok", rows_in=len(due), rows_out=len(updates),
               counters={"changed": changed, "alerted": alerted, "failed": failed})

Rules of the module:
  - Writes happen only when QA_COMMIT=true (the workflows set it). Otherwise finish() prints the
    summary and writes nothing: dry run by default, like every script in this repository.
  - Inert without a token: no AIRTABLE_QA_TOKEN -> nothing is written, one line says so.
  - Never raises into the host script. A failure of the QA log is printed and swallowed, because
    the QA log must never kill a run. Set QA_STRICT=true (the tests do) to raise instead.
  - Codes are a closed list (CODES). An unknown code is a programming error: raised under
    QA_STRICT, otherwise printed once and dropped.

Env: AIRTABLE_QA_TOKEN, AIRTABLE_QA_BASE (default appJQ2mRtjHjzLq9E), QA_COMMIT, QA_STRICT,
     GITHUB_WORKFLOW, GITHUB_RUN_ID, GITHUB_SHA, GITHUB_EVENT_NAME, GITHUB_SERVER_URL,
     GITHUB_REPOSITORY (all optional; set by GitHub Actions).
"""
import collections, datetime as dt, hashlib, json, os, sys, time, traceback
from urllib.parse import quote

API = "https://api.airtable.com/v0"
QA_BASE_DEFAULT = "appJQ2mRtjHjzLq9E"
T_RUNS, T_FAILURES, T_LABELS, T_EVALS = "runs", "failures", "labels", "evals"

CODES = (
    "PAGE_UNREACHABLE", "PAGE_GONE", "PAGE_BLOCKED", "PAGE_EMPTY", "PAGE_UNHARVESTED", "PAGE_WRONG_TYPE",
    "DOC_FETCH_FAIL", "DOC_UNREADABLE", "DOC_DROPPED", "DOC_DUPLICATE", "DOC_WRONG_ROW",
    "TYPE_DISAGREE", "TYPE_MISSING", "LABEL_CORRECTION", "DOC_FLAGGED",
    "CO_GAP", "CO_MISSED_KNOWN", "RUN_FAILED",
)
SEVERITIES = ("High", "Medium", "Low")
# Default severity per code when the caller does not say; the rule in the design doc refines it
# per case (year, family, domain) and the caller passes severity= explicitly for those.
DEFAULT_SEVERITY = {
    "PAGE_UNREACHABLE": "Medium", "PAGE_GONE": "Medium", "PAGE_BLOCKED": "High", "PAGE_EMPTY": "Medium",
    "PAGE_UNHARVESTED": "High", "PAGE_WRONG_TYPE": "Medium",
    "DOC_FETCH_FAIL": "Medium", "DOC_UNREADABLE": "Medium", "DOC_DROPPED": "High",
    "DOC_DUPLICATE": "Low", "DOC_WRONG_ROW": "Medium",
    "TYPE_DISAGREE": "High", "TYPE_MISSING": "High", "LABEL_CORRECTION": "High",
    "DOC_FLAGGED": "High",
    "CO_GAP": "Medium", "CO_MISSED_KNOWN": "High", "RUN_FAILED": "High",
}
# Context fields a failure may carry (anything else is dropped with a strict-mode error).
FAILURE_FIELDS = ("wba_id", "company_name", "page_url", "document_url", "library_id", "record_id",
                  "http_status", "message", "stage")
LABEL_FIELDS = ("target", "field", "stratum", "company_name", "wba_id", "doc_label", "document_url",
                "page_url", "library_id", "record_id", "cover_snippet", "cover_english", "doc_language",
                "robot_value", "ai_value", "suggested_value", "verdict", "true_type", "true_value",
                "sure", "reason", "labelled_by", "labelled_on", "batch_date", "in_review", "reviewed",
                "split", "failure", "year_value", "year_evidence", "suggested_year", "true_year",
                "title_value", "suggested_title", "title_wrong", "hidden_values", "your_call",
                "claude_confidence", "claude_evidence", "check_reason")
MESSAGE_CHARS = 500
WRITE_BATCH = 10
WRITE_PAUSE = 0.2
STRICT = os.environ.get("QA_STRICT", "").lower() in ("true", "1", "yes")


def key(code, *parts):
    """Deterministic failure_key: sha1 of the code and the parts that identify the failure."""
    raw = "|".join([code] + [str(p or "").strip().lower() for p in parts])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


_key = key   # the module function, kept reachable inside methods whose parameter is also named `key`


def _today():
    return dt.date.today().isoformat()


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def github_context():
    """Workflow name, run id, commit and run URL from the GitHub Actions environment, or blanks."""
    repo, rid = _env("GITHUB_REPOSITORY"), _env("GITHUB_RUN_ID")
    server = _env("GITHUB_SERVER_URL", "https://github.com")
    return {
        "workflow": _env("GITHUB_WORKFLOW"),
        "run_id": rid,
        "commit": _env("GITHUB_SHA")[:7],
        "event": _env("GITHUB_EVENT_NAME"),
        "url": f"{server}/{repo}/actions/runs/{rid}" if repo and rid else "",
    }


def trigger_from_event(event, script=""):
    if event == "schedule":
        return "schedule"
    if event == "workflow_run":
        return "chain"
    if event == "workflow_dispatch":
        return "dispatch"
    if script == "scan_now.py" or _env("QA_TRIGGER") == "scan_now":
        return "scan_now"
    return "manual"


def _request(method, url, headers, payload=None, params=None):
    """Airtable call with the repository's retry ladder (monitor_core.airtable_request)."""
    from monitor_core import airtable_request
    return airtable_request(method, url, headers, payload=payload, params=params)


class Run:
    """One script execution. Collects failures in memory; writes everything at finish()."""

    def __init__(self, script, workflow=None, trigger=None, commit=None, base=None, token=None):
        gh = github_context()
        self.script = script
        self.workflow = workflow or gh["workflow"] or script
        self.run_id = gh["run_id"] or dt.datetime.now().strftime("%Y%m%d%H%M%S")
        self.run_key = f"{self.workflow}#{self.run_id}"
        part = (os.environ.get("QA_RUN_PART") or "").strip()
        if part:                       # one job running a script several times (pc_fetch --repeat-hours)
            self.run_key += f".{part}"
        self.trigger = trigger or trigger_from_event(gh["event"], script)
        self.commit_sha = commit or gh["commit"]
        self.github_url = gh["url"]
        self.started = _now()
        self.t0 = time.time()
        self.token = token if token is not None else _env("AIRTABLE_QA_TOKEN")
        self.base = base or _env("AIRTABLE_QA_BASE", QA_BASE_DEFAULT)
        self.do_commit = _env("QA_COMMIT").lower() in ("true", "1", "yes")
        self._failures = {}            # failure_key -> dict of fields
        self._labels = {}              # label_id -> dict of fields
        self._bad_codes = set()
        self._run_record_id = None
        self.counts = collections.Counter()

    # ---- collecting ---------------------------------------------------------------------------
    def failure(self, code, detail, key=None, severity=None, **fields):
        """Record one failure. `key` is the string that identifies it (page URL, library_id,
        domain, wba_id + detail ...); when omitted the strongest context field is used."""
        if code not in CODES:
            if STRICT:
                raise ValueError(f"qa: unknown failure code {code!r}")
            if code not in self._bad_codes:
                self._bad_codes.add(code)
                print(f">>> qa: unknown failure code {code!r}; dropped.", flush=True)
            return None
        unknown = [k for k in fields if k not in FAILURE_FIELDS]
        if unknown:
            if STRICT:
                raise ValueError(f"qa: unknown failure fields {unknown}")
            for k in unknown:
                fields.pop(k)
        if key is None:
            key = (fields.get("page_url") or fields.get("library_id") or fields.get("document_url")
                   or fields.get("wba_id") or "")
        fk = _key(code, key, detail if code in ("CO_GAP", "LABEL_CORRECTION") else "")
        sev = severity or DEFAULT_SEVERITY[code]
        if sev not in SEVERITIES:
            if STRICT:
                raise ValueError(f"qa: bad severity {sev!r}")
            sev = DEFAULT_SEVERITY[code]
        row = {"failure_key": fk, "code": code, "detail": (detail or "")[:100], "severity": sev,
               "script": self.script}
        for k, v in fields.items():
            if v in (None, ""):
                continue
            if k == "message":
                v = str(v)[:MESSAGE_CHARS]
            row[k] = v
        if fk not in self._failures:
            self.counts[code] += 1
        self._failures[fk] = row
        return fk

    def label(self, label_id, **fields):
        """Record one label row (golden set). Written only if no row with this label_id exists:
        a person's label is never overwritten by the robot."""
        unknown = [k for k in fields if k not in LABEL_FIELDS]
        if unknown:
            if STRICT:
                raise ValueError(f"qa: unknown label fields {unknown}")
            for k in unknown:
                fields.pop(k)
        row = {"label_id": label_id}
        row.update({k: v for k, v in fields.items() if v not in (None, "")})
        self._labels[label_id] = row
        return label_id

    # ---- writing ------------------------------------------------------------------------------
    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _url(self, table):
        return f"{API}/{self.base}/{quote(table)}"

    def _get_all(self, table, fields, formula=None):
        params = [("pageSize", "100")] + [("fields[]", f) for f in fields]
        if formula:
            params.append(("filterByFormula", formula))
        out = []; offset = None
        while True:
            p = list(params) + ([("offset", offset)] if offset else [])
            j = _request("GET", self._url(table), self._headers(), params=p).json()
            out.extend(j.get("records", []))
            offset = j.get("offset")
            if not offset:
                break
            time.sleep(0.12)
        return out

    def _write(self, table, records, method):
        """POST creates, PATCH updates (records carry 'id'). Returns the records Airtable sent back."""
        out = []
        for i in range(0, len(records), WRITE_BATCH):
            r = _request(method, self._url(table), self._headers(),
                         {"records": records[i:i + WRITE_BATCH], "typecast": True})
            try:
                out.extend(r.json().get("records", []))
            except Exception:
                pass
            time.sleep(WRITE_PAUSE)
        return out

    def _existing_by_key(self, table, key_field, keys, fields):
        """{key: record} for the keys already in the table, fetched in OR() chunks."""
        found = {}
        keys = [k for k in keys if k]
        for i in range(0, len(keys), 40):
            chunk = keys[i:i + 40]
            formula = "OR(" + ",".join("{%s}='%s'" % (key_field, k.replace("'", "\\'")) for k in chunk) + ")"
            for rec in self._get_all(table, fields, formula):
                k = rec.get("fields", {}).get(key_field)
                if k:
                    found[k] = rec
        return found

    def _write_run(self, status, rows_in, rows_out, counters, notes):
        fields = {"run_key": self.run_key, "script": self.script, "workflow": self.workflow,
                  "trigger": self.trigger, "started": self.started, "finished": _now(),
                  "duration_min": round((time.time() - self.t0) / 60, 1), "status": status,
                  "failures_recorded": len(self._failures)}
        if self.commit_sha:
            fields["commit_sha"] = self.commit_sha
        if self.github_url:
            fields["github_url"] = self.github_url
        if rows_in is not None:
            fields["rows_in"] = rows_in
        if rows_out is not None:
            fields["rows_out"] = rows_out
        if counters:
            fields["counters"] = json.dumps(counters, ensure_ascii=False, default=str)[:90000]
        if notes:
            fields["notes"] = str(notes)[:90000]
        existing = self._existing_by_key(T_RUNS, "run_key", [self.run_key], ["run_key"])
        if self.run_key in existing:
            rid = existing[self.run_key]["id"]
            self._write(T_RUNS, [{"id": rid, "fields": fields}], "PATCH")
        else:
            made = self._write(T_RUNS, [{"fields": fields}], "POST")
            rid = made[0]["id"] if made else None
        self._run_record_id = rid
        return rid

    def _write_failures(self):
        if not self._failures:
            return 0, 0
        today = _today()
        existing = self._existing_by_key(
            T_FAILURES, "failure_key", list(self._failures),
            ["failure_key", "status", "occurrences", "first_seen", "first_commit", "review_note"])
        to_create, to_update = [], []
        for fk, row in self._failures.items():
            prev = existing.get(fk)
            if prev:
                pf = prev.get("fields", {})
                upd = dict(row)
                upd.pop("failure_key", None)
                upd["last_seen"] = today
                upd["occurrences"] = int(pf.get("occurrences") or 0) + 1
                if self.commit_sha:
                    upd["last_commit"] = self.commit_sha
                if self._run_record_id:
                    upd["last_run"] = [self._run_record_id]
                # a failure marked fixed or silently resolved that recurs is open again
                prev_status = pf.get("status") or ""
                if prev_status in ("fixed", "resolved_silently"):
                    upd["status"] = "open"
                    upd["review_note"] = (f"{today}: recurred after being marked {prev_status}.\n"
                                          + (pf.get("review_note") or ""))[:90000]
                to_update.append({"id": prev["id"], "fields": upd})
            else:
                new = dict(row)
                new.update({"status": "open", "first_seen": today, "last_seen": today, "occurrences": 1})
                if self.commit_sha:
                    new["first_commit"] = self.commit_sha
                    new["last_commit"] = self.commit_sha
                if self._run_record_id:
                    new["last_run"] = [self._run_record_id]
                to_create.append({"fields": new})
        self._write(T_FAILURES, to_create, "POST")
        self._write(T_FAILURES, to_update, "PATCH")
        return len(to_create), len(to_update)

    def _write_labels(self):
        if not self._labels:
            return 0
        existing = self._existing_by_key(T_LABELS, "label_id", list(self._labels), ["label_id"])
        to_create = [{"fields": row} for lid, row in self._labels.items() if lid not in existing]
        self._write(T_LABELS, to_create, "POST")
        return len(to_create)

    def finish(self, status="ok", rows_in=None, rows_out=None, counters=None, notes=None):
        """Write the run row, then the failures and labels. Prints a summary either way."""
        self._print_summary(status, counters)
        if not self.do_commit:
            print(f">>> qa: dry run, nothing written ({len(self._failures)} failure rows, "
                  f"{len(self._labels)} label rows collected). Set QA_COMMIT=true to write.", flush=True)
            return None
        if not self.token:
            print(">>> qa: AIRTABLE_QA_TOKEN not set; nothing written.", flush=True)
            return None
        try:
            rid = self._write_run(status, rows_in, rows_out, counters, notes)
            created, updated = self._write_failures()
            labels = self._write_labels()
            print(f">>> qa: run {self.run_key} written ({status}); failures {created} new, {updated} updated; "
                  f"labels {labels} new.", flush=True)
            return rid
        except Exception as e:
            if STRICT:
                raise
            print(f">>> qa: could not write the quality log ({type(e).__name__}: {e}); the run itself is "
                  f"unaffected.", flush=True)
            traceback.print_exc()
            return None

    def close_silent(self, days=30, script=None):
        """Close open failures (optionally only those a given script writes) whose last_seen is
        older than `days`: status resolved_silently, with a dated note. Returns the count."""
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        clauses = ["{status}='open'", "IS_BEFORE({last_seen}, '%s')" % cutoff]
        if script:
            clauses.append("{script}='%s'" % script.replace("'", ""))
        formula = "AND(" + ", ".join(clauses) + ")"
        rows = self._get_all(T_FAILURES, ["failure_key", "last_seen", "review_note", "last_commit"], formula)
        if not rows:
            return 0
        if not self.do_commit:
            print(f">>> qa: dry run, {len(rows)} open failures silent for {days}+ days would be closed.", flush=True)
            return len(rows)
        today = _today()
        ups = []
        for r in rows:
            f = r.get("fields", {})
            note = (f"{today}: closed, not seen since {f.get('last_seen')} (last code {f.get('last_commit') or '?'}).\n"
                    + (f.get("review_note") or ""))[:90000]
            ups.append({"id": r["id"], "fields": {"status": "resolved_silently", "review_note": note}})
        self._write(T_FAILURES, ups, "PATCH")
        print(f">>> qa: closed {len(ups)} failures silent for {days}+ days.", flush=True)
        return len(ups)

    # ---- reporting ----------------------------------------------------------------------------
    def summary_markdown(self, status="ok", counters=None, title=None):
        """The run's picture as markdown: rows per code and severity, labels, counters.
        Written to GITHUB_STEP_SUMMARY and posted on pull requests by the workflows."""
        by = collections.defaultdict(collections.Counter)
        for r in self._failures.values():
            by[r["code"]][r["severity"]] += 1
        lines = [f"## {title or self.script}: {status}, {'dry run' if not self.do_commit else 'committed'}",
                 "", f"Run `{self.run_key}`, commit `{self.commit_sha or '?'}`, "
                 f"{len(self._failures):,} distinct failures, {len(self._labels):,} labels.", "",
                 "| code | High | Medium | Low | total |", "| --- | ---: | ---: | ---: | ---: |"]
        for code in sorted(by, key=lambda c: -sum(by[c].values())):
            c = by[code]
            lines.append(f"| {code} | {c['High']:,} | {c['Medium']:,} | {c['Low']:,} | {sum(c.values()):,} |")
        tot = collections.Counter()
        for c in by.values():
            tot.update(c)
        lines.append(f"| **total** | {tot['High']:,} | {tot['Medium']:,} | {tot['Low']:,} | {sum(tot.values()):,} |")
        if counters:
            lines += ["", "Counters:", "", "```", json.dumps(counters, ensure_ascii=False, indent=1, default=str)[:6000], "```"]
        return "\n".join(lines)

    def write_step_summary(self, status="ok", counters=None, title=None, path=None):
        """Append the markdown summary to GITHUB_STEP_SUMMARY (when set) and to `path` (when given)."""
        md = self.summary_markdown(status, counters, title)
        for target in (_env("GITHUB_STEP_SUMMARY"), path or ""):
            if target:
                try:
                    with open(target, "a", encoding="utf-8") as fh:
                        fh.write(md + "\n")
                except Exception as e:
                    print(f">>> qa: could not write the summary to {target} ({e})", flush=True)
        return md

    def _print_summary(self, status, counters):
        by_sev = collections.Counter(r["severity"] for r in self._failures.values())
        print(f">>> qa: {self.script} [{self.run_key}] {status}; {len(self._failures)} distinct failures "
              f"({', '.join(f'{k} {v}' for k, v in sorted(by_sev.items())) or 'none'}); "
              f"{len(self._labels)} labels.", flush=True)
        for code, n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            print(f"      {code}: {n}", flush=True)
        if counters:
            print(f"      counters: {json.dumps(counters, ensure_ascii=False, default=str)[:600]}", flush=True)


def record_exception(script, exc, stage="run", **fields):
    """One-liner for a script's last-resort except: records RUN_FAILED and finishes the run as failed.
    Never raises."""
    try:
        run = Run(script)
        run.failure("RUN_FAILED", "job_failed", key=f"{run.workflow}|{_today()}", stage=stage,
                    message=f"{type(exc).__name__}: {exc}", **fields)
        run.finish("failed")
    except Exception:
        pass
