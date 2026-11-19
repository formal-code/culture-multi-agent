"""First-stage planner research pipeline. It never scores or selects scripts."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .input_adapter import read_planner
from .input_understanding import build_context, make_plan, understand
from .topic_identity import primary_topic, same_topic, topic_matches
from .material_checks import inspect_materials
from .reporting import material_check_evidence, material_dimension, render_markdown, validate_dimension
from .research_models import DIMENSIONS, DimensionBatch, FullAnalysis
from .storage import RunStore
from .subject_research import collect_subject_evidence, date_status, research_window
from .tools.http_client import HttpClient, ToolError
from .tools.page_reader import PageReader
from .tools.web_search import SearchHit, WebSearch


class _UnavailableLLM:
    def ask(self, *args, **kwargs):
        raise ToolError("missing_DATAOPENAI_API_KEY")


def _source_id(url: str) -> str:
    return "WEB-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_host(url: str | None) -> str | None:
    return urlsplit(url).hostname if url else None


def _evidence_kind(source) -> str:
    if source.retrieval_status == "success" and source.excerpt:
        return "independently_retrieved_page_body"
    if source.excerpt:
        return "upstream_excerpt_not_independently_verified"
    if source.search_snippet:
        return "search_snippet_not_original_text"
    return "no_readable_body"


def _store_source_body(store: RunStore, source):
    if source.excerpt and not source.raw_text_file:
        return source.model_copy(update={"raw_text_file": store.raw_text(source.source_id, source.excerpt)})
    return source


def _read_input_sources(packet, reader: PageReader, store: RunStore, page_budget: int):
    sources, attempted, attempts = [], set(), 0
    for original in packet.sources:
        source = original.model_copy(deep=True)
        if source.url and attempts < page_budget:
            attempts += 1
            attempted.add(source.url)
            try:
                page = reader.read(source.url)
                source = source.model_copy(update={
                    "title": page["title"] or source.title,
                    "url": page["url"], "original_url": source.url,
                    "excerpt": page["text"], "published_at": page["published_at"] or source.published_at,
                    "publication_date_basis": page["publication_date_basis"] or source.publication_date_basis,
                    "retrieved_at": _timestamp(), "retrieval_status": "success",
                    "retrieval_error": None, "publisher": source.publisher or _safe_host(page["url"]),
                    "publisher_verified": False, "is_upstream_excerpt": False,
                })
                store.log("page_reader", "success", source_id=source.source_id, host=_safe_host(page["url"]))
            except ToolError as exc:
                source = source.model_copy(update={
                    "retrieval_status": "upstream_excerpt" if source.excerpt else "failed",
                    "retrieval_error": str(exc), "is_upstream_excerpt": bool(source.excerpt),
                    "original_url": source.url,
                })
                store.log("page_reader", "failed", source_id=source.source_id, reason=str(exc))
        elif source.url:
            source = source.model_copy(update={"retrieval_status": "not_attempted_page_budget"})
        elif source.excerpt:
            source = source.model_copy(update={"retrieval_status": "upstream_excerpt",
                                               "is_upstream_excerpt": True})
        else:
            source = source.model_copy(update={"retrieval_status": "missing_source_text"})
        sources.append(_store_source_body(store, source))
    return sources, attempted, attempts


def _choose_fallback(settings: Settings) -> str | None:
    primary = settings.search_provider
    configured = settings.search_fallback_provider
    if configured == "none":
        return None
    if configured != "auto":
        return None if configured == primary else configured
    if primary == "bailian_responses":
        return "tavily" if settings.tavily_api_key else "bing_rss"
    if primary == "tavily":
        return "bing_rss"
    if primary == "bing_rss":
        return "tavily" if settings.tavily_api_key else "bailian_responses"
    return None


def _search_provider(plan: list[dict], provider: str, settings: Settings, store: RunStore,
                     window: dict, brief: dict, http: HttpClient) -> tuple[list[dict], list[dict]]:
    records, hits = [], []
    if provider not in {"bailian_responses", "tavily", "bing_rss"}:
        return ([{"provider": provider, "status": "failed", "error": "unknown_search_provider"}], [])
    if provider == "bailian_responses":
        try:
            from .tools.bailian_search import BailianSearch
            service = BailianSearch(settings, store)
            service.prepare(plan, brief, window)
            for item in plan:
                try:
                    found = service.search(item["query"])
                except ToolError as exc:
                    found = []
                    status, error = "failed", str(exc)
                else:
                    status, error = ("success" if found else "no_sources"), None
                rows = [_hit_row(hit, item, provider) for hit in found]
                hits.extend(rows)
                records.append({"provider": provider, "query": item["query"], "purpose": item["purpose"],
                                "status": status, "error": error, "hits": rows})
        except Exception as exc:
            store.log("bailian_web_search", "failed", error_type=type(exc).__name__)
            return ([{"provider": provider, "status": "failed", "error": "provider_request_failed"}], [])
        return records, hits

    service = WebSearch(replace(settings, search_provider=provider), http)
    for item in plan:
        try:
            found = service.search(item["query"])
            rows = [_hit_row(hit, item, provider) for hit in found]
            hits.extend(rows)
            records.append({"provider": provider, "query": item["query"], "purpose": item["purpose"],
                            "status": "success" if rows else "no_sources", "hits": rows})
        except ToolError as exc:
            records.append({"provider": provider, "query": item["query"], "purpose": item["purpose"],
                            "status": "failed", "error": str(exc), "hits": []})
            store.log(provider + "_search", "failed", purpose=item["purpose"], reason=str(exc))
    return records, hits


def _hit_row(hit: SearchHit, plan_item: dict, provider: str) -> dict:
    return {"provider": provider, "query": plan_item["query"], "purpose": plan_item["purpose"],
            "research_group": plan_item.get("research_group", plan_item["purpose"]),
            "title": hit.title, "url": hit.url, "snippet": hit.snippet,
            "published_at": hit.published_at}


def _unique_hits(rows: list[dict]) -> list[dict]:
    merged = {}
    for row in rows:
        url = row.get("url", "")
        try:
            parts = urlsplit(url)
            valid = (len(url) <= 3000 and parts.scheme in {"http", "https"} and parts.hostname
                     and not parts.username and not parts.password)
            _ = parts.port
        except (TypeError, ValueError):
            valid = False
        if not valid:
            continue
        row = {**row, "title": str(row.get("title", ""))[:1000],
               "snippet": str(row.get("snippet", ""))[:6000]}
        if url not in merged:
            merged[url] = {**row, "purposes": [row["purpose"]], "queries": [row["query"]],
                           "providers": [row["provider"]]}
            continue
        current = merged[url]
        for key, value in (("purposes", row["purpose"]), ("queries", row["query"]),
                           ("providers", row["provider"])):
            if value not in current[key]:
                current[key].append(value)
        if not current.get("snippet") and row.get("snippet"):
            current["snippet"] = row["snippet"]
    return list(merged.values())


def _read_search_hits(rows: list[dict], reader: PageReader, store: RunStore, budget: int,
                      attempted: set[str]) -> tuple[list, dict[str, str], int]:
    sources, statuses, attempts = [], {}, 0
    for hit in _unique_hits(rows):
        if hit["url"] in attempted or attempts >= budget:
            statuses[hit["url"]] = "not_attempted_page_budget" if attempts >= budget else "already_attempted"
            continue
        attempts += 1
        attempted.add(hit["url"])
        source_id = _source_id(hit["url"])
        try:
            page = reader.read(hit["url"])
            source = {
                "source_id": source_id, "title": page["title"] or hit["title"] or _safe_host(hit["url"]),
                "url": page["url"], "original_url": hit["url"], "publisher": _safe_host(page["url"]),
                "excerpt": page["text"], "search_snippet": hit["snippet"] or "",
                "published_at": page["published_at"], "retrieved_at": _timestamp(), "origin": "web_search",
                "retrieval_status": "success", "publisher_verified": False, "retrieved_via": ",".join(hit["providers"]),
                "research_purposes": hit["purposes"], "subject_match": None,
                "publication_date_basis": page["publication_date_basis"], "quality_signals": [],
                "selection_reasons": [], "is_upstream_excerpt": False, "retrieval_error": None,
            }
            from .schemas import Source
            source = Source.model_validate(source)
            source = source.model_copy(update={"raw_text_file": store.raw_text(source_id, page["text"])})
            store.log("page_reader", "success", source_id=source_id, host=_safe_host(page["url"]))
            statuses[hit["url"]] = "success"
        except ToolError as exc:
            from .schemas import Source
            source = Source.model_validate({
                "source_id": source_id, "title": hit["title"] or _safe_host(hit["url"]),
                "url": hit["url"], "publisher": _safe_host(hit["url"]), "excerpt": "",
                "search_snippet": hit["snippet"] or "", "origin": "web_search",
                "retrieval_status": str(exc), "retrieved_via": ",".join(hit["providers"]),
                "research_purposes": hit["purposes"], "original_url": hit["url"],
                "retrieval_error": str(exc),
            })
            store.log("page_reader", "failed", source_id=source_id, reason=str(exc))
            statuses[hit["url"]] = str(exc)
        sources.append(source)
    return sources, statuses, attempts


def _update_search_records(records: list[dict], statuses: dict[str, str]) -> list[dict]:
    updated = []
    for record in records:
        row = dict(record)
        row["hits"] = [{**hit, "page_read_status": statuses.get(hit["url"], "not_attempted")}
                       for hit in record.get("hits", [])]
        updated.append(row)
    return updated


def _summary_payload(packet, understanding, context, source_evidence, material_checks, sources, settings) -> dict:
    evidence = []
    for source in sources:
        if not source.excerpt:
            continue
        evidence.append({"evidence_id": source.source_id, "title": source.title, "url": source.url,
                         "publisher": source.publisher, "published_at": source.published_at,
                         "publication_date_basis": source.publication_date_basis,
                         "origin": source.origin, "evidence_kind": _evidence_kind(source),
                         "retrieval_status": source.retrieval_status,
                         "research_purposes": source.research_purposes,
                         "date_status": date_status(source.published_at, research_window(settings)),
                         "text": source.excerpt[:2600]})
    materials = [{"evidence_id": row.evidence_id, "kind": row.kind, "title": row.title,
                  "url": row.url, "platform": row.platform, "published_at": row.published_at,
                  "sampling_method": row.sampling_method, "text": row.text[:1200]}
                 for row in packet.research_materials[:12]]
    return {"task_id": packet.task_id, "topic": primary_topic(packet), "brief": packet.brief.model_dump(),
            "topic_policy": "以当前主题核查主张，景点陪衬不可替代主题；未定义事实编号待核实，系统受众是假设。",
            "research_requirements": packet.research_requirements,
            "script_context": context, "script_understanding": understanding,
            "planner_facts": [fact.model_dump() for fact in packet.facts[:80]],
            "materials_check": material_checks,
            "sources_with_evidence_text": evidence[:20], "additional_research_materials": materials,
            "research_window": research_window(settings)}


def _reuse_sources(directory: Path, packet, store):
    """Reuse local page snapshots; do not imply a fresh retrieval or updated policy."""
    from .schemas import Source
    directory = directory.expanduser().resolve()
    previous = json.loads((directory / "input_packet.json").read_text(encoding="utf-8-sig"))
    if not same_topic(previous, packet):
        raise ValueError("复用资料主题或地点与当前输入不一致，必须重新搜索")
    sources = []
    for row in json.loads((directory / "sources.json").read_text(encoding="utf-8-sig")):
        source = Source.model_validate(row)
        source = source.model_copy(update={"raw_text_file": None})
        sources.append(_store_source_body(store, source))
    store.log("source_cache", "reused", source_count=len(sources))
    return sources


def run_research(input_path: Path, output_dir: Path, settings: Settings | None = None,
                 *, reuse_sources: Path | None = None) -> tuple[Path, dict]:
    input_path = input_path.expanduser().resolve()
    settings = settings or Settings.from_env()
    packet, resource_links = read_planner(input_path)
    store = RunStore(output_dir.expanduser().resolve(), packet.task_id, demo=False)
    context = build_context(packet)
    store.save("input_packet.json", packet.model_dump())
    store.save("input_resource_links.json", resource_links)

    try:
        from .llm_client import BailianClient
        llm = BailianClient(settings, store)
    except ToolError as exc:
        llm = _UnavailableLLM()
        store.log("llm", "unavailable", reason=str(exc))

    understanding = understand(packet, context, llm, store)
    plan = make_plan(packet, understanding, settings)
    material_checks = inspect_materials(packet, input_path)
    store.save("script_understanding.json", understanding)
    store.save("research_plan.json", plan)
    store.save("material_checks.json", material_checks)

    http = HttpClient(settings.http_timeout, settings.request_interval,
                      max_requests=max(100, settings.max_pages * 6 + settings.max_queries * 3))
    reader = PageReader(http)
    window = research_window(settings)
    if reuse_sources is not None:
        sources = _reuse_sources(reuse_sources, packet, store)
        primary_records, fallback_records, all_search_records = [], [], []
        fallback_provider = None
        store.save("search_records.json", {"reused_from": str(reuse_sources.resolve()),
                   "note": "Reused saved page bodies; no fresh search or retrieval in this run."})
    else:
        input_sources, attempted_urls, input_page_attempts = _read_input_sources(
            packet, reader, store, settings.max_pages)
        page_budget = max(0, settings.max_pages - input_page_attempts)
        primary_records, primary_hits = _search_provider(plan, settings.search_provider, settings, store, window,
                                                         packet.brief.model_dump(), http)
        read_sources, page_statuses, used_pages = _read_search_hits(
            primary_hits, reader, store, page_budget, attempted_urls)
        sources = input_sources + read_sources

        available_dimensions = {purpose for source in sources if source.retrieval_status == "success" and source.excerpt
                                for purpose in source.research_purposes}
        missing_dimensions = set(DIMENSIONS) - available_dimensions
        fallback_provider = _choose_fallback(settings)
        fallback_records, fallback_hits = [], []
        fallback_sources = []
        if missing_dimensions and fallback_provider:
            fallback_plan = [item for item in plan if item["purpose"] in missing_dimensions]
            fallback_records, fallback_hits = _search_provider(
                fallback_plan, fallback_provider, settings, store, window, packet.brief.model_dump(), http)
            remaining_budget = max(0, settings.max_pages - input_page_attempts - used_pages)
            fallback_sources, fallback_statuses, _ = _read_search_hits(
                fallback_hits, reader, store, remaining_budget, attempted_urls)
            sources.extend(fallback_sources)
            page_statuses.update(fallback_statuses)
            available_dimensions = {purpose for source in sources if source.retrieval_status == "success" and source.excerpt
                                    for purpose in source.research_purposes}
            missing_dimensions = set(DIMENSIONS) - available_dimensions

        all_search_records = _update_search_records(primary_records + fallback_records, page_statuses)
        store.save("search_records.json", {"primary_provider": settings.search_provider,
                                           "fallback_provider": fallback_provider if fallback_records else None,
                                           "primary": _update_search_records(primary_records, page_statuses),
                                           "fallback": _update_search_records(fallback_records, page_statuses),
                                           "note": "搜索摘要仅用于发现来源，不作为原文引用。"})
    sources = [_store_source_body(store, source) for source in sources]
    store.save("sources.json", [source.model_dump() for source in sources])

    source_evidence = collect_subject_evidence(packet, sources, all_search_records, settings)
    store.save("subject_research.json", source_evidence)
    material_quote = material_check_evidence(material_checks)
    evidence_text = {source.source_id: {"text": source.excerpt, "kind": _evidence_kind(source),
                                        "title": source.title, "url": source.url,
                                        "published_at": source.published_at,
                                        "publication_date_basis": source.publication_date_basis,
                                        "date_status": date_status(source.published_at, window)}
                    for source in sources if source.excerpt}
    evidence_text["MATCHECK"] = {"text": material_quote, "kind": "structured_material_check"}

    script_analyses = {script.script_id: {} for script in packet.scripts}
    rejected, failed_dimensions = [], []
    model_dimensions = list(DIMENSIONS)
    dimension_labels = {name: label for name, label in DIMENSIONS.items()}
    payload = _summary_payload(packet, understanding, context, source_evidence, material_checks,
                              sources, settings)
    pending_by_dimension = {dimension: [] for dimension in model_dimensions}
    retry_feedback = {}
    base_instruction = (
        "按脚本逐一分析事实可靠性、目标受众、地方特色、开场吸引力、素材可执行性和热门话题六个维度。"
        "事实可靠性对每条独立事实主张分别输出发现及状态，不要把多个主张合并；claim_status只能是‘有支持’/‘存在差异’/‘暂无法确认’。"
        "受众适配、开场吸引力可以结合简报与脚本合理推断，必须逐字引用脚本原句，说明属于创作分析，不承诺受众行为或播放效果。"
        "地方特色判断应结合来源正文和脚本原句。热门话题topic_basis标为recent_source或evergreen_inference："
        "recent_source必须引用研究窗口内且日期可确认的正文；evergreen_inference必须是合理推断，不能声称近期热度。"
        "有近期报道只证明存在近期动态，不证明已经热门或是热点；请使用‘近期动态’或‘选题线索’。"
        "受众和开场用可能、适配性等措辞，不声称有效吸引或已经验证留存；地方元素不等于其他景点绝无此元素。"
        "地方特色若只是评价脚本呈现方式，可以只引用脚本并标inference；涉及新增历史或地理事实必须引用网页原文。"
        "每条发现说明结论、依据与推理、判断性质、适用范围和局限；建议必须对应发现。"
        "素材可执行性根据materials_check和镜头需求作inference，引用MATCHECK的逐字摘要，"
        "分析缺失素材、替代拍摄条件和镜头依赖，不声称观看了媒体或核实了许可。"
        "每条发现必须提供当前script_id的script_references，素材维度也应引用对应镜头visual。"
        "按输入已有条件判断，不擅自增加专业测绘、高空设备等必备条件；引用公告应标注转载来源，不冒称官方直接核实。"
        "网页引用仅可来自sources_with_evidence_text；素材检查另可引用material_check_evidence，"
        "都必须是对应text逐字连续片段；搜索摘要、标题、模型知识不能冒充引用。"
        "证据不足时可结合脚本与已提供资料作有限推断，标明不确定性并提出待核实问题。不得虚构，不评分、不排名、不比较或筛选脚本。"
    )
    payload["material_check_evidence"] = {"evidence_id": "MATCHECK", "text": material_quote}
    for script in packet.scripts:
        if isinstance(llm, _UnavailableLLM):
            for dimension in model_dimensions:
                pending_by_dimension[dimension].append(script.script_id)
            continue
        try:
            script_payload = {**payload, "target_script_ids": [script.script_id],
                              "script_context": {**context, "scripts": [
                                  row for row in context["scripts"] if row["script_id"] == script.script_id]}}
            response = llm.ask("six_dimension_research_summary", base_instruction +
                               "只分析target_script_ids所列这一个脚本。除事实逐条核查外，各维度1条发现和1条建议，"
                               "每个说明字段控制在60字左右，脚本引用和来源引用各取最短必要片段。",
                               script_payload, FullAnalysis,
                               max_tokens=settings.llm_max_tokens, enable_thinking=False)
            store.save(f"analysis_{packet.scripts.index(script) + 1}.json", response.model_dump())
            returned = {row.script_id: row.dimensions for row in response.analyses
                        if row.script_id == script.script_id}
            analysis = returned.get(script.script_id)
            for dimension in model_dimensions:
                candidate = getattr(analysis, dimension) if analysis else None
                if candidate is None:
                    pending_by_dimension[dimension].append(script.script_id)
                    retry_feedback[(dimension, script.script_id)] = [{"reason": "script_analysis_missing"}]
                    continue
                if not candidate.findings and not candidate.open_questions:
                    pending_by_dimension[dimension].append(script.script_id)
                    retry_feedback[(dimension, script.script_id)] = [{"reason": "empty_dimension_result"}]
                    continue
                section, issues = validate_dimension(candidate, dimension, script.script_id,
                                                     packet, evidence_text)
                if issues:
                    rejected.extend(issues)
                    pending_by_dimension[dimension].append(script.script_id)
                    retry_feedback[(dimension, script.script_id)] = issues
                    if section["findings"]:
                        script_analyses[script.script_id][dimension] = section
                else:
                    script_analyses[script.script_id][dimension] = section
        except Exception as exc:
            failure = str(exc) if isinstance(exc, ToolError) else type(exc).__name__
            store.log("six_dimension_research_summary", "failed", reason=failure)
            for dimension in model_dimensions:
                pending_by_dimension[dimension].append(script.script_id)
                retry_feedback[(dimension, script.script_id)] = [{"reason": failure}]

    for dimension in model_dimensions:
        pending_ids = pending_by_dimension[dimension]
        if pending_ids and not isinstance(llm, _UnavailableLLM):
            feedback = [issue for script_id in pending_ids
                        for issue in retry_feedback.get((dimension, script_id), [])]
            retry_instruction = (
                f"只修复维度 {dimension}（{dimension_labels[dimension]}）中 target_script_ids 所列脚本的分析。"
                + base_instruction + "上次具体校验反馈：" +
                json.dumps(feedback, ensure_ascii=False)[:3500]
            )
            try:
                response = llm.ask(f"script_dimension_{dimension}", retry_instruction,
                                   {**payload, "target_dimension": dimension,
                                    "target_script_ids": pending_ids}, DimensionBatch,
                                   max_tokens=min(settings.llm_max_tokens, 6000), enable_thinking=False)
                returned = {row.script_id: row.summary for row in response.analyses
                            if row.script_id in pending_ids}
                for script_id in pending_ids:
                    candidate = returned.get(script_id)
                    if candidate is None:
                        rejected.append({"dimension": dimension, "script_id": script_id,
                                         "issues": [{"reason": "retry_analysis_missing"}]})
                        failed_dimensions.append({"script_id": script_id, "dimension": dimension})
                        continue
                    section, issues = validate_dimension(candidate, dimension, script_id,
                                                         packet, evidence_text)
                    rejected.extend(issues)
                    if not issues:
                        script_analyses[script_id][dimension] = section
                    else:
                        failed_dimensions.append({"script_id": script_id, "dimension": dimension})
                        if section["findings"] and not script_analyses[script_id].get(dimension):
                            script_analyses[script_id][dimension] = section
            except Exception as exc:
                failure = str(exc) if isinstance(exc, ToolError) else type(exc).__name__
                store.log("script_dimension_" + dimension, "retry_failed", error=failure)
                failed_dimensions.extend({"script_id": script_id, "dimension": dimension}
                                         for script_id in pending_ids)

        for script_id in pending_ids:
            if not script_analyses[script_id].get(dimension):
                script_analyses[script_id][dimension] = {
                    "label": dimension_labels[dimension], "findings": [], "recommendations": [],
                    "open_questions": ["模型无法生成或校验此维度结论；保留其他维度结果。"]}
                item = {"script_id": script_id, "dimension": dimension}
                if item not in failed_dimensions:
                    failed_dimensions.append(item)
    for script in packet.scripts:
        baseline = material_dimension(material_checks)
        model_material = script_analyses[script.script_id].get("material_feasibility")
        if model_material:
            baseline["findings"].extend(model_material["findings"])
            baseline["recommendations"].extend({**row, "based_on_findings": [
                number + 1 for number in row["based_on_findings"]]}
                for row in model_material["recommendations"])
            baseline["open_questions"].extend(model_material["open_questions"])
        script_analyses[script.script_id]["material_feasibility"] = baseline
    dimensions = script_analyses
    partially_validated_dimensions = [item for item in failed_dimensions
        if dimensions[item["script_id"]][item["dimension"]]["findings"] and (
            item["dimension"] != "material_feasibility" or
            len(dimensions[item["script_id"]][item["dimension"]]["findings"]) > 1)]
    failed_dimensions = [item for item in failed_dimensions if item not in partially_validated_dimensions]
    llm_summary_ok = not failed_dimensions and not isinstance(llm, _UnavailableLLM)
    material_quote = material_check_evidence(material_checks)
    material_check_citation_valid = all(
        analysis["material_feasibility"]["findings"][0]["citations"][0]["evidence_id"] == "MATCHECK"
        and analysis["material_feasibility"]["findings"][0]["citations"][0]["quote"] == material_quote
        for analysis in dimensions.values())
    store.save("citation_validation.json", {
        "rejected_items": rejected,
        "validated_finding_count": sum(len(section["findings"]) for analysis in dimensions.values()
                                        for section in analysis.values()),
        "programmatic_material_check_quote_valid": material_check_citation_valid,
        "model_dimension_overrides": [],
        "programmatic_dimension_supplements": ["material_feasibility"],
        "rule": "网页结论引用必须逐字存在于对应正文或明确标记的上游摘录；素材可执行性由程序检查素材清单和路径，不用无关网页替代。",
    })

    readable_count = sum(source.retrieval_status == "success" and bool(source.excerpt) for source in sources)
    upstream_excerpt_count = sum(_evidence_kind(source) == "upstream_excerpt_not_independently_verified"
                                 for source in sources)
    source_by_id = {source.source_id: source for source in sources}
    independently_cited_dimensions = {dimension for dimension in DIMENSIONS
        if any(_evidence_kind(source_by_id[citation["evidence_id"]]) == "independently_retrieved_page_body"
               for analysis in dimensions.values() for finding in analysis[dimension]["findings"]
               for citation in finding["citations"] if citation["evidence_id"] in source_by_id)}
    dimensions_without_independent_citation = [dimension for dimension in DIMENSIONS
                                               if dimension not in independently_cited_dimensions]
    dimensions_without_findings = [f"{script_id}/{dimension}" for script_id, analysis in dimensions.items()
                                   for dimension, section in analysis.items() if not section["findings"]]
    status = ("completed" if llm_summary_ok and readable_count > 0 and not dimensions_without_findings
              else "partial")
    limitations = [
        "搜索结果标题和摘要只用于发现候选来源，不证明正文内容。",
        "页面读取受站点抓取规则、动态渲染、网络可达性和来源发布时间标注影响。",
        "素材检查只检查输入声明、路径存在性、扩展名、授权备注和镜头关联；没有查看图像或播放音视频。",
        "未实现评分、权重、排名或脚本筛选。",
    ]
    if reuse_sources:
        limitations.append("本轮复用既有网页快照，保留原抓取时间；未重新搜索或读取，开放安排等动态信息需另行更新。")
    no_web_citation = [dimension for dimension in dimensions_without_independent_citation
                       if dimension in {"factual_reliability", "local_distinctiveness", "topic_relevance"}]
    if no_web_citation:
        limitations.append("以下维度没有被本报告中的独立网页正文引用支持：" +
                           "、".join(DIMENSIONS[d] for d in no_web_citation) + "。")
    if "material_feasibility" in dimensions_without_independent_citation:
        limitations.append("素材可执行性结论依据结构化文件和状态检查，不引用网页正文。")
    if upstream_excerpt_count:
        limitations.append(f"有 {upstream_excerpt_count} 条上游摘录未由本流程独立读取原网页。")
    if rejected:
        limitations.append(f"有 {len(rejected)} 条模型生成内容因引用或脚本原文校验未通过而被剔除。")
    if failed_dimensions:
        limitations.append(f"{len(failed_dimensions)} 个脚本维度在重试后仍无通过校验的模型结论；其他脚本维度已保留。")
    if partially_validated_dimensions:
        limitations.append(f"{len(partially_validated_dimensions)} 个脚本维度仅部分内容通过校验；已保留有效结论，剔除无效引文及其建议。")
    if dimensions_without_findings:
        limitations.append("以下脚本维度目前只有待核实问题，没有可用结论，因此报告状态为部分完成：" +
                           "、".join(dimensions_without_findings) + "。")

    source_rows = []
    source_index = {}
    for source in sources:
        kind = _evidence_kind(source)
        row = {"source_id": source.source_id, "title": source.title, "url": source.url,
               "original_url": source.original_url, "publisher": source.publisher,
               "origin": source.origin, "evidence_kind": kind,
               "retrieved_via": source.retrieved_via, "retrieval_status": source.retrieval_status,
               "retrieval_error": source.retrieval_error, "published_at": source.published_at,
               "publication_date_basis": source.publication_date_basis,
               "retrieved_at": source.retrieved_at, "search_snippet": source.search_snippet,
               "research_purposes": source.research_purposes, "raw_text_file": source.raw_text_file}
        source_rows.append(row)
        source_index[source.source_id] = row
    source_index["MATCHECK"] = {"source_id": "MATCHECK", "title": "程序化素材检查",
                                "url": "material_checks.json", "evidence_kind": "structured_material_check"}

    report = {
        "schema_version": "1.1", "task_id": packet.task_id, "run_id": store.root.name,
        "reused_sources_from": str(reuse_sources.resolve()) if reuse_sources else None,
        "created_at": _timestamp(), "status": status,
        "validation_status": "passed_with_rejections" if rejected else "passed",
        "subject": {"spot_name": packet.brief.spot_name, "city": packet.brief.city,
                    "province": packet.brief.province, "aliases": packet.brief.subject_aliases},
        "input_summary": {"script_count": len(packet.scripts),
                           "shot_count": sum(len(script.shots) for script in packet.scripts),
                           "fact_count": len(packet.facts), "material_count": len(packet.materials)},
        "understanding": understanding, "script_context": context, "research_plan": plan,
        "material_checks": material_checks,
        "search": {"primary_provider": settings.search_provider,
                   "fallback_provider": fallback_provider if fallback_records else None,
                   "records": all_search_records},
        "sources": source_rows,
        "source_index": source_index,
        "source_summary": {"readable_body_count": readable_count,
                           "fresh_readable_body_count": 0 if reuse_sources else readable_count,
                           "reused_body_count": readable_count if reuse_sources else 0,
                           "upstream_excerpt_count": upstream_excerpt_count,
                           "candidate_count": len(source_rows),
                           "dimensions_with_independent_citations": sorted(independently_cited_dimensions),
                           "dimensions_without_independent_citations": dimensions_without_independent_citation,
                           "attention_evidence": source_evidence["attention_evidence"],
                           "research_window": source_evidence["research_window"]},
        "structured_evidence": {"MATCHECK": material_quote},
        "subject_research": source_evidence, "dimensions": dimensions,
        "citation_validation": {"rejected_items": rejected,
                                 "validated_finding_count": sum(len(section["findings"]) for analysis in dimensions.values()
                                                                 for section in analysis.values()),
                                 "programmatic_material_check_quote_valid": material_check_citation_valid,
                                 "model_dimension_overrides": [],
                                 "programmatic_dimension_supplements": ["material_feasibility"],
                                 "dimensions_without_findings": dimensions_without_findings,
                                 "partially_validated_dimensions": partially_validated_dimensions,
                                 "failed_dimensions": failed_dimensions},
        "limitations": limitations,
    }
    store.save("report.json", report)
    store.save("research_memory.json", {
        "schema_version": "1.0", "status": status, "created_at": report["created_at"],
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "brief": packet.brief.model_dump(), "topic": primary_topic(packet), "model": settings.model,
        "script_analyses": dimensions, "source_index": source_index,
        "limitations": limitations,
        "usage": "后续筛选可复用分析与出处；不是热度预测结果，动态信息需更新。",
    })
    (store.root / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return store.root, report
