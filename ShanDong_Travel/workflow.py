"""后台工作流；通过事件回调向前端报告真实状态，不依赖 Streamlit。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import app_config as config
import team_interfaces as team
from script_adapter import normalize_selected_script
from integrated_agent import _validate_inputs, create_dashscope_model_call, generate_adaptations

STAGES = ('research', 'planning', 'evaluation', 'adaptation', 'video')
StageCallback = Callable[[str, str, str], None]


@dataclass
class WorkflowState:
    prompt: str
    job_id: str = field(default_factory=lambda: uuid4().hex)
    research: dict[str, Any] | None = None
    candidates: dict[str, Any] | None = None
    evaluated: dict[str, Any] | None = None
    adaptations: dict[str, Any] | None = None
    assets_base_dir: Path | None = None
    videos: dict[str, Any] = field(default_factory=dict)
    video_reviews: dict[str, Any] = field(default_factory=dict)
    research_directory: str | None = None
    stages: dict[str, tuple[str, str]] = field(default_factory=lambda: {key: ('pending', '等待开始') for key in STAGES})


def read_json(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('后台 JSON 顶层必须是对象')
    return data


def validate_research(data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or data.get('schema_version') != '1.0':
        raise ValueError('A 返回的资料必须是 schema_version 为 1.0 的对象')
    if not isinstance(data.get('reference_videos'), list):
        raise ValueError('A 的资料缺少 reference_videos 数组')
    if 'media_assets' in data and not isinstance(data['media_assets'], list):
        raise ValueError('media_assets 必须是数组')


def run_content_workflow(state: WorkflowState, on_event: StageCallback, model_call=None) -> WorkflowState:
    """依次调用团队模块。异常交给前端处理，state 中已完成结果不会丢失。"""
    def emit(stage: str, status: str, message: str) -> None:
        state.stages[stage] = (status, message)
        on_event(stage, status, message)

    if not state.prompt.strip():
        raise ValueError('创作要求不能为空')
    if config.PIPELINE_MODE not in {'agents', 'local'}:
        raise ValueError('TRAVEL_PIPELINE_MODE 只能为 agents 或 local')
    local = config.PIPELINE_MODE == 'local'
    active = 'research'
    try:
        emit(active, 'running', '正在搜集相关资料及图片、视频素材……' if not local else '正在读取后台已准备的资料……')
        if local:
            package = team.ResearchPackage(read_json(config.RESEARCH_PATH), config.RESEARCH_PATH.parent)
        else:
            source_dir = config.GENERATED_DIR / state.job_id / 'source'
            source_dir.mkdir(parents=True, exist_ok=True)
            package = team.collect_research(
                state.prompt, source_dir,
                on_progress=lambda message: emit("research", "running", message),
            )
        validate_research(package.data)
        state.research = package.data
        state.assets_base_dir = Path(package.base_dir).resolve()
        emit(active, 'complete', '资料已就绪' if not local else '已读取本地资料，未执行在线搜集')

        active = "planning"

        if local:
            if config.USE_REAL_C_IN_LOCAL:
                state.candidates = read_json(
                    config.CANDIDATES_SCRIPT_PATH
                )

                rows = state.candidates.get("candidates")

                if not isinstance(rows, list) or not rows:
                    raise ValueError(
                        "候选脚本文件必须包含非空 candidates 数组"
                    )

                emit(
                    active,
                    "skipped",
                    "已读取候选脚本，未调用脚本策划服务",
                )
            else:
                emit(
                    active,
                    "skipped",
                    "本地联调：跳过候选脚本策划",
                )
        else:
            emit(
                active,
                "running",
                "正在根据资料策划候选脚本……",
            )

            state.candidates = team.plan_scripts(
                state.prompt,
                state.research,
                on_progress=lambda message: emit("planning", "running", message),
            )

            if (
                    not isinstance(state.candidates, dict)
                    or not isinstance(
                state.candidates.get("candidates"),
                list,
            )
                    or not state.candidates["candidates"]
            ):
                raise ValueError("B 必须返回非空 candidates 数组")

            emit(active, "complete", "候选脚本已生成")

        active = "evaluation"
        call_real_c = not local or config.USE_REAL_C_IN_LOCAL

        if call_real_c:
            emit(
                active,
                "running",
                "正在评估候选脚本并筛选最终方案……",
            )

            started = time.monotonic()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    team.evaluate_scripts,
                    state.prompt,
                    state.research,
                    state.candidates,
                    assets_base_dir=state.assets_base_dir,
                    on_research=lambda directory: setattr(state, "research_directory", directory),
                )

                while True:
                    completed, _ = wait([future], timeout=1)

                    if completed:
                        break

                    elapsed = int(time.monotonic() - started)

                    emit(
                        active,
                        "running",
                        f"正在测评与筛选，已等待 {elapsed} 秒……",
                    )

                evaluated = future.result()
        else:
            emit(
                active,
                "running",
                "正在读取后台筛选脚本……",
            )

            evaluated = read_json(
                config.EVALUATED_SCRIPT_PATH
            )

        evaluated = normalize_selected_script(evaluated)

        _validate_inputs(state.research, evaluated)
        state.evaluated = evaluated

        emit(
            active,
            "complete",
            "真实测评已完成"
            if call_real_c
            else "已读取筛选脚本，未重新测评",
        )

        active = 'adaptation'
        emit(active, 'running', '正在生成抖音、快手和小红书版本……')
        if (
                local
                and not config.USE_REAL_C_IN_LOCAL
                and config.ADAPTATIONS_PATH.is_file()
        ):
            data = read_json(config.ADAPTATIONS_PATH)
            from integrated_agent import _assemble_output
            candidate = state.evaluated['candidates'][0]
            if data.get('topic') != state.evaluated['topic'] or data.get('source_candidate_id') != candidate['candidate_id']:
                raise ValueError('本地适配结果与当前筛选脚本主题或编号不一致')
            state.adaptations = _assemble_output(data, candidate['script'], candidate['candidate_id'], {'topic': state.evaluated['topic']})
        else:
            call = model_call if model_call is not None else create_dashscope_model_call()
            state.adaptations = generate_adaptations(state.research, state.evaluated, call)
        emit(active, 'complete', '三个平台版本已就绪')
        emit('video', 'pending', '请选择一个平台生成视频')
        return state
    except team.IntegrationPending as exc:
        emit(active, 'waiting', str(exc))
        raise
    except Exception:
        emit(active, 'error', '当前步骤未完成，请稍后重试或联系项目维护者')
        raise


def run_video_review(state: WorkflowState, platform: str, on_progress=None):
    """Only the produced video is reviewed; errors never erase video delivery or other platforms."""
    video = state.videos.get(platform)
    if not video or not Path(video["path"]).is_file():
        raise ValueError("请先生成对应平台的成片")
    state.video_reviews[platform] = {"status": "running", "video_path": video["path"]}
    try:
        directory, result = team.analyze_finished_video(
            video["path"], prompt=state.prompt, research=state.research or {}, selected=state.evaluated or {},
            platform_content=(state.adaptations or {}).get("platforms", {}).get(platform, {}),
            platform=platform, output_dir=config.VIDEO_REVIEW_DIR / state.job_id,
            research_directory=state.research_directory, on_progress=on_progress)
        state.video_reviews[platform] = {"status": result["status"], "video_path": video["path"],
                                         "directory": str(directory), "result": result}
        return result
    except Exception as exc:
        from evaluation_agent.video_review.client import ReviewAPIError
        message = str(exc) if isinstance(exc, ReviewAPIError) else "成片分析未完成，请检查模型配置和后台日志；视频仍可下载"
        state.video_reviews[platform] = {"status": "failed", "video_path": video["path"],
                                         "error": message, "error_type": type(exc).__name__}
        raise
