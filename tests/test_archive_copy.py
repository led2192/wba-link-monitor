import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import archive_copy as C


class Resp:
    def __init__(self, status=200, content=b"", data=None, text=""):
        self.status_code, self.content, self._j, self.text = status, content, data, text
    def json(self): return self._j


class Net:
    """A storage and a file host in memory: uploads are kept, the list answers with their sizes."""
    def __init__(self, pdf=b"%PDF-1.7 hello", limit=None, lose=0):
        self.pdf, self.limit, self.lose, self.objects, self.bucket, self.calls = pdf, limit, lose, {}, False, []
    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if "/storage/v1/bucket/" in url:
            return Resp(200 if self.bucket else 404)
        return Resp(200, self.pdf)
    def post(self, url, headers=None, json=None, data=None, **kw):
        self.calls.append(("POST", url))
        if url.endswith("/storage/v1/bucket"):
            self.bucket = True; return Resp(200, data={})
        if "/object/list/" in url:
            return Resp(200, data=[{"name": k.rsplit("/", 1)[1], "metadata": {"size": len(v) - self.lose}} for k, v in self.objects.items()
                                   if k.startswith(json["prefix"] + "/") and json["search"] in k])
        if self.limit is not None and len(data) > self.limit:
            return Resp(413, text="The object exceeded the maximum allowed size")
        assert headers["x-upsert"] == "true" and headers["Authorization"] == "Bearer k"
        self.objects[url.split("/object/archive/", 1)[1]] = data
        return Resp(200, data={})


class Copy(unittest.TestCase):
    def test_path_and_note(self):
        self.assertEqual(C.object_path("PT_00001", "PT_00001-ab12"), "PT_00001/PT_00001-ab12.pdf")
        self.assertEqual(C.object_path("../x", "a b/c"), ".._x/a_b_c.pdf".replace("..", ".."))      # nothing climbs out of the folder
        self.assertEqual(C.stored_note("PT_1/x.pdf", 14), "archive/PT_1/x.pdf (14 bytes)")

    def test_the_bucket_is_made_once(self):
        net = Net(); st = C.Storage("https://p.example.co/", "k", net)
        self.assertEqual(st.ensure_bucket(), "made"); self.assertEqual(st.ensure_bucket(), "there")

    def test_a_file_is_stored_only_when_its_size_is_confirmed(self):
        net = Net(); st = C.Storage("https://p.example.co", "k", net)
        what, note = C.copy_one(st, net, "PT_00001", "L1", {"url": "https://files.example/x", "size": 14})
        self.assertEqual((what, note), ("stored", "archive/PT_00001/L1.pdf (14 bytes)"))
        self.assertEqual(net.objects["PT_00001/L1.pdf"], b"%PDF-1.7 hello")
        net2 = Net(lose=1); st2 = C.Storage("https://p.example.co", "k", net2)
        self.assertEqual(C.copy_one(st2, net2, "PT_00001", "L1", {"url": "u", "size": 14}), ("size differs", None))   # not marked as stored

    def test_what_cannot_be_copied_is_said_not_marked(self):
        net = Net(limit=5); st = C.Storage("https://p.example.co", "k", net)
        self.assertEqual(C.copy_one(st, net, "PT_1", "L", {"url": "u", "size": 14}), ("too large", None))
        self.assertEqual(C.copy_one(st, net, "PT_1", "L", None), ("no file", None))
        self.assertEqual(C.copy_one(st, net, "PT_1", "L", {"url": "u", "size": C.MAX_BYTES + 1}), ("too large", None))
        class Down(Net):
            def get(self, url, **kw): return Resp(404)
        d = Down(); self.assertEqual(C.copy_one(C.Storage("https://p", "k", d), d, "PT_1", "L", {"url": "u"})[0], "failed: download HTTP 404")


class Purge(unittest.TestCase):
    def test_an_archived_row_is_purged_only_with_its_copy(self):
        os.environ.setdefault("AIRTABLE_TOKEN", "t"); os.environ.setdefault("AIRTABLE_BASE", "appX")
        import purge_discarded as P
        self.assertFalse(P.has_copy({"discard_reason": P.ARCHIVED}))
        self.assertFalse(P.has_copy({"discard_reason": P.ARCHIVED, "stored_file": "  "}))
        self.assertTrue(P.has_copy({"discard_reason": P.ARCHIVED, "stored_file": "archive/PT_1/x.pdf (9 bytes)"}))
        self.assertTrue(P.has_copy({"discard_reason": "Duplicate of another document"}))      # other discards: as before
        self.assertIn(P.ARCHIVED, P.MACHINE_REASONS)


if __name__ == "__main__":
    unittest.main()
