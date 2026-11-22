"""scoremodifier.index_meta.fetch_index_html: the SSRF-guarded index fetch.

No network: DNS (`_resolve`) and the HTTPS connection (`_open_connection`) are
replaced by fakes that serve scripted responses per (host, path).
"""

import pytest

from scoremodifier import index_meta as im

ALLOWED = ["www.figureskatingresults.fi", "figureskatingresults.fi"]
PUBLIC_IP = "93.184.215.14"
INDEX = b"<html><title>Cup</title></html>"


class FakeResponse:
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


class FakeConnection:
    def __init__(self, net, host, port, ip):
        self.net, self.host, self.port, self.ip = net, host, port, ip
        self.sock = None
        self.closed = False

    def request(self, method, target, headers=None):
        self.net.requests.append((method, self.host, self.port, self.ip, target, headers))
        self.target = target

    def getresponse(self):
        return self.net.routes[(self.host, self.target)]()

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

    def open(self, host, port, ip, timeout):
        assert timeout > 0
        conn = FakeConnection(self, host, port, ip)
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
    def boom(host, port, ip, timeout):
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
