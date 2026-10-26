#!/usr/bin/env python3
"""
resolve_doc_conflicts.py: keep open pull requests mergeable when the only thing that collides is documentation.

Almost every pull request adds a line at the top of docs/CHANGELOG.md, and many append to docs/pitfalls.md or
docs/decisions.md or add a name to the re-run list. So merging one makes the next show "This branch has
conflicts", although nothing in the code collides, and the owner had to wait for a person or Claude to fix it
(owner's complaint, 26 Oct 2026). This runs in GitHub after every push to main: for each open pull request
from this repository it merges main into the branch; when the only conflicts are in docs/*.md or in the
workflow-name list of .github/workflows/rerun_failed.yml, it keeps both sides (in that order, each line once
in the list) and pushes. Any other conflict is left alone and noted on the pull request, once.

It never merges a pull request into main and never touches code conflicts.
Env: GH_TOKEN (PR_UPDATE_TOKEN when set, else the workflow's own token), GITHUB_REPOSITORY.
"""
import json, os, re, subprocess, sys, time

DOC = re.compile(r"^docs/.+\.md$")
LISTS = {".github/workflows/rerun_failed.yml"}
MARK = re.compile(r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", re.S)


def keep_both(path, text):
    """Resolve every conflict block of one file by keeping both sides."""
    def one(m):
        a, b = m.group(1), m.group(2)
        if path in LISTS:
            seen, out = set(), []
            for line in (a + b).splitlines(True):
                if line not in seen:
                    seen.add(line); out.append(line)
            return "".join(out)
        sep = "" if (a.endswith("\n\n") or b.startswith("\n") or path.endswith("CHANGELOG.md")) else "\n"
        return a + sep + b
    return MARK.sub(one, text)


def resolvable(paths):
    return bool(paths) and all(DOC.match(p) or p in LISTS for p in paths)


def sh(*args, check=True):
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr[-500:]}")
    return r


def gh(path, method="GET", body=None):
    args = ["gh", "api", "-X", method, path]
    if body is not None:
        args += ["--input", "-"]
    r = subprocess.run(args, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    return json.loads(r.stdout or "null")


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    sh("git", "config", "user.name", "github-actions[bot]")
    sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    prs = gh(f"repos/{repo}/pulls?state=open&per_page=50") or []
    for pr in prs:
        n, head = pr["number"], pr["head"]
        if head["repo"] is None or head["repo"]["full_name"] != repo:
            continue                                     # never push to forks
        state = None
        for _ in range(6):                               # GitHub computes mergeability lazily
            state = (gh(f"repos/{repo}/pulls/{n}") or {}).get("mergeable_state")
            if state != "unknown":
                break
            time.sleep(10)
        if state != "dirty":
            print(f"#{n}: {state}, nothing to do")
            continue
        br = head["ref"]
        sh("git", "fetch", "-q", "origin", br, "main")
        sh("git", "checkout", "-q", "-B", br, f"origin/{br}")
        r = sh("git", "merge", "--no-ff", "-m", "Merge main (documentation conflicts kept from both sides)", "origin/main", check=False)
        if r.returncode == 0:
            ok = sh("git", "push", "-q", "origin", br, check=False).returncode == 0
            print(f"#{n}: merged main without conflicts, {'pushed' if ok else 'push refused'}")
            continue
        paths = sh("git", "diff", "--name-only", "--diff-filter=U").stdout.split()
        if not resolvable(paths):
            sh("git", "merge", "--abort", check=False)
            print(f"#{n}: conflicts outside documentation ({', '.join(paths)}), left alone")
            comments = gh(f"repos/{repo}/issues/{n}/comments?per_page=100") or []
            if not any("conflicts outside documentation" in (c.get("body") or "") for c in comments):
                gh(f"repos/{repo}/issues/{n}/comments", "POST",
                   {"body": "Merging main into this branch gives conflicts outside documentation ("
                            + ", ".join(f"`{p}`" for p in paths) + "). They need a person or Claude to resolve."})
            continue
        for p in paths:
            with open(p) as fh:
                text = fh.read()
            with open(p, "w") as fh:
                fh.write(keep_both(p, text))
            sh("git", "add", p)
        sh("git", "commit", "-q", "--no-edit")
        r = sh("git", "push", "-q", "origin", br, check=False)
        if r.returncode:
            # the built-in token may not push a merge that brings a changed workflow file (needs PR_UPDATE_TOKEN)
            print(f"#{n}: resolved but the push was refused: {r.stderr.strip()[-200:]}")
            continue
        print(f"#{n}: documentation conflicts kept from both sides ({', '.join(paths)}), pushed")


if __name__ == "__main__":
    sys.exit(main())
