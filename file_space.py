#!/usr/bin/env python3
"""
file_space.py: how much room lanterN's own copy of the documents needs, and what could be saved.

Measured before choosing where the files live (the Disclosure Library holds 755 GB of attachments, 14 Nov 2026).
Two parts, both read-only:

  1. The whole library, from lanterN's nightly copy (public.library): how many documents have a file, how many
     GB they add up to, how much of that is the same file stored more than once (content_hash), how the bytes
     are spread over sizes, years and types.
  2. A sample (--sample, default 500) of attached PDFs, downloaded to this machine and measured three ways:
     as they are; compressed whole without loss (zstd, level 19); and rewritten with their images brought down
     to 150 dots an inch (Ghostscript /ebook), which is no longer the original file. For the rewritten file it
     also checks that the pages and the text are still there.

It prints numbers only: no address, no title, no company. Nothing is written anywhere but the quality log's run
row; the downloaded files are deleted as each is measured.
Env: SUPABASE_DB_URL, AIRTABLE_TOKEN, AIRTABLE_BASE. Needs gs (ghostscript) and the zstandard package for part 2;
without them that measure is reported as not made.
"""
import argparse, collections, os, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

SIZE_BANDS = [(0, 1, "under 1 MB"), (1, 5, "1 to 5 MB"), (5, 20, "5 to 20 MB"), (20, 50, "20 to 50 MB"), (50, 10 ** 9, "50 MB and over")]


def band(mb):
    for lo, hi, name in SIZE_BANDS:
        if lo <= mb < hi:
            return name
    return SIZE_BANDS[-1][2]


def whole(rows):
    """rows: [(size in MB or None, content hash or None, year or None, type or None)], one a document with a file.
    Returns a dict of the totals."""
    sized = [(float(s), h, y, t) for s, h, y, t in rows if s is not None and float(s) > 0]
    total = sum(s for s, _, _, _ in sized)
    seen, extra_mb, extra_n = {}, 0.0, 0
    for s, h, _, _ in sized:
        if not h or h == "unavailable":
            continue
        if h in seen:
            extra_mb += s
            extra_n += 1
        else:
            seen[h] = s
    by_band, by_year, by_type = collections.Counter(), collections.Counter(), collections.Counter()
    n_band = collections.Counter()
    for s, _, y, t in sized:
        by_band[band(s)] += s
        n_band[band(s)] += 1
        by_year[y if y and y >= 2015 else ("before 2015" if y else "no year")] += s
        by_type[t or "no type"] += s
    return {"documents": len(rows), "with_size": len(sized), "total_mb": total, "repeat_files": extra_n, "repeat_mb": extra_mb,
            "by_band": by_band, "n_band": n_band, "by_year": by_year, "by_type": by_type}


def gb(mb):
    return f"{mb / 1024:,.1f} GB"


def whole_lines(w):
    t = w["total_mb"] or 1
    out = [f"documents with a file: {w['documents']:,}; with a size on the row: {w['with_size']:,}; together {gb(w['total_mb'])}",
           f"the same file stored again (same hash): {w['repeat_files']:,} files, {gb(w['repeat_mb'])} ({100 * w['repeat_mb'] / t:.1f}% of the bytes)",
           "by size of file:"]
    for _, _, name in SIZE_BANDS:
        out.append(f"  {name}: {w['n_band'][name]:,} files, {gb(w['by_band'][name])} ({100 * w['by_band'][name] / t:.0f}%)")
    out.append("by year: " + "; ".join(f"{k} {gb(v)}" for k, v in sorted(w["by_year"].items(), key=lambda kv: str(kv[0]))))
    out.append("by type, the ten largest: " + "; ".join(f"{k} {gb(v)}" for k, v in w["by_type"].most_common(10)))
    return out


def gs_cmd(src, dst):
    """Ghostscript, images to 150 dots an inch (/ebook): the usual 'smaller PDF' setting."""
    return ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6",
            "-dPDFSETTINGS=/ebook", f"-sOutputFile={dst}", src]


def text_of(path):
    """(pages, characters of text) of a PDF; (None, None) when it cannot be read."""
    try:
        from pypdf import PdfReader
        r = PdfReader(path)
        n = len(r.pages)
        chars = sum(len((r.pages[i].extract_text() or "")) for i in range(min(n, 30)))     # the first 30 pages are enough to compare
        return n, chars
    except Exception:
        return None, None


def measure(path, tmp):
    """One file three ways. Returns a dict of byte counts and what the rewrite kept."""
    out = {"bytes": os.path.getsize(path), "zstd": None, "gs": None, "gs_pages_same": None, "gs_text_kept": None}
    try:
        import zstandard
        with open(path, "rb") as fh:
            out["zstd"] = len(zstandard.ZstdCompressor(level=19).compress(fh.read()))
    except Exception:
        pass
    if shutil.which("gs"):
        dst = os.path.join(tmp, os.path.basename(path) + ".gs.pdf")
        try:
            r = subprocess.run(gs_cmd(path, dst), timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                out["gs"] = os.path.getsize(dst)
                p0, c0 = text_of(path)
                p1, c1 = text_of(dst)
                if p0 is not None and p1 is not None:
                    out["gs_pages_same"] = (p0 == p1)
                    out["gs_text_kept"] = (c1 >= 0.95 * c0) if c0 else (c1 == 0)
        except Exception:
            pass
        finally:
            if os.path.exists(dst):
                os.remove(dst)
    return out


def sample_lines(results, asked):
    """What the sample says. results: the dicts of measure()."""
    n = len(results)
    if not n:
        return [f"sample: none of the {asked} files could be downloaded"]
    b = sum(r["bytes"] for r in results)
    out = [f"sample: {n:,} of {asked:,} files downloaded and measured, {b / 1e9:.2f} GB as they are"]
    z = [r for r in results if r["zstd"] is not None]
    if z:
        zb, zo = sum(r["zstd"] for r in z), sum(r["bytes"] for r in z)
        out.append(f"compressed whole without loss (zstd 19): {100 * (1 - zb / zo):.1f}% smaller ({len(z):,} files)")
    else:
        out.append("compressed whole without loss: not measured (zstandard is not installed)")
    g = [r for r in results if r["gs"] is not None]
    if g:
        go = sum(r["bytes"] for r in g)
        kept = sum(min(r["gs"], r["bytes"]) for r in g)                 # a rewrite that comes out larger is not kept
        smaller = sum(1 for r in g if r["gs"] < 0.9 * r["bytes"])
        larger = sum(1 for r in g if r["gs"] >= r["bytes"])
        pages = [r for r in g if r["gs_pages_same"] is not None]
        out.append(f"images brought down to 150 dpi (Ghostscript /ebook): {100 * (1 - kept / go):.1f}% smaller over {len(g):,} files "
                   f"when the original is kept wherever the rewrite is not smaller; {smaller:,} files at least 10% smaller, {larger:,} came out larger; "
                   f"{n - len(g):,} could not be rewritten")
        if pages:
            out.append(f"  the rewritten file kept its pages in {sum(1 for r in pages if r['gs_pages_same']):,} of {len(pages):,} "
                       f"and its text (95% of the characters of the first 30 pages) in {sum(1 for r in pages if r['gs_text_kept']):,} of {len(pages):,}")
        for lo, hi, name in SIZE_BANDS:
            part = [r for r in g if lo <= r["bytes"] / 1048576 < hi]
            if part:
                po = sum(r["bytes"] for r in part)
                out.append(f"    {name}: {len(part):,} files, {100 * (1 - sum(min(r['gs'], r['bytes']) for r in part) / po):.0f}% smaller")
    else:
        out.append("images brought down to 150 dpi: not measured (gs is not installed)")
    return out


def attachment_url(record_id):
    from monitor_core import airtable_request
    base, token = os.environ["AIRTABLE_BASE"], os.environ["AIRTABLE_TOKEN"]
    j = airtable_request("GET", f"https://api.airtable.com/v0/{base}/{quote('report_library')}/{record_id}",
                         {"Authorization": f"Bearer {token}"}).json()      # one record: Airtable refuses fields[] here (422)
    files = j.get("fields", {}).get("file") or []
    return files[0].get("url") if files else None


def one(record_id, tmp):
    import requests
    try:
        url = attachment_url(record_id)
        if not url:
            return None
        path = os.path.join(tmp, record_id + ".pdf")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        try:
            return measure(path, tmp)
        finally:
            os.remove(path)
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    for v in ("SUPABASE_DB_URL", "AIRTABLE_TOKEN", "AIRTABLE_BASE"):
        if not os.environ.get(v):
            sys.exit(f"Set {v}.")
    import evidence_db, qa
    run = qa.Run("file_space.py")
    with evidence_db.connect() as conn:
        cur = conn.cursor()
        cur.execute("select nullif(fields->>'file_size_mb', '')::float, nullif(fields->>'content_hash', ''), year_ours, type_ours "
                    "from public.library where gone is null and nullif(fields->>'file_size_mb', '') is not null")
        rows = cur.fetchall()
        cur.execute("select count(*), count(*) filter (where discard) from public.library where gone is null")
        docs, discarded = cur.fetchone()
        cur.execute("select record_id from public.library where gone is null and nullif(fields->>'file_size_mb', '') is not null "
                    "order by md5(library_id) limit %s", (a.sample,))
        picked = [r[0] for r in cur.fetchall()]
    w = whole(rows)
    print(f">>> the whole library: {docs:,} documents in lanterN's copy ({discarded:,} of them discarded)")
    print("\n".join(whole_lines(w)), flush=True)
    results = []
    if picked:
        t0 = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            with ThreadPoolExecutor(max_workers=a.workers) as ex:
                results = [r for r in ex.map(lambda rid: one(rid, tmp), picked) if r]
        print(f">>> the sample ({time.time() - t0:.0f} s)")
        print("\n".join(sample_lines(results, len(picked))), flush=True)
    run.finish("ok", rows_in=len(rows), rows_out=0,
               counters={"documents": docs, "with_size": w["with_size"], "total_gb": round(w["total_mb"] / 1024, 1),
                         "repeat_gb": round(w["repeat_mb"] / 1024, 1), "sample": len(results)}, notes="read-only measure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
