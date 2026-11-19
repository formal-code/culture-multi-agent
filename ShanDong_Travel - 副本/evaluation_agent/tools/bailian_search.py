"""Use actual Responses web_search records, never model-invented URLs."""
import time
from itertools import zip_longest
from urllib.parse import urlsplit
from .web_search import SearchHit
from .http_client import ToolError


def provider_sources(response: dict) -> list[dict]:
    found, seen = [], set()
    for item in response.get("output", []):
        candidates = []
        if item.get("type") == "web_search_call":
            if item.get("status") not in {None, "completed"}:
                continue
            candidates.extend(item.get("action", {}).get("sources", []))
        # url_citation is service-returned metadata, not a link in answer text.
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                for annotation in part.get("annotations", []) or []:
                    if annotation.get("type") == "url_citation":
                        candidates.append(annotation.get("url_citation", annotation))
        for candidate in candidates:
            url = candidate.get("url")
            if not isinstance(url, str):
                continue
            try:
                parts = urlsplit(url)
                if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                    continue
                _ = parts.port
                # Provider redirects remain unverified until PageReader reads the destination.
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            found.append({"url": url, "title": candidate.get("title") or parts.hostname,
                          "snippet": candidate.get("snippet") or ""})
    return found


class BailianSearch:
    def __init__(self, settings, store):
        from openai import OpenAI
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                             timeout=settings.llm_timeout, max_retries=0)
        self.settings, self.store = settings, store
        self.cache = {}

    def prepare(self, plan: list[dict], brief: dict, window: dict) -> None:
        self.cache = {item["query"]: [] for item in plan}
        grouped = {}
        for item in plan:
            purpose = item["purpose"]
            group = item.get("research_group") or ("facts_and_culture" if purpose in {"fact_check", "local_culture"} else (
                "audience_and_examples" if purpose in {"audience_context", "opening_examples"} else "spot_attention")
            )
            grouped.setdefault(group, []).append(item)
        all_sources, records = [], []
        for purpose, items in grouped.items():
            # One request per research purpose preserves why a source was found.
            prompt = (
                "你正在为一份景点短视频策划收集可核查的公开来源。只检索，不评价或筛选脚本。必须实际调用web_search。"
                "调查对象为" + brief["city"] + "的" + brief["spot_name"] + "，不要用全国热榜替代景点定向研究。"
                f"研究日期{window['as_of']}，近期窗口{window['start_date']}至{window['end_date']}。"
                "优先寻找政府、景区、可靠媒体或与该景点直接相关的公开页面。来源日期不详时不能称为近期；"
                "跨平台数据不能混用；不要输出无法从搜索来源确认的热度数字。"
                f"本轮研究维度：{purpose}。最多使用{self.settings.search_max_tool_calls}次搜索工具调用。"
                "检索任务：\n" + "\n".join(f"- {p['query']}" for p in items) +
                "\n只需简短汇总，不必编写完整报告，不输出虚构链接。"
            )
            started = time.monotonic()
            try:
                response = self.client.responses.create(
                    model=self.settings.model, input=prompt,
                    tools=[{"type": "web_search"}],
                    include=["web_search_call.action.sources"],
                    max_tool_calls=self.settings.search_max_tool_calls,
                    max_output_tokens=3500,
                    extra_body={"enable_thinking": False},
                )
                data = response.model_dump()
            except Exception as exc:
                self.store.log("bailian_web_search", "failed", purpose=purpose, error_type=type(exc).__name__)
                records.append({"purpose": purpose, "status": "failed", "error": "provider_request_failed"})
                continue
            sources = provider_sources(data)
            original_purposes = list(dict.fromkeys(i["purpose"] for i in items))
            for source in sources:
                source["research_purposes"] = original_purposes
                source["research_groups"] = [purpose]
            all_sources.extend(sources)
            search_calls = [item for item in data.get("output", []) if item.get("type") == "web_search_call"]
            records.append({
                "purpose": purpose, "status": "success" if sources else "no_sources",
                "response_status": data.get("status"),
                "search_calls": [{"status": item.get("status"), "action": item.get("action")} for item in search_calls],
                "tool_usage": data.get("tool_usage"), "accepted_sources": sources,
                "elapsed_sec": round(time.monotonic() - started, 2),
            })
        # Deduplicate sources while retaining all research purposes.
        by_url = {}
        for source in all_sources:
            if source["url"] not in by_url:
                by_url[source["url"]] = source
            else:
                by_url[source["url"]]["research_purposes"] = sorted(set(
                    by_url[source["url"]].get("research_purposes", []) + source.get("research_purposes", [])))
                by_url[source["url"]]["research_groups"] = sorted(set(
                    by_url[source["url"]].get("research_groups", []) + source.get("research_groups", [])))
        # Keep coverage across dimensions when the provider returns many URLs.
        ordered_urls = dict.fromkeys(
            source["url"] for batch in zip_longest(*(r.get("accepted_sources", []) for r in records))
            for source in batch if source
        )
        sources = [by_url[url] for url in ordered_urls][:min(60, self.settings.max_pages * 3)]
        self.store.save("search_provider_records.json", {
            "provider": "bailian_responses", "research_groups": records,
            "accepted_sources": sources,
            "notes": "来源取自Responses web_search工具元数据；模型正文和推理过程不作为搜索证据。",
        })
        if not sources:
            raise ToolError("no_verifiable_web_search_sources")
        self.hits = [SearchHit(s["title"], s["url"], s["snippet"],
                               research_purposes=s.get("research_purposes", [])) for s in sources]
        for item in plan:
            if item.get("research_group"):
                urls = {s["url"] for s in sources if item["research_group"] in s["research_groups"]}
                self.cache[item["query"]] = [h for h in self.hits if h.url in urls]
            else:
                self.cache[item["query"]] = [h for h in self.hits if item["purpose"] in (h.research_purposes or [])]

        self.store.log("bailian_web_search", "success", source_count=len(self.hits), attribution="purpose_grouped")

    def search(self, query):
        if query not in self.cache:
            raise ToolError("search_plan_not_prepared")
        return self.cache[query]
