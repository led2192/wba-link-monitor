// Send to lanterN: the sending window. It lists the PDF links of the tab it was opened for, downloads the ticked
// ones with this browser (the site sees an ordinary visit, with the person's own session) and stores each in the
// evidence database's private bucket, registered first with public.upload_begin (who, company, where from, size,
// SHA-256). browser_uploads.py turns them into rows of the library. Nothing is sent anywhere else.
"use strict";
const MAX_BYTES = 50 * 1024 * 1024;
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
  throw new Error(/Failed to fetch/.test(last.message) ? "lanterN's database did not answer (busy): try again in a few minutes" : last.message);
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
    const maybe = !sure && (/pdf|download|descarga|t[eé]l[eé]charg|herunterladen/i.test(href + " " + text) || a.hasAttribute("download")) && !/^(#|javascript:|mailto:)/i.test(href);
    if (sure || maybe) add(href, better(a, text), sure);
  }
  for (const f of document.querySelectorAll("iframe[src], embed[src], object[data]")) {
    const src = f.getAttribute("src") || f.getAttribute("data") || "";
    if (/\.pdf($|[?#])/i.test(src)) add(src, document.title, true);
  }
  return { pdf: false, links: out.slice(0, 400) };
}
async function listDocuments() {
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
function nameOf(url, disposition, text) {
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition || "");
  let n = m ? m[1] : "";
  if (!n) { try { n = decodeURIComponent(new URL(url).pathname.split("/").filter(Boolean).pop() || ""); } catch (e) {} }
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
async function download(url) {
  // first as the extension (any site, the person's session); if the site says no or sends something else, as the page
  let why = "";
  try {
    const r = await fetch(url, { credentials: "include", referrer: TAB.url || undefined });
    if (r.ok) {
      const buf = await r.arrayBuffer();
      if (looksPdf(buf)) return { buf, disposition: r.headers.get("content-disposition") || "" };
      why = "not a PDF";
    } else why = `the site answered ${r.status}`;
  } catch (e) { why = "could not be reached"; }
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
  const { buf, disposition } = await download(doc.url);
  if (buf.byteLength > MAX_BYTES) throw new Error(`larger than 50 MB (${Math.round(buf.byteLength / 1048576)} MB)`);
  const sha = hex(await crypto.subtle.digest("SHA-256", buf));
  const name = nameOf(doc.url, disposition, doc.text);
  const j = await rpc("upload_begin", { company: company.id, filename: name, bytes: buf.byteLength, sha256: sha, source_url: doc.url,
                                         page_url: doc.url === TAB.url.split("#")[0] ? null : TAB.url, client: CLIENT });
  if (j.known) return "already in lanterN";
  if (j.again) return "already sent";
  const t = await token(); let r;
  for (let i = 0; ; i++) {
    try { r = await fetch(`${conf.url}/storage/v1/object/uploads/${j.path}`, { method: "POST",
      headers: { apikey: conf.key, Authorization: `Bearer ${t}`, "Content-Type": "application/pdf" }, body: buf }); if (r.status < 500 || i >= 2) break; }
    catch (e) { if (i >= 2) throw new Error("lanterN's storage did not answer (busy): try again in a few minutes"); }
    await pause(2000 * (i + 1));
  }
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    if (!(r.status === 409 || /exists|duplicate/i.test(e.message || e.error || ""))) throw new Error(`not stored: ${e.message || e.error || "HTTP " + r.status}`);
  }
  await rpc("upload_done", { upload_id: j.id });
  return `sent (${(buf.byteLength / 1048576).toFixed(1)} MB)`;
}

// ---- the window ---------------------------------------------------------------------------------------------------
const fold = s => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
const hostOf = u => { try { return new URL(/^https?:/.test(u) ? u : "https://" + u).hostname.replace(/^www\./, "").toLowerCase(); } catch (e) { return ""; } };
function companyOfHost(host) {
  if (!host) return null;
  const parts = host.split(".");
  const cands = companies.filter(c => { const h = hostOf(c.website || ""); return h && (h === host || host.endsWith("." + h) || h.endsWith("." + parts.slice(-2).join(".")) || h === parts.slice(-2).join(".")); });
  return cands.length === 1 ? cands[0] : null;
}
let asked = null;          // a company this person has just asked for: { id: "REQ_<request>", name }
const chosenCompany = () => { const v = fold($("company").value); return companies.find(c => fold(c.name) === v) || (asked && fold(asked.name) === v ? asked : null); };
// A company that is not in the list. It is asked for (the same request "Missing a company?" makes in lanterN), a
// person approves it, and the documents sent meanwhile wait for it. The database answers "already a company" when
// the name or the website is one it holds, and that company is chosen instead: no second row for one company.
function newCompany() {
  $("cobox").hidden = false; $("coname").value = $("company").value.trim(); if (!$("cosite").value) $("cosite").value = "https://" + hostOf(TAB.url); $("coname").focus();
  $("cocancel").onclick = () => { $("cobox").hidden = true; };
  $("coask").onclick = async () => {
    const name = $("coname").value.trim(), isin = $("coisin").value.trim().toUpperCase();
    if (name.length < 2) { $("comsg").textContent = "Write the company's name."; return; }
    if (isin && !/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(isin)) { $("comsg").textContent = "An ISIN has 12 characters, like US0378331005."; return; }
    try {
      const j = await rpc("request_company", { name, website: $("cosite").value.trim(), lei: $("colei").value.trim(), note: isin ? `ISIN ${isin}` : null, client: CLIENT });
      if (j.held) { $("company").value = j.name; $("msg").textContent = `${j.name} is already a company of lanterN: chosen.`; }
      else { asked = { id: `REQ_${j.request}`, name: j.name }; $("company").value = j.name;
        $("msg").textContent = `${j.name} is asked for. You can send its documents now: they wait until the company is approved (table company_requests in Airtable, box approve).`; }
      $("cobox").hidden = true; await markHeld();
    } catch (e) { $("comsg").textContent = `Not added: ${e.message}`; }
  };
}
function render() {
  const list = $("list"); list.innerHTML = "";
  if (!docs.length) list.append(el("div", "empty", "No link to a PDF on this page. Open the page that lists the documents, or the PDF itself, and click the button again."));
  docs.forEach((d, i) => {
    const row = el("label", "doc" + (d.sure ? "" : " maybe")); const box = el("input"); box.type = "checkbox"; box.checked = d.sure && looksReport(d) && !d.held; box.dataset.i = i;
    box.onchange = count;
    row.append(box, el("span", "t", d.text || d.url.split("/").pop()), el("span", "u", d.url)); const s = el("span", "s", ""); row.append(s); d.status = s; d.box = box;
    if (d.held) { box.disabled = true; s.textContent = "already in lanterN"; s.className = "s ok"; }
    list.append(row);
  });
  count();
}
// Ticked from the start only when the link reads like a report or a policy: a home page also links magazines,
// leaflets and terms of use, and those are for the person to tick, not for the button to send by default.
const REPORT = /annual|report|informe|memoria|rapport|bericht|relat[oó]rio|bilan|sustainab|sostenib|nachhaltig|durab|esg|csr|rse|integrated|integrado|climat|clima|tcfd|cdp|gri|polic|pol[ií]tica|politique|code|c[oó]digo|conduct|ethic|[eé]tic|human.?rights|derechos|slavery|10-?k|20-?f|accounts|cuentas|financ|governance|gobierno|gouvernance|remunera|statement|universal.registration|proxy/i;
const pathWords = u => { try { return decodeURIComponent(new URL(u).pathname).replace(/[-_+./]/g, " "); } catch (e) { return ""; } };
const looksReport = d => REPORT.test(d.text + " " + pathWords(d.url));
// Before anything is downloaded: the links lanterN already holds for the chosen company are shown as held, not offered.
async function markHeld() {
  const company = chosenCompany(); docs.forEach(d => { d.held = false; });
  if (company && docs.length) { try { const held = new Set(await rpc("upload_known", { company: company.id, urls: docs.map(d => d.url) })); docs.forEach(d => { d.held = held.has(d.url); }); } catch (e) {} }
  render();
}
function count() { const n = docs.filter(d => d.box && d.box.checked).length; const h = docs.filter(d => d.held).length; $("count").textContent = `${docs.length} document link${docs.length === 1 ? "" : "s"} on this page, ${n} ticked${h ? `, ${h} already in lanterN` : ""}`; $("go").disabled = !n; }
async function start() {
  const over = await store.get("config"); if (over && over.url) conf = over;          // a test database, set by hand
  if (/__SUPABASE_/.test(conf.url)) { document.querySelector("main").textContent = "This copy of the extension was not built by the site: download it from the lanterN page."; return; }
  $("out").onclick = async e => { e.preventDefault(); await store.set("session", null); location.reload(); };
  if (!(await token())) { showSignIn(); return; }
  $("sign").hidden = true; $("work").hidden = false; $("out").hidden = false;
  $("page").textContent = TAB.title ? `${TAB.title} · ${hostOf(TAB.url)}` : TAB.url;
  try { companies = await rpc("upload_companies"); } catch (e) { $("msg").textContent = `The companies could not be read: ${e.message}`; if (/sign in/.test(e.message)) showSignIn(); return; }
  const dl = $("companies"); for (const c of companies) { const o = el("option"); o.value = c.name; dl.append(o); }
  const guess = companyOfHost(hostOf(TAB.url)); if (guess) $("company").value = guess.name;
  docs = await listDocuments(); await markHeld();
  $("company").onchange = markHeld;
  $("all").onclick = e => { e.preventDefault(); docs.forEach(d => { if (!d.box.disabled) d.box.checked = true; }); count(); };
  $("none").onclick = e => { e.preventDefault(); docs.forEach(d => d.box.checked = false); count(); };
  $("go").onclick = sendAll; $("watch").onclick = watchPage; $("newco").onclick = e => { e.preventDefault(); newCompany(); };
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
async function sendAll() {
  const company = chosenCompany(); if (!company) { $("msg").textContent = "Choose the company from the list first."; $("company").focus(); return; }
  const todo = docs.filter(d => d.box.checked && !d.done && !d.held); if (!todo.length) return;
  $("go").disabled = true; let ok = 0, bad = 0;
  for (const d of todo) {
    d.status.className = "s"; d.status.textContent = "downloading…";
    try { d.status.textContent = await sendOne(d, company); d.status.className = "s ok"; d.done = true; d.box.checked = false; d.box.disabled = true; ok++; }
    catch (e) { d.status.textContent = `not sent: ${e.message}`; d.status.className = "s bad"; bad++; }
    $("msg").textContent = `${ok + bad} of ${todo.length}…`;
  }
  $("msg").textContent = `${ok} sent to ${company.name}${bad ? `, ${bad} not sent` : ""}. ` + (/^REQ_/.test(company.id) ? "They wait for the company to be approved (company_requests in Airtable)."
    : "They appear in the library within the half hour, and in the search after the night's load.");
  count();
}
async function watchPage() {
  const company = chosenCompany(); if (!company) { $("msg").textContent = "Choose the company from the list first."; $("company").focus(); return; }
  if (/^REQ_/.test(company.id)) { $("msg").textContent = "The page can be added a quarter of an hour after the company is approved (close this window and click the button again)."; return; }
  if (!/^https?:/.test(TAB.url) || isPdfUrl(TAB.url)) { $("msg").textContent = "This is a document, not a page that lists documents."; return; }
  try { const j = await rpc("add_sources", { company: company.id, urls: [TAB.url.split("#")[0]], client: CLIENT });
    $("msg").textContent = j.added ? `Page added for ${company.name}: lanterN looks at it for new documents.` : j.again ? "You had already added this page." : j.known ? "lanterN already holds this address." : "Not a link lanterN can keep."; }
  catch (e) { $("msg").textContent = `Not added: ${e.message}`; }
}
start();
