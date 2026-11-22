"""The served pages lose their comments and nothing a browser needs (build_page.py)."""
import os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build_page as B

try:
    import rjsmin  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False


@unittest.skipUnless(HAVE, "rjsmin is not installed")
class Compact(unittest.TestCase):
    def test_the_search_page_keeps_what_it_runs_and_loses_its_notes(self):
        src = open(os.path.join(ROOT, "site", "search.html"), encoding="utf-8").read()
        out = B.compact("search.html", src)
        self.assertLess(len(out), len(src) * 0.9)
        self.assertEqual(B.problems(src, out, ["lantern v", "rpc/link_enter", "__SUPABASE_URL__"]), [])
        self.assertNotIn("// v1", out); self.assertNotIn("<!--", out); self.assertNotIn("/*", out.split("</style>")[0])
        self.assertIn("https://lantrna.com/#k=", out)                    # "//" inside a string is not a comment
        self.assertEqual(out.count("<script"), src.count("<script")); self.assertEqual(out.count("</style>"), src.count("</style>"))

    def test_every_page_and_the_extension_compact_cleanly(self):
        for folder, names in (("site", [n for n in os.listdir(os.path.join(ROOT, "site")) if n.endswith(".html")]),
                              ("extension", [n for n in os.listdir(os.path.join(ROOT, "extension")) if n.endswith((".js", ".html"))])):
            for n in names:
                src = open(os.path.join(ROOT, folder, n), encoding="utf-8").read()
                out = B.compact(n, src)
                self.assertTrue(out.strip(), n); self.assertLessEqual(len(out), len(src), n)

    def test_small_cases(self):
        self.assertEqual(B.compact_js("// why\nconst a = 1; /* note */ const u = 'http://x//y'; const r = /\\/\\//;\n"),
                         "const a=1;const u='http://x//y';const r=/\\/\\//;")
        self.assertEqual(B.compact_css("a { color: red; } /* why */\n\n  b { }"), "a { color: red; }\nb { }")
        html = '<p>x</p>\n<!-- note -->\n<script src="a.js"></script>\n<script>\n// c\nlet a = 1;\n</script>'
        self.assertEqual(B.compact_html(html), '<p>x</p>\n<script src="a.js"></script>\n<script>let a=1;</script>')

    def test_a_result_that_lost_something_is_not_written(self):
        self.assertEqual(B.problems("abc KEEP", "ab", ["KEEP"]), ["lost 'KEEP'"])
        self.assertEqual(B.problems("abc", "abcd", []), ["larger"]); self.assertEqual(B.problems("abc", "abc", []), [])
        self.assertEqual(B.problems("abc", " ", []), ["empty"])


if __name__ == "__main__":
    unittest.main()


class TemplatesKept(unittest.TestCase):
    """22 Nov 2026: the served page read "1,245terms, 712norms" and "in the 2025Integrated Report,page 95". rjsmin
    reads a template literal inside another one's ${ } as code and removes its spaces."""
    def test_a_template_inside_a_template_keeps_its_spaces(self):
        import build_page as B
        src = 'const s = `It shows ${[1, 2].map(x => `${x} ${x}s`).join(", ")}${k ? `; loaded ${k}` : ""}. Below`;   // a `tick` in a comment\n'
        out = B.compact_js(src)
        self.assertIn('`It shows ${[1, 2].map(x => `${x} ${x}s`).join(", ")}${k ? `; loaded ${k}` : ""}. Below`', out)
        self.assertNotIn("comment", out); self.assertNotIn("\x01", out)

    def test_ticks_and_quotes_in_a_regular_expression_a_string_or_a_comment_start_no_template(self):
        import build_page as B
        src = "const r = /[`\"']/g, d = a / b / c; /* a ` here */ const t = `x ${ {a: `}`}.a }  y`; f(/`/.test(s) ? `a  b` : \"c`d\");"
        self.assertEqual([src[a:b] for a, b in B.template_spans(src)], ["`x ${ {a: `}`}.a }  y`", "`a  b`"])
        out = B.compact_js(src)
        for kept in ("/[`\"']/g", "d=a/b/c", "`x ${ {a: `}`}.a }  y`", "`a  b`", '"c`d"'):
            self.assertIn(kept, out)

    def test_every_template_of_the_search_page_is_served_as_written(self):
        import re, build_page as B
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        page = open(os.path.join(root, "site", "search.html"), encoding="utf-8").read()
        script = max(re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S), key=len)
        spans = B.template_spans(script); out = B.compact_js(script)
        self.assertGreater(len(spans), 300)
        for a, b in spans:
            self.assertIn(script[a:b], out)
        self.assertLess(len(out), len(script) * 0.9); self.assertNotIn("\x01", out)
