import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from .config import Settings, load_env
from .pipeline import run_research
from .selection import run_selection
from .tools.http_client import ToolError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="接收策划JSON，研究资料并从原始候选中选择一条脚本；不改写、不预测热度。")
    parser.add_argument("input", type=Path, help="策划 Agent JSON 文件")
    parser.add_argument("--output", type=Path, default=Path("outputs"), help="筛选或研究结果的输出目录，默认 outputs")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="本地环境变量文件，默认当前目录 .env")
    parser.add_argument("--mode", choices=("select", "research"), default="select",
                        help="默认select自动筛选；research只运行六维研究")
    parser.add_argument("--research-dir", type=Path, help="筛选时使用的既有研究目录；默认自动查找匹配资料")
    parser.add_argument("--reuse-sources", type=Path, help="复用同一景点某次输出目录中的正文，不重新搜索；动态信息须另行更新")
    parser.add_argument("--search-provider", choices=("bailian_responses", "tavily", "bing_rss"),
                        help="覆盖 SEARCH_PROVIDER")
    parser.add_argument("--fallback-provider", choices=("auto", "bailian_responses", "tavily", "bing_rss", "none"),
                        help="覆盖 SEARCH_FALLBACK_PROVIDER；默认 auto")
    args = parser.parse_args(argv)

    try:
        load_env(args.env_file.expanduser().resolve(), override=True)
        settings = Settings.from_env()
        if args.search_provider:
            settings = replace(settings, search_provider=args.search_provider)
        if args.fallback_provider:
            fallback = args.fallback_provider
            settings = replace(settings, search_fallback_provider=fallback)
        if args.mode == "select":
            run_dir, handoff = run_selection(args.input, args.output, settings,
                                             research_dir=args.research_dir or args.reuse_sources)
            result = {"status": "selected", "selected_script_id": handoff["selected_script_id"],
                      "selected_script_file": str(run_dir / "selected_script.json")}
        else:
            if args.research_dir:
                raise ValueError("--research-dir用于筛选；仅研究并复用正文请用--reuse-sources")
            run_dir, report = run_research(args.input, args.output, settings, reuse_sources=args.reuse_sources)
            result = {"status": report["status"], "report_directory": str(run_dir)}
    except (OSError, ValueError, ToolError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
