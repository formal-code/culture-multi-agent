# -*- coding: utf-8 -*-
import json, os, re, sys

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

FACT_PAT = re.compile(r"\d{3,4}年|第[一二三四五六七八九十]|[\u4e00-\u9fff]{2,6}(县|楼|塔|寺|桥|镇)")

def gate(script, facts, asset_dir="."):
    issues = []
    segs = script["script"]["segments"]
    known = {f["id"] for f in facts["facts"]}

    for s in segs:
        for fid in s.get("fact_ids", []):
            if fid not in known:
                issues.append({"stage": "B", "location": f"seg.{s['no']}",
                    "problem": f"事实编号{fid}不存在", "suggestion": "换成真实编号或删掉", "severity": "high"})

    for s in segs:
        if not s.get("fact_ids") and FACT_PAT.search(s["vo"]):
            issues.append({"stage": "B", "location": f"seg.{s['no']}",
                "problem": "口播含事实性说法但未标注来源", "suggestion": "补编号或改写", "severity": "high"})

    total = sum(s["duration"] for s in segs)
    if total != script["script"]["total_duration"]:
        issues.append({"stage": "B", "location": "script",
            "problem": f"分镜合计{total}秒，声明{script['script']['total_duration']}秒",
            "suggestion": "调整时长", "severity": "medium"})

    for s in segs:
        p = os.path.join(asset_dir, s.get("asset", ""))
        if not s.get("asset") or not os.path.exists(p):
            issues.append({"stage": "B", "location": f"seg.{s['no']}",
                "problem": f"素材不存在：{s.get('asset')}", "suggestion": "换成库里真实图片", "severity": "high"})

    return {"pass": len(issues) == 0, "issues": issues}

if __name__ == "__main__":
    script = load(sys.argv[1])
    facts = load("data/facts.json")
    print(json.dumps(gate(script, facts), ensure_ascii=False, indent=2))