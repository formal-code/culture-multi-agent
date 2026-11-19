"""Documented input adapter; never guesses field mappings using a model."""
import json
import hashlib
from .research_models import ResearchInput


def _candidate_input(body):
    """Deterministic mapping for topic/candidates; raw objects are never edited."""
    topic = body.get("topic")
    candidates = body.get("candidates")
    if not isinstance(topic, str) or not topic.strip() or not isinstance(candidates, list):
        raise ValueError("topic须为非空文本，candidates须为列表")
    brief = dict(body.get("brief") or {})
    brief.setdefault("spot_name", topic)
    city = body.get("city", "聊城" if topic.startswith("聊城") else "")
    brief.setdefault("city", city)
    brief.setdefault("province", body.get("province", "山东" if city == "聊城" else ""))
    for name in ("target_audience", "communication_goal", "target_platforms"):
        if name in body:
            brief.setdefault(name, body[name])
    scripts, materials = [], []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("script"), dict):
            raise ValueError("每个candidate须包含script对象")
        identifier = candidate.get("candidate_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("candidate_id须为非空文本")
        script = candidate["script"]
        segments = script.get("segments")
        if not isinstance(segments, list):
            raise ValueError("script.segments须为列表")
        shots = []
        for segment in segments:
            if not isinstance(segment, dict) or "no" not in segment:
                raise ValueError("每个segment须为含no的对象")
            shot_id = str(segment["no"])
            material_ids = []
            if segment.get("asset"):
                material_id = "ASSET_" + hashlib.sha256((identifier + "/" + shot_id).encode()).hexdigest()[:16]
                materials.append({"material_id": material_id, "type": segment.get("asset_type", "image"),
                                  "file_path": segment["asset"], "description": segment.get("visual", "")})
                material_ids.append(material_id)
            shots.append({"shot_id": shot_id, "duration_sec": segment.get("duration"),
                          "voiceover": segment.get("vo", ""), "subtitle": segment.get("subtitle", ""),
                          "visual": segment.get("visual", ""), "material_ids": material_ids,
                          "fact_ids": segment.get("fact_ids", []), "removable": segment.get("removable")})
        scripts.append({"script_id": identifier, "title": script.get("title"),
                        "core_idea": candidate.get("angle", ""), "hook": script.get("hook", ""),
                        "shots": shots, "declared_duration_sec": script.get("total_duration"),
                        "call_to_action": script.get("call_to_action", "")})
    task = body.get("task_id") or "planner-" + hashlib.sha256(topic.encode()).hexdigest()[:16]
    return {"task_id": task, "topic": topic, "brief": brief, "scripts": scripts,
            "materials": [*body.get("materials", []), *materials],
            "facts": body.get("facts", []), "sources": body.get("sources", []),
            "research_requirements": body.get("research_requirements", "")}


def parse_planner(value):
    if not isinstance(value, dict):
        raise ValueError("输入必须为JSON对象")
    if set(value) == {"planner_output"}:
        value = value["planner_output"]
    if not isinstance(value, dict):
        raise ValueError("planner_output 必须为对象")
    value = json.loads(json.dumps(value, ensure_ascii=False))
    if "candidates" in value:
        if "scripts" in value:
            raise ValueError("不能同时提供scripts和candidates，避免候选歧义")
        value = _candidate_input(value)
    links = []
    scripts = value.get("scripts", [])
    if not isinstance(scripts, list):
        raise ValueError("scripts 必须为列表")
    for script in scripts:
        if not isinstance(script, dict):
            raise ValueError("每个脚本必须为对象")
        resources = script.pop("resources", {})
        if not isinstance(resources, dict):
            raise ValueError("script.resources 必须为对象")
        for key, rows in resources.items():
            id_key = {"sources": "source_id", "facts": "fact_id", "materials": "material_id"}.get(key)
            if not id_key or not isinstance(rows, list):
                raise ValueError("resources 只接受 sources/facts/materials 列表")
            target = value.setdefault(key, [])
            if not isinstance(target, list):
                raise ValueError(f"{key} 必须为列表")
            for row in rows:
                if not isinstance(row, dict) or not row.get(id_key):
                    raise ValueError(f"{key} 缺少 {id_key}")
                existing = next((r for r in target if isinstance(r, dict) and r.get(id_key) == row[id_key]), None)
                if existing is not None and existing != row:
                    raise ValueError(f"{key} 同编号内容冲突")
                if existing is None:
                    target.append(row)
                links.append({"script_id": script.get("script_id"), "resource_type": key,
                              "resource_id": row[id_key]})
    return ResearchInput.model_validate(value), links


def read_planner(path):
    if path.stat().st_size > 4_000_000:
        raise ValueError("输入超过4MB，请减少原文或脚本")
    return parse_planner(json.loads(path.read_text(encoding="utf-8-sig")))
