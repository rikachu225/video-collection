"""Streaming launcher tiles: URL validation boundary, defaults, and persistence.

These tiles open a service in a new tab — nothing is embedded (Netflix sends
X-Frame-Options: DENY, Max sends frame-ancestors 'none', and DRM binds licences to
the service's own origin regardless). The security surface is therefore the URL
itself, which lands in an anchor href in our own origin.
"""
import http.client
import importlib
import io
import json
import socket
import urllib.request

import pytest


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server, server.app.test_client()


def _post(client, services):
    return client.post("/api/streaming", json={"services": services})


def _get(client):
    return client.get("/api/streaming").get_json()["services"]


def _by_id(services):
    return {s["id"]: s for s in services}


# ── Defaults ──
def test_fresh_install_ships_the_default_services(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    services = _get(client)
    assert len(services) == len(server.DEFAULT_STREAMING_SERVICES)
    assert "netflix" in _by_id(services)


def test_every_shipped_default_is_https_and_valid(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    for svc in server.DEFAULT_STREAMING_SERVICES:
        url, err = server._validate_service_url(svc["url"])
        assert err is None, f"{svc['id']}: {err}"
        assert url.startswith("https://")
        assert server._safe_accent(svc["accent"]) == svc["accent"].lower()


def test_defaults_are_enabled_and_not_custom(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    assert all(s["enabled"] and not s["custom"] for s in _get(client))


# ── URL validation: the security boundary ──
@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)  ",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///C:/Windows/System32",
    "http://insecure.example.com",
    "//evil.example.com",
    "ftp://example.com",
    "chrome://settings",
])
def test_dangerous_schemes_are_rejected(tmp_path, monkeypatch, bad):
    server, client = make_client(tmp_path, monkeypatch)
    assert server._validate_service_url(bad)[1] is not None
    res = _post(client, [{"name": "Evil", "url": bad}])
    assert res.status_code == 400


def test_control_characters_cannot_smuggle_a_scheme(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    for bad in ["java\tscript:alert(1)", "java\nscript:alert(1)", "https://x.com/\x00"]:
        assert server._validate_service_url(bad)[1] is not None
        assert _post(client, [{"name": "Evil", "url": bad}]).status_code == 400


def test_embedded_credentials_are_rejected(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    assert server._validate_service_url("https://user:pass@evil.example.com")[1] is not None
    assert _post(client, [{"name": "Evil", "url": "https://u:p@evil.com"}]).status_code == 400


def test_url_length_is_capped(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    huge = "https://example.com/" + ("a" * server.STREAMING_URL_MAX)
    assert server._validate_service_url(huge)[1] is not None
    assert _post(client, [{"name": "Huge", "url": huge}]).status_code == 400


def test_missing_or_non_string_url_is_rejected(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    for bad in [None, "", "   ", 42, {"nested": "obj"}]:
        assert server._validate_service_url(bad)[1] is not None
    assert _post(client, [{"name": "NoUrl"}]).status_code == 400


def test_a_normal_https_url_passes(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    url, err = server._validate_service_url("https://www.netflix.com/browse?x=1#y")
    assert err is None and url == "https://www.netflix.com/browse?x=1#y"


# ── Accent validation (lands in an inline CSS custom property) ──
@pytest.mark.parametrize("bad", [
    "red; background-image: url(https://evil.com/x)",
    "#fff",
    "#12345g",
    "rgb(1,2,3)",
    "",
    None,
    123,
])
def test_non_hex_accents_fall_back_to_the_default(tmp_path, monkeypatch, bad):
    server, _ = make_client(tmp_path, monkeypatch)
    assert server._safe_accent(bad) == server.DEFAULT_ACCENT


def test_valid_hex_accent_is_kept_and_lowercased(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    assert server._safe_accent("#E50914") == "#e50914"


def test_a_hostile_accent_never_survives_a_save(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"name": "X", "url": "https://x.com", "accent": "red; background: url(//evil)"}])
    assert _get(client)[0]["accent"] == server.DEFAULT_ACCENT


# ── Payload shape ──
def test_non_list_payload_is_rejected(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    assert client.post("/api/streaming", json={"services": "netflix"}).status_code == 400
    assert client.post("/api/streaming", json={}).status_code == 400


def test_non_object_entry_is_rejected(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    assert _post(client, ["netflix"]).status_code == 400


def test_entry_without_a_name_is_rejected(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    assert _post(client, [{"url": "https://x.com"}]).status_code == 400
    assert _post(client, [{"name": "   ", "url": "https://x.com"}]).status_code == 400


def test_too_many_services_is_rejected(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    many = [{"name": f"S{i}", "url": f"https://s{i}.example.com"}
            for i in range(server.STREAMING_MAX_SERVICES + 1)]
    assert _post(client, many).status_code == 400


def test_a_rejected_save_leaves_the_previous_list_untouched(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"name": "Keep", "url": "https://keep.example.com"}])
    _post(client, [{"name": "Keep", "url": "https://keep.example.com"},
                   {"name": "Evil", "url": "javascript:alert(1)"}])
    saved = [s for s in _get(client) if s["id"] == "keep"]
    assert len(saved) == 1 and saved[0]["url"] == "https://keep.example.com"


# ── Custom services ──
def test_custom_service_round_trips(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"name": "Nebula", "url": "https://nebula.tv", "accent": "#3b82f6", "custom": True}])
    svc = _by_id(_get(client))["nebula"]
    assert (svc["name"], svc["url"], svc["accent"], svc["custom"]) == \
           ("Nebula", "https://nebula.tv", "#3b82f6", True)


def test_id_is_slugged_from_the_name_when_absent(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"name": "My Weird  Service!! ", "url": "https://x.example.com"}])
    assert "my-weird-service" in _by_id(_get(client))


def test_duplicate_ids_are_disambiguated_not_dropped(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"name": "Same", "url": "https://a.example.com"},
                   {"name": "Same", "url": "https://b.example.com"}])
    ids = [s["id"] for s in _get(client) if s["id"].startswith("same")]
    assert sorted(ids) == ["same", "same-2"]


def test_a_hostile_id_is_replaced_with_a_safe_slug(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"id": "../../etc/passwd", "name": "Sneaky", "url": "https://x.example.com"}])
    assert "sneaky" in _by_id(_get(client))
    assert all("/" not in s["id"] for s in _get(client))


# ── Overrides and merge behaviour ──
def test_renaming_and_repointing_a_builtin_persists(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    services = _get(client)
    for s in services:
        if s["id"] == "netflix":
            s["name"], s["url"] = "Netflix (Kids)", "https://www.netflix.com/browse/genre/783"
    _post(client, services)
    netflix = _by_id(_get(client))["netflix"]
    assert netflix["name"] == "Netflix (Kids)"
    assert netflix["url"] == "https://www.netflix.com/browse/genre/783"


def test_a_hidden_builtin_stays_hidden(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    services = _get(client)
    for s in services:
        if s["id"] == "hulu":
            s["enabled"] = False
    _post(client, services)
    assert _by_id(_get(client))["hulu"]["enabled"] is False


def test_a_builtin_dropped_from_the_list_is_restored(tmp_path, monkeypatch):
    # Built-ins are hidden via enabled=False, never deleted — so a list that simply
    # omits one gets it back. This is what keeps upgrades additive.
    _, client = make_client(tmp_path, monkeypatch)
    _post(client, [s for s in _get(client) if s["id"] != "netflix"])
    assert "netflix" in _by_id(_get(client))


def test_order_is_preserved(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    services = _get(client)
    reordered = list(reversed(services))
    _post(client, reordered)
    assert [s["id"] for s in _get(client)] == [s["id"] for s in reordered]


def test_a_new_shipped_default_is_appended_to_an_existing_install(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    _post(client, [{"id": "netflix", "name": "Netflix", "url": "https://www.netflix.com/browse"}])
    ids = [s["id"] for s in _get(client)]
    assert ids[0] == "netflix"                       # the user's entry keeps its place
    assert len(ids) == len(server.DEFAULT_STREAMING_SERVICES)
    assert "twitch" in ids                           # the rest are appended


# ── Resilience against a hand-edited config ──
def test_corrupt_entries_are_dropped_not_fatal(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(json.dumps({
        "mediaPaths": [],
        "streamingServices": [
            "not-an-object",
            {"id": "ok", "name": "Fine", "url": "https://fine.example.com"},
            {"id": "bad-scheme", "name": "Bad", "url": "javascript:alert(1)"},
            {"id": "no url", "name": "Spacey id", "url": "https://x.example.com"},
            {"name": "No id", "url": "https://y.example.com"},
        ],
    }), encoding="utf-8")
    ids = [s["id"] for s in _get(client)]
    assert "ok" in ids
    assert "bad-scheme" not in ids     # invalid URL dropped on read
    assert "no url" not in ids         # invalid id dropped on read
    assert "netflix" in ids            # defaults still merge in


def test_streaming_key_of_the_wrong_type_falls_back_to_defaults(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(
        json.dumps({"mediaPaths": [], "streamingServices": "oops"}), encoding="utf-8")
    assert len(_get(client)) == len(server.DEFAULT_STREAMING_SERVICES)


# ── Service icons: SSRF guard ──
@pytest.mark.parametrize("bad", [
    "127.0.0.1",            # loopback
    "10.0.0.5",             # RFC1918
    "192.168.1.1",          # RFC1918 — the user's own router
    "172.16.0.1",           # RFC1918
    "169.254.169.254",      # cloud metadata endpoint
    "0.0.0.0",              # unspecified
    "224.0.0.1",            # multicast
    "240.0.0.1",            # reserved
    "::1",                  # IPv6 loopback
    "fe80::1",              # IPv6 link-local
    "fc00::1",              # IPv6 unique local
    "fec0::1",              # IPv6 site-local (deprecated; is_global on some Pythons)
    "100.64.0.1",           # CGNAT — Tailscale's range; no flag set, only is_global=False
    "100.100.100.100",      # Tailscale MagicDNS resolver
    "0.0.0.1",              # "this network"
    "198.18.0.1",           # benchmarking
    "192.0.0.1",            # IETF protocol assignments
    "::ffff:127.0.0.1",     # IPv4-mapped loopback
    "::ffff:100.64.0.1",    # IPv4-mapped CGNAT (is_global=True on 3.11)
    "64:ff9b::7f00:1",      # NAT64 -> 127.0.0.1 (is_global=True on 3.11)
    "64:ff9b::a00:1",       # NAT64 -> 10.0.0.1
    "64:ff9b:1::a00:1",     # local-use NAT64 -> 10.0.0.1
    "2002:c0a8:101::1",     # 6to4 -> 192.168.1.1
    "2002:7f00:1::1",       # 6to4 -> 127.0.0.1
    "2002:a9fe:a9fe::1",    # 6to4 -> 169.254.169.254
    "2001:0:c0a8:101::1",   # Teredo, server 192.168.1.1
])
def test_private_addresses_are_refused(tmp_path, monkeypatch, bad):
    import ipaddress as _ip
    server, _ = make_client(tmp_path, monkeypatch)
    assert server._is_public_ip(_ip.ip_address(bad)) is False


@pytest.mark.parametrize("good", ["8.8.8.8", "1.1.1.1", "2001:4860:4860::8888",
                                  "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(tmp_path, monkeypatch, good):
    import ipaddress as _ip
    server, _ = make_client(tmp_path, monkeypatch)
    assert server._is_public_ip(_ip.ip_address(good)) is True


def test_embedded_ipv4_is_unwrapped_from_every_tunnel_form(tmp_path, monkeypatch):
    # Pins the unwrap itself, independent of how a given Python version flags the
    # outer IPv6 address — several of these report is_global=True on 3.11.
    import ipaddress as _ip
    server, _ = make_client(tmp_path, monkeypatch)
    v4 = _ip.IPv4Address
    assert server._embedded_ipv4(_ip.ip_address("::ffff:100.64.0.1")) == [v4("100.64.0.1")]
    assert server._embedded_ipv4(_ip.ip_address("64:ff9b::7f00:1")) == [v4("127.0.0.1")]
    assert server._embedded_ipv4(_ip.ip_address("64:ff9b:1::a00:1")) == [v4("10.0.0.1")]
    assert server._embedded_ipv4(_ip.ip_address("2002:c0a8:101::1")) == [v4("192.168.1.1")]
    assert server._embedded_ipv4(_ip.ip_address("2606:4700:4700::1111")) == []
    assert server._embedded_ipv4(_ip.ip_address("8.8.8.8")) == []


def test_teredo_is_refused_when_either_embedded_ipv4_is_private(tmp_path, monkeypatch):
    import ipaddress as _ip
    server, _ = make_client(tmp_path, monkeypatch)
    # Teredo stores the client address bit-inverted in the last 32 bits.
    client_10_0_0_1 = (~int(_ip.IPv4Address("10.0.0.1"))) & 0xFFFFFFFF
    addr = _ip.IPv6Address((0x2001 << 112) | (int(_ip.IPv4Address("65.54.227.120")) << 64)
                           | client_10_0_0_1)
    assert addr.teredo == (_ip.IPv4Address("65.54.227.120"), _ip.IPv4Address("10.0.0.1"))
    assert _ip.IPv4Address("10.0.0.1") in server._embedded_ipv4(addr)
    assert server._is_public_ip(addr) is False


def test_a_name_resolving_to_cgnat_fails_the_host_guard(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server.socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(2, 1, 6, "", ("100.101.102.103", 0))])
    assert server._host_is_public("tailnet.example") is False


def test_localhost_never_passes_the_host_guard(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    assert server._host_is_public("localhost") is False
    assert server._host_is_public("this-name-does-not-resolve.invalid") is False


def test_icon_fetch_refuses_non_https_and_private_hosts(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    # No network is reached for any of these — they fail before connecting.
    assert server._icon_http_get("http://example.com/x.png") is None
    assert server._icon_http_get("file:///C:/Windows/win.ini") is None
    assert server._icon_http_get("https://localhost/x.png") is None
    assert server._icon_http_get("https://127.0.0.1/x.png") is None


# ── Service icons: the real fetch path, driven by a fake network ──
# Only the transport is fake: socket.getaddrinfo answers from FAKE_DNS, and the opener
# the code builds is the REAL urllib chain (with whatever handlers the code passes, i.e.
# _NoRedirect) whose http/https transport is swapped for canned bytes. So dropping
# _NoRedirect in favour of urllib's own redirect following fails these tests.
FAKE_DNS = {
    "public.example": ["93.184.216.34"],
    "cdn.example": ["93.184.216.35"],
    "internal.example": ["10.0.0.5"],
    "tailnet.example": ["100.101.102.103"],
    "split.example": ["93.184.216.36", "192.168.1.10"],   # one public, one private
}


class _Wire(io.BytesIO):
    """Socket file for http.client; `on_read` makes every body read slow and 1 byte."""
    def __init__(self, data, on_read=None):
        super().__init__(data)
        self._on_read = on_read

    def read1(self, n=-1):
        if self._on_read:
            self._on_read()
            n = 1
        return super().read1(n)


class _FakeTransport(urllib.request.HTTPSHandler):
    """routes: {url: ("redirect", code, location) | ("ok", ctype, body[, on_read])}"""
    handler_order = 100                       # ahead of the stock HTTP handler too

    def __init__(self, routes, opened):
        super().__init__()
        self.routes, self.opened = routes, opened

    def https_open(self, req):
        url = req.full_url
        self.opened.append((url, req.timeout))
        route = self.routes.get(url, ("status", 404))
        on_read = None
        if route[0] == "redirect":
            head, body = f"HTTP/1.1 {route[1]} Moved\r\nLocation: {route[2]}\r\n", b""
        elif route[0] == "ok":
            head, body = f"HTTP/1.1 200 OK\r\nContent-Type: {route[1]}\r\n", route[2]
            on_read = route[3] if len(route) > 3 else None
        else:
            head, body = f"HTTP/1.1 {route[1]} Nope\r\n", b""
        wire = (head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body

        class _Sock:
            def makefile(self, mode):
                return _Wire(wire, on_read)

        resp = http.client.HTTPResponse(_Sock(), method=req.get_method())
        resp.begin()
        resp.url, resp.msg = url, resp.reason     # what AbstractHTTPHandler.do_open sets
        return resp

    http_open = https_open


def _fake_network(server, monkeypatch, routes):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in FAKE_DNS:
            raise socket.gaierror("unknown host")
        return [(2, 1, 6, "", (ip, 0)) for ip in FAKE_DNS[host]]

    opened = []
    real_build_opener = urllib.request.build_opener
    monkeypatch.setattr(server.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(server, "build_opener",
                        lambda *handlers: real_build_opener(*handlers, _FakeTransport(routes, opened)))
    return opened


def _fake_clock(server, monkeypatch, start=1000.0):
    now = [start]
    monkeypatch.setattr(server, "_icon_clock", lambda: now[0])
    return now


@pytest.mark.parametrize("target", [
    "https://127.0.0.1/x.png",
    "https://internal.example/x.png",       # name -> RFC1918
    "https://tailnet.example/x.png",        # name -> CGNAT (Tailscale)
    "https://split.example/x.png",          # one public + one private A record
    "http://cdn.example/x.png",             # https -> http downgrade
])
def test_a_redirect_is_rechecked_before_it_is_followed(tmp_path, monkeypatch, target):
    server, _ = make_client(tmp_path, monkeypatch)
    opened = _fake_network(server, monkeypatch, {
        "https://public.example/icon.png": ("redirect", 302, target),
        target: ("ok", "image/png", PNG),
    })
    assert server._icon_http_get("https://public.example/icon.png") is None
    assert [u for u, _ in opened] == ["https://public.example/icon.png"]


def test_a_redirect_to_another_public_https_host_is_followed(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    _fake_network(server, monkeypatch, {
        "https://public.example/icon.png": ("redirect", 301, "https://cdn.example/i.png"),
        "https://cdn.example/i.png": ("ok", "image/png", PNG),
    })
    assert server._icon_http_get("https://public.example/icon.png") == \
        ("https://cdn.example/i.png", "image/png", PNG)


def test_the_redirect_chain_is_capped(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    hops = server.ICON_MAX_REDIRECTS + 2
    routes = {f"https://public.example/{n}": ("redirect", 302, f"/{n + 1}") for n in range(hops)}
    routes[f"https://public.example/{hops}"] = ("ok", "image/png", PNG)
    opened = _fake_network(server, monkeypatch, routes)
    assert server._icon_http_get("https://public.example/0") is None
    assert len(opened) == server.ICON_MAX_REDIRECTS + 1


def test_a_body_over_the_size_cap_is_refused(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    _fake_network(server, monkeypatch, {
        "https://public.example/exact.png": ("ok", "image/png", b"x" * server.ICON_MAX_BYTES),
        "https://public.example/over.png": ("ok", "image/png", b"x" * (server.ICON_MAX_BYTES + 1)),
    })
    got = server._icon_http_get("https://public.example/exact.png")
    assert got is not None and len(got[2]) == server.ICON_MAX_BYTES
    assert server._icon_http_get("https://public.example/over.png") is None


def test_a_slow_drip_body_is_abandoned_at_the_deadline(tmp_path, monkeypatch):
    # Each read returns one byte "5 seconds" later: never trips a per-read socket
    # timeout, so only the overall deadline can stop it.
    server, _ = make_client(tmp_path, monkeypatch)
    now = _fake_clock(server, monkeypatch)
    reads = []

    def tick():
        reads.append(1)
        now[0] += 5

    _fake_network(server, monkeypatch, {
        "https://public.example/drip.png": ("ok", "image/png", b"x" * 10_000, tick),
    })
    assert server._icon_http_get("https://public.example/drip.png",
                                 deadline=now[0] + server.ICON_BUDGET) is None
    assert len(reads) <= server.ICON_BUDGET // 5 + 1


@pytest.mark.parametrize("raw", [
    b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: %d\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nTransfer-Encoding: chunked\r\n\r\n",
])
def test_a_real_http_response_is_read_to_the_end(tmp_path, monkeypatch, raw):
    # _read_icon_body relies on read1(); pin it against the stdlib response class
    # (over an in-memory socket) for both body framings.
    server, _ = make_client(tmp_path, monkeypatch)
    body = PNG * 3000                                  # > one ICON_READ_CHUNK
    if b"chunked" in raw:
        wire = raw + b"".join(b"%x\r\n%s\r\n" % (len(body[i:i + 5000]), body[i:i + 5000])
                              for i in range(0, len(body), 5000)) + b"0\r\n\r\n"
    else:
        wire = (raw % len(body)) + body

    class _Sock:
        def makefile(self, mode):
            return io.BytesIO(wire)

    resp = http.client.HTTPResponse(_Sock())
    resp.begin()
    assert server._read_icon_body(resp, None) == body


def test_a_truncated_body_is_refused_not_cached_half_written(tmp_path, monkeypatch):
    # The connection drops after 72 of 1000 promised bytes. http.client hands back the
    # short body without raising, so the reader must check what was still owed.
    server, _ = make_client(tmp_path, monkeypatch)
    wire = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 1000\r\n\r\n" + PNG

    class _Sock:
        def makefile(self, mode):
            return io.BytesIO(wire)

    resp = http.client.HTTPResponse(_Sock())
    resp.begin()
    assert server._read_icon_body(resp, None) is None


def test_the_socket_timeout_never_exceeds_the_remaining_budget(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    now = _fake_clock(server, monkeypatch)
    opened = _fake_network(server, monkeypatch, {
        "https://public.example/i.png": ("ok", "image/png", PNG),
    })
    assert server._icon_http_get("https://public.example/i.png", deadline=now[0] + 3)[2] == PNG
    assert opened[0][1] <= 3
    assert server._icon_http_get("https://public.example/i.png", deadline=now[0] - 1) is None
    assert len(opened) == 1          # an expired deadline never opens a connection


def test_one_lookup_tries_a_bounded_number_of_candidates(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    page = "".join(f'<link rel="icon" href="https://host{n}.example/i.png">'
                   for n in range(500)).encode()
    calls = []

    def fake(url, accept="*/*", deadline=None):
        calls.append(url)
        return (url, "text/html", page) if url == "https://www.netflix.com/" else None

    monkeypatch.setattr(server, "_icon_http_get", fake)
    assert server._fetch_service_icon({"id": "netflix", "url": "https://www.netflix.com/browse"}) is None
    icon_calls = calls[1:]                   # calls[0] is the homepage
    assert len(icon_calls) == server.ICON_MAX_CANDIDATES
    assert icon_calls[-1] == "https://www.netflix.com/favicon.ico"   # last resort survives
    assert server._icon_miss_marker("netflix").exists()


def test_one_lookup_stops_when_the_overall_budget_is_spent(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    now = _fake_clock(server, monkeypatch)
    calls = []

    def slow(url, accept="*/*", deadline=None):
        assert deadline is not None and deadline <= 1000.0 + server.ICON_BUDGET
        calls.append(url)
        now[0] += 7                          # every hop burns 7 "seconds"
        return None

    monkeypatch.setattr(server, "_icon_http_get", slow)
    assert server._fetch_service_icon({"id": "hulu", "url": "https://www.hulu.com/hub"}) is None
    assert len(calls) == 3                   # 0s, 7s, 14s start; 21s is past the 20s budget
    assert server._icon_miss_marker("hulu").exists()


# ── Service icons: caching, overrides, content types ──
def _fake_fetch(mapping):
    """Stub for _icon_http_get: {url_substring: (ctype, body)}."""
    def inner(url, accept="*/*", deadline=None):
        for key, (ctype, body) in mapping.items():
            if key in url:
                return (url, ctype, body)
        return None
    return inner


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def test_icon_route_rejects_a_bad_service_id(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    assert client.get("/api/service-icon/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/api/service-icon/Bad Id").status_code in (400, 404)


def test_icon_route_404s_for_an_unknown_service(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_icon_http_get", _fake_fetch({}))
    assert client.get("/api/service-icon/ghost").status_code == 404


def test_icon_is_fetched_and_cached_then_served_from_disk(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    calls = []

    def counting(url, accept="*/*", deadline=None):
        calls.append(url)
        return (url, "image/png", PNG) if "apple-touch-icon.png" in url else None

    monkeypatch.setattr(server, "_icon_http_get", counting)
    res = client.get("/api/service-icon/netflix")
    assert res.status_code == 200 and res.data == PNG
    assert (server.SERVICE_ICONS_AUTO / "netflix.png").exists()

    calls.clear()
    assert client.get("/api/service-icon/netflix").status_code == 200
    assert calls == []                       # second hit never touches the network


def test_a_user_drop_in_beats_the_fetched_copy(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    (server.SERVICE_ICONS_AUTO / "netflix.png").write_bytes(PNG)
    mine = b"\x89PNG\r\n\x1a\n" + b"MINE" * 16
    (server.SERVICE_ICONS_DIR / "netflix.png").write_bytes(mine)
    assert client.get("/api/service-icon/netflix").data == mine


def test_non_image_content_types_are_rejected(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_icon_http_get",
                        _fake_fetch({"": ("text/html", b"<html>nope</html>")}))
    assert client.get("/api/service-icon/netflix").status_code == 404
    assert not any(server.SERVICE_ICONS_AUTO.glob("netflix.*png"))


def test_a_failed_lookup_is_negatively_cached(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(server, "_icon_http_get",
                        lambda url, accept="*/*", deadline=None: calls.append(url) or None)
    assert client.get("/api/service-icon/netflix").status_code == 404
    assert server._icon_miss_marker("netflix").exists()
    first = len(calls)
    assert client.get("/api/service-icon/netflix").status_code == 404
    assert len(calls) == first               # no retry storm on every page load


def test_delete_clears_the_fetched_copy_but_not_the_user_file(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    (server.SERVICE_ICONS_AUTO / "netflix.png").write_bytes(PNG)
    server._icon_miss_marker("hulu").write_text("", encoding="utf-8")
    mine = b"\x89PNG\r\n\x1a\nMINE"
    (server.SERVICE_ICONS_DIR / "netflix.png").write_bytes(mine)

    assert client.delete("/api/service-icon/netflix").status_code == 200
    assert not (server.SERVICE_ICONS_AUTO / "netflix.png").exists()
    assert (server.SERVICE_ICONS_DIR / "netflix.png").read_bytes() == mine   # untouched
    assert client.delete("/api/service-icon/hulu").status_code == 200
    assert not server._icon_miss_marker("hulu").exists()


def test_candidate_order_prefers_declared_sizes(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    html = (b'<html><head>'
            b'<link rel="icon" sizes="16x16" href="/small.png">'
            b'<link rel="apple-touch-icon" href="/touch.png">'
            b'<link rel="icon" sizes="192x192" href="/big.png">'
            b'</head></html>')
    monkeypatch.setattr(server, "_icon_http_get",
                        _fake_fetch({"https://www.netflix.com/": ("text/html", html)}))
    order = server._icon_candidates("https://www.netflix.com/browse")
    assert order[0].endswith("/big.png")                    # 192 beats everything
    assert order.index("https://www.netflix.com/touch.png") < \
           order.index("https://www.netflix.com/small.png")  # apple-touch (180) beats 16
    assert order[-1].endswith("/favicon.ico")               # bare favicon is last resort


def test_icon_link_parsing_reads_real_world_markup(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    html = ('<LINK REL="Shortcut Icon" HREF="/fav.ico">'
            '<link rel=icon href=/unquoted.png sizes=32x32>'
            '<link rel="icon" href="/q.png?a=1&amp;b=2" sizes="16x16 48x48">'
            '<link rel="apple-touch-icon-precomposed" href="/pre.png">'
            '<link rel="mask-icon" href="/mask.svg">'
            '<link rel="stylesheet" href="/icon.css">'
            '<link rel="icon" sizes="' + "9" * 1500 + 'x1" href="/huge.png">'
            '<link rel="icon" href="/too-long.png" data-x="' + "x" * 3000 + '">'
            '<link rel="icon">')
    assert server._icon_links(html) == [
        (2, "/fav.ico"),
        (32, "/unquoted.png"),
        (48, "/q.png?a=1&b=2"),          # entities decoded; largest declared size ranks
        (180, "/pre.png"),               # apple-touch without sizes
        (2, "/huge.png"),                # absurd size ignored, no huge int()
    ]                                    # mask-icon, stylesheet, over-long tag, no href: skipped


@pytest.mark.parametrize("unit", ["<link rel=icon", "<link <a ", '<link rel="icon" href="'])
def test_hostile_unterminated_link_markup_parses_quickly(tmp_path, monkeypatch, unit):
    # The old regex was quadratic on this input (~217s for 400K chars), and so is a
    # plain HTMLParser feed on 3.11.9 — either one freezes every waitress thread.
    import time
    server, _ = make_client(tmp_path, monkeypatch)
    html = (unit * (400_000 // len(unit) + 1))[:400_000]
    start = time.perf_counter()
    assert server._icon_links(html) == []
    assert time.perf_counter() - start < 2.0


def test_icon_link_parsing_is_capped(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    html = "".join(f'<link rel="icon" href="/i{n}.png">' for n in range(500))
    links = server._icon_links(html)
    assert len(links) == server.ICON_MAX_LINKS
    assert links[0] == (2, "/i0.png")


# ── AI assistant integration ──
def test_open_streaming_service_is_a_registered_ui_command(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    import ai_agent
    from ai_agent import TOOL_NAMES, execute_tool, Sink
    assert "open_streaming_service" in TOOL_NAMES
    assert "open_streaming_service" in ai_agent.UI_COMMAND_TOOLS

    sink = Sink()
    result = execute_tool("open_streaming_service", {"service": "Netflix"}, {}, sink)
    assert result["status"] == "queued"
    assert {"command": "open_streaming_service", "args": {"service": "Netflix"}} in sink.ui_commands


def test_switch_view_can_reach_the_streaming_view(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    import ai_agent
    enum = ai_agent._TOOL_DEFS["switch_view"][1]["properties"]["view"]["enum"]
    assert "streaming" in enum


def test_prompt_lists_the_configured_services(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    import ai_agent
    importlib.reload(ai_agent)
    prompt = ai_agent.build_system_prompt({"theaterClips": [], "currentVideos": []})
    assert "Netflix" in prompt
    assert "open_streaming_service" in prompt


def test_prompt_omits_hidden_services(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    services = _get(client)
    for s in services:
        if s["id"] == "netflix":
            s["enabled"] = False
    _post(client, services)

    import ai_agent
    importlib.reload(ai_agent)
    prompt = ai_agent.build_system_prompt({"theaterClips": [], "currentVideos": []})
    assert "Netflix" not in prompt
    assert "Max" in prompt
