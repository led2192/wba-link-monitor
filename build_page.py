#!/usr/bin/env python3
"""
build_page.py: the pages and the extension as they are served, without their comments.

The source in site/ and extension/ is written to be read: every choice has its reason and its date beside it. A
browser needs none of that, and whoever opens "view source" on the served page should not get the design notes
with it (21 Nov 2026, the owner). This script rewrites the built files in place: inline scripts and .js files lose
their comments and spare whitespace (rjsmin: names are not changed, so an error message still points at a real
name), styles lose their comments, HTML comments go. What a browser runs is the same.

It does not make the page secret: a browser must receive what it runs. The ranking, the reader and the index are
in the database and on the server, which no visitor sees.

    python build_page.py public/search.html public/index.html build/send-to-lantern/send.js ...

Each file is rewritten only if every check passes: the result is not empty, is not larger, and keeps the strings a
page cannot work without (given with --keep). Exit code 1 when a file fails, so the deploy stops.
"""
import argparse, re, sys


def compact_js(text):
    import rjsmin
    return rjsmin.jsmin(text, keep_bang_comments=False)


def compact_css(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(l.strip() for l in text.split("\n") if l.strip())


def compact_html(text):
    """Inline scripts and styles compacted, HTML comments removed, the rest left as it is."""
    def script(m):
        attrs, body = m.group(1), m.group(2)
        if "src=" in attrs or not body.strip() or re.search(r'type="(?!text/javascript|module)', attrs):
            return m.group(0)
        return f"<script{attrs}>{compact_js(body)}</script>"

    def style(m):
        return f"<style{m.group(1)}>{compact_css(m.group(2))}</style>"

    parts = re.split(r"(<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>)", text, flags=re.S | re.I)
    out = []
    for part in parts:
        if part[:7].lower() == "<script":
            out.append(re.sub(r"<script\b([^>]*)>(.*?)</script>", script, part, count=1, flags=re.S | re.I))
        elif part[:6].lower() == "<style":
            out.append(re.sub(r"<style\b([^>]*)>(.*?)</style>", style, part, count=1, flags=re.S | re.I))
        else:
            part = re.sub(r"<!--(?!\[if).*?-->", "", part, flags=re.S)
            out.append("\n".join(l.rstrip() for l in part.split("\n") if l.strip()) + ("\n" if part.endswith("\n") else ""))
    return "".join(out)


def compact(path, text):
    return compact_js(text) if path.endswith(".js") else compact_html(text)


def problems(before, after, keep):
    """Why the compacted text must not replace the source; empty when it may."""
    out = []
    if not after.strip():
        out.append("empty")
    if len(after) > len(before):
        out.append("larger")
    out += [f"lost {k!r}" for k in keep if k in before and k not in after]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--keep", action="append", default=[], help="a string that must survive in any file that had it")
    a = ap.parse_args()
    bad = 0
    for path in a.files:
        before = open(path, encoding="utf-8").read()
        after = compact(path, before)
        why = problems(before, after, a.keep)
        if why:
            bad += 1
            print(f"  {path}: left as it was ({', '.join(why)})")
            continue
        open(path, "w", encoding="utf-8").write(after)
        print(f"  {path}: {len(before):,} -> {len(after):,} characters")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
