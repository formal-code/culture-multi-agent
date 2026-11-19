"""Run: python -m evaluation_agent.video_review understand|report VIDEO ..."""
import argparse
import json
from pathlib import Path
import sys

from ..config import load_env
from .config import ReviewSettings
from .pipeline import review_video, understand_video


def main(argv=None):
    parser = argparse.ArgumentParser(description="第一步理解成片；第二步输出分析报告；不做热度预测")
    parser.add_argument("step", choices=["understand", "report"])
    parser.add_argument("video", type=Path)
    parser.add_argument("--env-file", type=Path, default=Path("c_agent.env"))
    parser.add_argument("--output", type=Path, default=Path("video_reviews"))
    parser.add_argument("--context", type=Path, help="可选已有任务信息；缺少时允许有标记的受众推断，不强制另建文件")
    parser.add_argument("--platform", choices=["douyin", "kuaishou", "xiaohongshu", "unspecified"], default="unspecified")
    parser.add_argument("--search", action="store_true", help="报告阶段补充最多两项定向搜索")
    args = parser.parse_args(argv)
    try:
        load_env(args.env_file.expanduser().resolve(), override=True)
        context = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else {}
        if not isinstance(context, dict):
            raise ValueError("任务上下文必须是JSON对象")
        progress = lambda message: print(message, file=sys.stderr, flush=True)
        if args.step == "understand":
            directory, result = understand_video(args.video, args.output, ReviewSettings.from_env(), progress=progress)
        else:
            directory, result = review_video(args.video, args.output, ReviewSettings.from_env(), context=context,
                                             platform=args.platform, progress=progress, search=args.search)
        print(json.dumps({"status": result["status"], "step": args.step, "directory": str(directory),
                          "coverage": result["coverage"], "api_error": result.get("api_error")}, ensure_ascii=False))
        return 0 if result["status"] == "complete" else 3 if result["status"] == "partial" else 2
    except Exception as exc:
        # Exceptions deliberately omit raw SDK bodies, URLs and credentials.
        safe = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else type(exc).__name__
        print(json.dumps({"status": "failed", "error": safe}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
