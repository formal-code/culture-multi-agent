import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from .http_client import HttpClient, ToolError


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    published_at: str | None = None
    research_purposes: list[str] | None = None


class WebSearch:
    def __init__(self, settings, http: HttpClient):
        self.settings, self.http = settings, http

    def search(self, query: str) -> list[SearchHit]:
        limit = self.settings.results_per_query
        if self.settings.search_provider == "bing_rss":
            response = self.http.get("https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"}))
            if b"<!DOCTYPE" in response.body.upper() or b"<!ENTITY" in response.body.upper():
                raise ToolError("unsafe_search_xml")
            try:
                root = ET.fromstring(response.body)
            except ET.ParseError as exc:
                raise ToolError("search_response_not_rss") from exc
            if root.tag != "rss":
                raise ToolError("search_response_not_rss")
            hits = [SearchHit(item.findtext("title", ""), item.findtext("link", ""),
                              item.findtext("description", "")) for item in root.findall("./channel/item")]
            # RSS pubDate is not necessarily the original page's publication time.
        elif self.settings.search_provider == "tavily":
            if not self.settings.tavily_api_key:
                raise ToolError("missing_tavily_api_key")
            response = self.http.post_json("https://api.tavily.com/search", {
                "query": query, "max_results": limit, "include_raw_content": False,
                "search_depth": "basic"}, headers={"Authorization": "Bearer " + self.settings.tavily_api_key})
            try:
                payload = json.loads(response.text())
                if not isinstance(payload.get("results"), list):
                    raise ValueError()
                hits = [SearchHit(str(r.get("title", "")), str(r.get("url", "")),
                                  str(r.get("content", "")), r.get("published_date"))
                        for r in payload["results"] if isinstance(r, dict)]
            except (ValueError, TypeError) as exc:
                raise ToolError("invalid_search_response") from exc
        else:
            raise ToolError("unknown_search_provider")
        valid = []
        for hit in hits:
            try:
                parsed = urlsplit(hit.url)
                if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                    valid.append(hit)
            except ValueError:
                continue
        return valid[:limit]
