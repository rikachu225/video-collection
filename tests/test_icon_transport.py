"""The icon fetcher's lookup deadline, over REAL sockets and a real TLS handshake.

tests/test_streaming.py drives the fetch path with a fake transport, which can't show
what happens inside http.client while the status line and headers are read: there,
every recv only has the per-operation socket timeout, so a server that sends one byte
at a time never trips it. These tests run a throwaway TLS server on 127.0.0.1 (a random
free port) that drips its reply, and call the fetch layer directly.

Only two things are swapped in: the SSRF host check (it refuses 127.0.0.1, by design)
and the trust store (a certificate generated per test run; no key material is kept in
the repo). The connection, TLS and HTTP parsing are the production code paths.
"""
import datetime
import importlib
import ipaddress
import socket
import ssl
import threading
import time

import pytest

x509 = pytest.importorskip("cryptography.x509")   # installed with google-auth (google-genai)
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

DRIP_DELAY = 0.05          # seconds between bytes: far below any per-operation timeout
BUDGET = 1.0               # the lookup deadline these tests set
MARGIN = 1.0               # allowed overrun (thread scheduling, a slow CI runner)
CONTROL_BUDGET = 30.0      # the non-drip controls aren't about timing
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def make_server(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    return server


def _self_signed(tmp_path):
    """A certificate for 127.0.0.1, written to tmp_path. Returns (cert, key) paths."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=False,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _DripServer:
    """Serves ONE TLS connection: reads the request head, sends `head` at once, then
    `drip` one byte per DRIP_DELAY. Stops as soon as the client goes away."""

    def __init__(self, cert, key, head=b"", drip=b""):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.head, self.drip = head, drip
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def url(self, path="/icon.png"):
        return f"https://127.0.0.1:{self.port}{path}"

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        try:
            with self.ctx.wrap_socket(conn, server_side=True) as tls:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = tls.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                if self.head:
                    tls.sendall(self.head)
                for byte in self.drip:
                    if self.stop.wait(DRIP_DELAY):
                        return
                    tls.sendall(bytes([byte]))
        except OSError:                      # the client gave up and closed: expected
            pass

    def close(self):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=10)


@pytest.fixture
def tls_env(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    cert, key = _self_signed(tmp_path)
    monkeypatch.setattr(server, "_host_is_public", lambda host: True)   # 127.0.0.1 is private
    monkeypatch.setattr(server, "_icon_tls_context",
                        lambda: ssl.create_default_context(cafile=str(cert)))
    started = []

    def serve(head=b"", drip=b""):
        srv = _DripServer(cert, key, head, drip)
        started.append(srv)
        return srv

    yield server, serve
    for srv in started:
        srv.close()


def _timed_get(server, url, budget=BUDGET):
    start = time.monotonic()
    got = server._icon_http_get(url, accept="image/*", deadline=start + budget)
    return got, time.monotonic() - start


def test_the_real_transport_fetches_a_normal_response(tls_env):
    # Control: the deadline-aware connection still does a plain HTTPS GET end to end.
    server, serve = tls_env
    srv = serve(head=(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                      b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(PNG)) + PNG)
    got, _ = _timed_get(server, srv.url(), CONTROL_BUDGET)
    assert got == (srv.url(), "image/png", PNG)


def test_a_chunked_response_is_read_over_the_real_transport(tls_env):
    server, serve = tls_env
    body = b"".join(b"%x\r\n%s\r\n" % (len(PNG[i:i + 20]), PNG[i:i + 20])
                    for i in range(0, len(PNG), 20)) + b"0\r\n\r\n"
    srv = serve(head=b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n" + body)
    assert _timed_get(server, srv.url(), CONTROL_BUDGET)[0] == (srv.url(), "image/png", PNG)


@pytest.mark.parametrize("head,drip", [
    # status line, one byte at a time
    (b"", b"HTTP/1.1 200 OK" + b" " * 120 + b"\r\n\r\n"),
    # status line at once, then an endless-looking header
    (b"HTTP/1.1 200 OK\r\n", b"X-Slow: " + b"a" * 120 + b"\r\n\r\n"),
    # many short headers, each well within http.client's limits
    (b"HTTP/1.1 200 OK\r\n", b"X: 1\r\n" * 30 + b"\r\n"),
    # chunked body: the chunk-size line drips inside a single read1()
    (b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nTransfer-Encoding: chunked\r\n\r\n",
     b"0" * 120 + b"4\r\nabcd\r\n0\r\n\r\n"),
    # chunked body: the trailer section drips after the last chunk
    (b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nTransfer-Encoding: chunked\r\n\r\n"
     b"4\r\nabcd\r\n0\r\n", b"X-Trailer: 1\r\n" * 10 + b"\r\n"),
], ids=["status-line", "header-line", "many-headers", "chunk-size-line", "chunk-trailer"])
def test_a_dripping_server_is_cut_off_at_the_deadline(tls_env, head, drip):
    # Every byte arrives well inside the per-operation timeout, so only the lookup
    # deadline can end this. Before the socket-level deadline the call ran until the
    # drip finished (6s here; with a hostile server, indefinitely).
    server, serve = tls_env
    assert len(drip) * DRIP_DELAY >= BUDGET + MARGIN + 2       # the drip outlasts the check
    srv = serve(head=head, drip=drip)
    got, elapsed = _timed_get(server, srv.url())
    assert got is None
    assert elapsed < BUDGET + MARGIN, f"lookup ran {elapsed:.2f}s past a {BUDGET}s budget"


def test_an_expired_deadline_never_reads_again(tls_env):
    # The per-call timeout is the time LEFT, never a fresh ICON_TIMEOUT.
    server, _ = tls_env
    with pytest.raises(TimeoutError):
        server._icon_op_timeout(time.monotonic() - 0.001)
    assert server._icon_op_timeout(None) == server.ICON_TIMEOUT
    assert 0 < server._icon_op_timeout(time.monotonic() + 0.5) <= 0.5
