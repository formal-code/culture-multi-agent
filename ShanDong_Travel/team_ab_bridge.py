"""A/B接入层。放在streamlit_app.py旁边；不改变A/B/C源码或模型配置。"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import urlsplit

from team_interfaces import NoSuitableMaterials

ROOT = Path(__file__).resolve().parent
EVENT_PREFIX = "TEAM_EVENT:"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def read_json(path):
    result = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("交接JSON顶层必须为对象")
    return result


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def safe_message(value):
    message = str(value)
    for name, secret in os.environ.items():
        if secret and any(s in name.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(r"(?i)Bearer\s+[^\s\"',}]+", "Bearer [REDACTED]", message)
    return message[:1200]


def _http_url(value):
    try:
        parsed = urlsplit(value or "")
        _ = parsed.port
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname
                    and not parsed.username and not parsed.password)
    except ValueError:
        return False


def _local_asset(base_dir, value):
    """所有相对素材都必须属于本次A交付目录，拒绝旧电脑的绝对路径。"""
    relative = Path(str(value).replace("\\", "/"))
    if relative.is_absolute() or relative.drive or ":" in str(relative):
        raise ValueError("本次素材路径须为相对路径")
    root = Path(base_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("素材越出本次目录、文件缺失或为空：" + relative.name)
    return path


def normalize_research(raw, delivery_dir):
    """保留A原文，新增D要求的media_assets和reference_videos，不伪造参考视频。"""
    result = deepcopy(raw)
    assets = raw.get("assets") if "assets" in raw else raw.get("media_assets")
    if not isinstance(assets, list):
        raise ValueError("A素材清单必须为数组")
    if not assets:
        raise NoSuitableMaterials("A本次没有交付可用素材")
    selected, excluded, ids, names = [], [], set(), set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("A素材条目必须为对象")
        row = deepcopy(asset)
        path = _local_asset(delivery_dir, row.get("file_path", ""))
        media_type = row.get("media_type")
        supported = path.suffix.lower() in (IMAGE_SUFFIXES if media_type == "image"
                                            else VIDEO_SUFFIXES if media_type == "video" else set())
        if not supported:
            excluded.append({"id": row.get("id"), "file_path": row.get("file_path"),
                             "reason": "现有视频模块不支持该媒体类型或扩展名"})
            continue
        if not row.get("id") or row["id"] in ids or path.name.casefold() in names:
            raise ValueError("素材ID或文件名重复，不能可靠关联脚本")
        ids.add(row["id"])
        names.add(path.name.casefold())
        row["file_path"] = path.relative_to(Path(delivery_dir).resolve()).as_posix()
        if not isinstance(row.get("keywords", []), list):
            raise ValueError("素材keywords须为数组")
        selected.append(row)
    if not selected:
        raise NoSuitableMaterials("本次素材均不受现有视频模块支持")
    refs = result.setdefault("reference_videos", [])
    if not isinstance(refs, list):
        raise ValueError("reference_videos须为数组")
    result["schema_version"] = "1.0"
    result["media_assets"] = selected
    result["integration_excluded_assets"] = excluded
    return result


def run_worker(stage, payload, *, timeout, on_progress=None):
    """子进程隔离A的顶层config/culture导入，避免污染Streamlit及C的配置。"""
    child_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "team_ab_worker.py"), stage],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=child_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    messages = Queue(maxsize=128)
    def reader():
        try:
            for line in process.stdout:
                messages.put(line.rstrip("\r\n"))
        finally:
            messages.put(None)
    reader_thread = Thread(target=reader, daemon=True)
    reader_thread.start()
    started, last_emit, closed, error, result = time.monotonic(), 0., False, None, None
    error_code = None
    detail = "正在搜集并审核本次素材" if stage == "a" else "正在策划并自查候选脚本"
    try:
        process.stdin.write(json.dumps(payload, ensure_ascii=False))
        process.stdin.close()
        while not closed or process.poll() is None:
            if time.monotonic() - started > timeout:
                raise TimeoutError("A采集等待超时" if stage == "a" else "B策划等待超时")
            try:
                line = messages.get(timeout=.25)
            except Empty:
                line = ""
            if line is None:
                closed = True
            elif line.startswith(EVENT_PREFIX):
                event = json.loads(line[len(EVENT_PREFIX):])
                if event.get("kind") == "progress":
                    detail = str(event["message"])
                elif event.get("kind") == "result":
                    result = event["result"]
                elif event.get("kind") == "error":
                    error = safe_message(event["message"])
                    error_code = event.get("code")
            elif line:
                print(f"[{stage.upper()}] {safe_message(line)}", flush=True)
            now = time.monotonic()
            if on_progress and now-last_emit >= 1:
                on_progress(f"{detail}，已等待 {int(now-started)} 秒……")
                last_emit = now
        code = process.wait(timeout=5)
        if code or error or result is None:
            if stage == "a" and error_code == NoSuitableMaterials.code:
                raise NoSuitableMaterials(error or "A本轮未找到合格素材")
            raise RuntimeError(error or f"{stage.upper()}未返回本次任务结果，退出码 {code}")
        return result
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        # 继续消费，避免子进程退出后读取线程堵在有界队列上。
        while reader_thread.is_alive():
            try:
                messages.get(timeout=.1)
            except Empty:
                pass
        reader_thread.join()
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def collect_with_a(prompt, work_dir, on_progress=None):
    if not prompt.strip() or len(prompt) > 1600:
        raise ValueError("创作要求请控制在1至1600个字符")
    if not os.getenv("DATA_OPENAI_API_KEY", "").strip():
        raise ValueError("请在启动Streamlit的环境中配置A使用的DATA_OPENAI_API_KEY")
    if not os.getenv("LLM_API_KEY", "").strip():
        raise ValueError("请先配置B使用的LLM_API_KEY，避免采集完成后才发现无法策划")
    delivery = Path(work_dir).resolve() / "a_delivery"
    result = run_worker("a", {"prompt": prompt, "delivery": str(delivery)},
                        timeout=100*60, on_progress=on_progress)
    report = read_json(delivery / "collection_report.json")
    if report.get("approved_counts", {}).get("total", 0) < 1:
        raise NoSuitableMaterials("本轮未搜集到符合当前要求的可用素材")
    raw = read_json(delivery / "research.json")
    research = normalize_research(raw, delivery)
    research["sources"] = read_json(delivery / "sources.json").get("sources", [])
    research["facts"] = read_json(delivery / "fact.json").get("facts", [])
    research["_team_handoff"] = {
        "delivery_dir": str(delivery),
        "planning_dir": str(delivery.parent / "b_planning"),
        "prompt": prompt,
        "plan": report.get("plan", {}),
        "collection_status": report.get("status"),
    }
    write_json(delivery / "research_for_d.json", research)
    return research, delivery


def _source_records(research, facts):
    """C使用自己的严格schema；观察描述不冒充文章原文或已核实事实。"""
    wanted_urls = {f.get("source") for f in facts}
    indexed = {}
    originals = research.get("sources") or []
    originals = sorted(originals, key=lambda row: row.get("url") not in wanted_urls)
    for raw in originals:
        if not isinstance(raw, dict) or not _http_url(raw.get("url")):
            continue
        sid = raw.get("id") or raw.get("source_id")
        if not sid or sid in indexed:
            continue
        excerpt = "\n".join(filter(None, [
            "文章标题：" + str(raw.get("title") or ""),
            str(raw.get("article_text") or ""),
            str(raw.get("evidence") or ""),
        ]))
        indexed[sid] = {
            "source_id": sid, "title": str(raw.get("title") or "来源文章")[:1000],
            "url": raw["url"], "publisher": raw.get("publisher"),
            "excerpt": excerpt[:40000], "published_at": raw.get("published_at") or None,
            "retrieved_at": raw.get("accessed_at") or None,
            "origin": "upstream", "retrieval_status": "provided",
            "publisher_verified": False, "is_upstream_excerpt": True,
        }
        if len(indexed) == 50:
            break
    facts_out = []
    for row in facts:
        basis = row.get("basis") or "unknown"
        article_basis = basis in {"source_text", "article_title", "article_caption"}
        refs = [sid for sid, source in indexed.items()
                if article_basis and source["url"] == row.get("source")]
        facts_out.append({
            "fact_id": row["id"], "content": row["text"], "source_ids": refs,
            "verification_status": ("source_text_unverified" if article_basis
                                    else f"{basis}_not_independently_verified"),
        })
    return list(indexed.values()), facts_out


def assemble_candidates(raw, facts, research, prompt, base_dir):
    """只转换交接格式，过滤无法执行的候选；不改写B的任何分镜文字。"""
    if raw.get("self_check", {}).get("mode") == "mock":
        raise ValueError("B返回了演示脚本，正式链路不接受mock")
    allowed = {row["file_path"]: row for row in research["media_assets"]}
    fact_map = {f["id"]: f for f in facts["facts"]}
    asset_map = {row["file"]: row for row in facts["assets"]}
    candidates, rejected, seen_ids = [], [], set()
    for candidate in raw.get("candidates", []):
        try:
            cid = candidate["candidate_id"]
            if not isinstance(cid, str) or not cid or cid in seen_ids:
                raise ValueError("候选编号无效或重复")
            script = candidate["script"]
            segments = script["segments"]
            total = script["total_duration"]
            if type(total) is not int or total <= 0 or not isinstance(segments, list) or not segments:
                raise ValueError("分镜或总时长无效")
            if any(not isinstance(script.get(key), str) or not script[key].strip()
                   for key in ("title",)):
                raise ValueError("脚本标题无效")
            seen_no, summed = set(), 0
            for segment in segments:
                no, duration = segment["no"], segment["duration"]
                if type(no) is not int or no in seen_no or type(duration) is not int or duration <= 0:
                    raise ValueError("分镜编号或整数秒时长无效")
                seen_no.add(no)
                summed += duration
                for key in ("vo", "subtitle", "visual"):
                    if not isinstance(segment.get(key), str):
                        raise ValueError("分镜缺少口播、字幕或画面文本")
                asset = segment["asset"]
                if asset not in allowed:
                    raise ValueError("分镜引用了本轮素材清单之外的文件")
                _local_asset(base_dir, asset)
                if segment.get("asset_type") != allowed[asset]["media_type"]:
                    raise ValueError("分镜素材类型与实际清单不同")
                refs = segment.get("fact_ids", [])
                if not isinstance(refs, list) or any(x not in fact_map for x in refs):
                    raise ValueError("分镜引用了未定义事实编号")
                if set(refs)-set(asset_map.get(Path(asset).name, {}).get("facts", [])):
                    raise ValueError("分镜事实编号未关联到对应素材")
            if summed != total:
                raise ValueError("分镜时长之和与总时长不同")
            candidates.append(deepcopy(candidate))
            seen_ids.add(cid)
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({"candidate_id": candidate.get("candidate_id") if isinstance(candidate, dict) else None,
                             "reason": str(exc)})
    if not candidates:
        raise ValueError("B本轮未产生可执行候选，请查看b_planning中的原始结果及B自查信息")
    plan = research.get("_team_handoff", {}).get("plan", {})
    subjects = plan.get("targets") or plan.get("themes") or []
    topic = str(research.get("city") or "聊城")
    if subjects:
        topic += "·" + "、".join(str(x) for x in subjects)
    topic = topic[:200]
    sources, source_facts = _source_records(research, facts["facts"])
    result = {
        "topic": topic,
        # 让现有c_bridge把本次prompt填入communication_goal，避免重复拼接超长。
        "brief": {
            "spot_name": topic, "city": research.get("city") or "聊城", "province": "山东",
            "target_audience": None, "communication_goal": "",
            "target_platforms": ["douyin", "kuaishou", "xiaohongshu"],
            "target_duration_sec": candidates[0]["script"]["total_duration"],
        },
        "candidates": candidates, "facts": source_facts, "sources": sources,
        "planner_brief": deepcopy(raw.get("brief", {})),
        "self_check": deepcopy(raw.get("self_check", {})),
        "integration_rejected_candidates": rejected,
        "research_requirements": (
            "依据本次用户要求比较候选；上游source_text仍待核实。"
            "visual是图片观察，video_unverified仅依据来源文字，不证明视频画面。"
            "B的self_check及needs_review记录仅供检查，不是C的评分或最终结论。"
        ),
    }
    # 在联网测评前用C自己的schema做离线结构校验。
    from evaluation_agent.input_adapter import parse_planner
    parse_planner(result)
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 3_800_000:
        raise ValueError("候选与来源超过C可接收的大小，请减少上游资料")
    return result


def plan_with_b(prompt, research, on_progress=None):
    handoff = research.get("_team_handoff", {})
    if handoff.get("prompt") != prompt:
        raise ValueError("缺少本次A任务交接信息，或本次需求与采集需求不一致")
    if not os.getenv("LLM_API_KEY", "").strip():
        raise ValueError("请配置B的LLM_API_KEY；不会回退到mock脚本")
    delivery, planning = Path(handoff["delivery_dir"]), Path(handoff["planning_dir"])
    result = run_worker("b", {"prompt": prompt, "delivery": str(delivery),
                              "planning": str(planning)},
                        timeout=20*60, on_progress=on_progress)
    raw, facts = read_json(result["raw_path"]), read_json(result["facts_path"])
    candidates = assemble_candidates(raw, facts, research, prompt, delivery)
    write_json(planning / "candidate_scripts.json", candidates)
    return candidates
