import httpx
import pytest

from app.config import settings
from app.services import fetching
from app.services.fetching import FetchError, UnsafeURL, fetch_html, validate_url

PUBLIC_IP = "93.184.216.34"


@pytest.mark.parametrize(
    "ip, public",
    [
        (PUBLIC_IP, True),
        ("8.8.8.8", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public(ip, public):
    assert fetching._is_public(ip) is public


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/health",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.0.0.5/admin",
        "http://localhost/",
        "ftp://example.com/file",
        "file:///etc/passwd",
        "http:///nohost",
    ],
)
async def test_validate_url_rejects(url, fake_dns):
    fake_dns["localhost"] = ["127.0.0.1"]
    with pytest.raises(UnsafeURL):
        await validate_url(url)


async def test_validate_url_rejects_host_with_any_private_address(fake_dns):
    fake_dns["mixed.example"] = [PUBLIC_IP, "10.0.0.1"]
    with pytest.raises(UnsafeURL):
        await validate_url("https://mixed.example/")


async def test_validate_url_accepts_public_host(fake_dns):
    fake_dns["example.com"] = [PUBLIC_IP]
    await validate_url("https://example.com/article")


async def test_validate_url_allows_private_only_when_enabled(fake_dns, monkeypatch):
    monkeypatch.setattr(settings, "allow_private_urls", True)
    await validate_url("http://127.0.0.1:9000/")


class MockHttp(list):
    """The requests the downloader made (this list) plus a switchable response handler."""

    def __init__(self):
        super().__init__()
        self.respond = lambda request: httpx.Response(200, text="<html></html>")

    def set_handler(self, fn):
        self.respond = fn


@pytest.fixture
def mock_http(monkeypatch, fake_dns):
    mock = MockHttp()

    def handler(request):
        mock.append(request)
        return mock.respond(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(fetching.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    fake_dns["example.com"] = [PUBLIC_IP]
    return mock


async def test_fetch_connects_to_validated_ip_but_keeps_hostname(mock_http):
    body = await fetch_html("https://example.com/post?a=1")
    assert body == b"<html></html>"
    request = mock_http[0]
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.url.path == "/post" and request.url.query == b"a=1"


async def test_fetch_follows_redirect_to_public_host(mock_http, fake_dns):
    fake_dns["other.example"] = ["8.8.8.8"]
    mock_http.set_handler(
        lambda r: httpx.Response(302, headers={"location": "https://other.example/x"})
        if r.headers["host"] == "example.com"
        else httpx.Response(200, text="ok")
    )
    assert await fetch_html("https://example.com/") == b"ok"


@pytest.mark.parametrize("target", ["http://127.0.0.1/admin", "http://169.254.169.254/", "http://localhost:5432/"])
async def test_fetch_blocks_redirect_to_internal_address(mock_http, fake_dns, target):
    fake_dns["localhost"] = ["127.0.0.1"]
    mock_http.set_handler(lambda r: httpx.Response(302, headers={"location": target}))
    with pytest.raises(UnsafeURL):
        await fetch_html("https://example.com/")
    assert len(mock_http) == 1  # the internal address was never requested


async def test_fetch_stops_after_too_many_redirects(mock_http):
    mock_http.set_handler(lambda r: httpx.Response(302, headers={"location": "https://example.com/again"}))
    with pytest.raises(FetchError, match="redirects"):
        await fetch_html("https://example.com/")
    assert len(mock_http) == settings.max_redirects + 1


async def test_fetch_rejects_oversized_body(mock_http, monkeypatch):
    monkeypatch.setattr(settings, "max_page_mb", 1)
    mock_http.set_handler(lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))
    with pytest.raises(FetchError, match="larger"):
        await fetch_html("https://example.com/")


async def test_fetch_rejects_oversized_content_length(mock_http, monkeypatch):
    monkeypatch.setattr(settings, "max_page_mb", 1)
    huge = {"content-length": str(50 * 1024 * 1024)}
    mock_http.set_handler(lambda r: httpx.Response(200, headers=huge, content=b"x"))
    with pytest.raises(FetchError, match="larger"):
        await fetch_html("https://example.com/")


async def test_fetch_rejects_non_html(mock_http):
    mock_http.set_handler(lambda r: httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK"))
    with pytest.raises(FetchError, match="content type"):
        await fetch_html("https://example.com/")


async def test_fetch_reports_http_errors(mock_http):
    mock_http.set_handler(lambda r: httpx.Response(404))
    with pytest.raises(FetchError, match="404"):
        await fetch_html("https://example.com/")


async def test_fetch_wraps_network_errors(mock_http):
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    mock_http.set_handler(boom)
    with pytest.raises(FetchError):
        await fetch_html("https://example.com/")
