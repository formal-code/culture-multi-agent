"""Build attraction-specific research evidence without scoring candidate scripts."""
from datetime import date, datetime, timedelta, timezone
import re
import unicodedata
from urllib.parse import urlsplit
from .topic_identity import topic_matches


CHINA_TZ = timezone(timedelta(hours=8))
METRIC_RE = re.compile(
    r"(?:播放量|浏览量|阅读量|点赞(?:数)?|评论(?:数)?|收藏(?:数)?|分享(?:数)?)"
    r"[\s:：约达超过为已有累计近+]*([0-9][0-9,.]*(?:万|亿|千)?[+＋]?)",
    re.I,
)


def research_window(settings):
    as_of = date.fromisoformat(settings.research_date) if settings.research_date else datetime.now(CHINA_TZ).date()
    return {"as_of": as_of.isoformat(),
            "start_date": (as_of - timedelta(days=settings.research_window_days)).isoformat(),
            "end_date": as_of.isoformat(), "window_days": settings.research_window_days}


def _norm(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def subject_match(text, brief):
    haystack = _norm(text)
    names = [brief.spot_name, *brief.subject_aliases]
    return any(len(_norm(name)) >= 2 and _norm(name) in haystack for name in names)


def date_status(value, window):
    if not value:
        return "unknown_date"
    match = re.search(r"(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})(?!\d)", value)
    if not match:
        us_date = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)", value)
        if not us_date:
            return "unknown_date"
    try:
        if match:
            published = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        else:
            published = date(int(us_date.group(3)), int(us_date.group(1)), int(us_date.group(2)))
    except ValueError:
        return "unknown_date"
    start, end = date.fromisoformat(window["start_date"]), date.fromisoformat(window["end_date"])
    return "future_date" if published > end else "within_window" if published >= start else "older_than_window"


def _metric_mentions(source, brief):
    body = source.excerpt or ""
    found = []
    for match in METRIC_RE.finditer(body):
        quote = body[max(0, match.start() - 120):match.end() + 80].strip()
        if subject_match(quote, brief):
            found.append({"source_id": source.source_id, "url": source.url,
                          "published_at": source.published_at, "quote": quote,
                          "metric_text": match.group(0), "attraction_metric_verified": False,
                          "metric_scope": "single_page_or_report_only",
                          "note": "页面中出现的原文数值；不代表景点总体热度或未来播放量。"})
    return found[:20]


def collect_subject_evidence(packet, sources, search_results, settings):
    window = research_window(settings)
    source_rows, metrics = [], []
    for source in sources:
        matched = topic_matches(source.excerpt, packet)
        candidate_match = topic_matches(" ".join((source.title, source.search_snippet)), packet)
        readable = source.retrieval_status in {"success", "provided", "demo"} and bool(source.excerpt)
        purposes = list(source.research_purposes or [])
        status = date_status(source.published_at, window)
        host = urlsplit(source.url or "").hostname or ""
        signals = []
        if host.endswith(".gov.cn"):
            signals.append("government_domain")
        if source.retrieval_status == "success" and source.excerpt:
            signals.append("page_body_read")
        if source.origin == "upstream":
            signals.append("upstream_material_not_independently_verified")
        row = {"source_id": source.source_id, "title": source.title, "url": source.url,
               "publisher": source.publisher, "retrieval_status": source.retrieval_status,
               "original_url": source.original_url, "retrieval_error": source.retrieval_error,
               "published_at": source.published_at, "date_status": status,
               "subject_match": matched, "candidate_title_or_snippet_match": candidate_match,
               "readable": readable, "research_purposes": purposes,
               "publication_date_basis": source.publication_date_basis,
               "quality_signals": signals, "excerpt_file": source.raw_text_file,
               "excerpt": source.excerpt[:5000] if source.excerpt else "",
               "selection_reasons": (["exact_spot_name_or_alias_in_body"] if matched else [])}
        source_rows.append(row)
        if matched and source.retrieval_status == "success":
            metrics.extend(_metric_mentions(source, packet.brief))

    eligible = [s for s in source_rows if s["subject_match"] and s["readable"]]
    recent = [s["source_id"] for s in eligible if s["date_status"] == "within_window"
              and s["retrieval_status"] == "success"]
    undated = [s["source_id"] for s in eligible if s["date_status"] == "unknown_date"]
    older = [s["source_id"] for s in eligible if s["date_status"] == "older_than_window"]
    return {
        "subject": {"name": packet.brief.spot_name, "city": packet.brief.city,
                    "aliases": packet.brief.subject_aliases},
        "research_window": window,
        "source_evidence": source_rows,
        "recent_subject_source_ids": recent,
        "undated_subject_source_ids": undated,
        "older_subject_source_ids": older,
        "attention_evidence": {"status": "page_metric_mentions_need_scope_check" if metrics else "no_verifiable_subject_specific_metrics",
                               "mentions": metrics,
                               "candidate_page_count": len(recent) + len(undated) + len(older),
                               "candidate_page_count_meaning": "检索到的相关页面数量，不是热度指标。"},
        "search_statuses": [{"purpose": row.get("purpose"), "status": row.get("status"),
                             "hit_count": len(row.get("hits", []))} for row in search_results],
        "dimensions_to_research": [
            {"dimension": "factual_reliability", "evidence_inputs": ["script_claims", "source_bodies", "citations"]},
            {"dimension": "target_audience", "evidence_inputs": ["user_brief", "subject_specific_audience_sources"]},
            {"dimension": "local_distinctiveness", "evidence_inputs": ["local_culture_sources", "script_claims"]},
            {"dimension": "opening_hook", "evidence_inputs": ["first_shot_text", "platform_opening_examples"]},
            {"dimension": "material_feasibility", "evidence_inputs": ["shot_material_references", "file_checks"]},
            {"dimension": "subject_attention_and_topics", "evidence_inputs": ["recent_subject_sources", "literal_page_metrics"]},
        ],
        "disclaimer": "仅整理可追溯材料，不评分、不排名、不预测热度。搜索结果不足或无日期时明确保留未知。",
    }


def script_context(packet):
    """Save first-five-second overlapping shots, not invented word timestamps."""
    records = []
    for script in packet.scripts:
        elapsed, opening = 0.0, []
        for shot in script.shots:
            if elapsed >= 5:
                break
            opening.append({"shot_id": shot.shot_id, "start_sec": elapsed,
                            "overlap_end_sec": min(5, elapsed + shot.duration_sec),
                            "full_shot": shot.model_dump()})
            elapsed += shot.duration_sec
        records.append({"script_id": script.script_id, "version": script.version,
                        "target_audience_from_user": packet.brief.target_audience,
                        "opening_shots_overlapping_first_5_sec": opening,
                        "timing_note": "保留相交镜头的完整口播；没有逐字时间轴，不能认定整段口播均在5秒内。"})
    return records

