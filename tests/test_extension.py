import json, os, re, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
read = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()


class Extension(unittest.TestCase):
    def test_manifest_names_files_that_exist(self):
        m = json.loads(read("extension", "manifest.json"))
        self.assertEqual(m["manifest_version"], 3)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "extension", m["background"]["service_worker"])))
        for f in re.findall(r'(?:src|href)="([^"#]+)"', read("extension", "send.html")):
            self.assertTrue(os.path.exists(os.path.join(ROOT, "extension", f)), f)
        self.assertIn(m["version"], read("extension", "config.js"))                       # the client name carries the version

    def test_no_secret_and_no_inline_script(self):
        conf = read("extension", "config.js")
        self.assertIn('"__SUPABASE_URL__"', conf); self.assertIn('"__SUPABASE_PUBLISHABLE_KEY__"', conf)   # filled in when the site is published
        for f in os.listdir(os.path.join(ROOT, "extension")):
            self.assertNotRegex(read("extension", f), r"eyJ[A-Za-z0-9_-]{20,}|service_role|sb_secret_")
        self.assertNotRegex(read("extension", "send.html"), r"<script(?![^>]*\bsrc=)")    # an extension page may not run inline script

    def test_it_registers_before_it_stores_and_checks_the_file(self):
        js = read("extension", "send.js")
        self.assertLess(js.index('rpc("upload_begin"'), js.index("/storage/v1/object/uploads/"))
        self.assertLess(js.index("/storage/v1/object/uploads/"), js.index('rpc("upload_done"'))
        self.assertIn('head.includes("%PDF-")', js)                                       # what is not a PDF is not sent
        self.assertIn("MAX_BYTES = 50 * 1024 * 1024", js)

    def test_what_is_held_is_not_offered_and_only_reports_are_ticked(self):
        js = read("extension", "send.js")
        self.assertIn('rpc("upload_known"', js)                                           # asked before anything is downloaded
        self.assertIn("box.checked = d.sure && looksReport(d) && !d.held", js)
        m = re.search(r"const REPORT = /(.+)/i;", js); rx = re.compile(m.group(1), re.I)
        for yes in ("Annual report 2024", "memoria anual 2023", "Informe de sostenibilidad", "Code of Conduct", "Política de derechos humanos"):
            self.assertTrue(rx.search(yes), yes)
        m2 = re.search(r"const NOT_REPORT = /(.+)/i;", js); nx = re.compile(m2.group(1), re.I)
        ticked = lambda t: bool(rx.search(t)) and not nx.search(t)
        for yes in ("Vitol GHG Methodology 2025 Vitol GHG Emissions Calculation Methodology 2025", "Modern Slavery Policy Statement 2025/26", "Human rights policy"):
            self.assertTrue(ticked(yes), yes)
        for no in ("¡Look Felin e ideas para regalar! document es revista noviembre 2026 pdf", "Ticket Digital CondicionesTicketDigital pdf",
                   "Colombia privacy policy 05 11 2025 Politica de Tratamiento de Datos Colombia 2025", "Vitol in Africa Vitol in Africa Brochure ENG", "Vitol Metals GTCs"):
            self.assertFalse(ticked(no), no)                                              # a retailer's home page and a trader's archive, 11 Nov 2026
        self.assertIn('store.set("hosts", hostMemory)', js)                               # a site sent from before brings its company

    def test_one_level_deeper_stays_on_the_site_and_is_bounded(self):
        js = read("extension", "send.js")
        self.assertIn("u.host !== location.host", js)                                      # only pages of the same site are opened
        self.assertIn("func: collectDeeper, args: [40]", js)                               # forty pages at most
        self.assertIn("setTimeout(() => c.abort(), 15000)", js)
        self.assertIn('id="deeper"', read("extension", "send.html"))

    def test_the_site_builds_and_offers_it(self):
        wf = read(".github", "workflows", "search_page.yml")
        self.assertIn("extension/**", wf); self.assertIn("public/send-to-lantern.zip", wf)
        self.assertIn('placeholders left in the extension', wf)
        self.assertIn('href="send-to-lantern.zip"', read("site", "send-to-lantern.html"))


if __name__ == "__main__":
    unittest.main()
