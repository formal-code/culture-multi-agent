"""A/B独立进程入口。由team_ab_bridge调用，不要在Streamlit页面直接导入运行。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from team_ab_bridge import EVENT_PREFIX, ROOT, read_json, safe_message, write_json
from team_interfaces import NoSuitableMaterials


def emit(kind, **kwargs):
    print(EVENT_PREFIX+json.dumps({"kind": kind, **kwargs}, ensure_ascii=False), flush=True)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_a(payload):
    a_root = ROOT / "culture2"
    if not (a_root / "culture/collector.py").is_file():
        raise ValueError("请将culture2目录放在主项目根目录中")
    # 独立进程里优先加载A自己的config.py，不与app_config或C的配置混用。
    sys.path.insert(0, str(a_root))
    import config as a_config
    from culture.collector import collect
    delivery = Path(payload["delivery"]).resolve()
    delivery.mkdir(parents=True, exist_ok=False)
    a_config.OUTPUT_DIR = delivery
    def progress(message):
        message = safe_message(message)
        print(message, flush=True)
        match = re.search(r"通过\s*(\d+)\s*份", message)
        if match:
            emit("progress", message=f"正在搜集并审核素材，当前通过 {match[1]} 份")
    # 保留A原有模型失败语义；模型停止时不偷偷继续付费调用其他Agent。
    report = collect(payload["prompt"], progress=progress)
    if report.get("approved_counts", {}).get("total", 0) < 1:
        raise NoSuitableMaterials("A本轮未找到合格素材，没有使用包内历史资料")
    for name in ("research.json", "fact.json", "sources.json", "collection_report.json"):
        if not (delivery / name).is_file():
            raise ValueError("A本次交付缺少：" + name)
    return {"delivery": str(delivery), "status": report.get("status")}


def run_b(payload):
    if not os.getenv("LLM_API_KEY", "").strip():
        raise ValueError("缺少LLM_API_KEY，禁止使用mock代替本次策划")
    planner_root = ROOT / "liaoyun/planner"
    if not (planner_root / "planner.py").is_file():
        raise ValueError("请将liaoyun目录放在主项目根目录中")
    interface = load_module("team_facts_from_a", planner_root / "facts_from_a.py")
    planner = load_module("team_planner", planner_root / "planner.py")
    delivery = Path(payload["delivery"]).resolve()
    planning = Path(payload["planning"]).resolve()
    planning.mkdir(parents=True, exist_ok=False)
    data_dir = planning / "data"
    data_dir.mkdir()
    # 只给B本次视频模块可消费的清单，保留A原始research.json。
    normalized = read_json(delivery / "research_for_d.json")
    adapter_input = deepcopy(normalized)
    adapter_input["assets"] = deepcopy(normalized["media_assets"])
    write_json(data_dir / "research.json", adapter_input)
    write_json(data_dir / "fact.json", read_json(delivery / "fact.json"))
    facts, _ = interface.build(data_dir)
    usable_names = {Path(row["file_path"]).name for row in normalized["media_assets"]}
    facts["assets"] = [row for row in facts["assets"] if row["file"] in usable_names]
    if not facts["facts"] or not facts["assets"]:
        raise ValueError("本次素材没有足够的可用描述供B策划")
    facts_path = data_dir / "facts.json"
    write_json(facts_path, facts)
    write_json(data_dir / "asset_manifest.json", {"assets": sorted(usable_names)})
    # B的所有可写路径指向独立任务目录，不覆盖liaoyun/planner/data中的样例。
    planner.DATA = str(data_dir)
    planner.OUT = str(planning / "output")
    planner.MANIFEST = str(data_dir / "asset_manifest.json")
    planner.ASSETS_DIR = str(delivery / "assets")
    # 原包brief含固定“年轻家庭/民俗”演示要求，这里必须改成本次输入。
    # 沿用B现有60秒默认；B本身仍为4至6段、每段8至15秒的生成方案。
    brief = {
        "audience": {"value": "", "source": "未提供，按本次需求判断"},
        "goal": {"value": payload["prompt"], "source": "user"},
        "platforms": {"value": ["douyin", "kuaishou", "xiaohongshu"], "source": "项目默认"},
        "target_duration": {"value": 60, "source": "系统默认"},
    }
    emit("progress", message="已接收A本次素材，正在生成候选创作角度")
    try:
        angles = planner.pick_angles(facts, payload["prompt"], planner.ACTIVE)
    except Exception as exc:
        # 不在鉴权失败后继续套用降级角度产生更多调用；保留错误供维护者处理。
        raise RuntimeError("B生成角度失败：" + safe_message(exc)) from exc
    emit("progress", message=f"正在并行生成并自查 {len(angles)} 个候选脚本")
    asset_map = planner.asset_index(facts)
    with ThreadPoolExecutor(max_workers=len(angles)) as executor:
        outputs = list(executor.map(
            lambda angle: planner.gen_one(angle, facts, payload["prompt"], brief, asset_map),
            angles,
        ))
    raw = {"topic": facts.get("topic") or "聊城", "brief": brief,
           "candidates": [], "self_check": {}}
    for index, (angle, (script, issues)) in enumerate(zip(angles, outputs)):
        if not isinstance(script, dict):
            raise ValueError("B未返回脚本对象")
        if issues:
            script["needs_review"] = True
        identifier = f"cand_{index+1}"
        raw["candidates"].append({"candidate_id": identifier, "angle": angle[0], "script": script})
        raw["self_check"][identifier] = issues or "通过"
    raw_path = planning / "planner_raw.json"
    write_json(raw_path, raw)
    return {"raw_path": str(raw_path), "facts_path": str(facts_path)}


def main():
    try:
        payload = json.loads(sys.stdin.read())
        stage = sys.argv[1]
        result = run_a(payload) if stage == "a" else run_b(payload) if stage == "b" else None
        if result is None:
            raise ValueError("无效Agent阶段")
        emit("result", result=result)
        return 0
    except NoSuitableMaterials as exc:
        emit("error", code=NoSuitableMaterials.code, message=safe_message(str(exc)))
        return 1
    except Exception as exc:
        emit("error", message=safe_message(f"{type(exc).__name__}: {exc}"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
