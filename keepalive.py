"""One connection to Airtable kept open for a whole run (22 Nov 2026).

Every call to Airtable opened a new connection: `requests.request` builds a session, connects, does the TLS
handshake, sends one request and closes. A sweep of report_library is about 2,800 calls one after another. From
GitHub's machines (in the United States, like Airtable) the handshake cost little: 21 minutes for the sweep in
Key reports. From lantrNa's server in Germany each handshake crosses the Atlantic several times: the same sweep
took 43 minutes (0.92 s a call against 0.45 s), and Key reports was cancelled at its 60 minute limit on its first
run there.

`session()` gives a `requests.Session` that keeps its connection open, one per thread (a session is not promised
to be safe when shared between threads). A connection the other side closed in the meantime fails like any dropped
connection and is retried by the caller's own ladder.
"""
import threading

_local = threading.local()


def session():
    import requests
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s
