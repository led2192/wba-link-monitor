"""tls_chain.py: a site that sends its certificate without its issuer's is read as a browser reads it, and nothing
more is trusted. A real chain (root, intermediate, site) is made here and served on this machine: no network."""
import datetime, os, socket, ssl, sys, tempfile, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

import tls_chain as T
import recover_attach as R

NOW = datetime.datetime.now(datetime.timezone.utc)
PEM = lambda c: c.public_bytes(serialization.Encoding.PEM).decode()
DER = lambda c: c.public_bytes(serialization.Encoding.DER)


def make(name, issuer=None, issuer_key=None, ca=False, aia=None, san=None, days=30):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer.subject if issuer else subject).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(NOW - datetime.timedelta(days=1)).not_valid_after(NOW + datetime.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    b = b.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key((issuer_key or key).public_key()), critical=False)
    b = b.add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False, key_agreement=False,
                                      key_cert_sign=ca, crl_sign=ca, encipher_only=False, decipher_only=False), critical=True)
    if aia:
        b = b.add_extension(x509.AuthorityInformationAccess([x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(aia))]), critical=False)
    if san:
        b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
    return b.sign(issuer_key or key, hashes.SHA256()), key


class Chain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.root_key = make("Test Root", ca=True)
        cls.mid, cls.mid_key = make("Test Intermediate", cls.root, cls.root_key, ca=True, aia="http://ca.test/root.cer")
        cls.leaf, cls.leaf_key = make("localhost", cls.mid, cls.mid_key, aia="http://ca.test/mid.cer", san="localhost")
        cls.rogue, cls.rogue_key = make("Test Intermediate", ca=True)            # signs itself under the issuer's name
        cls.dir = tempfile.mkdtemp()
        cls.roots = os.path.join(cls.dir, "roots.pem"); open(cls.roots, "w").write(PEM(cls.root))
        cert = os.path.join(cls.dir, "leaf.pem"); open(cert, "w").write(PEM(cls.leaf))              # the leaf alone: the chain is incomplete
        key = os.path.join(cls.dir, "leaf.key")
        open(key, "wb").write(cls.leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); srv.load_cert_chain(cert, key)
        cls.sock = socket.socket(); cls.sock.bind(("127.0.0.1", 0)); cls.sock.listen(8); cls.port = cls.sock.getsockname()[1]

        def serve():
            while True:
                try:
                    c, _ = cls.sock.accept()
                except OSError:
                    return
                try:
                    with srv.wrap_socket(c, server_side=True) as t:
                        t.recv(4096); t.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\n%PDF-1.7")
                except Exception:
                    pass
        threading.Thread(target=serve, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.sock.close()

    def handshake(self, ctx):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s, ctx.wrap_socket(s, server_hostname="localhost") as t:
            return t.version()

    def test_the_missing_certificate_is_fetched_from_where_the_chain_says(self):
        asked = []
        fetch = lambda url: (asked.append(url), {"http://ca.test/mid.cer": DER(self.mid), "http://ca.test/root.cer": DER(self.root)}[url])[1]
        got = T.intermediates("localhost", leaf=lambda h, p: PEM(self.leaf), fetch=fetch)
        self.assertEqual(got, [PEM(self.mid)])                                 # the root, which signs itself, is never taken from the network
        self.assertEqual(asked, ["http://ca.test/mid.cer", "http://ca.test/root.cer"])
        self.assertEqual(T.intermediates("localhost", leaf=lambda h, p: PEM(self.leaf), fetch=lambda u: PEM(self.mid).encode()), [PEM(self.mid)])   # PEM is read too

    def test_a_certificate_that_signs_itself_or_of_another_name_is_thrown_away(self):
        self.assertEqual(T.intermediates("localhost", leaf=lambda h, p: PEM(self.leaf), fetch=lambda u: DER(self.rogue)), [])
        other, _ = make("Somebody Else", self.root, self.root_key, ca=True)
        self.assertEqual(T.intermediates("localhost", leaf=lambda h, p: PEM(self.leaf), fetch=lambda u: DER(other)), [])
        self.assertEqual(T.intermediates("localhost", leaf=lambda h, p: PEM(self.leaf), fetch=lambda u: b"not a certificate"), [])
        self.assertEqual(T.intermediates("localhost", leaf=lambda h, p: PEM(self.leaf), fetch=mock.Mock(side_effect=OSError("down"))), [])

    def test_the_site_is_read_only_when_the_chain_ends_in_a_known_root(self):
        with self.assertRaises(ssl.SSLCertVerificationError):                   # as today: the chain is incomplete
            self.handshake(T.context([], roots=self.roots))
        self.assertTrue(self.handshake(T.context([PEM(self.mid)], roots=self.roots)))     # completed: read
        unknown = os.path.join(self.dir, "other.pem"); open(unknown, "w").write(PEM(self.rogue))
        with self.assertRaises(ssl.SSLCertVerificationError):                   # the intermediate alone is not a root: no partial chain
            self.handshake(T.context([PEM(self.mid)], roots=unknown))
        with self.assertRaises(ssl.SSLCertVerificationError):                   # a self-signed certificate handed in is not loaded
            self.handshake(T.context([PEM(self.root)], roots=unknown))
        ctx = T.context([PEM(self.mid)], roots=self.roots)
        with self.assertRaises(ssl.SSLCertVerificationError):                   # the name must still match
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s, ctx.wrap_socket(s, server_hostname="other.example"):
                pass

    def test_the_download_tries_the_completed_chain_once_and_only_for_that_failure(self):
        ex = requests.exceptions
        chain = ex.SSLError("certificate verify failed: unable to get local issuer certificate")
        ok = mock.Mock(status_code=200); ok.iter_content = lambda n: [b"%PDF-1.7 x"]
        with mock.patch.object(R.requests, "get", side_effect=chain), mock.patch.object(R.tls_chain, "get", return_value=ok) as again:
            self.assertEqual(R.download("https://x.example/a.pdf"), (b"%PDF-1.7 x", "ok"))
            self.assertEqual(again.call_count, 1)
        with mock.patch.object(R.requests, "get", side_effect=chain), mock.patch.object(R.tls_chain, "get", return_value=None):
            self.assertEqual(R.download("https://x.example/a.pdf"), (None, "error_tls_chain"))
        with mock.patch.object(R.requests, "get", side_effect=chain), mock.patch.object(R.tls_chain, "get", side_effect=chain):
            self.assertEqual(R.download("https://x.example/a.pdf"), (None, "error_tls_chain"))
        with mock.patch.object(R.requests, "get", side_effect=ex.SSLError("certificate has expired")), mock.patch.object(R.tls_chain, "get") as again:
            self.assertEqual(R.download("https://x.example/a.pdf"), (None, "error_tls_expired"))
            again.assert_not_called()

    def test_any_reader_can_ask_for_the_completed_chain_and_other_failures_pass_through(self):
        ex = requests.exceptions
        chain = ex.SSLError("certificate verify failed: unable to get local issuer certificate")
        ok = mock.Mock(status_code=200)
        with mock.patch.object(T.requests, "get", side_effect=chain), mock.patch.object(T, "get", return_value=ok) as again:
            self.assertIs(T.requests_get("https://x.example/a.pdf", timeout=5), ok)
            again.assert_called_once_with("https://x.example/a.pdf", timeout=5)
        with mock.patch.object(T.requests, "get", side_effect=chain), mock.patch.object(T, "get", return_value=None):
            with self.assertRaises(ex.SSLError):
                T.requests_get("https://x.example/a.pdf")
        for other in (ex.SSLError("certificate has expired"), ex.ConnectTimeout("slow"), ex.ConnectionError("down")):
            with mock.patch.object(T.requests, "get", side_effect=other), mock.patch.object(T, "get") as again:
                with self.assertRaises(type(other)):
                    T.requests_get("https://x.example/a.pdf")
                again.assert_not_called()
        with mock.patch.object(T.requests, "get", return_value=ok), mock.patch.object(T, "get") as again:
            self.assertIs(T.requests_get("https://x.example/a.pdf"), ok); again.assert_not_called()

    def test_a_whole_request_through_the_session_of_a_site(self):
        T._sessions.clear()
        with mock.patch.object(T, "intermediates", return_value=[PEM(self.mid)]), mock.patch.object(T, "context", lambda pems: T.__dict__["_ctx_for_test"](pems)):
            T._ctx_for_test = lambda pems: ssl_context_with(self.roots, pems)
            r = T.get(f"https://localhost:{self.port}/a.pdf", timeout=5)
            self.assertEqual((r.status_code, r.content), (200, b"%PDF-1.7"))
        T._sessions.clear()
        with mock.patch.object(T, "intermediates", return_value=[]):
            self.assertIsNone(T.get(f"https://localhost:{self.port}/a.pdf", timeout=5))
        T._sessions.clear(); del T._ctx_for_test


_real_context = T.context
def ssl_context_with(roots, pems):
    return _real_context(pems, roots=roots)


if __name__ == "__main__":
    unittest.main()
