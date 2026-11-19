"""Step 1: understand actual media. Step 2: evidence-based report. No popularity prediction."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .client import OmniClient, ReviewAPIError, atomic_json, media_item
from .config import ReviewSettings
from .context import collect_context, search_sources, source_record
from .media import padded_bounds, prepare_chunk, probe, render_record, sha256, windows
from .models import (AnalysisDraft, AudioObservation, DIMENSIONS, Finding,
                     VisualObservation, check_analysis, check_audio, check_visual)

VERSION = "video-review-1.1"
REPORT_VERSION = "video-analysis-1.4"
INFERENCE_MARKERS = ("旨在", "意在", "试图", "可能", "潜在", "适合", "符合", "促进", "提升",
                     "吸引注意力", "留住", "悬念", "动机", "有助于", "导致", "预期", "有望",
                     "属于", "意味着", "配合")


def identity(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def now():
    return datetime.now(timezone.utc).isoformat()


def checked_observation(path, schema, seconds=None):
    try:
        value = schema.model_validate(read_json(path))
        if seconds is not None:
            check_visual(value, seconds)
        elif schema is AudioObservation:
            check_audio(value)
        return value.model_dump() if value.accessed else None
    except (OSError, ValueError, TypeError):
        return None


def understand_video(video_path, output_dir, settings=None, *, progress=None, client=None):
    """Return (directory, dict). Source is read-only; media/results are cached by content/model."""
    settings = settings or ReviewSettings.from_env()
    video_path = Path(video_path).expanduser().resolve()
    metadata = probe(video_path)
    if metadata["duration_seconds"] > settings.max_seconds:
        raise ValueError(f"第一版支持至多 {settings.max_seconds} 秒成片，请先提供短片段")
    if not 5 <= settings.chunk_seconds <= 30:
        raise ValueError("分析分段长度须在5到30秒之间")
    config_hash = identity({"version": VERSION, "model": settings.model, "endpoint": settings.base_url,
                            "chunk_seconds": settings.chunk_seconds})[:12]
    directory = Path(output_dir).expanduser().resolve() / f"{metadata['sha256'][:16]}_{config_hash}"
    directory.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": VERSION, "step": "understand_video", "created_at": now(),
              "model": settings.model, "video": metadata, "status": "running", "chunks": [],
              "limitations": ["视频理解由模型完成，不能保证每帧覆盖；分镜时间为模型近似观察，片段边界由程序确定",
                               "音频按程序片段提供整体转写，不冒充逐字精确时间戳；静音与未成功读取音频是不同状态"]}
    atomic_json(directory / "understanding.json", result)
    fatal = False
    spans = windows(metadata["duration_seconds"], settings.chunk_seconds)
    for index, (start, end) in enumerate(spans, 1):
        part_dir = directory / "media" / f"{index:03d}"
        row = {"index": index, "start_seconds": start, "end_seconds": end,
               "time_basis": "program_clip_bounds", "errors": []}
        if progress:
            progress(f"理解第{index}/{len(spans)}段：原片{start:g}—{end:g}秒")
        try:
            media_start, media_end = padded_bounds(start, end, metadata["duration_seconds"])
            media_seconds = media_end - media_start
            row.update(media_start_seconds=round(media_start, 3), media_end_seconds=round(media_end, 3),
                       core_start_in_media=round(start - media_start, 3),
                       core_end_in_media=round(end - media_start, 3))
            visual, audio = prepare_chunk(video_path, part_dir, media_start, media_end, metadata["has_audio_stream"])
            row["visual"] = checked_observation(part_dir / "visual_observation.json", VisualObservation, media_seconds)
            if row["visual"] is None:
                client = client or OmniClient(settings, directory / "model_calls", progress)
                instruction = (f"你收到的是成片的无声分析副本，长度{media_seconds:.3f}秒；对应原片{media_start:g}—{media_end:g}秒。"
                    f"主要报告范围为原片{start:g}—{end:g}秒；两端额外画面只用于理解转场上下文。"
                    "仅观察实际画面，不猜音轨、地点、人物身份。按主要内容变化分段，start/end使用当前输入片段内的相对秒数。"
                    "画面里的标题和字幕逐字记录，看不清就不补写。开场片段内前5秒详细观察。"
                    "不能凭少量采样断言全程静止、所有转场为硬切或没有动作；无法确认写入uncertainty。"
                    "不要评价热度、打分或改写视频。")
                row["visual"] = client.ask(f"画面理解_{index:03d}", instruction, VisualObservation,
                    media=[media_item(visual, "video")], check=lambda v: check_visual(v, media_seconds))
                atomic_json(part_dir / "visual_observation.json", row["visual"])
            if audio:
                row["audio"] = checked_observation(part_dir / "audio_observation.json", AudioObservation)
                if row["audio"] is None:
                    client = client or OmniClient(settings, directory / "model_calls", progress)
                    row["audio"] = client.ask(f"音频理解_{index:03d}",
                        f"请只听本次音频。输入长度{media_seconds:.3f}秒，对应原片{media_start:g}—{media_end:g}秒；"
                        f"本段主体范围是原片{start:g}—{end:g}秒，前后短暂重叠只用于听清跨边界句子。停顿均保留。"
                        "主要转写主体范围内实际听到的语音；句子跨边界时可结合重叠上下文转写完整，并说明跨界。"
                        "相邻段落可能含有同一句边界语音，汇总时要去除重叠造成的重复，不要改写原话。"
                        "不润色不补全，同音字或听不清须说明。"
                        "sound_description简述人声、音乐、环境声、停顿与明显问题，不猜视频画面。"
                        "没有听到人声可留空transcript，但须描述实际声音。不能读取音频时accessed=false。"
                        "不编造精确时间、不参考字幕、不预测热度。",
                        AudioObservation, media=[media_item(audio, "audio")], check=check_audio)
                    atomic_json(part_dir / "audio_observation.json", row["audio"])
            else:
                row["audio"] = {"status": "no_audio_stream", "accessed": False}
        except Exception as exc:
            message = str(exc)[:400] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            message = message.replace(settings.api_key, "[REDACTED]") if settings.api_key else message
            row["errors"].append(message)
            if isinstance(exc, ReviewAPIError):
                fatal = True
                result["api_error"] = {"code": exc.code, "http_status": exc.status, "request_id": exc.request_id,
                                       "message": message}
        result["chunks"].append(row)
        atomic_json(directory / "understanding.json", result)
        if fatal:
            break
    visual_ok = sum(bool(r.get("visual", {}).get("accessed")) for r in result["chunks"] if r.get("visual"))
    audio_ok = sum(bool((r.get("audio") or {}).get("accessed")) for r in result["chunks"])
    result["coverage"] = {"expected_chunks": len(spans), "visual_chunks_read": visual_ok,
                          "audio_chunks_read": audio_ok, "audio_chunks_expected": len(spans) if metadata["has_audio_stream"] else 0,
                          "note": "覆盖表示每个片段调用成功，非逐帧识别准确率"}
    complete = visual_ok == len(spans) and (not metadata["has_audio_stream"] or audio_ok == len(spans))
    result["status"] = "complete" if complete else ("partial" if visual_ok or audio_ok else "failed")
    if sha256(video_path) != metadata["sha256"]:
        result["status"] = "failed"
        result["limitations"].append("分析期间原视频发生变化，结果不能用于新版本，必须重新运行")
    result["render_record"] = render_record(video_path, metadata)
    atomic_json(directory / "understanding.json", result)
    return directory, result


def build_evidence(understanding, context):
    task, evidence, notes = collect_context(context or {})
    evidence["MEDIA"] = {"kind": "media_metadata", "text": understanding["video"]}
    rows = understanding["chunks"]
    reconciled_audio = {}
    for current, following in zip(rows, rows[1:]):
        current_audio = current.get("audio") or {}
        following_audio = following.get("audio") or {}
        current_text = str(current_audio.get("transcript") or "").rstrip()
        following_text = str(following_audio.get("transcript") or "").lstrip()
        if current_text.endswith("第") and following_text.startswith("第三"):
            audio = dict(current_audio)
            audio["transcript"] = current_text[:-1]
            audio["uncertainty"] = [note for note in audio.get("uncertainty", [])
                                    if not ("第" in note and any(word in note for word in ("结尾", "末尾", "后")))]
            clauses = str(audio.get("sound_description") or "").replace("。", "；").split("；")
            audio["sound_description"] = "；".join(
                clause for clause in clauses
                if not ("结尾" in clause and any(word in clause for word in ("中断", "截断", "切断", "缺失")))
            )
            reconciled_audio[current.get("index")] = audio
    for row in rows:
        index, start, end = row["index"], row["start_seconds"], row["end_seconds"]
        media_start = row.get("media_start_seconds", start)
        media_end = row.get("media_end_seconds", end)
        visual = row.get("visual") or {}
        if visual.get("accessed"):
            scenes = []
            for scene in visual["scenes"]:
                scene = dict(scene)
                scene["start"] = round(media_start + scene["start"], 3)
                scene["end"] = round(min(media_end, media_start + scene["end"]), 3)
                scenes.append(scene)
            evidence[f"V{index:03d}"] = {"kind": "video_observation", "start_seconds": start,
                "end_seconds": end, "scenes": scenes, "uncertainty": visual["uncertainty"],
                "time_basis": "model_estimate_plus_program_media_start_offset",
                "core_range_seconds": [start, end]}
        audio = reconciled_audio.get(index, row.get("audio") or {})
        if audio.get("accessed"):
            evidence[f"A{index:03d}"] = {"kind": "audio_observation", "start_seconds": start,
                "end_seconds": end, "text": audio["transcript"], "sound_description": audio["sound_description"],
                "uncertainty": audio["uncertainty"], "time_basis": "program_clip_bounds_not_utterance_alignment",
                "core_range_seconds": [start, end], "media_range_seconds": [media_start, media_end],
                "note": ("已依据相邻重叠片段去除重复边界字；原始逐段识别保存在understanding.json。"
                         "时间范围是程序切片，并非逐句时间戳，不据此判断精确音画同步")}
    record = understanding.get("render_record", {})
    if record.get("data"):
        data = record["data"]
        compact = {k: data.get(k) for k in ["platform", "fps", "duration_seconds", "audio", "warnings", "quality_limits", "style_basis"]}
        compact["segments"] = [{k: row.get(k) for k in ["no", "start_frame", "end_frame", "captions"]}
                                for row in data.get("segments", [])]
        evidence["RENDER"] = {"kind": "render_record", "text": compact, "note": record["note"]}
    else:
        notes.append(record.get("note", "没有可用生成记录"))
    if not task["user_request"] and not task["brief"]:
        notes.append("没有用户要求或brief；受众与传播目标只能作系统推断")
    if any(row.get("audio", {}).get("accessed") for row in understanding["chunks"]):
        notes.append("相邻音频片段包含短暂重叠；片段边界处的残缺词语须结合相邻片段判断，不能单独据此认定原成片音轨中断")
    return task, evidence, notes


def validate_dimension(key, data, evidence):
    finding = Finding.model_validate(data)
    if finding.nature == "成片观察":
        markers = [marker for marker in INFERENCE_MARKERS if marker in finding.conclusion]
        if markers:
            raise ValueError(f"结论中的{','.join(markers)}属于推断性表述；请改标合理推断或待核实，或改写为直接观察")
    if key == "pacing":
        audio_boundaries = {float(item["start_seconds"]) for item in evidence.values()
                            if item.get("kind") == "audio_observation" and float(item.get("start_seconds", 0)) > 0}
        pause_markers = ("停顿", "静音", "中断", "截断", "骤停", "突然停止")
        for boundary in audio_boundaries:
            time_labels = (f"{boundary:g}秒", f"{boundary:g} 秒")
            if any(label in finding.conclusion for label in time_labels) and any(
                    marker in finding.conclusion for marker in pause_markers):
                raise ValueError("音频时间范围是程序切片边界，不是停顿时间戳；不得据此断言该时点有停顿或音轨中断")
    # Reuse the same grounded-reference checks for each successful dimension independently.
    from .models import Analysis
    value = Analysis(overall="维度证据校验", dimensions={name: finding for name in DIMENSIONS}, strengths=[], limitations=[])
    check_analysis(value, evidence)
    return finding.model_dump()


def markdown_report(report):
    text = ["# 成片分析报告", "", f"状态：{report['status']}；平台：{report['platform']}；模型：{report['model']}",
            "", "本阶段不打分，不预测播放量或爆款概率。片段覆盖不代表逐帧准确识别。", ""]
    text.extend(["## 总体判断", report.get("overall") or "分析尚未完成。", ""])
    evidence = report["evidence"]
    for key, label in DIMENSIONS.items():
        text.extend(["## " + label, ""])
        finding = report.get("dimensions", {}).get(key)
        if not finding:
            text.extend(["该维度尚未成功完成，未用模板补造结论。", ""]); continue
        text.extend([finding["conclusion"], "", "判断性质：" + finding["nature"], ""])
        for item in finding["evidence_ids"]:
            e = evidence[item]
            span = (f"约{e['start_seconds']:g}—{e['end_seconds']:g}秒" if "start_seconds" in e else "")
            source = (" " + e["url"] if e.get("url") else "")
            if e.get("scenes"):
                details = []
                for scene in e["scenes"]:
                    line = f"{scene.get('start', '?')}—{scene.get('end', '?')}秒：{scene.get('description', '')}"
                    if scene.get("on_screen_text"):
                        line += "；画面文字：" + " / ".join(scene["on_screen_text"])
                    details.append(line)
                detail = "；".join(details)
            else:
                detail = e.get("title") or e.get("text") or e.get("note") or ""
            text.append(f"- [{item}] {span} {e['kind']}：{str(detail)[:1000]}{source}")
        text.extend(["", "不确定性：" + (finding["uncertainty"] or "仍需实际发布验证，不能据内容推断真实流量"), ""])
    text.extend(["## 主要优势", *["- " + x for x in report.get("strengths", [])], "", "## 限制与未完成项",
                 *["- " + x for x in report.get("limitations", [])], ""])
    return "\n".join(text)


def review_video(video_path, output_dir, settings=None, *, context=None, platform="unspecified",
                 progress=None, client=None, search=False):
    """Run/resume step 1, then produce a separate grounded report; never mutate selected_script."""
    settings = settings or ReviewSettings.from_env()
    directory, understanding = understand_video(video_path, output_dir, settings, progress=progress, client=client)
    task, evidence, notes = build_evidence(understanding, context or {})
    if search and understanding["status"] != "failed":
        try:
            found = search_sources(task["topic"], str(task["brief"].get("target_audience", "")), platform, directory / "research")
            for i, row in enumerate(found["sources"], 1):
                rec = source_record(row, "N", i); evidence[rec["id"]] = rec
        except Exception as exc:
            notes.append("补充搜索未完成：" + type(exc).__name__ + "；未编造来源")
    stable_understanding = {k: v for k, v in understanding.items() if k != "created_at"}
    report_id = identity({"version": REPORT_VERSION, "understanding": stable_understanding,
                          "context": evidence, "platform": platform})[:16]
    report_dir = directory / ("report_" + report_id)
    report_dir.mkdir(exist_ok=True)
    result = {"schema_version": VERSION, "step": "video_analysis_report", "created_at": now(),
              "model": settings.model, "platform": platform, "video_sha256": understanding["video"]["sha256"],
              "understanding_status": understanding["status"], "coverage": understanding["coverage"],
              "status": "running", "task": task, "evidence": evidence, "dimensions": {},
              "overall": "", "strengths": [], "limitations": understanding["limitations"] + notes,
              "prediction": {"enabled": False, "note": "第三步尚未实现：本报告不输出评分、播放量或爆款概率"}}
    draft_path = report_dir / "analysis_draft.json"
    if understanding.get("api_error"):
        result["api_error"] = understanding["api_error"]
        result["limitations"].append(understanding["api_error"]["message"])
    if understanding["status"] != "failed" and not understanding.get("api_error"):
        instruction = ("根据下列四类依据输出成片分析报告：实际画面与独立声音观察、用户任务、程序可核对记录、主题资料。"
            "六维键必须为opening,audience,pacing,local_culture,sharing,platform_topic。每维简洁给出conclusion、evidence_ids、nature、uncertainty。"
            "nature仅可为成片观察/来源记载/合理推断/待核实。优势和限制也须对应已给依据。"
            "模型观察不是逐帧事实；音频整体片段转写不是逐句时间对齐，因此不得声称字幕与口播高度同步。报告证据已依短重叠对相邻片段去重，原始逐段识别另行保留。内部边界不能作为原视频音轨被截断或出现停顿的依据，也不是声音事件的时间戳。只有片段内声音观察明确支持，才可报告具体停顿；只有实际成片末尾的声音观察明确支持，才可报告音轨骤停。"
            "不凭主题猜具体地点或猫品种；看不清的文字不补全；不确定的连续动作和转场不得断言。"
            "用户缺少明确受众时，audience必须写成‘从视频措辞/内容推断可能面向……’，nature标为合理推断，不能称受众明确或已知；本次用户要求优先于冲突的旧brief。"
            "开场须区分疑问/信息缺口与祈使句；‘先别划走’本身只是祈使句，不能称为悬念。开场与节奏分析须区分观察和推断；不能声称强钩子、符合儿童认知速度、提升停留或互动效果，除非给出相应观众研究或实测依据。可分析信息是否清楚、节奏变化与互动设计意图。"
            "分享动机若涉及观众可能的行为，nature必须标为合理推断；不能仅凭互动提问就断言分享效果。没有平台元数据时不得称符合某个平台常见特征或算法要求。"
            "地方标签只证明视频画面出现了该文字，不证明实际地点、来源或文化事实；无正文来源时须说明未核实。"
            "上游脚本、自称verified、搜索摘要与已读正文必须区分。没有近期数据不称热门，不能把景点介绍当观众喜好调查。"
            "当前只做前两步，不打分、不预测播放量或爆款概率，不改写脚本，不自动编辑视频。"
            "证据不足仍给谨慎分析，并在对应维度写清限制；不得输出无依据的确定结论。"
            "输入资料：" + json.dumps({"platform": platform, "task": task, "evidence": evidence}, ensure_ascii=False))
        try:
            if draft_path.is_file():
                draft = AnalysisDraft.model_validate(read_json(draft_path)).model_dump()
            else:
                client = client or OmniClient(settings, report_dir / "model_calls", progress)
                draft = client.ask("成片分析报告", instruction, AnalysisDraft)
                atomic_json(draft_path, draft)
            result.update(overall=draft["overall"], strengths=draft["strengths"],
                          limitations=result["limitations"] + draft["limitations"])
            for key in DIMENSIONS:
                path = report_dir / (key + ".json")
                try:
                    data = read_json(path) if path.is_file() else draft["dimensions"].get(key, {})
                    try:
                        checked = validate_dimension(key, data, evidence)
                    except (ValueError, TypeError):
                        client = client or OmniClient(settings, report_dir / "model_calls", progress)
                        checked = client.ask("单维修正_" + key, instruction + "\n只修复并输出" + key + "这一维度对象，"
                            "引用实际存在且类型相符的证据编号；不要重新输出其余成功维度。", Finding,
                            check=lambda f: validate_dimension(key, f.model_dump(), evidence))
                    result["dimensions"][key] = checked
                    atomic_json(path, checked)
                except ReviewAPIError as exc:
                    result["api_error"] = {"code": exc.code, "request_id": exc.request_id, "message": str(exc)}
                    result["limitations"].append(str(exc))
                    break
                except Exception as exc:
                    result["limitations"].append(DIMENSIONS[key] + "未完成：" + type(exc).__name__)
                atomic_json(report_dir / "analysis_report.json", result)
        except ReviewAPIError as exc:
            result["api_error"] = {"code": exc.code, "request_id": exc.request_id, "message": str(exc)}
            result["limitations"].append(str(exc))
        except Exception as exc:
            result["limitations"].append("报告生成未完成：" + type(exc).__name__)
    else:
        result["limitations"].append("实际成片理解失败或接口不可用；未生成虚假结论，成功的片段结果保留以便重试")
    result["status"] = ("complete" if len(result["dimensions"]) == len(DIMENSIONS) and understanding["status"] == "complete"
                        else "partial" if result["dimensions"] else "failed")
    result["report_directory"] = str(report_dir)
    atomic_json(report_dir / "analysis_report.json", result)
    (report_dir / "analysis_report.md").write_text(markdown_report(result), encoding="utf-8")
    atomic_json(directory / "latest_report.json", {"directory": str(report_dir), "status": result["status"]})
    return report_dir, result
