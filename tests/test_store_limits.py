"""Offline tests: the text store waits for GitHub's hourly reset instead of failing, and a full-text
shard reads only the index parts of its own run (27 Oct 2026: eight shards each downloading all 597
docs files spent the token's 5,000 calls an hour, and the night's text jobs failed together)."""
import os, sys, time, unittest
from unittest import mock

os.environ.setdefault("AIRTABLE_TOKEN", "x"); os.environ.setdefault("AIRTABLE_BASE", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import text_core as tc  # noqa: E402
import build_fulltext as FT  # noqa: E402


class Resp:
    def __init__(self, status, headers=None, text="", body=None):
        self.status_code, self.headers, self.text, self._body = status, headers or {}, text, body

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def request(self, method, url, **kw):
        self.calls += 1
        return self.responses.pop(0)


class HourlyLimit(unittest.TestCase):
    def test_spent_allowance_waits_for_the_reset(self):
        reset = str(int(time.time()) + 1200)
        spent = Resp(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}, "API rate limit exceeded")
        s = Session([spent, Resp(200, body=[])])
        store = tc.ReleaseStore("owner/repo", "token", session=s)
        with mock.patch.object(tc.time, "sleep") as sleep:
            r = store._req("GET", "https://api.github.com/x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(s.calls, 2)
        self.assertTrue(any(1150 <= c.args[0] <= 1260 for c in sleep.call_args_list))

    def test_other_errors_are_not_waited_for(self):
        self.assertIsNone(tc.ReleaseStore.hourly_reset_wait(Resp(403, {"X-RateLimit-Remaining": "12"})))
        self.assertIsNone(tc.ReleaseStore.hourly_reset_wait(Resp(500)))
        far = str(int(time.time()) + 3 * 3600)
        self.assertIsNone(tc.ReleaseStore.hourly_reset_wait(Resp(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": far})))


class ShardIndex(unittest.TestCase):
    def test_a_shard_reads_only_its_own_runs_parts(self):
        listing = {n: {} for n in [
            "docs-20261026T010000-r40300000000a1-00-001.jsonl.gz", "pages-20261026T010000-r40300000000a1-00-001.jsonl.gz",
            "docs-20261027T011500-r40428519492a1-03-001.jsonl.gz", "pages-20261027T011500-r40428519492a1-03-001.jsonl.gz",
            "docs-20261027T012100-r40428519492a2-03-001.jsonl.gz", "pages-20261027T012100-r40428519492a2-03-001.jsonl.gz"]}
        store = mock.Mock()
        store.assets.return_value = listing
        with mock.patch.object(tc, "sync_down") as down, mock.patch.object(tc, "load_docs_index", return_value={}):
            FT.load_index(store, "/tmp", "40428519492")
        self.assertEqual(sorted(down.call_args.args[1]), ["docs-20261027T011500-r40428519492a1-03-001.jsonl.gz",
                                                          "docs-20261027T012100-r40428519492a2-03-001.jsonl.gz"])
        with mock.patch.object(tc, "sync_down") as down, mock.patch.object(tc, "load_docs_index", return_value={}):
            FT.load_index(store, "/tmp")
        self.assertEqual(len(down.call_args.args[1]), 3)


if __name__ == "__main__":
    unittest.main()
