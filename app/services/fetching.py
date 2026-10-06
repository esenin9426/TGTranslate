"""Download user-supplied URLs safely (SSRF protection, size and time limits)."""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings

USER_AGENT = "Mozilla/5.0 (compatible; TGTranslateBot/0.1)"
HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class UnsafeURL(ValueError):
    """The URL is malformed or points to a non-public address."""


class FetchError(ValueError):
    """The page could not be downloaded."""


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def _split(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("Only http(s) links are supported")
    try:
        return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("Invalid port in the link") from exc


async def resolve_public(host: str, port: int) -> str:
    """Resolve `host` and return one IP, refusing the whole host if any address is not public."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve {host}") from exc
    ips = [info[4][0] for info in infos]
    if not ips:
        raise FetchError(f"Could not resolve {host}")
    if not all(_is_public(ip) for ip in ips):
        raise UnsafeURL("The link points to a non-public address")
    return ips[0]


async def validate_url(url: str) -> None:
    """Cheap pre-check for the API so the user gets an immediate error instead of a failed source."""
    host, port = _split(url)
    if not settings.allow_private_urls:
        await resolve_public(host, port)


def _content_length(resp: httpx.Response) -> int:
    try:
        return int(resp.headers.get("content-length") or 0)
    except ValueError:  # a garbage header: the streamed size check below still applies
        return 0


async def fetch_html(url: str) -> bytes:
    """GET a page following at most `max_redirects` redirects, validating every hop.

    The request goes to the IP that was validated (with Host/SNI set to the real name), so a DNS record that
    changes between the check and the connection (DNS rebinding) cannot redirect it to an internal address.
    """
    limit = settings.max_page_mb * 1024 * 1024
    async with httpx.AsyncClient(timeout=settings.fetch_timeout, follow_redirects=False) as client:
        for _ in range(settings.max_redirects + 1):
            host, port = _split(url)
            headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"}
            extensions = {}
            request_url = url
            if not settings.allow_private_urls:
                ip = await resolve_public(host, port)
                parts = urlsplit(url)
                netloc = f"[{ip}]" if ":" in ip else ip
                request_url = parts._replace(netloc=f"{netloc}:{port}").geturl()
                headers["Host"] = parts.netloc.rsplit("@", 1)[-1]
                extensions["sni_hostname"] = host

            try:
                async with client.stream("GET", request_url, headers=headers, extensions=extensions) as resp:
                    if resp.is_redirect and (location := resp.headers.get("location")):
                        url = urljoin(url, location)
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"The site answered HTTP {resp.status_code}")
                    content_type = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
                    if content_type not in HTML_TYPES:
                        raise FetchError(f"Unsupported content type: {content_type}")
                    if _content_length(resp) > limit:
                        raise FetchError(f"The page is larger than {settings.max_page_mb} MB")
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > limit:
                            raise FetchError(f"The page is larger than {settings.max_page_mb} MB")
                    return bytes(body)
            except httpx.HTTPError as exc:
                raise FetchError(f"Could not download the page ({type(exc).__name__})") from exc
    raise FetchError("Too many redirects")
