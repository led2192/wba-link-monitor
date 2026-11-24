// Send to lantrNa: the sending window. It lists the PDF links of the tab it was opened for, downloads the ticked
// ones with this browser (the site sees an ordinary visit, with the person's own session) and stores each in the
// evidence database's private bucket, registered first with public.upload_begin (who, company, where from, size,
// SHA-256). browser_uploads.py turns them into rows of the library. Nothing is sent anywhere else.
"use strict";
const MAX_BYTES = 300 * 1024 * 1024;      // as the database, the storage and the text reader (50 until 0.4.1, 150 until 0.4.3, 200 until 0.6.3)
const args = new URLSearchParams(location.search);
const TAB = { id: Number(args.get("tab")), url: args.get("url") || "", title: args.get("title") || "" };
const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
let conf = CONFIG, companies = [], docs = [];

// ---- storage of the session (this extension's own sign-in, apart from the search page's) -----------------------
const store = {
  get: async k => (await chrome.storage.local.get(k))[k] || null,
  set: (k, v) => v == null ? chrome.storage.local.remove(k) : chrome.storage.local.set({ [k]: v }),
};
async function authCall(path, body) {
  const r = await fetch(`${conf.url}/auth/v1/${path}`, { method: "POST", headers: { apikey: conf.key, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.msg || j.error_description || j.message || `HTTP ${r.status}`);
  return j;
}
async function token() {
  let s = await store.get("session"); if (!s) return null;
  if (s.expires_at && s.expires_at * 1000 < Date.now() + 60000) {
    try { const j = await authCall("token?grant_type=refresh_token", { refresh_token: s.refresh_token }); s = { access_token: j.access_token, refresh_token: j.refresh_token, expires_at: j.expires_at, email: s.email }; await store.set("session", s); }
    catch (e) { await store.set("session", null); return null; }
  }
  return s.access_token;
}
// A busy database drops a call now and then ("Failed to fetch", a statement timeout): each call is tried three times.
const pause = ms => new Promise(r => setTimeout(r, ms));
async function rpc(fn, body) {
  let last;
  for (let i = 0; i < 3; i++) {
    try { return await rpcOnce(fn, body); }
    catch (e) { last = e; if (!/Failed to fetch|timeout|HTTP 5\d\d|NetworkError/i.test(e.message)) throw e; await pause(2000 * (i + 1)); }
  }
  throw new Error(/Failed to fetch/.test(last.message) ? "lantrNa's database did not answer (busy): try again in a few minutes" : last.message);
}
async function rpcOnce(fn, body) {
  const t = await token(); if (!t) throw new Error("sign in again");
  const r = await fetch(`${conf.url}/rest/v1/rpc/${fn}`, { method: "POST", headers: { apikey: conf.key, Authorization: `Bearer ${t}`, "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
  return j;
}

// ---- the page's documents -----------------------------------------------------------------------------------------
const isPdfUrl = u => /\.pdf($|[?#])/i.test(u);
// Runs inside the tab: every link that is, or may be, a PDF. Kept free of outer variables (it is serialised).
function collectLinks() {
  const out = [], seen = new Set();
  const add = (href, text, sure) => {
    let u; try { u = new URL(href, location.href); } catch (e) { return; }
    if (!/^https?:$/.test(u.protocol)) return;
    u.hash = ""; const k = u.href; if (seen.has(k)) return; seen.add(k);
    out.push({ url: k, text: (text || "").replace(/\s+/g, " ").trim().slice(0, 200), sure });
  };
  if (document.contentType === "application/pdf") return { pdf: true, links: [] };
  const GENERIC = /^(open( the)?( document)?|download( (the )?(pdf|document|report|file))?|pdf|view( pdf)?|read( more)?|here|click here|ver|abrir|descargar( pdf)?|descarga|t[eé]l[eé]charger|herunterladen|baixar|link)?$/i;
  const better = (a, text) => {
    if (!GENERIC.test(text.replace(/\s+/g, " ").trim())) return text;
    const row = a.closest("tr, li, article, .card, [class*=item], [class*=row]");
    const around = row ? (row.textContent || "").replace(a.textContent || "", " ").replace(/\s+/g, " ").trim() : "";
    if (around && around.length <= 160 && !GENERIC.test(around)) return around;
    try { return decodeURIComponent(new URL(a.href).pathname.split("/").pop() || "").replace(/\.pdf$/i, "").replace(/[-_+]+/g, " ").trim() || text; } catch (e) { return text; }
  };
  for (const a of document.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href") || "", text = a.innerText || a.textContent || a.getAttribute("title") || a.getAttribute("aria-label") || "";   // innerText keeps the space between "Download" and the title
    const sure = /\.pdf($|[?#])/i.test(href);
    // Documents served with no ".pdf" in their address (the platforms of half the investor sites: /static-files/<id>,
    // /getattachment/, /files/<name>, ?documentId=): known by the address, or by the PDF icon or class on the link.
    const endpoint = /\/static-files\/|\/getattachment\/|\/files\/[^/.]+\/?$|[?&](document|edition|asset|media|file)[-_]?id=|(drive|docs)\.google\.com\/(file\/d\/|(uc|open)\?)/i.test(href);   // and a file on Google Drive (0.6.1)
    const marked = /pdf/i.test((a.getAttribute("type") || "") + " " + (a.className || "")) || !!a.querySelector('img[src*="pdf" i], img[alt*="pdf" i], svg[class*="pdf" i], i[class*="pdf" i], span[class*="pdf" i]');
    // 0.6.4: a link to this same page with other choices of its filters is not a document. A documents archive whose
    // address holds "downloads" offered its own topic filters ("Climate (124)") as sixty documents.
    let samePage = false; try { const u = new URL(href, location.href); samePage = u.origin === location.origin && u.pathname === location.pathname && !endpoint && !marked && !a.hasAttribute("download"); } catch (e) {}
    if (samePage && !sure) continue;
    const maybe = !sure && (endpoint || marked || /pdf|download|descarga|t[eé]l[eé]charg|herunterladen/i.test(href + " " + text) || a.hasAttribute("download")) && !/^(#|javascript:|mailto:)/i.test(href);
    if (sure || maybe) add(href, better(a, text), sure);
  }
  for (const f of document.querySelectorAll("iframe[src], embed[src], object[data]")) {
    const src = f.getAttribute("src") || f.getAttribute("data") || "";
    if (/\.pdf($|[?#])/i.test(src)) add(src, document.title, true);
  }
  return { pdf: false, links: out.slice(0, 400) };
}
// Runs inside the tab: one level deeper. Some report pages show a button per report ("VER", "View") that leads to
// another page of the same site, and the PDFs are there (an insurer's report room, 11 Nov 2026). The pages this
// page links to, on the same site, are fetched as the person's browser would and their PDF links collected. At
// most 40 pages, four at a time, 15 seconds each; nothing is downloaded but those pages. Self-contained (serialised).
async function collectDeeper(max) {
  const isPdf = h => /\.pdf($|[?#])/i.test(h);
  const here = location.href.split("#")[0], pages = [], seen = new Set([here]);
  const WANT = /\bver\b|view|read|leer|more|m[aá]s|report|informe|memoria|annual|anual|sustain|sosten|document|download|descarg|publica|result|governance|gobierno|cuentas|accounts|esg|rapport|bericht|relat[oó]rio/i;
  const cands = [];
  for (const a of document.querySelectorAll("a[href]")) {
    let u; try { u = new URL(a.getAttribute("href"), location.href); } catch (e) { continue; }
    if (!/^https?:$/.test(u.protocol) || u.host !== location.host) continue;
    u.hash = ""; const k = u.href; if (seen.has(k) || isPdf(k) || /\.(jpe?g|png|gif|svg|zip|xlsx?|docx?|pptx?|mp4)($|\?)/i.test(u.pathname)) continue;
    seen.add(k); const t = (a.innerText || a.textContent || "") + " " + u.pathname;
    cands.push({ url: k, want: WANT.test(t), inMain: !a.closest("header, nav, footer") });
  }
  cands.sort((x, y) => (y.want + y.inMain * 2) - (x.want + x.inMain * 2));
  const todo = cands.slice(0, max), out = [], got = new Set();
  const GENERIC = /^(open( the)?( document)?|download( (the )?(pdf|document|report|file))?|pdf|view( pdf)?|read( more)?|here|click here|ver|abrir|descargar( pdf)?|descarga|t[eé]l[eé]charger|herunterladen|baixar|link)?$/i;
  const one = async p => {
    try {
      const c = new AbortController(); const timer = setTimeout(() => c.abort(), 15000);
      const r = await fetch(p.url, { credentials: "include", signal: c.signal }); clearTimeout(timer);
      if (!r.ok || !/html/i.test(r.headers.get("content-type") || "")) return;
      const doc = new DOMParser().parseFromString(await r.text(), "text/html"); pages.push(p.url);
      const title = (doc.querySelector("h1") || doc.querySelector("title") || {}).textContent || "";
      for (const a of doc.querySelectorAll("a[href]")) {
        const href = a.getAttribute("href") || "";
        const likely = /\/static-files\/|\/getattachment\/|\/files\/[^/.]+\/?$|[?&](document|edition|asset|media|file)[-_]?id=/i.test(href) || !!a.querySelector('img[src*="pdf" i], img[alt*="pdf" i]');
        if (!isPdf(href) && !likely) continue;
        let u; try { u = new URL(href, p.url); } catch (e) { continue; }
        u.hash = ""; if (got.has(u.href)) continue; got.add(u.href);
        let text = (a.textContent || a.getAttribute("title") || "").replace(/\s+/g, " ").trim();
        if (GENERIC.test(text)) text = (title.replace(/\s+/g, " ").trim() + " " + decodeURIComponent(u.pathname.split("/").pop() || "").replace(/\.pdf$/i, "").replace(/[-_+]+/g, " ")).trim();
        out.push({ url: u.href, text: text.slice(0, 200), sure: isPdf(href), from: p.url });
      }
    } catch (e) {}
  };
  for (let i = 0; i < todo.length; i += 4) await Promise.all(todo.slice(i, i + 4).map(one));
  return { pages: pages.length, asked: todo.length, more: cands.length - todo.length, links: out.slice(0, 400) };
}
async function lookDeeper() {
  $("deeper").textContent = "Reading the pages this page links to…";
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: TAB.id }, func: collectDeeper, args: [40] });
    const j = (res && res.result) || { pages: 0, links: [] }; const have = new Set(docs.map(d => d.url)); let n = 0;
    const fresh = []; for (const d of j.links.map(viaDrive)) if (!have.has(d.url)) { have.add(d.url); docs.push(d); fresh.push(d); n++; }
    await confirmMaybes(fresh); await markHeld();
    $("deeper").textContent = `${n} more document${n === 1 ? "" : "s"} found in ${j.pages} page${j.pages === 1 ? "" : "s"} this page links to` + (j.more > 0 ? ` (${j.more} other links were not opened)` : "") + ". Look again";
  } catch (e) { $("deeper").textContent = "This page could not be read one level deeper. Try again"; }
}
// A file kept on Google Drive (0.6.1; Decathlon, 18 Nov 2026: 35 reports linked as drive.google.com/file/d/<id>/view).
// That address is a viewer page, so the download answered "not a PDF". The file is served by an address built from
// the same id: the link is offered under that address (the one the library keeps for it, monitor_core.drive_downloads)
// and the older form is tried when the first does not answer with a PDF. Folders, Docs and Sheets are not files.
const driveDownloads = url => {
  const m = /^https?:\/\/(?:drive|docs)\.google\.com\/(?:file\/d\/|(?:uc|open)\?(?:[^#]*&)?id=)([A-Za-z0-9_-]{20,})/i.exec(url || "");
  return m ? [`https://drive.usercontent.google.com/download?id=${m[1]}&export=download&confirm=t`, `https://drive.google.com/uc?export=download&id=${m[1]}`] : [];
};
const viaDrive = d => { const v = driveDownloads(d.url); return v.length ? { ...d, url: v[0], alt: v.slice(1) } : d; };
const oncePerUrl = list => { const seen = new Set(); return list.filter(d => !seen.has(d.url) && seen.add(d.url)); };
async function listDocuments() {
  // the tab is Drive's own viewer of one file: that file is the document
  if (driveDownloads(TAB.url).length) return [viaDrive({ url: TAB.url.split("#")[0], text: (TAB.title || "").replace(/\s+-\s+Google Drive$/i, "").replace(/\.pdf$/i, ""), sure: false })];
  return oncePerUrl((await listOfPage()).map(viaDrive));
}
async function listOfPage() {
  if (isPdfUrl(TAB.url)) return [{ url: TAB.url.split("#")[0], text: TAB.title, sure: true }];
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: TAB.id }, func: collectLinks });
    if (res && res.result && res.result.pdf) return [{ url: TAB.url.split("#")[0], text: TAB.title, sure: true }];
    return (res && res.result && res.result.links) || [];
  } catch (e) {
    // the browser's own PDF viewer and a few protected pages cannot be read: the tab itself is the document
    return /^https?:/.test(TAB.url) ? [{ url: TAB.url.split("#")[0], text: TAB.title, sure: false }] : [];
  }
}
// The library's document types, for the sender who knows what the document is (0.4.0). The same list the database checks.
const TYPES = ["Annual Report", "Integrated Report", "Sustainability Report", "ESG Report", "CSR Report", "Impact Report", "Climate Report", "Environmental Reports", "Social Reports", "Financial Statement", "10K Form", "Registration Document", "Interim Reports", "CDP Report", "GRI", "SASB Index", "Human Rights Policy", "Code of Conduct", "Policy Documents", "Statutory Documents", "Company Presentations", "Engagement Report", "Certifications"];
function nameOf(url, disposition, text) {
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition || "");
  let n = m ? m[1] : "";
  if (!n && /\.pdf($|[?#])/i.test(url)) { try { n = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || ""); } catch (e) {} }   // an address with no ".pdf" ends in an id: the link's title names the file better
  try { n = decodeURIComponent(n); } catch (e) {}
  if (!n || !/[a-z0-9]/i.test(n)) n = (text || "document").slice(0, 80);
  n = n.replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ").replace(/\s+/g, " ").trim();
  return /\.pdf$/i.test(n) ? n : n + ".pdf";
}
const looksPdf = buf => { const head = new TextDecoder("latin1").decode(new Uint8Array(buf.slice(0, 1024))); return head.includes("%PDF-"); };
const hex = buf => [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");

// Runs inside the tab: the file fetched as the page itself would (same site, same session), as base64.
async function fetchInTab(url) {
  try {
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) return { error: `HTTP ${r.status}` };
    const buf = await r.arrayBuffer();
    if (buf.byteLength > 30 * 1024 * 1024) return { error: "too large to pass through the page" };
    let s = ""; const b = new Uint8Array(buf);
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return { b64: btoa(s), disposition: r.headers.get("content-disposition") || "" };
  } catch (e) { return { error: String(e && e.message || e) }; }
}
// The body of a reply read piece by piece, so that a large file shows how far it is (0.4.2: a report of 100 MB said
// "downloading…" for minutes and nobody knew whether it was moving).
async function readAll(r, say) {
  if (!r.body || !say) return r.arrayBuffer();
  const reader = r.body.getReader(), parts = []; let n = 0, last = 0;
  for (;;) {
    const { done, value } = await reader.read(); if (done) break;
    parts.push(value); n += value.byteLength;
    if (n - last > 2 * 1048576) { last = n; say(`downloading… ${Math.round(n / 1048576)} MB`); }
  }
  const out = new Uint8Array(n); let at = 0; for (const p of parts) { out.set(p, at); at += p.byteLength; }
  return out.buffer;
}
// The file stored, with its progress. Resolves to { status, body } as fetch would give them; rejects when the storage cannot be reached.
function putFile(url, headers, buf, say) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest(); x.open("POST", url); for (const [k, v] of Object.entries(headers)) x.setRequestHeader(k, v);
    x.upload.onprogress = e => { if (e.lengthComputable && say) say(`sending… ${Math.round(100 * e.loaded / e.total)}% of ${Math.round(e.total / 1048576)} MB`); };
    x.onload = () => resolve({ status: x.status, body: x.responseText }); x.onerror = x.ontimeout = () => reject(new Error("unreachable"));
    x.send(buf);
  });
}
async function download(url, say, alt) {
  // first as the extension (any site, the person's session); if the site says no or sends something else, as the page.
  // alt: other addresses of the same file (a Google Drive file has two), tried in turn before the page is asked
  let why = "";
  for (const u of [url, ...(alt || [])]) {
    try {
      const r = await fetch(u, { credentials: "include", referrer: TAB.url || undefined });
      if (r.ok) {
        const buf = await readAll(r, say);
        if (looksPdf(buf)) return { buf, disposition: r.headers.get("content-disposition") || "" };
        why = "not a PDF";
      } else why = `the site answered ${r.status}`;
    } catch (e) { why = "could not be reached"; }
  }
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: TAB.id }, func: fetchInTab, args: [url] });
    const j = res && res.result;
    if (j && j.b64) {
      const bin = atob(j.b64), b = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) b[i] = bin.charCodeAt(i);
      if (looksPdf(b.buffer)) return { buf: b.buffer, disposition: j.disposition };
      why = "not a PDF";
    }
  } catch (e) {}
  throw new Error(why || "could not be downloaded");
}

async function sendOne(doc, company) {
  // a PDF chosen from this computer (0.4.0: a site that only downloads after a captcha) is read as it is; it has no address
  const say = t => { if (doc.status) doc.status.textContent = t; }, t0 = Date.now();
  const { buf, disposition } = doc.file ? { buf: await doc.file.arrayBuffer(), disposition: "" } : await download(doc.url, say, doc.alt);
  const got = Date.now();
  if (doc.file && !looksPdf(buf)) throw new Error("not a PDF");
  if (buf.byteLength > MAX_BYTES) throw new Error(`larger than 300 MB (${Math.round(buf.byteLength / 1048576)} MB)`);
  const sha = hex(await crypto.subtle.digest("SHA-256", buf));
  const name = doc.file ? nameOf("", "", doc.file.name.replace(/\.pdf$/i, "")) : nameOf(doc.url, disposition, doc.text);
  const page = /^https?:/.test(TAB.url || "") ? TAB.url : null;
  const year = doc.year && /^(19|20)\d\d$/.test(doc.year.value.trim()) ? +doc.year.value.trim() : null;
  const j = await rpc("upload_begin", { company: company.id, filename: name, bytes: buf.byteLength, sha256: sha, source_url: doc.file ? null : doc.url,
                                         page_url: doc.queue ? null : doc.file ? page : (doc.url === TAB.url.split("#")[0] ? null : TAB.url), client: CLIENT,
                                         sent_type: doc.kind && doc.kind.value || null, sent_year: year });
  if (j.known) return "already in lantrNa";
  if (j.again) return "already sent";
  const t = await token(); let r;
  for (let i = 0; ; i++) {
    try { r = await putFile(`${conf.url}/storage/v1/object/uploads/${j.path}`, { apikey: conf.key, Authorization: `Bearer ${t}`, "Content-Type": "application/pdf" }, buf, say);
      if (r.status < 500 || i >= 2) break; }
    catch (e) { if (i >= 2) throw new Error("lantrNa's storage did not answer (busy): try again in a few minutes"); }
    await pause(2000 * (i + 1));
  }
  if (!(r.status >= 200 && r.status < 300)) {
    let e = {}; try { e = JSON.parse(r.body); } catch (x) {}
    if (r.status === 413 || /maximum allowed size|too large/i.test(e.message || e.error || "")) throw new Error(`not stored: lantrNa's storage takes smaller files than this one (${Math.round(buf.byteLength / 1048576)} MB); tell the owner`);
    if (!(r.status === 409 || /exists|duplicate/i.test(e.message || e.error || ""))) throw new Error(`not stored: ${e.message || e.error || "HTTP " + r.status}`);
  }
  await rpc("upload_done", { upload_id: j.id });
  // where the time went: a slow site and a slow line look the same from the outside
  const secs = ms => `${Math.max(1, Math.round(ms / 1000))} s`;
  return `sent (${(buf.byteLength / 1048576).toFixed(1)} MB${buf.byteLength > 10 * 1048576 ? `; ${doc.file ? "read" : "downloaded"} in ${secs(got - t0)}, stored in ${secs(Date.now() - got)}` : ""})`;
}

// A link that may be a PDF is asked what it is (the headers only, nothing downloaded): the ones the site calls a PDF
// are treated as the others. At most 80 links, six at a time.
async function confirmMaybes(list) {
  const doubt = list.filter(d => !d.sure).slice(0, 80);
  const ask = async d => {
    try {
      const c = new AbortController(); const timer = setTimeout(() => c.abort(), 10000);
      let r = await fetch(d.url, { method: "HEAD", credentials: "include", signal: c.signal });
      if (r.status === 405 || r.status === 501) r = await fetch(d.url, { method: "GET", credentials: "include", signal: c.signal, headers: { Range: "bytes=0-0" } });
      clearTimeout(timer);
      if (r.ok && /application\/pdf/i.test(r.headers.get("content-type") || "")) d.sure = true;
    } catch (e) {}
  };
  for (let i = 0; i < doubt.length; i += 6) await Promise.all(doubt.slice(i, i + 6).map(ask));
}

// ---- the window ---------------------------------------------------------------------------------------------------
const fold = s => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
const hostOf = u => { try { return new URL(/^https?:/.test(u) ? u : "https://" + u).hostname.replace(/^www\./, "").toLowerCase(); } catch (e) { return ""; } };
// The site a host belongs to: its last two labels, or three when the last two are a country's second level
// ("ana.co.jp", "tesco.co.uk", "vale.com.br"). 0.3.1 took two labels always, so every company on .co.jp shared the
// site "co.jp", none was the only candidate, and no company was offered on any such site (13 Nov 2026, ANA Holdings).
const SECOND_LEVEL = /^(co|com|org|net|ac|gov|edu|ne|or|go|gob|ltd|plc|nom)$/;
function siteOf(host) {
  const p = (host || "").split(".");
  return p.slice(p.length >= 3 && p[p.length - 1].length === 2 && SECOND_LEVEL.test(p[p.length - 2]) ? -3 : -2).join(".");
}
function companyOfHost(host) {
  if (!host) return null;
  const site = siteOf(host);
  const cands = companies.filter(c => { const h = hostOf(c.website || ""); return h && siteOf(h) === site; });
  if (cands.length === 1) return cands[0];
  const exact = cands.filter(c => hostOf(c.website || "") === host);      // several companies on one site (a group and its units): the one whose own host this is
  return exact.length === 1 ? exact[0] : null;
}
let hostMemory = {};
async function rememberHost(company) { const h = hostOf(TAB.url); if (!h || /^REQ_/.test(company.id) || hostMemory[h] === company.id) return; hostMemory[h] = company.id; await store.set("hosts", hostMemory); }
let asked = null;          // a company asked for with an older database: { id: "REQ_<request>", name }
const chosenCompany = () => { const v = fold($("company").value); return companies.find(c => fold(c.name) === v) || (asked && fold(asked.name) === v ? asked : null); };
// The company's name as the page's title gives it: the last part of "Annual report 2025 | Acme Group" (or the first,
// when the last is a word like "Investors"). Only a proposal: the person reads and corrects it.
function nameFromTitle(title) {
  const parts = (title || "").split(/\s+[|·–—-]\s+|\s*[|·]\s*/).map(x => x.trim()).filter(x => x.length >= 2 && x.length <= 60);
  if (!parts.length) return "";
  const generic = /^(home|homepage|investors?|investor relations|reports?|annual reports?|sustainability|downloads?|publications?|news|about( us)?|welcome)\b/i;
  const last = parts[parts.length - 1];
  return (!generic.test(last) ? last : parts.find(x => !generic.test(x)) || "").slice(0, 120);
}
// A company that is not in the list (0.6.0): it is made at once, with its id, and its documents can be sent from this
// moment; nobody has to approve it first. The database refuses a clear duplicate (the same name, website or LEI) and
// answers the company it holds, which is chosen instead. A doubt (a name close to another company's) does not stop
// the company: it is noted for the owner, who accepts or rejects the company later.
function newCompany() {
  $("cobox").hidden = false; $("coname").value = $("company").value.trim() || nameFromTitle(TAB.title);
  if (!$("cosite").value && /^https?:/.test(TAB.url)) $("cosite").value = "https://" + hostOf(TAB.url); $("coname").focus(); $("coname").select();
  $("cocancel").onclick = () => { $("cobox").hidden = true; };
  $("coask").onclick = async () => {
    const name = $("coname").value.trim(), isin = $("coisin").value.trim().toUpperCase();
    if (name.length < 2) { $("comsg").textContent = "Write the company's name."; return; }
    if (isin && !/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(isin)) { $("comsg").textContent = "An ISIN has 12 characters, like US0378331005."; return; }
    try {
      $("coask").disabled = true;
      const j = await rpc("request_company", { name, website: $("cosite").value.trim(), lei: $("colei").value.trim(), note: isin ? `ISIN ${isin}` : null, client: CLIENT });
      if (j.held) { $("company").value = j.name; $("msg").textContent = `${j.name} is already a company of lantrNa: chosen.`; }
      else if (j.again && j.review === "rejected") { $("comsg").textContent = `${j.name} was added before and not accepted${j.why ? `: ${j.why}` : ""}.`; return; }
      else if (j.id) {
        const made = { id: j.id, name: j.name, website: j.website || "" };
        if (!companies.some(c => c.id === made.id)) { companies.push(made); const o = el("option"); o.value = made.name; $("companies").append(o); }
        $("company").value = made.name; $("newco").textContent = "Not in the list? Add the company";
        const doubts = (j.doubts || []).filter(d => !/^no website/.test(d));
        $("msg").textContent = `${j.name} is a company of lantrNa now. Send its documents.` + (doubts.length ? ` Noted for the owner: ${doubts[0]}.` : "");
      }
      else { asked = { id: `REQ_${j.request}`, name: j.name }; $("company").value = j.name; $("msg").textContent = `${j.name} is asked for. You can send its documents now.`; }
      $("cobox").hidden = true; await markHeld();
    } catch (e) { $("comsg").textContent = `Not added: ${e.message}`; } finally { $("coask").disabled = false; }
  };
}
function render() {
  const list = $("list"); list.innerHTML = "";
  if (!docs.length) list.append(el("div", "empty", "No link to a PDF on this page. If its buttons lead to the reports, use \"Look one level deeper\" above; or open the page that lists the documents, or the PDF itself, and click the button again."));
  docs.forEach((d, i) => {
    const row = el("label", "doc" + (d.sure ? "" : " maybe")); const box = el("input"); box.type = "checkbox"; box.checked = d.file ? !d.done : (d.sure && looksReport(d) && !d.held); if (d.done) box.disabled = true; box.dataset.i = i;
    box.onchange = count;
    row.append(box, el("span", "t", d.text || d.url.split("/").pop()), el("span", "u", d.file ? "from this computer" : d.url)); const s = el("span", "s", ""); row.append(s); d.status = s; d.box = box;
    // optional: what the sender knows of the document. Kept across repaints; nothing is asked of anyone who leaves it closed.
    const more = el("span", "more"); const open = el("a", null, "type and year (optional)"); open.href = "#";
    const kind = el("select"); const none = el("option", null, "type: let lantrNa decide"); none.value = ""; kind.append(none); for (const t of TYPES) { const o = el("option", null, t); o.value = t; kind.append(o); }
    const year = el("input"); year.placeholder = "year on the report"; year.maxLength = 4; year.inputMode = "numeric"; year.size = 14;
    kind.value = d.kindValue || ""; year.value = d.yearValue || ""; kind.hidden = year.hidden = !(d.kindValue || d.yearValue); open.hidden = !kind.hidden;
    kind.onchange = () => { d.kindValue = kind.value; }; year.oninput = () => { d.yearValue = year.value.trim(); };
    open.onclick = e => { e.preventDefault(); kind.hidden = year.hidden = false; open.hidden = true; };
    more.append(open, kind, year); row.append(more); d.kind = kind; d.year = year;
    // 0.6.4: what is held is not ticked, and can still be ticked (the owner could not send again a document whose
    // file lantrNa had never read; the database decides what it does with a file it already holds)
    if (d.held) { s.textContent = "already in lantrNa (tick to send it again)"; s.className = "s ok"; }
    list.append(row);
  });
  count();
}
// Ticked from the start only when the link reads like a report or a policy: a home page also links magazines,
// leaflets and terms of use, and those are for the person to tick, not for the button to send by default.
const REPORT = /annual|report|informe|memoria|rapport|bericht|relat[oó]rio|bilan|sustainab|sostenib|nachhaltig|durab|esg|csr|rse|integrated|integrado|climat|clima|tcfd|cdp|gri|ghg|emission|emision|carbon|net.?zero|methodolog|metodolog|polic|pol[ií]tica|politique|code|c[oó]digo|conduct|ethic|[eé]tic|human.?rights|derechos|slavery|labour|labor|10-?k|20-?f|accounts|cuentas|financ|governance|gobierno|gouvernance|remunera|statement|universal.registration|proxy/i;
// Papers a company also files beside its reports, and that are not disclosures: never ticked from the start.
const NOT_REPORT = /privacy|privacidad|datos personales|data protection|protecci[oó]n de datos|cookie|terms (and|&) conditions|general terms|gtcs?\b|condiciones (generales|de venta|de uso)|job applicant|fair processing|brochure|folleto|catalog/i;
const pathWords = u => { try { return decodeURIComponent(new URL(u).pathname).replace(/[-_+./]/g, " "); } catch (e) { return ""; } };
const looksReport = d => { const t = d.text + " " + pathWords(d.url); return REPORT.test(t) && !NOT_REPORT.test(t); };
// Before anything is downloaded: the links lantrNa already holds for the chosen company are shown as held, not offered.
async function markHeld() {
  const company = chosenCompany(); docs.forEach(d => { d.held = false; });
  if (company && docs.length) { try { const held = new Set(await rpc("upload_known", { company: company.id, urls: docs.filter(d => !d.file).map(d => d.url) })); docs.forEach(d => { d.held = !d.file && held.has(d.url); }); } catch (e) {} }
  render();
}
function count() { const n = docs.filter(d => d.box && d.box.checked).length; const h = docs.filter(d => d.held).length; $("count").textContent = `${docs.length} document link${docs.length === 1 ? "" : "s"} on this page, ${n} ticked${h ? `, ${h} already in lantrNa` : ""}`; $("go").disabled = !n; }
async function start() {
  const over = await store.get("config"); if (over && over.url) conf = over;          // a test database, set by hand
  if (/__SUPABASE_/.test(conf.url)) { document.querySelector("main").textContent = "This copy of the extension was not built by the site: download it from the lantrNa page."; return; }
  $("out").onclick = async e => { e.preventDefault(); await store.set("session", null); location.reload(); };
  if (!(await token())) { showSignIn(); return; }
  $("sign").hidden = true; $("work").hidden = false; $("out").hidden = false;
  $("page").textContent = TAB.title ? `${TAB.title} · ${hostOf(TAB.url)}` : TAB.url;
  try { companies = await rpc("upload_companies"); } catch (e) { $("msg").textContent = `The companies could not be read: ${e.message}`; if (/sign in/.test(e.message)) showSignIn(); return; }
  const dl = $("companies"); for (const c of companies) { const o = el("option"); o.value = c.name; dl.append(o); }
  // the company's own site, or a site this person has sent documents from before (a company with two sites:
  // its reports on one, its website on another)
  hostMemory = (await store.get("hosts")) || {};
  const guess = companyOfHost(hostOf(TAB.url)) || companies.find(c => c.id === hostMemory[hostOf(TAB.url)]); if (guess) $("company").value = guess.name;
  docs = await listDocuments();
  // a company's own site that no company of lantrNa has: say so, and offer to add the company (0.6.0). Not when the
  // tab is itself a document (0.6.2, 18 Nov 2026): a report opened from a company's page is served by a file host
  // (api.mziq.com/mzfilemanager/..., no ".pdf" in the address), and "No company of lantrNa has mziq.com" was shown
  // beside the right company. The notice also goes once a company of the list is chosen.
  const tabIsDoc = isPdfUrl(TAB.url) || (docs.length === 1 && docs[0].sure && docs[0].url === TAB.url.split("#")[0]);
  if (!guess && /^https?:/.test(TAB.url) && !tabIsDoc) { $("newco").textContent = `No company of lantrNa has ${siteOf(hostOf(TAB.url))}. Add the company`; $("newco").style.fontWeight = "600"; }
  await confirmMaybes(docs); await markHeld();
  // PDFs from this computer (0.4.0): a file the site only hands over after a captcha is downloaded by the person and chosen here
  $("files").onchange = () => { for (const f of $("files").files) { if (!docs.some(d => d.file && d.file.name === f.name && d.file.size === f.size)) docs.push({ url: "", text: f.name.replace(/\.pdf$/i, ""), sure: true, file: f, held: false }); } $("files").value = ""; render(); };
  $("company").onchange = () => { if (companies.some(c => c.name === $("company").value.trim())) { $("newco").textContent = "Not in the list? Add the company"; $("newco").style.fontWeight = ""; } markHeld(); };
  $("all").onclick = e => { e.preventDefault(); docs.forEach(d => { if (!d.box.disabled) d.box.checked = true; }); count(); };
  $("none").onclick = e => { e.preventDefault(); docs.forEach(d => d.box.checked = false); count(); };
  showQueue();
  $("go").onclick = sendAll; $("watch").onclick = scanPage; $("capture").onclick = capturePage; showScan(false); $("newco").onclick = e => { e.preventDefault(); newCompany(); };
  $("deeper").hidden = tabIsDoc || !/^https?:/.test(TAB.url); $("deeper").onclick = e => { e.preventDefault(); lookDeeper(); };
}
function showSignIn() {
  $("work").hidden = true; $("sign").hidden = false;
  $("sendcode").onclick = async () => {
    try { $("sendcode").disabled = true; await authCall("otp", { email: $("mail").value.trim(), create_user: true }); $("signmsg").textContent = "Code sent. Type it here."; $("code").hidden = $("signin").hidden = false; $("code").focus(); }
    catch (e) { $("signmsg").textContent = `Could not send the code: ${e.message}`; } finally { $("sendcode").disabled = false; }
  };
  $("signin").onclick = async () => {
    try { const j = await authCall("verify", { type: "email", email: $("mail").value.trim(), token: $("code").value.trim() });
      await store.set("session", { access_token: j.access_token, refresh_token: j.refresh_token, expires_at: j.expires_at, email: $("mail").value.trim() }); location.reload(); }
    catch (e) { $("signmsg").textContent = `That code did not work: ${e.message}`; }
  };
}
// ---- the owner's queue (0.8.0) -----------------------------------------------------------------------------------
// Documents ticked in lantrNa's Admin (Documents to fetch) wait in a queue. This window downloads them one after
// another with this browser, sends each as any other document, and tells lantrNa what came of it. One at a time, with
// QUEUE_PAUSE between two, and longer between two of the same site: a person's browser, not a crawler.
const QUEUE_PAUSE = 4000, SAME_SITE_PAUSE = 12000;
let queueStop = false;
async function showQueue() {
  let rows = []; try { rows = await rpc("fetch_queue_mine", { max_rows: 500 }); } catch (e) { return; }
  const box = $("queue"); if (!rows.length) { box.hidden = true; return; }
  box.hidden = false; $("queuesay").textContent = `${rows.length} document${rows.length === 1 ? "" : "s"} wait${rows.length === 1 ? "s" : ""} in your queue (ticked in lantrNa's Admin).`;
  $("queuego").onclick = () => runQueue(rows); $("queuestop").onclick = () => { queueStop = true; $("queuestop").disabled = true; };
}
async function runQueue(rows) {
  queueStop = false; $("queuego").disabled = true; $("queuestop").hidden = false; $("queuestop").disabled = false;
  const log = $("queuelog"); log.innerHTML = ""; let ok = 0, bad = 0, lastHost = "";
  for (let i = 0; i < rows.length && !queueStop; i++) {
    const d = rows[i], line = el("div", "s"), host = hostOf(d.url);
    line.textContent = `${i + 1}/${rows.length} · ${d.company || d.wba_id} · ${d.year || "no year"} · downloading…`; log.prepend(line);
    const say = { set textContent(t) { line.textContent = `${i + 1}/${rows.length} · ${d.company || d.wba_id} · ${d.year || "no year"} · ${t}`; } };
    let result;
    try { result = await sendOne({ url: d.url, text: d.label || "", queue: true, status: say, year: { value: d.year ? String(d.year) : "" }, kind: { value: d.doc_type || "" } }, { id: d.wba_id });
      line.className = "s ok"; ok++; }
    catch (e) { result = `not sent: ${e.message}`; line.className = "s bad"; bad++; if (/sign in/.test(e.message)) { say.textContent = result; break; } }
    say.textContent = result;
    try { await rpc("fetch_queue_done", { library_id: d.library_id, result }); } catch (e) {}
    $("queuesay").textContent = `${i + 1} of ${rows.length} tried: ${ok} sent or already held, ${bad} not.`;
    if (i + 1 < rows.length && !queueStop) await pause(host && host === lastHost ? SAME_SITE_PAUSE : QUEUE_PAUSE);
    lastHost = host;
  }
  $("queuestop").hidden = true; $("queuego").disabled = false; $("queuego").textContent = "Fetch what is left";
  $("queuesay").textContent += queueStop ? " Stopped: the rest still waits." : " Done. What was not sent is marked in Admin.";
  showQueue();
}

async function sendAll() {
  const company = chosenCompany(); if (!company) { $("msg").textContent = "Choose the company from the list first."; $("company").focus(); return; }
  const todo = docs.filter(d => d.box.checked && !d.done && !d.held); if (!todo.length) return;
  $("go").disabled = true; let ok = 0, bad = 0;
  // two at a time (0.4.2): a large report no longer holds the small ones behind it; more would only share the same line
  const queue = todo.slice();
  const worker = async () => {
    for (let d; (d = queue.shift());) {
      d.status.className = "s"; d.status.textContent = "downloading…";
      try { d.status.textContent = await sendOne(d, company); d.status.className = "s ok"; d.done = true; d.box.checked = false; d.box.disabled = true; ok++; }
      catch (e) { d.status.textContent = `not sent: ${e.message}`; d.status.className = "s bad"; bad++; }
      $("msg").textContent = `${ok + bad} of ${todo.length}…`;
    }
  };
  await Promise.all([worker(), worker()]);
  if (ok) await rememberHost(company);
  $("msg").textContent = `${ok} sent to ${company.name}${bad ? `, ${bad} not sent` : ""}. ` + (/^REQ_/.test(company.id) ? "They wait for the company to be approved (company_requests in Airtable)."
    : "They appear in the library within the half hour, and in the search after the night's load.");
  count();
}
// "Scan this page" (0.5.0): lantrNa reads this page now. A page it already watches is scanned again; one it does
// not watch is added for the company and then read. The request waits in the database for the watch (a few
// minutes); the line under the buttons says what became of it, here and the next time the window opens on the page.
const pageUrl = () => TAB.url.split("#")[0];
const ago = t => { const m = Math.max(0, Math.round((Date.now() - new Date(t).getTime()) / 60000)); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
function scanLine(j) {
  if (!j) return "";
  if (j.status === "done") return `Scanned ${ago(j.done_at || j.at)}. ${j.result || ""}`;
  if (j.status === "failed") return `The scan asked ${ago(j.at)} gave no result. Ask again, or send the documents from this window.`;
  if (j.status === "added") return `Asked ${ago(j.at)}: a new page for lantrNa, on its way in. It is read within the half hour.`;
  return `Asked ${ago(j.at)}: waiting for the scan (a few minutes).`;
}
let scanTimer = null;
async function showScan(follow) {
  if (!/^https?:/.test(TAB.url) || isPdfUrl(TAB.url)) return;
  let j = null; try { j = await rpc("page_scan_last", { url: pageUrl() }); } catch (e) { return; }
  $("scanmsg").textContent = scanLine(j);
  clearTimeout(scanTimer);
  if (follow && j && (j.status === "new" || j.status === "queued" || j.status === "added")) scanTimer = setTimeout(() => showScan(true), 20000);
}
// "Send this page" (0.7.0): the text of the page as the person sees it becomes a document of type "Web page" for the
// company (page_capture), searchable at once. For what a company says on its site and in no PDF. Runs inside the tab,
// free of outer variables: the main part of the page when it has one with enough text, else the whole of it.
function pageText() {
  const main = document.querySelector("main, [role=main], article");
  const from = main && (main.innerText || "").trim().length > 800 ? main : document.body;
  return { title: document.title || "", text: ((from && from.innerText) || "").slice(0, 400000) };
}
async function capturePage() {
  const company = chosenCompany(); if (!company) { $("msg").textContent = "Choose the company from the list first."; $("company").focus(); return; }
  if (/^REQ_/.test(company.id)) { $("msg").textContent = "A page can be sent a quarter of an hour after the company is approved."; return; }
  if (!/^https?:/.test(TAB.url) || isPdfUrl(TAB.url)) { $("msg").textContent = "This is a document, not a page: send it with the first button."; return; }
  $("capture").disabled = true; $("msg").textContent = "Reading the page…";
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: TAB.id }, func: pageText });
    const got = (res && res.result) || { title: "", text: "" };
    if (got.text.trim().length < 300) { $("msg").textContent = "There is too little text on this page to keep (under 300 characters)."; return; }
    const j = await rpc("page_capture", { company: company.id, url: pageUrl(), title: got.title, body: got.text, client: CLIENT });
    $("msg").textContent = j.same ? "lantrNa already holds this page as it is now." : `The text of this page is in lantrNa for ${company.name}: ${j.pages} page${j.pages === 1 ? "" : "s"}, type "Web page", in the search now${j.again ? " (it replaces the page as sent before)" : ""}. Only what is on screen is sent: a list in several pages is sent a page at a time.`;
  } catch (e) { $("msg").textContent = "Not sent: " + (e.message || e); }
  finally { $("capture").disabled = false; }
}
async function scanPage() {
  const company = chosenCompany(); if (!company) { $("msg").textContent = "Choose the company from the list first."; $("company").focus(); return; }
  if (/^REQ_/.test(company.id)) { $("msg").textContent = "The page can be scanned a quarter of an hour after the company is approved (close this window and click the button again)."; return; }
  if (!/^https?:/.test(TAB.url) || isPdfUrl(TAB.url)) { $("msg").textContent = "This is a document, not a page that lists documents."; return; }
  try { const j = await rpc("scan_page", { company: company.id, url: pageUrl(), client: CLIENT });
    await rememberHost(company);
    $("msg").textContent = j.asked ? `Asked: lantrNa reads this page for ${company.name} now.` : "A scan of this page is already waiting.";
    showScan(true); }
  catch (e) { $("msg").textContent = `Not asked: ${e.message}`; }
}
start();
