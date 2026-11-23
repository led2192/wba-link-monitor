"""The connection to Airtable is kept open for a run (keepalive.py): one session a thread, used by the callers."""
import os, sys, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import keepalive
import monitor_core


class KeepAlive(unittest.TestCase):
    def test_one_session_a_thread(self):
        a = keepalive.session()
        self.assertIs(a, keepalive.session())
        other = []
        t = threading.Thread(target=lambda: other.append(keepalive.session())); t.start(); t.join()
        self.assertIsNot(other[0], a)

    def test_airtable_request_uses_it_and_never_a_new_connection_a_call(self):
        resp = mock.Mock(status_code=200); resp.json.return_value = {"records": []}
        s = mock.Mock(); s.request.return_value = resp
        with mock.patch.object(keepalive, "session", return_value=s), \
             mock.patch.object(monitor_core.requests, "request", side_effect=AssertionError("a new connection for one call")):
            monitor_core.airtable_request("GET", "https://api.airtable.com/v0/app/t", {})
            monitor_core.airtable_request("GET", "https://api.airtable.com/v0/app/t", {})
        self.assertEqual(s.request.call_count, 2)

    def test_the_sweeping_scripts_use_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("monitor_core.py", "base_health.py", "dedupe_library.py"):
            src = open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("keepalive.session()", src, name)
            self.assertNotIn("requests.request(", src, name)


if __name__ == "__main__":
    unittest.main()
