# -*- coding: utf-8 -*-
"""策划Agent：输入提示词 → 输出多个不同角度的候选脚本。
主题从 facts.json 的 topic 字段读，角度由模型根据素材内容动态提出，A 换主题这里自动跟着变。
输出两份：带时间戳的历史归档、按主题的固定入口（下游直接读这个）。
生成后自动自查（字数/时长/编造/字幕脏标记/hook重复），不合格带着问题重写，最多3版。
有 LLM_API_KEY 就真调大模型，没有就读 mock_output.json 跑通链路。"""
import json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")

# 固定使用B原始交付的接口
API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

# 模型名称从环境变量读取
MODEL = os.getenv("PLANNER_MODEL", "qwen-plus")

ACTIVE = 3   # 实际生成几个候选，演示前改成 6

# 仅当模型调用失败时的降级方案，正常流程不会用到
FALLBACK = [
    ("场景实拍", "按素材顺序平实地展示画面内容，配简短解说"),
    ("实用攻略", "以游客视角讲去哪、看什么、怎么走"),
    ("细节特写", "聚焦素材中一个有特点的细节展开"),
]

BANNED = [
    (r"[张王李赵刘陈杨黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙凤万段钱汤尹黎易常武乔贺赖龚文](师傅|老师|大师|老爷子|传承人|匠人|艺人)", "具体人名"),
    (r"(明清|唐宋|元明|汉代|唐代|宋代|明代|清代|民国)", "具体朝代"),
    (r"[一二三四五六七八九十百千万两0-9]+年", "具体年数"),
    (r"[一二三四五六七八九十百千0-9]+(世纪|载)", "世纪/载等时间说法"),
    (r"[一二三四五六七八九十两0-9]+(天|个月|小时)", "具体天数/月数"),
    (r"传习所|体验馆|展览馆|游客中心|非遗工坊", "具体机构名"),
    (r"不用预约|无需预约|免费开放|门票|开放时间", "服务承诺类信息"),
]

ASSETS_DIR = os.path.join(os.path.dirname(BASE), "assets")
MANIFEST = os.path.join(DATA, "asset_manifest.json")

# 事实/描述的依据类型，用来提醒模型别把"文章标题"写成画面动作
BASIS_HINT = {
    "source_text": "来源原文",
    "article_title": "文章标题",
    "article_caption": "文章图注",
    "visual": "图片画面观察",
    "video_unverified": "仅文章文字，视频画面未核实",
}

def list_assets():
    """优先读本轮 A 交付的素材清单，避免引用上一轮遗留文件。"""
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST, encoding="utf-8") as f:
                names = json.load(f).get("assets", [])
            names = [n for n in names if os.path.isfile(os.path.join(ASSETS_DIR, n))]
            if names:
                return ["assets/" + n for n in names]
        except (OSError, ValueError):
            pass
    if not os.path.isdir(ASSETS_DIR):
        return ["assets/01.jpg"]
    files = sorted(f for f in os.listdir(ASSETS_DIR)
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".mp4")))
    return ["assets/" + f for f in files]

def asset_index(facts):
    """文件名 → 素材信息，用于强制每段只能配对应素材和事实。"""
    return {
        a["file"]: a for a in facts.get("assets", [])
        if a.get("file")
    }


def safe_name(text):
    """把主题转成安全的文件名。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(text)).strip("_") or "topic"

def load_facts():
    with open(os.path.join(DATA, "facts.json"), encoding="utf-8") as f:
        return json.load(f)

def load_brief():
    """读可选的 data/brief.json；没填的字段标'未提供'，不假装是用户要求。"""
    path = os.path.join(DATA, "brief.json")
    user = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    return {
        "audience": {"value": user.get("audience", ""), "source": "user" if user.get("audience") else "未提供"},
        "goal": {"value": user.get("goal", ""), "source": "user" if user.get("goal") else "未提供"},
        "platforms": {"value": user.get("platforms", ["douyin", "xiaohongshu", "shipinhao"]), "source": "user" if user.get("platforms") else "系统默认"},
        "target_duration": {"value": user.get("target_duration", 60), "source": "user" if user.get("target_duration") else "系统默认"},
    }

def build_prompt(angle_name, angle_desc, facts, user_input, brief):
    facts_text = "\n".join(
        "- [%s] %s（依据：%s）" % (
            f["id"], f["text"],
            BASIS_HINT.get(f.get("basis", ""), "素材描述"),
        )
        for f in facts["facts"]
    )
    elements = "、".join(facts["cultural_elements"])
    asset_rows = []
    for a in facts.get("assets", []):
        if not a.get("file"):
            continue
        usable = "、".join(a.get("facts") or []) or "只能写 []"
        asset_rows.append("- assets/%s｜可配事实：%s｜画面原样使用：%s" % (
            a["file"], usable, a.get("hint") or "（无描述）"))
    asset_list = "\n".join(asset_rows) or "(没有可用素材)"
    fact_ids = "、".join(f["id"] for f in facts["facts"])
    dur = brief["target_duration"]["value"]
    extra = ""
    if brief["audience"]["value"]:
        extra += "\n目标受众：%s" % brief["audience"]["value"]
    if brief["goal"]["value"]:
        extra += "\n传播目标：%s" % brief["goal"]["value"]
    return """你是短视频策划。用户想做：%s%s

请从【%s】的角度写一份%d秒竖版短视频脚本。%s。

可用的事实（必须只用这些，不许编造）：
%s

地域元素词表（脚本中尽量自然用到）：%s

严格输出JSON（不要输出任何其他文字、不要用markdown代码块），格式：
{"title":"标题，30字内","hook":"开场钩子，前3秒说的话","total_duration":%d,"segments":[{"no":1,"duration":10,"vo":"配音文案","subtitle":"屏幕字幕，可比vo短","visual":"画面描述","asset":"assets/01.jpg","asset_type":"image","fact_ids":["F01"],"removable":false}]}

硬性禁令（违反即作废）：
1. 严禁编造事实里没有的信息：人名、年份、历史年数、天数、菜品名、机构名、作品名、服务信息。
2. hook 前3秒必须具体有画面，禁止"大家好""今天带大家""欢迎来到"。
3. hook 不能与第1段 vo 重复，必须是另一句更有钩子的话。
4. 依据标着「仅文章文字，视频画面未核实」的条目，只能当作文字线索，不要据此写具体动作、人物、现场细节。
5. 每段 asset 必须从下面清单里选；该段 fact_ids 只能填这个素材「可配事实」里的编号，填别的会作废。
6. visual 必须原样包含所选素材的「画面原样使用」文字，不得改写，不得添加素材里没写的人物、动作、道具。
7. hook 和 vo 里不许出现事实、素材描述以外的具体道具或动作，比如"唢呐""面塑""糖瓜""鞭子""舞狮"——这些写出来就是编造。

以下是自动检查项，命中任何一条会被打回重写：
- 具体人名（如"李师傅""王老师"）
- 具体朝代（如"清代""明清"）
- 具体年数（如"四百年""千年"）、"xx世纪"
- 具体天数（如"七天"）
- 具体机构名（如"传习所""体验馆"）
- 服务承诺（如"不用预约""免费开放"）
- hook 与第1段 vo 完全重复
- subtitle 里出现 [F01] 这类事实编号（编号只能写在 fact_ids 数组里）
- 某段 fact_ids 里出现该素材「可配事实」之外的编号
- 某段 visual 没有原样包含该素材的「画面原样使用」文字
- 单段时长不在 8-15 秒
- 某段 vo 字数低于 duration×2
- 同一个素材被用了两次以上

输出要求：4-6段，总时长必须=%d秒。
先写每段 vo：每段 40-60 字（含标点），只说一个信息点，不要堆砌修饰语。
再按 4 字/秒 反推 duration：duration = round(vo字数/4)，必须在 8-15 秒之间，各段相加必须正好 %d 秒。
从素材清单里挑 4-6 个最贴合本角度的素材，每个素材只能使用一次，不要重复用同一个文件。
素材清单（文件｜可配事实｜画面原样使用）：
%s
（asset_type：.mp4写"video"，图片写"image"）；fact_ids只能从该段素材的「可配事实」里选（全部可选范围：%s），没有就写[]。vo 只能说事实和素材描述里有的内容，不许自行补画面。""" % (user_input, extra, angle_name, dur, angle_desc, facts_text, elements, dur, dur, dur, asset_list, fact_ids)

def call_llm(prompt):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({
            "model": MODEL,
            "temperature": 0.7,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]

def parse_json(text):
    text = text.strip()
    if "```" in text or not text.startswith("{"):
        text = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)

def check_script(script, asset_map=None):
    asset_map = asset_map or {}
    issues = []
    segs = script.get("segments", [])
    if not segs:
        return ["segments 为空"]
    total = sum(s.get("duration", 0) for s in segs)
    want = script.get("total_duration", 60)
    if total != want:
        issues.append("时长合计 %s 秒，应为 %s 秒" % (total, want))
    for s in segs:
        dur = s.get("duration", 0)
        if dur < 8 or dur > 15:
            issues.append("第%s段时长 %s 秒，应在 8-15 秒之间" % (s.get("no"), dur))
        n = len(s.get("vo", ""))
        limit = dur * 4
        low = dur * 2
        if n > limit:
            issues.append("第%s段配音 %d 字，超过 %d 秒上限 %d 字，需删 %d 字" % (
                s.get("no"), n, dur, limit, n - limit))
        elif n < low:
            issues.append("第%s段配音 %d 字，不足 %d 秒下限 %d 字，念起来会拖" % (s.get("no"), n, dur, low))
    # 每个素材只能用一次：一张图反复缩放会让成片画面单调
    used = {}
    for s in segs:
        name = os.path.basename(s.get("asset") or "")
        if name:
            used.setdefault(name, []).append(str(s.get("no")))
    for name, nos in used.items():
        if len(nos) > 1:
            issues.append("素材 %s 在第 %s 段重复使用，每个素材只能出现一次" % (name, "、".join(nos)))
    # hook 与第1段vo重复，成片会把同一句播两遍
    if script.get("hook", "").strip() == segs[0].get("vo", "").strip():
        issues.append("hook 与第1段vo完全重复，需改写hook")
    # 字幕里混入事实编号，D 会当成文字印到画面上
    for s in segs:
        if re.search(r"\[F\d+\]", s.get("subtitle", "")):
            issues.append("第%s段字幕混入事实编号，需删掉方括号标记" % s.get("no"))
    # 每段只能配该素材关联的事实，visual 不能改写素材描述
    if asset_map:
        for s in segs:
            name = os.path.basename(s.get("asset") or "")
            info = asset_map.get(name)
            if not info:
                issues.append("第%s段引用了素材清单外的文件：%s" % (s.get("no"), s.get("asset")))
                continue
            extra = set(s.get("fact_ids") or []) - set(info.get("facts") or [])
            if extra:
                issues.append("第%s段引用了与该素材无关的事实：%s（该素材只能用 %s）" % (
                    s.get("no"), "、".join(sorted(extra)),
                    "、".join(info.get("facts") or []) or "无"))
            hint = (info.get("hint") or "").strip()
            if hint and hint not in (s.get("visual") or ""):
                issues.append("第%s段visual未原样包含素材描述：%s" % (s.get("no"), hint))
    text = script.get("hook", "") + "".join(s.get("vo", "") for s in segs)
    for pat, why in BANNED:
        hit = re.search(pat, text)
        if hit:
            issues.append("出现%s：「%s」" % (why, hit.group()))
    return issues

def gen_one(angle, facts, user_input, brief, asset_map=None, max_try=3):
    name, desc = angle
    prompt = build_prompt(name, desc, facts, user_input, brief)
    script, issues = None, []
    for attempt in range(max_try):
        script = parse_json(call_llm(prompt))
        issues = check_script(script, asset_map)
        if not issues:
            break
        print("  【%s】第%d版不合格：%s" % (name, attempt + 1, " / ".join(issues)))
        prompt = build_prompt(name, desc, facts, user_input, brief) + \
            "\n\n上一版脚本有以下问题，必须全部修正后重新输出完整JSON：\n- " + "\n- ".join(issues)
    if issues:
        print("  【%s】3版仍未全部通过，已标记 needs_review" % name)
    return script, issues

def pick_angles(facts, user_input, n):
    """让模型看完素材后自己提出 n 个互不重复的创作角度。"""
    facts_brief = "\n".join(
        "- %s（依据：%s）" % (
            f["text"],
            BASIS_HINT.get(f.get("basis", ""), "素材描述"),
        )
        for f in facts["facts"][:10]
    )
    elements = "、".join(facts["cultural_elements"][:12])
    asset_rows = [
        "- assets/%s：%s" % (a["file"], a.get("hint") or "无描述")
        for a in facts.get("assets", []) if a.get("file")
    ]
    assets = "\n".join(asset_rows) or "(无素材)"
    prompt = """你是资深短视频策划。先判断这批素材能拍什么，再提出 %d 个互不重复的创作角度。

主题：%s

可用事实：
%s

地域元素：%s

可用素材：
%s

要求：
1. 角度必须从这些素材真能拍出来的内容出发，不要提素材支撑不了的角度。
2. %d 个角度要在叙事方式上明显不同（比如讲人、讲过程、讲攻略、讲典故、讲味道、讲本地日常、讲对比、讲悬念），不要只换个说法。
3. 每个角度给 2-6 字的名字，加一句话说明，说明里讲清这个角度靠哪条事实支撑。

只输出JSON，不要其他文字：
{"angles":[{"name":"角度名","desc":"怎么拍、依据哪条事实"}],"reason":"为什么这几个角度适合这批素材"}""" % (n, user_input, facts_brief, elements, assets, n)

    data = parse_json(call_llm(prompt))
    out = []
    for a in data.get("angles", []):
        name = str(a.get("name") or "").strip()
        desc = str(a.get("desc") or "").strip()
        if name and desc and name not in [x[0] for x in out]:
            out.append((name, desc))
    if len(out) < n:
        raise ValueError("只生成了 %d 个有效角度" % len(out))
    print("  角度：" + "、".join(x[0] for x in out[:n]))
    print("  理由：" + str(data.get("reason", "")))
    return out[:n]

def save(result):
    os.makedirs(OUT, exist_ok=True)
    body = json.dumps(result, ensure_ascii=False, indent=2)
    stamp = time.strftime("%m%d_%H%M")
    name = safe_name(result["topic"])

    arch = os.path.join(OUT, "scripts_%s_%s.json" % (name, stamp))
    with open(arch, "w", encoding="utf-8") as f:
        f.write(body)

    fixed = os.path.join(OUT, "scripts_%s_v1.json" % name)
    with open(fixed, "w", encoding="utf-8") as f:
        f.write(body)

    print("已归档：" + arch)
    print("固定入口：" + fixed)

def main():
    facts = load_facts()
    user_input = sys.argv[1] if len(sys.argv) > 1 else facts.get("topic") or "聊城"
    brief = load_brief()
    result = {"topic": user_input, "brief": brief, "candidates": [], "self_check": {}}

    if not os.environ.get("LLM_API_KEY"):
        print("[提示] 未检测到 LLM_API_KEY，使用 mock_output.json 预设输出")
        with open(os.path.join(DATA, "mock_output.json"), encoding="utf-8") as f:
            result["candidates"] = json.load(f)["candidates"]
        result["self_check"]["mode"] = "mock"
    else:
        print("主题：%s" % user_input)
        try:
            picked = pick_angles(facts, user_input, ACTIVE)
        except Exception as e:
            print("  [角度生成失败，使用降级方案] %s" % e)
            picked = FALLBACK[:ACTIVE]
        print("开始生成 %d 个候选：%s" % (len(picked), "、".join(a[0] for a in picked)))
        asset_map = asset_index(facts)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=len(picked)) as ex:
            outs = list(ex.map(lambda a: gen_one(a, facts, user_input, brief, asset_map), picked))
        for i, (name, _) in enumerate(picked):
            script, issues = outs[i]
            if issues:
                script["needs_review"] = True
            result["candidates"].append({
                "candidate_id": "cand_%d" % (i + 1),
                "angle": name,
                "script": script,
            })
            result["self_check"]["cand_%d" % (i + 1)] = issues or "通过"
        print("耗时 %.1f 秒" % (time.time() - t0))

    save(result)

if __name__ == "__main__":
    main()