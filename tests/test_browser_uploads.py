import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import browser_uploads as B

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def up(i, wba="PT_1", url="https://example.com/doc/annual-report-2024.pdf", name="annual-report-2024.pdf", size=5000):
    return {"id": i, "wba_id": wba, "source_url": url, "filename": name, "bytes": size, "path": f"{wba}/x{i}.pdf"}


class Resp:
    def __init__(self, status=200, content=b"", headers=None, data=None, text=""):
        self.status_code, self.content, self.headers, self._j, self.text = status, content, headers or {}, data, text
    def json(self): return self._j


class Net:
    def __init__(self, body=b"%PDF-1.7 x", total=5000, status=206):
        self.body, self.total, self.status = body, total, status
    def get(self, url, headers=None, **kw):
        assert headers["Range"] == "bytes=0-1023" and headers["Authorization"] == "Bearer k"
        return Resp(self.status, self.body, {"Content-Range": f"bytes 0-1023/{self.total}"})
    def post(self, url, headers=None, json=None, **kw):
        assert "/object/sign/uploads/" in url and json == {"expiresIn": B.SIGN_SECONDS}
        return Resp(200, data={"signedURL": "/object/sign/uploads/PT_1/x.pdf?token=t"})


class Plan(unittest.TestCase):
    def test_a_blocked_row_gets_its_file_and_nothing_is_duplicated(self):
        # the harvester's row has its own kind of id and the same address with www and a tracking parameter
        rows = {"PT_1": [("recA", "PT_1-harvest01", "https://www.example.com/doc/annual-report-2024.pdf?utm_source=x", False, False)]}
        (u, act, lid, rec), = B.plan([up(1)], {"PT_1": "Co"}, rows)
        self.assertEqual((act, lid, rec), ("fill", "PT_1-harvest01", "recA"))

    def test_held_new_twice_and_orphan(self):
        rows = {"PT_1": [("recA", "PT_1-h1", "https://example.com/doc/annual-report-2024.pdf", True, False)]}
        ups = [up(1), up(2, url="https://example.com/new.pdf"), up(3, url="https://example.com/new.pdf"), up(4, wba="PT_9")]
        acts = [a for _, a, _, _ in B.plan(ups, {"PT_1": "Co"}, rows)]
        self.assertEqual(acts, ["known", "create", "known", "orphan"])

    def test_a_discarded_row_is_not_filled_and_its_id_is_not_made_again(self):
        url = "https://example.com/a.pdf"
        lid = B.library_id("PT_1", url, "a.pdf", 5000)
        self.assertEqual(B.plan([up(1, url=url)], {"PT_1": "Co"}, {"PT_1": [("recD", lid, url, False, True)]})[0][1], "known")          # same id, discarded: not made again
        self.assertEqual(B.plan([up(1, url=url)], {"PT_1": "Co"}, {"PT_1": [("recD", "PT_1-other", url, False, True)]})[0][1], "create")  # the person sent it: a new row

    def test_ids(self):
        import hashlib
        from ids import normalize
        url = "https://www.Example.com/a.pdf"
        self.assertEqual(B.library_id("PT_1", url, "a.pdf", 1), "PT_1-" + hashlib.sha1(normalize(url).encode()).hexdigest()[:10])   # the inbox's id for that address
        self.assertEqual(B.library_id("PT_1", "", "A.pdf", 7), "PT_1-" + hashlib.sha1(b"upload:PT_1:a.pdf:7").hexdigest()[:10])      # and for a file with none
        self.assertIn("downloaded from https://x.org/a.pdf", B.note_for("a.pdf", "https://x.org/a.pdf"))


class Asked(unittest.TestCase):
    def test_files_for_a_company_asked_for_wait_then_follow_it(self):
        ups = [up(1), up(2, wba="REQ_7"), up(3, wba="REQ_8"), up(4, wba="REQ_9"), up(5, wba="REQ_10"), up(6, wba="REQ_11")]
        reqs = {7: ("waiting", None), 8: ("added", "added as LN_0a1b2c3d"), 9: ("not added", "already a company: PT_00042 has the same name"), 10: ("not added", "no company name")}
        ready, waiting, refused = B.resolve_requests(ups, reqs)
        self.assertEqual([(u["id"], u["wba_id"]) for u in ready], [(1, "PT_1"), (3, "LN_0a1b2c3d"), (4, "PT_00042")])   # the duplicate goes to the company we had
        self.assertEqual([u["id"] for u in waiting], [2])
        self.assertEqual([u["id"] for u, _ in refused], [5, 6])                                                        # refused, or a request that is not there


class Store(unittest.TestCase):
    def test_check_and_sign(self):
        self.assertEqual(B.Storage("https://p.supabase.co/", "k", Net()).check("PT_1/x.pdf", 5000), "ok")
        self.assertIn("4000 bytes", B.Storage("https://p.supabase.co", "k", Net(total=4000)).check("PT_1/x.pdf", 5000))
        self.assertEqual(B.Storage("https://p.supabase.co", "k", Net(body=b"<html>")).check("PT_1/x.pdf", 5000), "the file is not a PDF")
        self.assertIn("HTTP 404", B.Storage("https://p.supabase.co", "k", Net(status=404)).check("PT_1/x.pdf", 5000))
        self.assertEqual(B.Storage("https://p.supabase.co", "k", Net()).signed("PT_1/x.pdf"), "https://p.supabase.co/storage/v1/object/sign/uploads/PT_1/x.pdf?token=t")


class Rules(unittest.TestCase):
    def test_the_script_keeps_the_house_rules(self):
        src = open(os.path.join(ROOT, "browser_uploads.py"), encoding="utf-8").read()
        self.assertIn('ap.add_argument("--commit", action="store_true")', src)               # dry run by default
        self.assertIn("and status = 'stored'", src)                                           # a rerun never takes a file twice
        self.assertNotIn("uploader", src.split("def main")[1])                                # who sent it is never read or printed
        import re
        self.assertIsNone(re.search(r"^\s+requests\s*=", src.split("def main")[1], re.M))      # the name of the library is not given to anything else (11 Nov 2026: the first real run died on it)
        sql = open(os.path.join(ROOT, "supabase", "search.sql"), encoding="utf-8").read()
        self.assertIn("with check (bucket_id = ''uploads'' and public.upload_expected(name))", sql)   # a person can store the registered object only
        self.assertIn("revoke all on function public.upload_begin(text, text, bigint, text, text, text, text) from public, anon;", sql)


if __name__ == "__main__":
    unittest.main()
