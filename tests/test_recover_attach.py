"""recover_attach.py: a failed download says which failure it was (18 Nov 2026: 1,362 of 1,563 rows a bare "error")."""
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests
import recover_attach as R


class Answer:
    def __init__(self, status, body=b""):
        self.status_code = status; self.body = body
    def iter_content(self, n):
        yield self.body
    def close(self):
        pass


class WhyADownloadFailed(unittest.TestCase):
    def test_each_failure_has_its_kind(self):
        ex = requests.exceptions
        cases = [(ex.SSLError("certificate verify failed"), "error_tls"),
                 (ex.ConnectTimeout("timed out"), "error_timeout"), (ex.ReadTimeout("read timed out"), "error_timeout"),
                 (ex.ConnectionError("Failed to resolve 'old.example' ([Errno -2] Name or service not known)"), "error_dns"),
                 (ex.ConnectionError("NameResolutionError"), "error_dns"),
                 (ex.ConnectionError("[Errno 111] Connection refused"), "error_refused"),
                 (ex.ConnectionError("('Connection aborted.', RemoteDisconnected('closed'))"), "error_reset"),
                 (ex.ConnectionError("Network is unreachable"), "error_connection"),
                 (ex.TooManyRedirects("30"), "error_redirects"), (ex.InvalidURL("no host"), "error_bad_url"), (ValueError("x"), "error_other")]
        for error, kind in cases:
            with mock.patch.object(R.requests, "get", side_effect=error):
                self.assertEqual(R.download("https://old.example/a.pdf"), (None, kind), error)

    def test_an_answer_of_the_site_keeps_its_code_and_the_known_ones_their_names(self):
        for status, body, want in [(503, b"", "http_503"), (429, b"", "http_429"), (403, b"", "forbidden"), (404, b"", "not_found"),
                                   (200, b"<html>", "not_pdf"), (200, b"%PDF-1.7 x", "ok")]:
            with mock.patch.object(R.requests, "get", return_value=Answer(status, body)):
                self.assertEqual(R.download("https://x.example/a.pdf")[1], want)
        self.assertEqual(set(R.REASON_TO_STATUS), {"not_found", "forbidden", "too_big", "not_pdf"})      # only these end a row: every other kind is retried


if __name__ == "__main__":
    unittest.main()
