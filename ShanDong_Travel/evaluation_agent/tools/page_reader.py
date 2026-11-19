import re
import json
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from .http_client import HttpClient, ToolError, USER_AGENT


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title_parts, self.parts = [], []
        self.published_at = None
        self.date_basis = None
        self.json_ld = False
        self.json_parts = []
        self.main_depth = 0
        self.main_parts = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag in {"article", "main"}:
            self.main_depth += 1
        if tag == "script" and data.get("type", "").lower() == "application/ld+json":
            self.json_ld = True
            self.json_parts = []
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "br", "li", "h1", "h2", "article"}:
            self.parts.append(" ")
        if tag == "meta":
            if data.get("property", data.get("name", "")).lower() in {
                "article:published_time", "datepublished", "pubdate", "publishdate",
                "date", "pub_date", "publish_time", "articlepubdate", "dc.date.issued"}:
                self.published_at = data.get("content")
                self.date_basis = "publication_meta_tag"

    def handle_endtag(self, tag):
        if tag == "script" and self.json_ld:
            self.json_ld = False
            try:
                value = json.loads("".join(self.json_parts))
                def dates(node):
                    if isinstance(node, dict):
                        if isinstance(node.get("datePublished"), str):
                            yield node["datePublished"]
                        for child in node.values():
                            yield from dates(child)
                    elif isinstance(node, list):
                        for child in node:
                            yield from dates(child)
                if not self.published_at:
                    self.published_at = next(dates(value), None)
                    if self.published_at:
                        self.date_basis = "json_ld_datePublished"
            except (ValueError, RecursionError):
                pass
        if tag in {"article", "main"}:
            self.main_depth = max(0, self.main_depth - 1)
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.json_ld:
            self.json_parts.append(data)
        if self.in_title:
            self.title_parts.append(data)
        if not self.hidden:
            self.parts.append(data)
            if self.main_depth:
                self.main_parts.append(data)


class PageReader:
    def __init__(self, http: HttpClient):
        self.http = http
        self.robots = {}

    def _allowed(self, url: str):
        p = urlsplit(url)
        origin = urlunsplit((p.scheme, p.netloc, "", "", ""))
        if origin not in self.robots:
            try:
                response = self.http.get(origin + "/robots.txt", max_bytes=150_000)
                robot = RobotFileParser()
                robot.parse(response.text().splitlines())
                self.robots[origin] = robot
            except ToolError as exc:
                if str(exc) == "http_404":
                    self.robots[origin] = True
                else:
                    self.robots[origin] = False
        robot = self.robots[origin]
        if robot is False or (robot is not True and not robot.can_fetch(USER_AGENT, url)):
            raise ToolError("robots_disallowed_or_unavailable")

    def read(self, url: str) -> dict:
        # Check robots before fetching each page, including redirect destinations.
        response = self.http.get(url, before_request=self._allowed)
        final = urlsplit(response.url)
        if (final.hostname == "google.com" or (final.hostname or "").endswith(".google.com")) and final.path.startswith("/goto"):
            raise ToolError("search_redirect_not_resolved")
        mime = response.headers.get("content-type", "").lower()
        if "html" not in mime and "text/plain" not in mime:
            raise ToolError("unsupported_page_type")
        text = response.text()
        if "html" in mime:
            parser = _TextParser()
            parser.feed(text)
            main = normalize_text(" ".join(parser.main_parts))
            content = main if len(main) >= 200 else normalize_text(" ".join(parser.parts))
            title = normalize_text(" ".join(parser.title_parts))
            published = parser.published_at
            basis = parser.date_basis
        else:
            content, title, published = normalize_text(text), "", None
            basis = None
        if len(content) < 80:
            raise ToolError("page_text_too_short_or_dynamic")
        if re.search(r"验证码|安全验证|访问过于频繁|请完成验证|captcha", title + " " + content[:300], re.I):
            raise ToolError("verification_page_not_article")
        return {"url": response.url, "title": title, "text": content[:40000],
                "published_at": published, "publication_date_basis": basis}
