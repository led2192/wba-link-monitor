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
        self.assertIn("MAX_BYTES = 200 * 1024 * 1024", js)

    def test_what_is_held_is_not_offered_and_only_reports_are_ticked(self):
        js = read("extension", "send.js")
        self.assertIn('rpc("upload_known"', js)                                           # asked before anything is downloaded
        self.assertIn("box.checked = d.file ? !d.done : (d.sure && looksReport(d) && !d.held)", js)       # a file chosen from the computer is ticked: the person chose it
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

    def test_the_sender_can_say_type_and_year_and_send_a_file_from_the_computer(self):
        js, html = read("extension", "send.js"), read("extension", "send.html")
        self.assertIn('sent_type: doc.kind && doc.kind.value || null, sent_year: year', js)
        self.assertIn('"type: let lanterN decide"', js)                                   # optional: nothing chosen, nothing said
        self.assertIn('id="files" type="file" accept="application/pdf,.pdf" multiple', html)
        self.assertIn('if (doc.file && !looksPdf(buf)) throw new Error("not a PDF");', js)
        self.assertIn('"version": "0.4.3"', read("extension", "manifest.json")); self.assertIn("send-to-lantern 0.4.3", read("extension", "config.js"))

    def test_a_large_file_shows_how_far_it_is(self):
        js = read("extension", "send.js")
        self.assertIn("say(`downloading… ${Math.round(n / 1048576)} MB`)", js)
        self.assertIn("x.upload.onprogress", js)
        self.assertIn("await Promise.all([worker(), worker()]);", js)                    # two at a time, not one behind the other
        self.assertIn("stored in ${secs(Date.now() - got)}", js)                          # the result says where the time went

    def test_one_level_deeper_stays_on_the_site_and_is_bounded(self):
        js = read("extension", "send.js")
        self.assertIn("u.host !== location.host", js)                                      # only pages of the same site are opened
        self.assertIn("func: collectDeeper, args: [40]", js)                               # forty pages at most
        self.assertIn("setTimeout(() => c.abort(), 15000)", js)
        self.assertIn('id="deeper"', read("extension", "send.html"))

    def test_documents_with_no_pdf_in_their_address_are_listed(self):
        js = read("extension", "send.js")
        self.assertIn("static-files", js); self.assertIn("getattachment", js)             # the platforms monitor_core.DOC_ENDPOINTS knows
        self.assertIn('img[src*="pdf" i]', js)                                            # or the PDF icon on the link
        self.assertIn('method: "HEAD"', js)                                               # the site is asked what a doubtful link is, nothing downloaded
        self.assertIn("await confirmMaybes(docs)", js)

    def test_a_company_on_a_two_level_country_domain_is_recognised(self):
        """ana.co.jp is ANA's site, not "co.jp" shared with every Japanese company. Run in node when it is there."""
        import shutil, subprocess
        js = read("extension", "send.js")
        self.assertIn("const SECOND_LEVEL = /^(co|com|org|net|ac|gov|edu|ne|or|go|gob|ltd|plc|nom)$/;", js)
        if not shutil.which("node"):
            self.skipTest("node not installed")
        body = js[js.index("const hostOf ="):js.index("let hostMemory = {};")]
        script = ("const companies=[{id:'A',website:'https://www.ana.co.jp/group/en/'},{id:'J',website:'jal.co.jp'},{id:'T',website:'https://www.tesco.co.uk'},"
                  "{id:'N',website:'nike.com'},{id:'G1',website:'group.example.com'},{id:'G2',website:'unit.example.com'}];" + body +
                  "console.log(JSON.stringify(['ana.co.jp','ir.jal.co.jp','tescoplc.co.uk','investors.nike.com','unit.example.com','www3.example.com','sony.co.jp']"
                  ".map(h => (companyOfHost(h) || {}).id || null)))")
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(out.stdout), ["A", "J", None, "N", "G2", None, None], out.stderr)

    def test_the_site_builds_and_offers_it(self):
        wf = read(".github", "workflows", "search_page.yml")
        self.assertIn("extension/**", wf); self.assertIn("public/send-to-lantern.zip", wf)
        self.assertIn('placeholders left in the extension', wf)
        self.assertIn('href="send-to-lantern.zip"', read("site", "send-to-lantern.html"))


if __name__ == "__main__":
    unittest.main()
