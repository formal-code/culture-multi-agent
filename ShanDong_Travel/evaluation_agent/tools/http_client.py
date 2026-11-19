"""Bounded public-HTTP client. No login, cookies, or access-control bypass."""
import http.client
import ipaddress
import json
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

USER_AGENT = "CultureEvidenceAgent/0.1"


class ToolError(Exception):
    """A short non-sensitive error code suitable for a saved tool log."""


def request_target(url: str) -> str:
    parts = urlsplit(url)
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&/?@:!$'()*+,;%-._~[]")
    return path + ("?" + query if query else "")


def public_target(url: str) -> tuple[str, str, int, str]:
    if any(ord(c) < 32 for c in url) or len(url) > 6000:
        raise ToolError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ToolError("invalid_url") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ToolError("public_http_url_required")
    if port not in {80, 443}:
        raise ToolError("port_not_allowed")
    host = parts.hostname.encode("idna").decode("ascii")
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ToolError("dns_failed") from exc
    # Some resolvers return a mix of usable public records and special-use
    # addresses (for example, a synthetic AAAA record). Never connect to a
    # non-public result, but do not discard a valid public result because a
    # different address in the same DNS answer is unusable. The selected IP is
    # pinned below, so the HTTP/TLS connection cannot re-resolve to a private
    # address between validation and connect.
    ips = list(dict.fromkeys(
        a[4][0] for a in addresses
        if ipaddress.ip_address(a[4][0]).is_global
    ))
    if not ips:
        raise ToolError("non_public_address_blocked")
    # Pin the validated address for the connection, including HTTPS SNI.
    return parts.scheme, host, port, ips[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self.ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)


class _PinnedHTTPS(_PinnedHTTP):
    def connect(self):
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


@dataclass
class Response:
    url: str
    headers: dict
    body: bytes

    def text(self) -> str:
        import re
        content_type = self.headers.get("content-type", "")
        match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
        enc = match.group(1) if match else "utf-8"
        if not match:
            # Honor common HTML charset declarations when HTTP omits charset.
            meta = re.search(br"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", self.body[:4096])
            if meta:
                enc = meta.group(1).decode("ascii")
        try:
            return self.body.decode(enc, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class HttpClient:
    def __init__(self, timeout: int, interval: float, max_requests: int = 100):
        self.timeout, self.interval = timeout, interval
        self.max_requests, self.request_count = max_requests, 0
        self.last_request = 0.0

    def get(self, url: str, *, max_bytes=2_000_000, headers=None, before_request=None) -> Response:
        return self.request(url, max_bytes=max_bytes, headers=headers, before_request=before_request)

    def post_json(self, url: str, payload: dict, *, headers=None) -> Response:
        return self.request(url, method="POST", data=json.dumps(payload).encode("utf-8"),
                            headers={"Content-Type": "application/json", **(headers or {})})

    def request(self, url: str, *, method="GET", data=None, headers=None, max_bytes=2_000_000, before_request=None) -> Response:
        for redirect in range(4):
            scheme, host, port, ip = public_target(url)
            if before_request:
                before_request(url)
            if self.request_count >= self.max_requests:
                raise ToolError("http_budget_exhausted")
            delay = self.interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            self.request_count += 1
            cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
            conn = cls(host, port, ip, self.timeout)
            parts = urlsplit(url)
            path = request_target(url)
            try:
                conn.request(method, path, body=data, headers={
                    "User-Agent": USER_AGENT, "Accept-Encoding": "identity", **(headers or {})})
                response = conn.getresponse()
                response_headers = {k.lower(): v for k, v in response.getheaders()}
                if response.status in {301, 302, 303, 307, 308}:
                    if method != "GET":
                        raise ToolError("api_redirect_not_allowed")
                    location = response_headers.get("location")
                    if not location:
                        raise ToolError("redirect_without_location")
                    # Revalidate every redirect. Never forward secrets to another origin.
                    url = urljoin(url, location)
                    headers = None
                    continue
                if response.status < 200 or response.status >= 300:
                    raise ToolError(f"http_{response.status}")
                content = response.read(max_bytes + 1)
                if len(content) > max_bytes:
                    raise ToolError("response_too_large")
                return Response(url, response_headers, content)
            except ToolError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise ToolError("http_connection_failed") from exc
            except UnicodeError as exc:
                raise ToolError("http_request_encoding_failed") from exc
            finally:
                conn.close()
        raise ToolError("too_many_redirects")
