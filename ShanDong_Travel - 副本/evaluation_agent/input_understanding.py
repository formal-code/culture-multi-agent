"""Keep script wording and resource links visible to the researcher."""
from itertools import zip_longest
from .research_models import Understanding, DIMENSIONS
from .tools.page_reader import normalize_text
from .tools.http_client import ToolError
from .topic_identity import audience_context, primary_topic


def locations(packet):
    values = {}
    for script in packet.scripts:
        for key in ("title", "core_idea", "hook", "call_to_action"):
            values[(script.script_id, key)] = getattr(script, key)
        for shot in script.shots:
            for key in ("visual", "voiceover", "subtitle"):
                values[(script.script_id, f"{shot.shot_id}.{key}")] = getattr(shot, key)
    return values


def valid_script_reference(reference, packet):
    text = locations(packet).get((reference.script_id, reference.location))
    return text is not None and normalize_text(reference.quote) in normalize_text(text)


def build_context(packet):
    material_ids = {m.material_id for m in packet.materials}
    fact_ids = {f.fact_id for f in packet.facts}
    source_ids = {s.source_id for s in packet.sources}
    issues, scripts = [], []
    for fact in packet.facts:
        for source in fact.source_ids:
            if source not in source_ids:
                issues.append({"fact_id": fact.fact_id, "source_id": source, "reason": "unknown_source"})
    for script in packet.scripts:
        elapsed, opening, shots = 0., [], []
        for shot in script.shots:
            if elapsed < 5:
                opening.append({"shot_id": shot.shot_id, "start_sec": elapsed,
                                "overlap_end_sec": min(5, elapsed + shot.duration_sec),
                                "full_shot_text": shot.model_dump()})
            elapsed += shot.duration_sec
            for key, known in (("material_ids", material_ids), ("fact_ids", fact_ids)):
                for identifier in getattr(shot, key):
                    if identifier not in known:
                        issues.append({"script_id": script.script_id, "shot_id": shot.shot_id,
                                       "reference": identifier, "reason": "unknown_" + key})
            shots.append(shot.model_dump())
        scripts.append({"script_id": script.script_id, "version": script.version,
                        "title": script.title, "core_idea": script.core_idea,
                        "hook": script.hook, "declared_duration_sec": script.declared_duration_sec,
                        "call_to_action": script.call_to_action, "shots": shots,
                        "opening_shots_overlapping_5_seconds": opening,
                        "planned_duration_sec": elapsed,
                        "duration_difference_sec": elapsed - packet.brief.target_duration_sec if packet.brief.target_duration_sec else None})
    return {"topic": primary_topic(packet), "audience_context": audience_context(packet),
            "brief": packet.brief.model_dump(), "research_requirements": packet.research_requirements,
            "scripts": scripts, "input_issues": issues,
            "timing_limit": "没有逐字时间轴，不能认定完整镜头口播都在前5秒内。"}


def understand(packet, context, llm, store):
    try:
        token_limit = min(getattr(getattr(llm, "settings", None), "llm_max_tokens", 2500), 2500)
        response = llm.ask("understand_planner",
            "理解策划JSON的脚本与素材需求，不评分、不选择脚本。提炼最多6个值得外部核查的问题，summary不超过120字。"
            "围绕六维和context.topic主主题，优先核查人物身份、原话、工艺、资质、时间和场所实际说法。"
            "光岳楼等陪衬地名不能替代主主题；没有事实表的编号不代表已核实。"
            "任何script_reference必须逐字来自指定脚本位置；素材编号必须来自输入。"
            "位置必须使用实际shot_id加字段，例如shot_id为1时用1.voiceover，不能自行写SH001。"
            "每个问题只给一条最短必要逐字引用，不能用省略号改写引用；可不附material_ids。"
            "summary只说明创作要求与内容，不能声称观看了图片视频或预判事实。",
            {"context": context, "materials": [m.model_dump() for m in packet.materials],
             "facts": [f.model_dump() for f in packet.facts][:80]}, Understanding,
            max_tokens=token_limit, enable_thinking=False)
    except ToolError as exc:
        store.log("understanding", "partial", reason=str(exc))
        return {"status": "incomplete", "summary": "已按程序读取脚本和资料引用；模型理解失败，使用基本六维检索任务。",
                "questions": [], "rejected": [], "error": str(exc)}
    valid, rejected = [], []
    known_materials = {m.material_id for m in packet.materials}
    for row in response.questions:
        if any(not valid_script_reference(ref, packet) for ref in row.script_references) or any(
                m not in known_materials for m in row.material_ids):
            rejected.append({"question": row.question, "reason": "nonliteral_or_unknown_input_reference"})
        else:
            valid.append(row.model_dump())
    return {"status": "completed" if not rejected else "partial", "summary": response.summary,
            "questions": valid, "rejected": rejected}


def make_plan(packet, understanding, settings):
    spot = primary_topic(packet)
    if packet.brief.city and packet.brief.city not in spot:
        spot = packet.brief.city + " " + spot
    platforms = {"douyin": "抖音", "xiaohongshu": "小红书", "bilibili": "哔哩哔哩"}
    platform = " ".join(platforms.get(p, p) for p in packet.brief.target_platforms)
    basics = [
        ("factual_reliability", f"{spot} 官方 介绍 历史 来源"),
        ("target_audience", f"{spot} {platform} 文化 旅游 体验 观众需求"),
        ("local_distinctiveness", f"{spot} 地方特色 文化内涵 传承"),
        ("opening_hook", f"{spot} {platform} 短视频 开头 讲解 观众反馈"),
        ("material_feasibility", f"{spot} 展示 体验 场所 拍摄 规定 公告"),
        ("topic_relevance", f"{spot} 近期 新闻 活动 讨论 {settings.research_date or ''}"),
    ]
    # Reserve one query for each dimension, then interleave specific questions.
    result = [{"query": q.strip(), "purpose": d, "scope": "subject",
               "research_group": d, "question": "基本研究任务", "script_references": []} for d, q in basics]
    groups = [[q for q in understanding["questions"] if q["dimension"] == d] for d in DIMENSIONS]
    questions = [q for batch in zip_longest(*groups) for q in batch if q]
    # Literal claims keep research useful even if the understanding call fails.
    claims = []
    for script in packet.scripts:
        first = script.shots[0]
        quote = (first.voiceover or first.subtitle or first.visual)[:180]
        claims.append({"dimension": "factual_reliability",
                       "query": (script.hook + " " + quote)[:220],
                       "question": "核查候选开场与首段具体主张，不视为事实",
                       "script_references": [{"script_id": script.script_id,
                           "location": first.shot_id + (".voiceover" if first.voiceover else
                                        ".subtitle" if first.subtitle else ".visual"),
                           "quote": quote}]})
    for batch in zip_longest(questions, claims):
        for question in batch:
            if question:
                result.append({"query": (spot + " " + question["query"])[:450],
                               "purpose": question["dimension"], "scope": "subject",
                               "research_group": question["dimension"], "question": question["question"],
                               "script_references": question["script_references"]})
    seen, unique = set(), []
    for row in result:
        key = (row["purpose"], row["query"])
        if key not in seen:
            unique.append(row)
            seen.add(key)
    return unique[:settings.max_queries]
