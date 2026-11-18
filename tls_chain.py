"""
tls_chain.py: read a site whose server sends its certificate without the one that signed it.

A browser reads such a site without complaint: the certificate says where its issuer's certificate is (the
"CA Issuers" address of its Authority Information Access field) and the browser fetches it. Python does not, and
fails with "unable to get local issuer certificate". On 18 Nov 2026 that was 693 of the 1,592 documents with no
file, from 20 sites (recover_attach.py).

This module does what the browser does and trusts nothing more than before:
  - the missing certificates are fetched from the address the site's own certificate names (up to four levels);
  - a fetched certificate is used only to build the chain, never as a root: one that signs itself is thrown away,
    and so is one whose name is not the issuer's name asked for;
  - the chain must still end in a root of the usual store (certifi), the name of the site must still match, and
    dates and signatures are checked as always. A partial chain is not accepted (Python 3.13 accepts one by default).
A site whose certificate is expired, of another name or signed by nobody known fails exactly as before.
"""
import ssl, threading

import certifi
import requests
from requests.adapters import HTTPAdapter
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

MAX_LEVELS = 4
MAX_BYTES = 200_000


def issuer_urls(cert):
    """The http addresses where the certificate says its issuer's certificate is."""
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    return [d.access_location.value for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS and str(getattr(d.access_location, "value", "")).lower().startswith("http")]


def parse_certs(data):
    """The certificates in what an issuer address returned: one in DER, one or more in PEM, or a PKCS7 bundle."""
    data = data or b""
    if b"-----BEGIN CERTIFICATE-----" in data:
        try:
            return x509.load_pem_x509_certificates(data)
        except ValueError:
            return []
    for load in (lambda: [x509.load_der_x509_certificate(data)], lambda: list(pkcs7.load_der_pkcs7_certificates(data))):
        try:
            return load()
        except Exception:
            continue
    return []


def fetch_http(url):
    r = requests.get(url, timeout=10, stream=True)
    if r.status_code != 200:
        r.close(); return b""
    data = r.raw.read(MAX_BYTES + 1, decode_content=True); r.close()
    return data if len(data) <= MAX_BYTES else b""


def leaf_pem(host, port=443):
    """The certificate the site presents. Read without checking it: nothing is sent or received but the handshake,
    and the certificate is checked afterwards, in the real request."""
    return ssl.get_server_certificate((host, port), timeout=10)


def intermediates(host, port=443, leaf=leaf_pem, fetch=fetch_http):
    """The certificates between the site's own and a root, as PEM, fetched from the addresses the chain itself names."""
    cert = x509.load_pem_x509_certificate(leaf(host, port).encode())
    found = []
    for _ in range(MAX_LEVELS):
        if cert.issuer == cert.subject:
            break
        nxt = None
        for url in issuer_urls(cert):
            try:
                got = parse_certs(fetch(url))
            except Exception:
                got = []
            nxt = next((c for c in got if c.subject == cert.issuer and c.issuer != c.subject), None)
            if nxt is not None:
                break
        if nxt is None:
            break
        found.append(nxt.public_bytes(serialization.Encoding.PEM).decode())
        cert = nxt
    return found


def context(extra_pems, roots=None):
    """A checking context: the usual roots (or the given ones, for tests), the fetched certificates to build the
    chain with, and no partial chain."""
    ctx = ssl.create_default_context(cafile=roots or certifi.where())
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    usable = [p for p in extra_pems if (lambda c: c.issuer != c.subject)(x509.load_pem_x509_certificate(p.encode()))]
    if usable:
        ctx.load_verify_locations(cadata="\n".join(usable))
    return ctx


class _Adapter(HTTPAdapter):
    def __init__(self, ctx, **kw):
        self._ctx = ctx; super().__init__(**kw)

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._ctx
        return super().init_poolmanager(*a, **kw)


_lock = threading.Lock()
_sessions = {}                     # host -> a session that completes its chain, or None when there is nothing to complete


def session_for(host, port=443):
    key = (host, port)
    with _lock:
        if key in _sessions:
            return _sessions[key]
    if True:
        try:
            pems = intermediates(host, port)
        except Exception:
            pems = []
        if pems:
            new = requests.Session(); new.mount("https://", _Adapter(context(pems)))
        else:
            new = None
    with _lock:
        return _sessions.setdefault(key, new)


def get(url, **kw):
    """requests.get with the site's chain completed; None when nothing could be fetched for that site."""
    from urllib.parse import urlparse
    u = urlparse(url)
    s = session_for((u.hostname or "").lower(), u.port or 443)
    return None if s is None else s.get(url, **kw)
