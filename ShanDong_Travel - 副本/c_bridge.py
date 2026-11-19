"""C 测评接入层：将内存候选写入后台文件，再调用 C 的原程序。"""

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import app_config as config


def call_c_agent(prompt, research, candidates, *, assets_base_dir, on_research=None):
    # 延迟导入：打开页面时不会执行 C。
    from evaluation_agent.config import Settings, load_env
    from evaluation_agent.selection import run_selection

    if not isinstance(candidates, dict):
        raise ValueError("C 的输入必须是候选脚本字典")

    payload = deepcopy(candidates)
    rows = payload.get("candidates")

    if not isinstance(rows, list) or not rows:
        raise ValueError("C 的输入缺少非空 candidates 数组")

    brief = payload.get("brief") or {}

    if not isinstance(brief, dict):
        raise ValueError("候选脚本中的 brief 必须是对象")

    # 将本次页面要求传给 C，保留候选文件已有的其他需求字段。
    brief = deepcopy(brief)
    original_goal = brief.get("communication_goal") or ""

    goal = (
        "本次用户要求（与原有目标冲突时以本次要求为准）："
        + prompt.strip()
    )

    if original_goal:
        goal += f"\n原有策划目标（补充参考）：{original_goal}"

    if len(goal) > 2000:
        raise ValueError(
            "本次要求与原有策划目标合计过长，请缩短至 2000 字符以内"
        )

    brief["communication_goal"] = goal
    payload["brief"] = brief

    # 在 A 的素材根目录保存输入，保持相对路径的解释基准。
    base_dir = Path(assets_base_dir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)

    token = uuid4().hex
    input_path = base_dir / f"c_input_{token}.json"

    input_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    load_env(config.C_ENV_FILE, override=True)
    settings = Settings.from_env()
    output_dir = config.C_OUTPUT_DIR / token

    print("[C测评] 开始调用真实测评程序", flush=True)

    run_dir, result = run_selection(
        input_path,
        output_dir,
        settings,
    )

    if not isinstance(result, dict):
        raise ValueError("C 返回的结果不是字典")

    # Internal callback only: selected_script.json stays an unchanged handoff.
    if on_research is not None:
        log_path = run_dir / "tool_log.json"
        try:
            log = json.loads(log_path.read_text(encoding="utf-8-sig"))
            entries = log if isinstance(log, list) else log.get("events", [])
            for entry in reversed(entries):
                directory = entry.get("research_directory") or (entry.get("details") or {}).get("research_directory")
                if directory:
                    on_research(str(directory))
                    break
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    print(
        f"[C测评] 完成，选中：{result.get('selected_script_id')}",
        flush=True,
    )
    print(
        f"[C测评] 本次结果：{run_dir / 'selected_script.json'}",
        flush=True,
    )

    return result
