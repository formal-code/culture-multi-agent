"""将 C 的筛选结果转换为项目原约定的内部结构。"""

from copy import deepcopy
from typing import Any


def normalize_selected_script(data: dict[str, Any]) -> dict[str, Any]:
    """
    同时支持：
    旧结构：{"topic": "...", "candidates": [候选对象]}
    C 的新结构：{"topic": "...", "candidate": 候选对象, ...}

    不修改原始数据，返回转换后的副本。
    """
    if not isinstance(data, dict):
        raise ValueError("C 的筛选结果必须是 JSON 对象")

    result = deepcopy(data)

    # 兼容原来的单元素 candidates 数组。
    if "candidates" in result:
        candidates = result["candidates"]

        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ValueError("candidates 必须是恰好包含一个候选脚本的数组")

        candidate = candidates[0]

        # 两种字段同时出现时，不能默默选择其中一份。
        if "candidate" in result and result["candidate"] != candidate:
            raise ValueError("candidate 与 candidates 中的候选脚本不一致")

    # 转换 C 当前交付的 candidate 对象。
    elif "candidate" in result:
        candidate = result["candidate"]

    else:
        raise ValueError("筛选结果缺少 candidate 或 candidates 字段")

    if not isinstance(candidate, dict):
        raise ValueError("候选脚本必须是对象")

    candidate_id = candidate.get("candidate_id")
    script = candidate.get("script")

    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("候选脚本缺少有效的 candidate_id")

    if not isinstance(script, dict):
        raise ValueError("候选脚本缺少 script 对象")

    # C 当前文件同时交付了两个脚本副本，检查是否一致。
    if "script" in result and result["script"] != script:
        raise ValueError("顶层 script 与 candidate 内的 script 不一致，请 C 确认")

    if (
        "selected_script_id" in result
        and result["selected_script_id"] != candidate_id
    ):
        raise ValueError("selected_script_id 与 candidate_id 不一致")

    # 转换为现有一体化 agent 要求的结构。
    result["candidates"] = [candidate]

    # 其他字段保留，包括 unresolved_references 等信息。
    return result