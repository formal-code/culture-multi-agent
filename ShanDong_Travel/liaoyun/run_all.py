# -*- coding: utf-8 -*-
"""A + B 串联入口：一条命令先跑文化资料Agent，再跑策划Agent。

用法：
    python run_all.py                          完整跑：A 采集 → facts.json → B 出脚本
    python run_all.py --skip-a                 跳过 A，直接用已有交付数据出脚本（演示用）
    python run_all.py --skip-a --topic 聊城     跳过 A，并指定主题
    python run_all.py --request "聊城的风土人情"  改 A 的采集需求
"""
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLANNER = HERE / "planner"

# 按新旧顺序探测 A 的交付目录
DELIVERY_PATTERNS = (
    "fact.json", "facts.json",
    "*/*/fact.json", "*/*/facts.json",
    "*/*/*/fact.json", "*/*/*/facts.json",
)


def candidate_deliveries():
    """扫桌面和正式交付目录，兼容 A 改文件夹名的各种情况。"""
    home = Path.home()
    roots = [home / "Documents" / "文化资料Agent交付"]
    roots += [p for p in home.glob("Desktop/*culture*") if p.is_dir()]
    roots += [p for p in home.glob("Desktop/*A*") if p.is_dir()]

    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for pat in DELIVERY_PATTERNS:
            for f in root.glob(pat):
                if not f.is_file():
                    continue
                d = f.parent
                if d in found:
                    continue
                if (d / "research.json").is_file():
                    found.append(d)
    return found


def find_delivery():
    """多份交付并存时，按 fact.json 的修改时间选最新一轮。"""
    cands = candidate_deliveries()
    if not cands:
        return None

    def stamp(d):
        return max(
            (d / n).stat().st_mtime
            for n in ("fact.json", "facts.json")
            if (d / n).is_file()
        )

    return max(cands, key=stamp)


def find_a_dir():
    """A 的项目目录。新版入口是 main.py，旧版是 collect_liaocheng.py。"""
    home = Path.home() / "Desktop"
    for base in (home / "culture2", home / "culture2" / "culture2"):
        if (base / "main.py").is_file():
            return base
    for cand in (home / "culture_infoagent" / "culture_infoagent", home / "culture_infoagent"):
        if (cand / "collect_liaocheng.py").is_file():
            return cand
    return None


def run(cmd, cwd, title, feed=None):
    print("\n" + "=" * 56)
    print("[%s]" % title)
    print("  " + " ".join(str(c) for c in cmd))
    print("=" * 56)
    r = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd),
        input=feed,
        text=True if feed else None,
    )
    if r.returncode != 0:
        raise SystemExit("\n失败：%s（退出码 %d）" % (title, r.returncode))
    print("完成：%s" % title)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-a", action="store_true", help="跳过 A 的采集，直接用已有数据")
    ap.add_argument("--topic", default="", help="B 的主题（留空则用 facts.json 里的 topic）")
    ap.add_argument("--request", default="聊城的风土人情", help="传给 A 的采集需求")
    ap.add_argument("--delivery", default="", help="手动指定 A 的交付目录（含 fact.json）")
    args = ap.parse_args()

    if not args.skip_a:
        a_dir = find_a_dir()
        if not a_dir:
            raise SystemExit("找不到 A 的项目目录（需要含 main.py 或 collect_liaocheng.py）")
        print("A 目录：%s" % a_dir)
        entry = "main.py" if (a_dir / "main.py").is_file() else "collect_liaocheng.py"
        run([sys.executable, entry], a_dir, "A 文化资料Agent：官网素材采集", feed=args.request + "\n")

    if args.delivery:
        delivery = Path(args.delivery)
        if not delivery.is_dir():
            raise SystemExit("指定的交付目录不存在：%s" % delivery)
    else:
        delivery = find_delivery()

    if delivery:
        print("交付目录：%s" % delivery)
        facts_cmd = [sys.executable, "facts_from_a.py", str(delivery)]
    else:
        print("[提示] 没找到 A 的交付目录，交给接口层自动探测")
        facts_cmd = [sys.executable, "facts_from_a.py"]

    run(facts_cmd, PLANNER, "接口层：A 的 fact → B 的 facts.json + 本轮素材清单")

    b_cmd = [sys.executable, "planner.py"]
    if args.topic:
        b_cmd.append(args.topic)
    run(b_cmd, PLANNER, "B 策划Agent：生成候选脚本")

    print("\n全部完成，脚本目录：%s" % (PLANNER / "output"))


if __name__ == "__main__":
    main()
