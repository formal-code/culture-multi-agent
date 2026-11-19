"""Task-specific context and traceable sources; snippets never become read bodies."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..tools.http_client import HttpClient
from ..tools.page_reader import PageReader
from ..tools.web_search import WebSearch


def normalize_topic(value):
    return "".join(str(value or "").split()).casefold()


def task_context(context):
    context = context or {}
    selected = context.get("selected", {}) or {}
    topic = context.get("topic") or selected.get("topic") or ""
    brief = context.get("brief") or selected.get("brief") or {}
    prompt = str(context.get("prompt") or "")
    return {"topic": topic, "user_request": prompt, "brief": brief,
            "platform_content": context.get("platform_content") or {},
            "selected_script": selected.get("script") or {},
            "audience_basis": "用户要求及上游brief，保留各字段source；未明确部分仅可推断",
            "current_user_request_precedence": "本次用户要求优先；已有brief冲突时须指出，不能把旧要求当成本次要求"}


def source_record(row, prefix, index):
    body = row.get("excerpt") or row.get("text") or ""
    status = row.get("retrieval_status")
    upstream = bool(row.get("is_upstream_excerpt"))
    kind = "source_body" if status == "success" and body and not upstream else (
        "upstream_excerpt" if body else "search_snippet")
    return {"id": f"{prefix}{index}", "kind": kind, "title": row.get("title", ""),
            "url": row.get("url", ""), "text": str(body or row.get("search_snippet") or row.get("snippet") or "")[:5000],
            "published_at": row.get("published_at"), "retrieved_at": row.get("retrieved_at"),
            "retrieval_status": status or "upstream_unverified", "purposes": row.get("research_purposes", []),
            "recentness": "日期需逐项核对；采集日期不代表发布日期，日期不明不能称为近期热门"}


def collect_context(context):
    task = task_context(context)
    evidence = {"TASK": {"kind": "task_context", "text": task}}
    notes = []
    directory = context.get("research_directory") if context else None
    if directory:
        try:
            directory = Path(directory)
            packet = json.loads((directory / "input_packet.json").read_text(encoding="utf-8-sig"))
            previous_topic = packet.get("topic") or (packet.get("brief") or {}).get("spot_name")
            if not normalize_topic(task["topic"]) or normalize_topic(previous_topic) != normalize_topic(task["topic"]):
                notes.append("旧研究具体主题不匹配，未复用；同属聊城不能作为主题匹配依据")
            else:
                for i, row in enumerate(json.loads((directory / "sources.json").read_text(encoding="utf-8-sig"))[:12], 1):
                    rec = source_record(row, "S", i)
                    evidence[rec["id"]] = rec
        except (OSError, ValueError, TypeError, AttributeError):
            notes.append("研究目录不可用，未宣称已读取旧来源")
    research = (context or {}).get("research") or {}
    for i, row in enumerate(research.get("reference_videos", [])[:8], 1):
        if isinstance(row, dict):
            evidence[f"U{i}"] = {"kind": "upstream_excerpt", "text": str(row)[:3000],
                                   "note": "上游参考资料，未独立确认视频内容或互动数据"}
    if not any(x["kind"] == "source_body" for x in evidence.values()):
        notes.append("当前没有可用的已读来源正文；文化事实和近期话题结论须降为待核实")
    return task, evidence, notes


def search_sources(topic, audience, platform, directory):
    """At most two bounded searches, with actual read-body provenance and persisted failures."""
    directory = Path(directory)
    topic = str(topic).strip()
    if not topic:
        raise ValueError("定向搜索需要具体主题，不能只按省市复用资料")
    queries = [f"{topic} 文化 文旅 官方", f"{topic} {audience} {platform} 观众 互动 调查 研究"]
    identity = hashlib.sha256(json.dumps(queries, ensure_ascii=False).encode()).hexdigest()[:16]
    path = directory / f"sources_{identity}.json"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
        # Current-topic supplementary searches have a one-day cache, not perpetual trend memory.
        if saved.get("created_date") == datetime.now(timezone.utc).date().isoformat():
            return saved
    settings = Settings.from_env()
    provider = settings.search_provider
    if provider == "bailian_responses":
        provider = "tavily" if settings.tavily_api_key else "bing_rss"
    settings = replace(settings, search_provider=provider, results_per_query=3)
    http = HttpClient(timeout=min(settings.http_timeout, 15), interval=.3, max_requests=18)
    search, reader = WebSearch(settings, http), PageReader(http)
    result = {"topic": topic, "created_date": datetime.now(timezone.utc).date().isoformat(),
              "queries": queries, "provider": provider, "records": [], "sources": []}
    seen = set()
    from .client import atomic_json
    for query in queries:
        try:
            hits = search.search(query)
            result["records"].append({"query": query, "status": "success", "hits": len(hits)})
        except Exception as exc:
            result["records"].append({"query": query, "status": "failed", "error_type": type(exc).__name__})
            atomic_json(path, result)
            continue
        for hit in hits:
            if hit.url in seen:
                continue
            seen.add(hit.url)
            row = {"title": hit.title, "url": hit.url, "search_snippet": hit.snippet,
                   "published_at": hit.published_at, "retrieval_status": "not_read", "excerpt": ""}
            try:
                page = reader.read(hit.url)
                row.update(url=page["url"], excerpt=page["text"], published_at=page["published_at"],
                           retrieval_status="success", retrieved_at=datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                row["retrieval_status"] = "failed"
                row["error_type"] = type(exc).__name__
                row["error"] = str(exc)[:100] if type(exc).__name__ == "ToolError" else "page_read_failed"
            result["sources"].append(row)
            atomic_json(path, result)
    atomic_json(path, result)
    return result
