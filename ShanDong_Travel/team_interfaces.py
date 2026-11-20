"""A/B/C 接口预留。只修改函数体即可接入团队模块，前端不需要改。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class IntegrationPending(RuntimeError):
    """接口尚未连接；不得伪造已完成的阶段或生成结果。"""


class NoSuitableMaterials(RuntimeError):
    """本轮采集没有可用素材，与服务异常分开处理。"""

    code = "no_suitable_materials"


@dataclass
class ResearchPackage:
    data: dict[str, Any]
    base_dir: Path
    # base_dir 是 research.json 所在目录；media_assets.file_path 相对此目录。


def collect_research(prompt: str, work_dir: Path, *, on_progress=None) -> ResearchPackage:
    """真实调用A；返回本次资料与本次素材根目录，不使用历史样例。"""
    from team_ab_bridge import collect_with_a
    research, base_dir = collect_with_a(prompt, work_dir, on_progress=on_progress)
    return ResearchPackage(data=research, base_dir=base_dir)


def plan_scripts(prompt: str, research: dict[str, Any], *, on_progress=None) -> dict[str, Any]:
    """真实调用B；转换brief、来源与事实结构后交给原C接口。"""
    from team_ab_bridge import plan_with_b
    return plan_with_b(prompt, research, on_progress=on_progress)


def evaluate_scripts(
    prompt,
    research,
    candidates,
    *,
    assets_base_dir=None,
    on_research=None,
):
    """真实调用 C，返回其原始筛选结果字典。"""
    from c_bridge import call_c_agent

    if assets_base_dir is None:
        raise ValueError("未提供 A 的素材根目录")

    return call_c_agent(
        prompt,
        research,
        candidates,
        assets_base_dir=assets_base_dir,
        on_research=on_research,
    )


def analyze_finished_video(video_path, *, prompt, research, selected, platform_content,
                           platform, output_dir, research_directory=None, on_progress=None):
    """只做成片理解与报告；不调用候选筛选，不预测播放量。"""
    from evaluation_agent.config import load_env
    from evaluation_agent.video_review import ReviewSettings, review_video
    import app_config
    load_env(app_config.C_ENV_FILE, override=False)
    context = {"prompt": prompt, "research": research, "selected": selected,
               "platform_content": platform_content, "research_directory": research_directory}
    return review_video(video_path, output_dir, ReviewSettings.from_env(), context=context,
                        platform=platform, progress=on_progress, search=app_config.VIDEO_REVIEW_SEARCH)
