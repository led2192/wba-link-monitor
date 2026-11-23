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


_BEFORE_REGEX = set("(,=:[!&|?{};+-*%<>~^")
_WORDS_BEFORE_REGEX = {"return", "typeof", "case", "in", "of", "void", "delete", "do", "else", "throw", "new", "yield", "await"}


def _skip_string(js, i):
    """The index after the quoted string that starts at i."""
    q, i, n = js[i], i + 1, len(js)
    while i < n and js[i] != q:
        if js[i] == "\\":
            i += 1
        elif js[i] == "\n":                     # an unterminated string: stop at the line's end
            break
        i += 1
    return min(i + 1, n)


def _skip_regex(js, i):
    """The index after the regular expression literal that starts at i (its flags are left to the caller's scan)."""
    i, n, cls = i + 1, len(js), False
    while i < n and js[i] != "\n":
        c = js[i]
        if c == "\\":
            i += 1
        elif c == "[":
            cls = True
        elif c == "]":
            cls = False
        elif c == "/" and not cls:
            return i + 1
        i += 1
    return i


def _skip_template(js, i):
    """The index after the template literal that starts at i, with whatever its ${ } hold (code, strings, other templates)."""
    i, n = i + 1, len(js)
    while i < n:
        c = js[i]
        if c == "\\":
            i += 2
        elif c == "`":
            return i + 1
        elif c == "$" and js[i + 1:i + 2] == "{":
            i = _scan(js, i + 2, None, until_brace=True)
        else:
            i += 1
    return n


def _scan(js, i, spans, until_brace=False):
    """Walks code from i. Top-level template literals are added to `spans` as (start, end). With until_brace, stops
    after the } that closes the ${ the walk started in and returns that index."""
    n, depth, last, word = len(js), 0, "", ""
    while i < n:
        c = js[i]
        if c == "/" and js[i + 1:i + 2] == "/":
            j = js.find("\n", i); i = n if j < 0 else j
            continue
        if c == "/" and js[i + 1:i + 2] == "*":
            j = js.find("*/", i + 2); i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            i = _skip_string(js, i); last, word = '"', ""
            continue
        if c == "`":
            j = _skip_template(js, i)
            if spans is not None:
                spans.append((i, j))
            i, last, word = j, "`", ""
            continue
        if c == "/" and (last == "" or last in _BEFORE_REGEX or word in _WORDS_BEFORE_REGEX):
            i = _skip_regex(js, i); last, word = "/", ""
            continue
        if until_brace:
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return i + 1
                depth -= 1
        if c.isalnum() or c in "_$":
            word = word + c if (last.isalnum() or last in "_$") and last != "" else c
            last = c
        elif not c.isspace():
            last, word = c, ""
        elif word and last != " ":
            last = last                                  # white space ends nothing by itself: "return /x/" keeps its word
        i += 1
    return n


def template_spans(js):
    """[(start, end)] of the template literals that are not inside another one."""
    spans = []
    _scan(js, 0, spans)
    return spans


def compact_js(text):
    """Comments and spare white space out (rjsmin). Template literals are set aside first and put back untouched:
    rjsmin reads a template inside another template's ${ } as code and removes its spaces. On 22 Nov 2026 the served
    page read "1,245terms, 712norms" and "in the 2025Integrated Report,page 95" where the source had the spaces."""
    import rjsmin
    spans = template_spans(text)
    held, parts, at = [], [], 0
    for a, b in spans:
        parts.append(text[at:a]); parts.append(f'"\x01TPL{len(held)}\x01"'); held.append(text[a:b]); at = b
    parts.append(text[at:])
    out = rjsmin.jsmin("".join(parts), keep_bang_comments=False)
    for n, tpl in enumerate(held):
        mark = f'"\x01TPL{n}\x01"'
        if out.count(mark) != 1:
            raise ValueError(f"template literal {n} was not kept once by the compactor")
        out = out.replace(mark, tpl)
    return out


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
