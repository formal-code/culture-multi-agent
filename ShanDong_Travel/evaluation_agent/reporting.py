"""Evidence-bounded summaries and readable report rendering."""
from .input_understanding import valid_script_reference
from .tools.page_reader import normalize_text
from .research_models import (DIMENSIONS, DimensionSummary, EvidenceQuote, Finding,
                              Recommendation, SixDimensionSummary)


def validate_dimension(summary: DimensionSummary, dimension: str, script_id: str, packet,
                       evidence: dict[str, dict]) -> tuple[dict, list[dict]]:
    """Validate one script/dimension so a bad result cannot erase other analyses."""
    rejected, findings, old_to_new = [], [], {}
    for old_index, finding in enumerate(summary.findings, 1):
        bad = []
        for citation in finding.citations:
            source = evidence.get(citation.evidence_id)
            if not source or not source.get("text"):
                bad.append({"reason": "evidence_text_unavailable", "evidence_id": citation.evidence_id})
            elif normalize_text(citation.quote) not in normalize_text(source["text"]):
                bad.append({"reason": "quote_not_found_in_evidence", "evidence_id": citation.evidence_id})
            elif dimension == "topic_relevance" and finding.topic_basis == "recent_source" and (
                    not source.get("published_at") or not source.get("publication_date_basis")
                    or source.get("date_status") != "within_window"):
                bad.append({"reason": "recent_source_date_unconfirmed", "evidence_id": citation.evidence_id})
        refs = finding.script_references
        invalid_references = [ref.model_dump() for ref in refs
                              if ref.script_id != script_id or not valid_script_reference(ref, packet)]
        if invalid_references:
            bad.append({"reason": "script_reference_not_literal", "references": invalid_references})
        if not refs:
            bad.append({"reason": "script_reference_required"})
        if dimension == "factual_reliability" and not finding.claim_status:
            bad.append({"reason": "fact_claim_status_required"})
        if (dimension == "factual_reliability" or
                dimension == "local_distinctiveness" and finding.basis_type == "source_statement") and not finding.citations and not (
                dimension == "factual_reliability" and finding.claim_status == "暂无法确认"):
            bad.append({"reason": "source_citation_required"})
        if dimension in {"target_audience", "opening_hook"} and (
                finding.basis_type != "inference" or not refs):
            bad.append({"reason": "script_based_inference_reference_required"})
        if dimension == "topic_relevance":
            popularity_words = ("热点", "热门", "爆火", "热搜", "高热度", "走红")
            if any(word in finding.statement for word in popularity_words) and not finding.statement.startswith(
                    ("无法确认", "尚不能确认", "暂无证据", "未证实")):
                bad.append({"reason": "reported_event_is_not_proof_of_popularity",
                            "hint": "只能表述为近期动态或选题线索；本流程未核实总体热度数据。"})
            if finding.topic_basis == "recent_source" and (finding.basis_type != "source_statement" or not finding.citations):
                bad.append({"reason": "recent_topic_requires_dated_source"})
            if finding.topic_basis == "evergreen_inference" and (
                    finding.basis_type != "inference" or finding.citations):
                bad.append({"reason": "evergreen_topic_must_be_labeled_inference_without_recent_citation"})
            if finding.topic_basis is None:
                bad.append({"reason": "topic_basis_required"})
        if bad:
            rejected.append({"dimension": dimension, "script_id": script_id,
                             "finding_index": old_index, "issues": bad})
            continue
        old_to_new[old_index] = len(findings) + 1
        findings.append(finding.model_dump())

    recommendations = []
    for index, recommendation in enumerate(summary.recommendations, 1):
        mapped = [old_to_new[number] for number in recommendation.based_on_findings if number in old_to_new]
        if not mapped:
            rejected.append({"dimension": dimension, "script_id": script_id,
                             "recommendation_index": index,
                             "issues": [{"reason": "no_validated_finding_reference"}]})
            continue
        row = recommendation.model_dump()
        row["based_on_findings"] = mapped
        recommendations.append(row)
    questions = list(summary.open_questions)
    if not findings and not questions:
        questions.append("目前没有通过校验的结论；需要补充可读取来源或进一步核实。")
    return ({"label": DIMENSIONS[dimension], "findings": findings,
             "recommendations": recommendations, "open_questions": questions}, rejected)


def empty_summary(packet, material_check: dict, reason: str) -> dict:
    """A clearly incomplete fallback when the language model cannot produce a report."""
    result = {}
    for dimension, label in DIMENSIONS.items():
        questions = [reason]
        if dimension == "material_feasibility":
            questions.extend(item["material_id"] + "：" + "；".join(item["warnings"])
                             for item in material_check["items"] if item["warnings"])
        result[dimension] = {"label": label, "findings": [], "recommendations": [],
                             "open_questions": questions[:6]}
    return result


def material_check_evidence(material_check: dict) -> str:
    summary = material_check["summary"]
    return (f"已声明素材{summary['declared']}项；本地文件可用{summary['available_local_files']}项；"
            f"未提供或缺失{summary['unavailable_or_missing']}项；远程未下载{summary['remote_not_downloaded']}项；"
            f"存在待处理警示{summary['with_warnings']}项。")


def material_dimension(material_check: dict) -> dict:
    evidence = material_check_evidence(material_check)
    summary = material_check["summary"]
    if summary["declared"]:
        statement = evidence
        action = ("补齐未提供的本地素材，或明确提供可访问的素材链接；逐项确认使用状态和授权后再安排剪辑。"
                  if summary["unavailable_or_missing"] or summary["remote_not_downloaded"]
                  else "逐项确认使用状态、来源和授权范围，再安排剪辑。")
        questions = [row["material_id"] + "：" + "；".join(row["warnings"])
                     for row in material_check["items"] if row["warnings"]][:6]
        if not questions:
            questions = ["素材文件已定位；仍需由制作方确认版权范围和现场拍摄许可是否适用。"]
    else:
        statement = "输入包没有声明任何待使用素材。"
        action = "根据镜头画面需求补充素材清单，并标明来源、文件位置、使用状态和授权说明。"
        questions = ["各镜头所需的画面或音频素材尚未登记。"]
    finding = Finding(
        statement=statement, basis_type="programmatic_check", reasoning_summary=evidence,
        applicability="仅说明输入包中登记的素材文件状态、镜头关联和授权备注完整度。",
        limitations="程序未打开图片或播放音视频；路径存在和授权备注不等于版权、肖像权或现场拍摄许可已获批准。",
        citations=[EvidenceQuote(evidence_id="MATCHECK", quote=evidence, relation="supports")],
    )
    recommendation = Recommendation(
        action=action, rationale="该建议直接依据输入包的素材清单和路径检查结果。",
        based_on_findings=[1], prerequisites="由素材提供方或制作负责人确认文件、来源、授权范围及拍摄许可。",
        limitations="不能据此判断素材画面质量、内容适配度或法律授权有效性。",
    )
    return {"label": DIMENSIONS["material_feasibility"], "findings": [finding.model_dump()],
            "recommendations": [recommendation.model_dump()], "open_questions": questions}


def render_markdown(report: dict) -> str:
    lines = [f"# 景点策划研究报告：{report['subject']['city']} {report['subject']['spot_name']}", "",
             f"运行状态：{report['status']}。本报告只做依据研究与处理建议，不评分、不排序、不筛选。", "",
             "## 输入与流程", "",
             f"- 任务编号：{report['task_id']}",
             f"- 脚本数：{report['input_summary']['script_count']}；镜头数：{report['input_summary']['shot_count']}",
             f"- 搜索来源：{len(report['sources'])}；成功读取正文：{report['source_summary']['readable_body_count']}",
             f"- 素材需求：{report['material_checks']['summary']['declared']}；有待处理项：{report['material_checks']['summary']['with_warnings']}", "",
             "## 六维研究", ""]
    script_titles = {row["script_id"]: row["title"] for row in report["script_context"]["scripts"]}
    basis_labels = {"source_statement": "来源记载", "inference": "合理推断",
                    "programmatic_check": "程序检查"}
    for script_id, analysis in report["dimensions"].items():
        lines.extend([f"### 脚本：{script_titles.get(script_id, script_id)}（{script_id}）", ""])
        for dimension in DIMENSIONS:
            section = analysis[dimension]
            lines.extend([f"#### {section['label']}", ""])
            for finding in section["findings"]:
                nature = basis_labels.get(finding["basis_type"], finding["basis_type"])
                if finding.get("claim_status"):
                    nature += "；事实核查：" + finding["claim_status"]
                if finding.get("topic_basis"):
                    nature += "；话题依据：" + ("近期来源" if finding["topic_basis"] == "recent_source"
                                               else "常青选题推断")
                lines.append(f"- **结论（{nature}）：** {finding['statement']}")
                lines.append(f"  依据与推理：{finding['reasoning_summary']}；适用范围：{finding['applicability']}；局限：{finding['limitations']}")
                for ref in finding["script_references"]:
                    lines.append(f"  脚本依据：{ref['script_id']} / {ref['location']}：{ref['quote']}")
                for citation in finding["citations"]:
                    source = report["source_index"].get(citation["evidence_id"], {})
                    lines.append(f"  来源依据：[{citation['evidence_id']}] {citation['quote']}（{source.get('title', '来源')}，{source.get('url', '无网址')}；{source.get('evidence_kind', '来源状态待确认')}）")
            for recommendation in section["recommendations"]:
                lines.append(f"- **处理建议：** {recommendation['action']} 理由：{recommendation['rationale']} 前置条件：{recommendation['prerequisites']} 局限：{recommendation['limitations']}")
            for question in section["open_questions"]:
                lines.append(f"- **待核实：** {question}")
            if not section["findings"] and not section["open_questions"]:
                lines.append("- 暂无结论。")
            lines.append("")

    lines.extend(["## 素材检查", ""])
    for item in report["material_checks"]["items"]:
        warnings = "；".join(item["warnings"]) if item["warnings"] else "未发现声明信息缺项"
        lines.append(f"- `{item['material_id']}`（{item['type']}）：{item['file_status']}；使用状态 {item['declared_usage_status']}；{warnings}")
    if not report["material_checks"]["items"]:
        lines.append("- 输入未声明素材。")
    lines.extend(["", "## 来源与限制", ""])
    for source in report["sources"]:
        lines.append(f"- [{source['source_id']}] {source['title']}：{source.get('url') or '无网址'}；读取状态 `{source['retrieval_status']}`；来源类型 `{source['evidence_kind']}`。")
    if not report["sources"]:
        lines.append("- 未获得来源。")
    for note in report["limitations"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)
