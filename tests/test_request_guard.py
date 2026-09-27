"""Browser-driven attacks the IP-based LAN guard cannot see.

A page the user visits reaches this server from the user's own browser, so it arrives
from 127.0.0.1 or the LAN and passes the IP check. What gives it away is the Host header
(DNS rebinding) and the Origin / Sec-Fetch-Site headers (cross-site writes). The app is
same-origin, so no CORS headers are served at all.
"""
import importlib
import socket

import pytest

HIJACK = {"services": [{"id": "netflix", "name": "Netflix", "url": "https://phish.example/login"}]}


def make_client(tmp_path, monkeypatch, allowed_hosts=None):
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    if allowed_hosts is None:
        monkeypatch.delenv("VIDCOL_ALLOWED_HOSTS", raising=False)
    else:
        monkeypatch.setenv("VIDCOL_ALLOWED_HOSTS", allowed_hosts)
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server, server.app.test_client()


def _netflix_url(client):
    services = client.get("/api/streaming").get_json()["services"]
    return next(s["url"] for s in services if s["id"] == "netflix")


# ── Cross-site writes ──
def test_a_cross_origin_post_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    before = _netflix_url(client)
    res = client.post("/api/streaming", json=HIJACK,
                      headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403
    assert _netflix_url(client) == before


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "null",                              # sandboxed iframe / file:// page
    "http://localhost:8080",             # another app on this machine: same-site, not same-origin
    "https://localhost",                 # scheme differs
])
def test_a_foreign_origin_is_refused(tmp_path, monkeypatch, origin):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.post("/api/streaming", json=HIJACK, headers={"Origin": origin})
    assert res.status_code == 403


def test_cross_site_fetch_metadata_is_refused_even_without_origin(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.post("/api/streaming", json=HIJACK, headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403


def test_every_state_changing_method_is_guarded(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    evil = {"Origin": "https://evil.example"}
    assert client.delete("/api/service-icon/netflix", headers=evil).status_code == 403
    assert client.post("/api/branding", json={"siteName": "Pwned"}, headers=evil).status_code == 403
    assert client.get("/api/branding").get_json()["siteName"] != "Pwned"
    assert client.put("/api/streaming", headers=evil).status_code == 403
    assert client.patch("/api/streaming", headers=evil).status_code == 403


def test_a_write_without_origin_is_allowed(tmp_path, monkeypatch):
    # Not a browser (curl, a script, Flask's test client): nothing to forge.
    _, client = make_client(tmp_path, monkeypatch)
    assert client.post("/api/streaming", json=HIJACK).status_code == 200


@pytest.mark.parametrize("base_url,remote", [
    ("http://localhost:7777", "127.0.0.1"),
    ("http://127.0.0.1:7777", "127.0.0.1"),      # also the pywebview desktop window
    ("http://[::1]:7777", "::1"),
    ("http://192.168.1.5:7777", "192.168.1.20"),  # a phone on the home LAN
    ("http://10.0.0.7:7777", "10.0.0.9"),
    ("http://172.20.0.2:7777", "172.20.0.3"),
    ("http://localhost", "127.0.0.1"),           # default port: no :port in Host or Origin
])
def test_same_origin_writes_from_every_local_address_are_allowed(tmp_path, monkeypatch,
                                                                  base_url, remote):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.post("/api/streaming", json=HIJACK, base_url=base_url,
                      environ_base={"REMOTE_ADDR": remote},
                      headers={"Origin": base_url, "Sec-Fetch-Site": "same-origin"})
    assert res.status_code == 200
    got = client.get("/api/streaming", base_url=base_url, environ_base={"REMOTE_ADDR": remote})
    assert got.status_code == 200


# ── Cross-site reads (fetch metadata) ──
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


@pytest.mark.parametrize("site", ["cross-site", "same-site", " Cross-Site "])
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/streaming"),
    ("GET", "/api/service-icon/netflix"),     # an <img> on any page: probes + outbound fetch
    ("GET", "/api/service-icon/guess-a-name"),
    ("GET", "/api/folders"),
    ("GET", "/api/thumbnail/some/clip.mp4"),
    ("GET", "/api/stream/some/clip.mp4"),
    ("POST", "/api/streaming"),
    ("DELETE", "/api/service-icon/netflix"),
])
def test_a_cross_site_or_same_site_api_request_is_refused(tmp_path, monkeypatch, site,
                                                          method, path):
    # A page on another site (or on another port of this machine — same-site) can make
    # the browser send GETs here through <img>/<video>/<script> without reading the
    # reply. The status still leaks (onload vs onerror), and a GET can trigger work.
    server, client = make_client(tmp_path, monkeypatch)
    (server.SERVICE_ICONS_AUTO / "netflix.png").write_bytes(PNG)
    calls = []
    monkeypatch.setattr(server, "_icon_http_get",
                        lambda url, accept="*/*", deadline=None, lookup=None: calls.append(url) or None)
    res = client.open(path, method=method, json=HIJACK if method == "POST" else None,
                      headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "no-cors",
                               "Sec-Fetch-Dest": "image"})
    assert res.status_code == 403
    assert calls == []
    assert (server.SERVICE_ICONS_AUTO / "netflix.png").exists()


@pytest.mark.parametrize("headers", [
    {},                                                        # curl, scripts, monitors
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"},
    {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
    {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
], ids=["no-metadata", "tile-img", "app-fetch", "typed-url"])
def test_same_origin_and_direct_requests_still_work(tmp_path, monkeypatch, headers):
    server, client = make_client(tmp_path, monkeypatch)
    (server.SERVICE_ICONS_AUTO / "netflix.png").write_bytes(PNG)
    res = client.get("/api/service-icon/netflix", headers=headers)   # the app's own tile image
    assert res.status_code == 200 and res.data == PNG
    assert client.get("/api/streaming", headers=headers).status_code == 200
    assert client.get("/api/health", headers=headers).status_code == 200


def test_a_link_from_another_site_still_opens_the_app(tmp_path, monkeypatch):
    # Only /api/* is guarded: a bookmark page or another site may link to the app itself.
    _, client = make_client(tmp_path, monkeypatch)
    res = client.get("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                   "Sec-Fetch-Dest": "document"})
    assert res.status_code == 200


# ── DNS rebinding ──
@pytest.mark.parametrize("path", ["/", "/api/streaming", "/api/folders", "/api/ai/config"])
def test_a_rebound_host_cannot_read_the_api(tmp_path, monkeypatch, path):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.get(path, base_url="http://evil.example:7777")
    assert res.status_code == 403


def test_a_rebound_host_cannot_write_even_same_origin(tmp_path, monkeypatch):
    # After rebinding, the attacker's page IS same-origin with itself.
    _, client = make_client(tmp_path, monkeypatch)
    before = _netflix_url(client)
    res = client.post("/api/streaming", json=HIJACK, base_url="http://evil.example:7777",
                      headers={"Origin": "http://evil.example:7777", "Sec-Fetch-Site": "same-origin"})
    assert res.status_code == 403
    assert _netflix_url(client) == before


@pytest.mark.parametrize("host", [
    "evil.example",
    "localhost.evil.example",
    "evil.example@localhost",
    "[not-an-ip]:7777",
    "8.8.8.8:7777",                      # a public IP literal is not this LAN app
    "localhost:7777:1",
])
def test_unrecognised_or_malformed_hosts_are_refused(tmp_path, monkeypatch, host):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.get("/api/streaming", environ_overrides={"HTTP_HOST": host})
    assert res.status_code == 403


def test_this_machines_own_names_are_allowed(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    name = socket.gethostname().lower()
    for host in (f"{name}:7777", f"{name}.local:7777", "LOCALHOST:7777", "localhost.:7777"):
        assert client.get("/api/streaming",
                          environ_overrides={"HTTP_HOST": host}).status_code == 200, host


def test_a_fully_qualified_hostname_also_allows_its_short_and_mdns_names(tmp_path, monkeypatch):
    # On many Linux and macOS hosts gethostname() is the FQDN, but mDNS advertises the
    # first label (media-box.local), and that is what a phone on the LAN opens.
    server, client = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server.socket, "gethostname", lambda: "Media-Box.example.lan")
    monkeypatch.setattr(server.socket, "getfqdn", lambda *a: "media-box.example.lan")
    server._own_hostnames.cache_clear()
    lan = {"REMOTE_ADDR": "192.168.1.20"}
    for host in ("media-box.local:7777", "MEDIA-BOX.LOCAL:7777", "media-box:7777",
                 "media-box.example.lan:7777", "media-box.example.lan.local:7777"):
        res = client.get("/api/health", environ_base=lan, environ_overrides={"HTTP_HOST": host})
        assert res.status_code == 200, host
    for host in ("media-box.evil.example:7777", "box.local:7777", "example.lan:7777",
                 "media.local:7777"):
        res = client.get("/api/health", environ_base=lan, environ_overrides={"HTTP_HOST": host})
        assert res.status_code == 403, host


def test_an_ip_literal_hostname_is_not_shortened(tmp_path, monkeypatch):
    server, _ = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(server.socket, "gethostname", lambda: "192.168.1.5")
    monkeypatch.setattr(server.socket, "getfqdn", lambda *a: "192.168.1.5")
    server._own_hostnames.cache_clear()
    assert "192" not in server._own_hostnames()


def test_extra_hosts_can_be_allowed_explicitly(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch, allowed_hosts="media.lan, Nas.Home.")
    assert client.get("/api/streaming", base_url="http://media.lan:7777").status_code == 200
    assert client.get("/api/streaming", base_url="http://nas.home:7777").status_code == 200
    assert client.get("/api/streaming", base_url="http://evil.example:7777").status_code == 403


def test_the_lan_ip_guard_still_applies(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.get("/api/streaming", environ_base={"REMOTE_ADDR": "8.8.8.8"})
    assert res.status_code == 403


# ── No CORS ──
def test_a_preflight_is_not_granted(tmp_path, monkeypatch):
    _, client = make_client(tmp_path, monkeypatch)
    res = client.options("/api/streaming", headers={
        "Origin": "https://evil.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert "Access-Control-Allow-Origin" not in res.headers
    assert "Access-Control-Allow-Methods" not in res.headers


def test_cross_origin_reads_get_no_cors_grant(tmp_path, monkeypatch):
    server, client = make_client(tmp_path, monkeypatch)
    (server.SERVICE_ICONS_AUTO / "netflix.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 16)
    for path in ("/api/streaming", "/api/service-icon/netflix", "/api/ai/config"):
        res = client.get(path, headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in res.headers, path
