"""scoremodifier.index_meta.fetch_index_html: the SSRF-guarded index fetch.

No network: DNS (`_resolve`) and the HTTPS connection (`_open_connection`) are
replaced by fakes that serve scripted responses per (host, path).
"""

import threading
import time

import pytest

from scoremodifier import index_meta as im

ALLOWED = ["www.figureskatingresults.fi", "figureskatingresults.fi"]
PUBLIC_IP = "93.184.215.14"
INDEX = b"<html><title>Cup</title></html>"


class FakeResponse:
    chunked = False

    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.read_sizes = []

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, amt=None):
        self.read_sizes.append(amt)
        chunk, self._body = (self._body, b"") if amt is None else (self._body[:amt], self._body[amt:])
        return chunk

    read1 = read


class FakeSock:
    def __init__(self):
        self.timeouts = []
        self.shut = threading.Event()

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shut.set()


class FakeConnection:
    def __init__(self, net, host, port, ip):
        self.net, self.host, self.port, self.ip = net, host, port, ip
        self.sock = self.fake_sock = FakeSock()
        self.closed = False

    def request(self, method, target, headers=None):
        self.net.requests.append((method, self.host, self.port, self.ip, target, headers))
        self.target = target

    def getresponse(self):
        resp = self.net.routes[(self.host, self.target)]()
        resp.sock = self.fake_sock
        # Like http.client for a closing response: the connection forgets
        # its socket, the response keeps reading from it.
        self.sock = None
        return resp

    def close(self):
        self.closed = True


class FakeNet:
    def __init__(self):
        self.dns = {}
        self.routes = {}
        self.requests = []
        self.connections = []

    def resolve(self, host, port):
        if host not in self.dns:
            raise OSError("no such host")
        return self.dns[host]

    def open(self, host, port, ip, remaining, watch):
        assert remaining() > 0
        conn = FakeConnection(self, host, port, ip)
        watch(conn.fake_sock)
        self.connections.append(conn)
        return conn

    def serve(self, host, path, status=200, body=b"", headers=None):
        self.routes[(host, path)] = lambda: FakeResponse(status, body, headers)


@pytest.fixture
def net(monkeypatch):
    n = FakeNet()
    n.dns = {"www.figureskatingresults.fi": [PUBLIC_IP], "figureskatingresults.fi": [PUBLIC_IP]}
    monkeypatch.setattr(im, "_resolve", n.resolve)
    monkeypatch.setattr(im, "_open_connection", n.open)
    return n


def fetch(url):
    return im.fetch_index_html(url, allowed_hosts=ALLOWED)


def test_happy_path(net):
    net.serve("www.figureskatingresults.fi", "/results/2526/X/index.htm", body=INDEX)
    assert fetch("https://www.figureskatingresults.fi/results/2526/X/index.htm") == INDEX.decode()
    (method, host, port, ip, target, headers), = net.requests
    assert (method, host, port, ip, target) == ("GET", "www.figureskatingresults.fi", 443, PUBLIC_IP, "/results/2526/X/index.htm")
    assert headers["User-Agent"] == im.USER_AGENT
    assert all(c.closed for c in net.connections)


def test_http_link_is_upgraded_and_never_sent_in_clear(net):
    net.serve("www.figureskatingresults.fi", "/r/index.htm", body=INDEX)
    assert fetch("http://www.figureskatingresults.fi/r/index.htm") == INDEX.decode()
    assert [(h, p) for _, h, p, *_ in net.requests] == [("www.figureskatingresults.fi", 443)]


@pytest.mark.parametrize("url", [
    "http://www.figureskatingresults.fi:8080/r/index.htm",   # http elsewhere than :80
    "ftp://www.figureskatingresults.fi/r/index.htm",
    "file:///etc/passwd",
    "https://evil.example/index.htm",
    "https://169.254.169.254/latest/meta-data",
    "https://www.figureskatingresults.fi.evil.example/",
    "https://user:pw@www.figureskatingresults.fi/r/index.htm",
    "https://www.figureskatingresults.fi:8443/r/index.htm",
    "https://www.figureskatingresults.fi:80/r/index.htm",
    "https:///nohost",
    "https://[::1/",
    "",
    "   ",
    None,
    123,
    ["https://www.figureskatingresults.fi/"],
])
def test_unacceptable_urls_are_rejected_before_any_request(net, url):
    with pytest.raises(im.IndexUrlRejected) as e:
        fetch(url)
    assert isinstance(e.value, ValueError)
    assert net.requests == []


def test_redirect_within_the_allow_list_is_followed(net):
    net.serve("figureskatingresults.fi", "/r/X", 301, headers={"Location": "https://www.figureskatingresults.fi/r/X/"})
    net.serve("www.figureskatingresults.fi", "/r/X/", 302, headers={"Location": "index.htm"})
    net.serve("www.figureskatingresults.fi", "/r/X/index.htm", body=INDEX)
    assert fetch("https://figureskatingresults.fi/r/X") == INDEX.decode()
    assert [t for *_, t, _h in net.requests] == ["/r/X", "/r/X/", "/r/X/index.htm"]


@pytest.mark.parametrize("location, code", [
    ("http://localhost:8080/metadata/instance", "redirect_scheme_not_allowed"),
    ("https://localhost/metadata/instance", "redirect_host_not_allowed"),
    ("https://evil.example/", "redirect_host_not_allowed"),
    ("http://www.figureskatingresults.fi/r/index.htm", "redirect_scheme_not_allowed"),  # no downgrade
    ("file:///etc/passwd", "redirect_invalid_url"),
])
def test_redirect_off_the_allow_list_or_to_http_is_refused(net, location, code):
    net.serve("www.figureskatingresults.fi", "/r/index.htm", 302, headers={"Location": location})
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == code
    # Not a ValueError: the caller's URL was fine, the target misbehaved.
    assert not isinstance(e.value, ValueError)
    assert len(net.requests) == 1


def test_redirect_loop_is_cut_off(net):
    net.serve("www.figureskatingresults.fi", "/a", 302, headers={"Location": "/a"})
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/a")
    assert e.value.code == "too_many_redirects"
    assert len(net.requests) == im.MAX_REDIRECTS + 1


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "240.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "fd12::1", "ff02::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
])
def test_non_public_addresses_are_refused(net, ip):
    net.dns["www.figureskatingresults.fi"] = [ip]
    net.serve("www.figureskatingresults.fi", "/r/index.htm", body=INDEX)
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "address_not_allowed"
    assert net.connections == []


def test_one_private_answer_among_public_ones_is_refused(net):
    net.dns["www.figureskatingresults.fi"] = [PUBLIC_IP, "10.0.0.5"]
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "address_not_allowed"


def test_redirect_target_is_resolved_and_checked_too(net):
    net.dns["figureskatingresults.fi"] = ["127.0.0.1"]
    net.serve("www.figureskatingresults.fi", "/r", 301, headers={"Location": "https://figureskatingresults.fi/r/"})
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r")
    assert e.value.code == "address_not_allowed"
    assert len(net.connections) == 1


@pytest.mark.parametrize("ip", [PUBLIC_IP, "2a00:1450:4010:c0e::64"])
def test_public_addresses_pass(ip):
    assert im._is_public_address(ip)


def test_oversize_body_is_refused(net):
    net.serve("www.figureskatingresults.fi", "/big", body=b"A" * (im.MAX_INDEX_BYTES + 10))
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/big")
    assert e.value.code == "too_large"


def test_oversize_content_length_is_refused_before_reading(net):
    resp = FakeResponse(200, b"A", {"Content-Length": str(im.MAX_INDEX_BYTES + 1)})
    net.routes[("www.figureskatingresults.fi", "/big")] = lambda: resp
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/big")
    assert e.value.code == "too_large"
    assert resp.read_sizes == []


def test_body_exactly_at_the_cap_is_accepted(net):
    net.serve("www.figureskatingresults.fi", "/max", body=b"A" * im.MAX_INDEX_BYTES)
    assert len(fetch("https://www.figureskatingresults.fi/max")) == im.MAX_INDEX_BYTES


def test_upstream_error_status_is_a_fetch_error(net):
    net.serve("www.figureskatingresults.fi", "/missing", 404)
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/missing")
    assert e.value.code == "upstream_status_404"


def test_connection_failures_are_wrapped(net, monkeypatch):
    def boom(host, port, ip, remaining, watch):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(im, "_open_connection", boom)
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert not isinstance(e.value, ValueError)


def test_unresolvable_host_is_a_fetch_error(net):
    del net.dns["www.figureskatingresults.fi"]
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "unresolvable"


def test_total_timeout_is_enforced(net, monkeypatch):
    clock = iter([0.0, im.TOTAL_TIMEOUT + 1] + [im.TOTAL_TIMEOUT + 2] * 10)
    monkeypatch.setattr(im, "_monotonic", lambda: next(clock))
    net.serve("www.figureskatingresults.fi", "/r/index.htm", body=INDEX)
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "timeout"


def test_odd_characters_in_the_path_are_quoted(net):
    net.serve("www.figureskatingresults.fi", "/r/a%20b/%C3%A4.htm", body=INDEX)
    assert fetch("https://www.figureskatingresults.fi/r/a b/ä.htm") == INDEX.decode()


def test_cli_mode_still_enforces_https_and_public_addresses(net):
    net.dns["intranet.local"] = ["10.0.0.1"]
    with pytest.raises(im.IndexFetchError):
        im.fetch_index_html("https://intranet.local/index.htm", allowed_hosts=None)
    with pytest.raises(im.IndexUrlRejected):
        im.fetch_index_html("ftp://intranet.local/index.htm", allowed_hosts=None)


# ── deadline, address fallback (review follow-ups) ────────────────────────────

def test_next_checked_address_is_tried_without_resolving_again(net, monkeypatch):
    v6, v4 = "2a00:1450:4010:c0e::64", PUBLIC_IP
    net.dns["www.figureskatingresults.fi"] = [v6, v4]
    lookups, attempts = [], []
    monkeypatch.setattr(im, "_resolve", lambda host, port: lookups.append(host) or net.dns[host])

    def open_(host, port, ip, remaining, watch):
        attempts.append(ip)
        if ip == v6:
            raise OSError("network unreachable")
        return net.open(host, port, ip, remaining, watch)

    monkeypatch.setattr(im, "_open_connection", open_)
    net.serve("www.figureskatingresults.fi", "/r/index.htm", body=INDEX)
    assert fetch("https://www.figureskatingresults.fi/r/index.htm") == INDEX.decode()
    assert attempts == [v6, v4]
    assert lookups == ["www.figureskatingresults.fi"]
    assert net.requests[0][3] == v4


def test_all_addresses_unreachable_is_a_fetch_error(net, monkeypatch):
    net.dns["www.figureskatingresults.fi"] = [PUBLIC_IP, "2a00:1450:4010:c0e::64"]
    attempts = []

    def refuse(host, port, ip, remaining, watch):
        attempts.append(ip)
        raise ConnectionRefusedError()

    monkeypatch.setattr(im, "_open_connection", refuse)
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "unreachable_ConnectionRefusedError"
    assert attempts == net.dns["www.figureskatingresults.fi"]


def test_slow_dns_is_bounded_and_cannot_answer_a_later_lookup(net, monkeypatch):
    monkeypatch.setattr(im, "TOTAL_TIMEOUT", 0.3)
    release, calls = threading.Event(), []

    def resolve(host, port):
        calls.append(host)
        if len(calls) == 1:
            release.wait(5)
            return ["10.0.0.1"]  # a late, poisoned answer
        return [PUBLIC_IP]

    monkeypatch.setattr(im, "_resolve", resolve)
    net.serve("www.figureskatingresults.fi", "/r/index.htm", body=INDEX)
    started = time.monotonic()
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "timeout"
    assert time.monotonic() - started < 2
    assert net.connections == []

    release.set()
    time.sleep(0.05)  # the abandoned lookup finishes now
    assert fetch("https://www.figureskatingresults.fi/r/index.htm") == INDEX.decode()
    assert net.requests[-1][3] == PUBLIC_IP


class TrickleResponse(FakeResponse):
    """One byte per read, each read costing a second of (fake) time."""

    def __init__(self, clock):
        super().__init__(200, b"A" * 1000)
        self.clock = clock

    def read(self, amt=None):
        self.clock[0] += 1.0
        return super().read(1)

    read1 = read


def test_trickled_body_of_a_closing_response_stops_at_the_deadline(net, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(im, "_monotonic", lambda: clock[0])
    resp = TrickleResponse(clock)
    net.routes[("www.figureskatingresults.fi", "/slow")] = lambda: resp
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/slow")
    assert e.value.code == "timeout"
    assert clock[0] <= im.TOTAL_TIMEOUT + 1
    # The deadline reached the socket even though the connection dropped it.
    sock = net.connections[0].fake_sock
    assert net.connections[0].sock is None
    assert len(sock.timeouts) >= im.TOTAL_TIMEOUT - 1
    assert all(0 < t <= 10.0 for t in sock.timeouts)
    assert sock.timeouts[-1] <= 1.0


class StalledResponse(FakeResponse):
    """A read that blocks until the socket is shut down (a stalled server)."""

    def read(self, amt=None):
        if not self.sock.shut.wait(5):
            return b"late"
        raise OSError("socket shut down")

    read1 = read


def test_a_stalled_read_is_cut_off_at_the_deadline(net, monkeypatch):
    monkeypatch.setattr(im, "TOTAL_TIMEOUT", 0.3)
    net.routes[("www.figureskatingresults.fi", "/stall")] = lambda: StalledResponse(200)
    started = time.monotonic()
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/stall")
    assert e.value.code == "timeout"
    assert time.monotonic() - started < 2
    assert net.connections[0].fake_sock.shut.is_set()


# ── second review pass: port, one budget, Content-Length ─────────────────────

def test_only_port_443_whatever_the_allow_list_says(net):
    # CLI mode (no allow-list): still 443 only.
    with pytest.raises(im.IndexUrlRejected) as e:
        im.fetch_index_html("https://example.com:8443/index.htm", allowed_hosts=None)
    assert e.value.code == "port_not_allowed"
    # A host:port entry does not open that port.
    with pytest.raises(im.IndexUrlRejected) as e:
        im.fetch_index_html("https://www.figureskatingresults.fi:8443/r/index.htm",
                            allowed_hosts=["www.figureskatingresults.fi:8443"])
    assert e.value.code == "port_not_allowed"
    assert im._allowed_set(["www.figureskatingresults.fi:8443"]) == {"www.figureskatingresults.fi:8443"}
    assert net.requests == []


def test_explicit_443_and_host_443_entries_work(net):
    net.serve("www.figureskatingresults.fi", "/r/index.htm", body=INDEX)
    assert fetch("https://www.figureskatingresults.fi:443/r/index.htm") == INDEX.decode()
    assert im.fetch_index_html("https://www.figureskatingresults.fi/r/index.htm",
                               allowed_hosts=[" WWW.FigureSkatingResults.fi:443 "]) == INDEX.decode()
    assert all(port == 443 for _, _, port, *_ in net.requests)


def test_redirect_to_another_port_is_refused(net):
    net.serve("www.figureskatingresults.fi", "/r/index.htm", 302,
              headers={"Location": "https://www.figureskatingresults.fi:8443/r/index.htm"})
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "redirect_port_not_allowed"
    assert len(net.requests) == 1


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _SlowTLS:
    """A TLS socket whose handshake needs `need` seconds; it gives up when
    its timeout is shorter (the timeout's worth of time has passed)."""

    def __init__(self, clock, need, log):
        self.clock, self.need, self.log = clock, need, log
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def do_handshake(self):
        self.log.append(("tls", self.timeout))
        self.clock.now += min(self.need, self.timeout)
        if self.need > self.timeout:
            raise TimeoutError("handshake timed out")

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


class _SlowContext:
    def __init__(self, clock, need, log):
        self.clock, self.need, self.log = clock, need, log
        self.wrapped = []

    def wrap_socket(self, sock, server_hostname, do_handshake_on_connect=True):
        assert do_handshake_on_connect is False
        tls = _SlowTLS(self.clock, self.need, self.log)
        self.wrapped.append((server_hostname, tls))
        return tls


@pytest.fixture
def slow_path(monkeypatch):
    """The real _open_connection with a fake clock, DNS, TCP and TLS layer:
    DNS 8 s, TCP connect 9 s, TLS handshake 9 s if allowed — 26 s in all."""
    clock, log = _Clock(), []
    monkeypatch.setattr(im, "_monotonic", clock)

    def resolve(host, port):
        clock.now += 8
        log.append(("dns", None))
        return [PUBLIC_IP, "2a00:1450:4010:c0e::64"]

    def tcp_connect(address, timeout):
        log.append(("tcp", timeout))
        clock.now += min(9, timeout)
        if 9 > timeout:
            raise TimeoutError("connect timed out")
        return object()

    ctx = _SlowContext(clock, 9, log)
    monkeypatch.setattr(im, "_resolve", resolve)
    monkeypatch.setattr(im, "_tcp_connect", tcp_connect)
    monkeypatch.setattr(im, "_tls_context", lambda: ctx)
    return clock, log, ctx


def test_dns_connect_and_handshake_share_one_budget(slow_path):
    clock, log, ctx = slow_path
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/r/index.htm")
    assert e.value.code == "timeout"
    assert clock.now <= im.TOTAL_TIMEOUT
    # TCP got the per-operation cap (12 s left), the handshake only what the
    # first two phases left over — never a fresh timeout per phase — and the
    # second address was not tried once the budget was spent.
    assert log == [("dns", None), ("tcp", 10.0), ("tls", 3.0)]
    (hostname, tls), = ctx.wrapped
    assert hostname == "www.figureskatingresults.fi" and tls.closed


def test_open_connection_hands_the_socket_to_the_watchdog_before_the_handshake(slow_path):
    clock, log, ctx = slow_path
    ctx.need = 1
    watched = []
    conn = im._open_connection("www.figureskatingresults.fi", 443, PUBLIC_IP,
                               lambda: 10.0, lambda s: watched.append((s, list(log))))
    tls = ctx.wrapped[0][1]
    assert conn.sock is tls and conn.host == "www.figureskatingresults.fi"
    assert watched == [(tls, [("tcp", 10.0)])]  # before the handshake ran
    assert log[-1] == ("tls", 10.0)


def test_response_shorter_than_its_content_length_is_refused(net):
    net.serve("www.figureskatingresults.fi", "/cut", body=b"A" * 50, headers={"Content-Length": "100"})
    with pytest.raises(im.IndexFetchError) as e:
        fetch("https://www.figureskatingresults.fi/cut")
    assert e.value.code == "incomplete_body"


def test_response_matching_its_content_length_is_accepted(net):
    net.serve("www.figureskatingresults.fi", "/ok", body=INDEX, headers={"Content-Length": str(len(INDEX))})
    assert fetch("https://www.figureskatingresults.fi/ok") == INDEX.decode()


def test_response_without_content_length_is_accepted(net):
    net.serve("www.figureskatingresults.fi", "/nolen", body=INDEX)
    assert fetch("https://www.figureskatingresults.fi/nolen") == INDEX.decode()


def test_chunked_response_is_not_held_to_a_content_length(net):
    resp = FakeResponse(200, INDEX, {"Content-Length": "100", "Transfer-Encoding": "chunked"})
    resp.chunked = True
    net.routes[("www.figureskatingresults.fi", "/chunked")] = lambda: resp
    assert fetch("https://www.figureskatingresults.fi/chunked") == INDEX.decode()
