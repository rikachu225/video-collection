"""The icon fetcher's lookup deadline, over REAL sockets and a real TLS handshake.

tests/test_streaming.py drives the fetch path with a fake transport, which can't show
what happens inside http.client while the status line and headers are read: there,
every recv only has the per-operation socket timeout, so a server that sends one byte
at a time never trips it. These tests run a throwaway TLS server on 127.0.0.1 (a random
free port) that drips its reply, and call the fetch layer directly.

Only two things are swapped in: the SSRF address classifier (_is_public_ip refuses
127.0.0.1, by design, and both the pre-check and the connect ask it) and the trust store
(a certificate generated per test run; no key material is kept in the repo). The
connection, TLS and HTTP parsing are the production code paths.

The SSRF tests at the end keep the real classifier, answer DNS from a table, and record
and refuse every dial, so nothing is ever sent anywhere.
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
SITE = "icons.test"        # a name the test certificate also carries; DNS is faked for it


def make_server(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    return server


def _self_signed(tmp_path):
    """A certificate for 127.0.0.1 and SITE, written to tmp_path. Returns (cert, key)."""
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
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName(SITE)]),
                critical=False)
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


class _IconSite:
    """A TLS site on 127.0.0.1 answering any number of connections, one request each:
    `routes` maps a path to (content_type, body), anything else is a 404."""

    def __init__(self, cert, key, routes):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.routes, self.requests = routes, []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:                  # closed: the test is over
                return
            try:
                with self.ctx.wrap_socket(conn, server_side=True) as tls:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = tls.recv(4096)
                        if not chunk:
                            break
                        request += chunk
                    path = request.split(b" ", 2)[1].decode() if request.count(b" ") >= 2 else ""
                    self.requests.append(path)
                    ctype, body = self.routes.get(path, (None, b""))
                    head = (f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n" if ctype
                            else "HTTP/1.1 404 Not Found\r\n")
                    tls.sendall(head.encode() + b"Content-Length: %d\r\nConnection: close\r\n\r\n"
                                % len(body) + body)
            except OSError:                  # the client went away
                pass

    def close(self):
        self.listener.close()
        self.thread.join(timeout=10)


@pytest.fixture
def tls_cert(tmp_path):
    return _self_signed(tmp_path)


@pytest.fixture
def tls_env(tmp_path, monkeypatch, tls_cert):
    server = make_server(tmp_path, monkeypatch)
    cert, key = tls_cert
    monkeypatch.setattr(server, "_is_public_ip", lambda ip: True)   # 127.0.0.1 is private
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


# ── Connect: a dead address must not cost the whole lookup ──
DEAD = "192.0.2.1"         # TEST-NET-1; its dial is simulated, never made


def test_a_dead_first_address_leaves_time_for_the_next_one(tls_env, tls_cert, monkeypatch):
    # Blackholed IPv6 on a dual-stack network, or one dead CDN edge: the first address
    # never accepts. With the whole 12s op timeout per address, the homepage and the
    # icon connection each waited it out, the 20s budget ran out, and the icon was
    # negative-cached for a day. The dead dial is simulated under a fake clock (it
    # "waits" its full timeout), the live one is a real TLS site on 127.0.0.1.
    server, _ = tls_env
    site = _IconSite(*tls_cert, {"/": ("text/html", b"<html></html>"),
                                 "/apple-touch-icon.png": ("image/png", PNG)})
    _fake_dns(server, monkeypatch, {SITE: [DEAD, "127.0.0.1"]})
    now = [1000.0]
    monkeypatch.setattr(server, "_icon_clock", lambda: now[0])
    dials, real_connect = [], server._DeadlineSocket.connect

    def connect(sock, sockaddr):
        dials.append((sockaddr[0], sock.gettimeout()))
        if sockaddr[0] == DEAD:
            now[0] += sock.gettimeout()
            raise TimeoutError("timed out")
        return real_connect(sock, sockaddr)

    monkeypatch.setattr(server._DeadlineSocket, "connect", connect)
    try:
        path = server._fetch_service_icon({"id": "icons", "url": f"https://{SITE}:{site.port}/"})
    finally:
        site.close()
    assert path is not None and path.read_bytes() == PNG
    assert site.requests == ["/", "/apple-touch-icon.png"]
    # One short try at the dead address; the address that answered is dialled first after.
    assert dials == [(DEAD, server.ICON_CONNECT_SLICE), ("127.0.0.1", server.ICON_TIMEOUT),
                     ("127.0.0.1", server.ICON_CONNECT_SLICE)]
    assert now[0] - 1000.0 == server.ICON_CONNECT_SLICE


def test_the_last_address_keeps_the_full_connect_timeout(no_dial, monkeypatch):
    # Only an address with another one behind it is cut to ICON_CONNECT_SLICE; a host's
    # last (or only) address still gets the whole op timeout on a slow link.
    server, _ = no_dial
    _fake_dns(server, monkeypatch, {"two.test": ["93.184.216.34", "93.184.216.35"],
                                    "one.test": ["93.184.216.36"]})
    timeouts = []

    def refuse(sock, sockaddr):
        timeouts.append(sock.gettimeout())
        raise ConnectionRefusedError("test: dials are recorded, never made")

    monkeypatch.setattr(server._DeadlineSocket, "connect", refuse)
    deadline = time.monotonic() + CONTROL_BUDGET
    for host in ("two.test", "one.test"):
        with pytest.raises(ConnectionRefusedError):
            server._icon_connect((host, 443), deadline, server._IconLookup())
    assert timeouts == [server.ICON_CONNECT_SLICE, server.ICON_TIMEOUT, server.ICON_TIMEOUT]


# ── SSRF: the address checked is the address dialled ──
@pytest.fixture
def no_dial(tmp_path, monkeypatch):
    """The real SSRF checks. Every dial is recorded and refused: nothing leaves the host."""
    server = make_server(tmp_path, monkeypatch)
    dials = []

    def refuse(sock, sockaddr):
        dials.append(sockaddr[0])
        raise ConnectionRefusedError("test: dials are recorded, never made")

    monkeypatch.setattr(server._DeadlineSocket, "connect", refuse)
    return server, dials


def _addrinfo(ip, port):
    if ":" in ip:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port or 0, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))


def _fake_dns(server, monkeypatch, table):
    """table: {host: [ip, ...] | callable(nth_query) -> [ip, ...]}. Returns the hosts asked."""
    asked = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        ips = table[host]
        if callable(ips):
            ips = ips(asked.count(host))
        return [_addrinfo(ip, port) for ip in ips]

    monkeypatch.setattr(server.socket, "getaddrinfo", getaddrinfo)
    return asked


def test_a_name_that_rebinds_after_the_check_is_only_dialled_at_the_checked_address(
        no_dial, monkeypatch):
    # DNS rebinding: public for the SSRF check, loopback a moment later for the connect.
    # One lookup resolves each host once, and the connect dials that same answer.
    server, dials = no_dial
    asked = _fake_dns(server, monkeypatch, {
        "rebind.test": lambda nth: ["93.184.216.34"] if nth == 1 else ["127.0.0.1"]})
    got = server._icon_http_get("https://rebind.test/icon.png",
                                deadline=time.monotonic() + CONTROL_BUDGET)
    assert got is None
    assert asked == ["rebind.test"]
    assert dials == ["93.184.216.34"]


@pytest.mark.parametrize("ips", [
    ["127.0.0.1"],
    ["192.168.1.10"],
    ["100.101.102.103"],                       # CGNAT (Tailscale)
    ["93.184.216.34", "192.168.1.10"],         # one public, one private: refused outright
    ["2606:4700:4700::1111", "::1"],
], ids=["loopback", "rfc1918", "cgnat", "mixed-v4", "mixed-v6"])
def test_the_connect_refuses_a_host_unless_every_address_is_public(no_dial, monkeypatch, ips):
    # The connect checks the answer it dials, whatever the pre-check was asked about.
    server, dials = no_dial
    _fake_dns(server, monkeypatch, {"lan.test": ips})
    with pytest.raises(OSError):
        server._icon_connect(("lan.test", 443), time.monotonic() + CONTROL_BUDGET,
                             server._IconLookup())
    assert dials == []


def test_a_percent_encoded_host_is_refused_before_it_resolves(no_dial, monkeypatch):
    # urlparse() keeps %2e in the host, but urllib connects to the unquoted name: with a
    # resolver that answers the literal label, the check passed for one name and the
    # connect dialled another.
    server, dials = no_dial
    asked = _fake_dns(server, monkeypatch, {"a%2eprivate.test": ["93.184.216.34"],
                                            "a.private.test": ["127.0.0.1"]})
    got = server._icon_http_get("https://a%2eprivate.test/icon.png",
                                deadline=time.monotonic() + CONTROL_BUDGET)
    assert got is None
    assert asked == [] and dials == []


def test_a_resolver_that_never_answers_is_abandoned_at_the_deadline(no_dial, monkeypatch):
    # getaddrinfo takes no timeout. A DNS server that never answers (internet down, LAN
    # up) used to hold the request for the OS resolver's timeout, per hop, twice.
    server, dials = no_dial
    release = threading.Event()

    def hang(host, *args, **kwargs):
        release.wait(30)
        return [_addrinfo("127.0.0.1", 443)]        # a late answer, private at that

    monkeypatch.setattr(server.socket, "getaddrinfo", hang)
    lookup = server._IconLookup()
    start = time.monotonic()
    try:
        got = server._icon_http_get("https://hang.test/icon.png", deadline=start + BUDGET,
                                    lookup=lookup)
        elapsed = time.monotonic() - start
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "icon-dns":
                thread.join(10)
    assert got is None
    assert elapsed < BUDGET + MARGIN, f"lookup ran {elapsed:.2f}s past a {BUDGET}s budget"
    with pytest.raises(TimeoutError):                # the late answer was dropped
        lookup.resolve("hang.test")
    assert dials == []
