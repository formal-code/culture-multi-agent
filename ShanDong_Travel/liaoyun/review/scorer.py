# -*- coding: utf-8 -*-
import json, sys

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def score_local(script, elements):
    s_ = script["script"]
    text = s_["hook"] + "".join(g["vo"] for g in s_["segments"])
    hits = [e for e in elements if e in text]
    density = len(hits) / max(len(text) / 100, 1)
    intro_phrases = ["大家好", "今天带大家", "今天给大家", "欢迎来到"]
    if any(p in s_["hook"] for p in intro_phrases):
        density *= 0.5   # 开场是自我介绍，密度砍半
    score = min(round(density * 3 + 2, 1), 10)
    ev = "命中地域元素" + str(len(hits)) + "处：" + ("、".join(hits) if hits else "无")
    return score, ev

def score_platform(ver, rule):
    s = 10
    if len(ver["title"]) > rule["title_max"]: s -= 3
    if ver["duration"] not in rule["durations"]: s -= 3
    if not (rule["tag_min"] <= len(ver.get("tags", [])) <= rule["tag_max"]): s -= 2
    ev = "标题" + str(len(ver["title"])) + "字，时长" + str(ver["duration"]) + "秒，标签" + str(len(ver.get("tags", []))) + "个"
    return max(s, 0), ev

def score_feasible(segs):
    s = 10
    notes = []
    for g in segs:
        if g["duration"] > 20:
            s -= 2; notes.append("seg." + str(g["no"]) + "超20秒")
        if not g.get("asset"):
            s -= 2; notes.append("seg." + str(g["no"]) + "无素材")
        if "无人机" in str(g.get("material", [])):
            s -= 3; notes.append("seg." + str(g["no"]) + "需无人机")
    return max(s, 0), ("；".join(notes) if notes else "所有镜头均有素材且时长合理")

if __name__ == "__main__":
    script = load(sys.argv[1])
    facts = load("data/facts.json")
    rule = load("data/platform_rule.json")["douyin"]
    segs = script["script"]["segments"]
    ver = {"title": script["script"]["title"],
           "duration": script["script"]["total_duration"],
           "tags": ["聊城", "年画"]}

    a = score_local(script, facts["cultural_elements"])
    b = score_platform(ver, rule)
    c = score_feasible(segs)
    print("地域特色   " + str(a[0]) + "   " + a[1])
    print("平台适配   " + str(b[0]) + "   " + b[1])
    print("拍摄可行性 " + str(c[0]) + "   " + c[1])