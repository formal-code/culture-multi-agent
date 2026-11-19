"""有时限、大小和请求总量约束的公共网页读取与媒体下载。"""
from dataclasses import dataclass
from functools import lru_cache
import ipaddress
from pathlib import Path
import socket
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

import config
from culture.types import BudgetExceeded, CollectionError

USER_AGENT = "Culture2MaterialCollector/1.0"


@lru_cache(maxsize=256)
def public_host(host):
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CollectionError("域名解析失败：" + host) from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise CollectionError("只读取公开网站地址：" + host)


def validate_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise CollectionError("地址不是公开 HTTP/HTTPS 链接")
    try:
        if p.port not in (None, 80, 443):
            raise CollectionError("不读取非标准网站端口")
    except ValueError as exc:
        raise CollectionError("网站端口无效") from exc
    public_host(p.hostname)
    return p


@dataclass
class Response:
    url: str
    content: bytes
    content_type: str
    encoding: str | None
    status: int = 200

    def text(self):
        # requests 对无 charset 的 HTML 默认 Latin-1；中文网页需检查 meta。
        import re
        match = re.search(br'charset\s*=\s*["\']?([\w-]+)', self.content[:8192], re.I)
        encoding = match.group(1).decode("ascii") if match else self.encoding
        if not encoding or encoding.lower() in {"iso-8859-1", "latin-1"}:
            encoding = "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


class WebClient:
    def __init__(self, budget, *, progress=None):
        self.budget = budget
        self.progress = progress or (lambda message: None)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.robots = {}
        self.robots_warnings = set()
        self.last_origin = {}

    def close(self):
        self.session.close()

    def _raw(self, url, max_bytes, timeout, *, referer="", robots=False, destination=None, hosts=None,
             media_type=""):
        deadline = time.monotonic() + min(timeout, self.budget.remaining_seconds)
        original_url = url
        for _ in range(6):
            p = validate_url(url)
            if hosts is not None and p.hostname not in hosts:
                raise CollectionError("页面跳转到未配置的官网之外，未继续读取")
            if not robots:
                self.allowed(url, media_type=media_type)
            self.budget.check()
            origin = f"{p.scheme}://{p.netloc}"
            delay = max(0, self.last_origin.get(origin, 0) + 0.4 - time.monotonic())
            if delay:
                time.sleep(min(delay, self.budget.remaining_seconds))
            remaining = min(deadline - time.monotonic(), self.budget.remaining_seconds)
            if remaining <= 0:
                raise CollectionError("本次读取超过时限")
            self.budget.take("requests")
            self.last_origin[origin] = time.monotonic()
            try:
                headers = {"Referer": referer} if referer else {}
                with self.session.get(url, headers=headers, timeout=(min(6, remaining), min(8, remaining)),
                                      allow_redirects=False, stream=True) as response:
                    if response.is_redirect or response.is_permanent_redirect:
                        target = response.headers.get("Location")
                        if not target:
                            raise CollectionError("网页跳转缺少目标地址")
                        url = urljoin(url, target)
                        continue
                    if robots and response.status_code in {403, 404, 410}:
                        # A robots 403 means the rules file is unavailable, not a Disallow rule
                        # for every resource (RFC 9309, 2.3.1.3). Media errors still stop below.
                        return Response(url, b"", "", None, response.status_code)
                    if response.status_code >= 400:
                        kind = "规则文件" if robots else ("媒体文件" if destination else "网页")
                        raise CollectionError(f"{kind}返回 HTTP {response.status_code}：{urlsplit(url).hostname}")
                    length = response.headers.get("Content-Length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise CollectionError("文件超过大小上限")
                    chunks, size = [], 0
                    handle = Path(destination).open("wb") if destination else None
                    try:
                        for chunk in response.iter_content(64 * 1024):
                            self.budget.check()
                            if time.monotonic() >= deadline:
                                raise CollectionError("读取超过时限")
                            if destination:
                                self.budget.take_download_bytes(len(chunk))
                            size += len(chunk)
                            if size > max_bytes:
                                raise CollectionError("响应内容超过大小上限")
                            if handle:
                                handle.write(chunk)
                            else:
                                chunks.append(chunk)
                    finally:
                        if handle:
                            handle.close()
                    return Response(url, b"" if destination else b"".join(chunks),
                                    response.headers.get("Content-Type", "").lower(), response.encoding)
            except requests.RequestException as exc:
                # 不输出整个带访问令牌的网址，也不把网络错误当成验证码重试。
                raise CollectionError(f"读取失败/超时：{urlsplit(url).hostname}（{type(exc).__name__}）") from exc
        raise CollectionError("跳转次数过多：" + urlsplit(original_url).hostname)

    def allowed(self, url, *, media_type=""):
        p = validate_url(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            try:
                response = self._raw(origin + "/robots.txt", 512 * 1024, 8, robots=True)
                if response.status in {403, 404, 410}:
                    self.robots[origin] = None
                    if response.status == 403:
                        self.progress(f"[访问诊断] {origin}/robots.txt 返回403，规则文件不可用；"
                                      "继续检查页面提供的公开地址，媒体文件若返回403仍停止。")
                else:
                    if "html" in response.content_type:
                        raise CollectionError("robots 返回异常 HTML")
                    parser = RobotFileParser()
                    parser.parse(response.text().splitlines())
                    self.robots[origin] = parser
            except BudgetExceeded:
                raise
            except CollectionError as exc:
                self.robots[origin] = str(exc)
        rule = self.robots[origin]
        if isinstance(rule, str):
            raise CollectionError("站点访问规则暂时无法确认：" + rule)
        if rule is not None and not rule.can_fetch(USER_AGENT, url):
            prefixes = config.VIDEO_ROBOTS_WARNING_ONLY.get(p.hostname, ())
            prefix = next((item for item in prefixes if p.path.startswith(item)), None)
            if (media_type == "video" and prefix is not None
                    and p.path.lower().endswith((".mp4", ".webm", ".mov"))):
                key = (origin, prefix)
                if key not in self.robots_warnings:
                    self.robots_warnings.add(key)
                    self.progress(f"[访问策略] {origin}{prefix} 的视频按指定配置将robots禁止声明仅作提示；"
                                  "尝试真实媒体地址，文件返回403或其他错误仍停止。")
                return
            raise CollectionError("站点规则不允许自动读取此地址")

    def html(self, url, *, hosts=None):
        response = self._raw(url, config.MAX_HTML_BYTES, config.PAGE_TIMEOUT, hosts=hosts)
        if response.content_type and not any(x in response.content_type for x in ("html", "xml", "text/plain")):
            raise CollectionError("该链接不是网页")
        return response.url, response.text()

    def download(self, url, destination, media_type, referer):
        limit = config.MAX_IMAGE_BYTES if media_type == "image" else config.MAX_VIDEO_BYTES
        try:
            return self._raw(url, limit, config.DOWNLOAD_TIMEOUT, referer=referer, destination=destination,
                             media_type=media_type)
        except Exception:
            Path(destination).unlink(missing_ok=True)
            raise
