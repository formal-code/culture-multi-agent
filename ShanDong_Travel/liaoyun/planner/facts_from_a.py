# -*- coding: utf-8 -*-
"""从 A 的交付目录自动生成 planner 用的 facts.json，并同步本轮素材。

兼容 A 的三种交付格式：
  1. 新版 fact.json + research.json(assets)
  2. 上一版 facts.json + research.json(media_assets)
  3. 更早的 ready_for_video.json(visual_description)

做三件事：
  - 生成 planner/data/facts.json
  - 把本轮素材复制到 liaoyun/assets/（只新增，不删除旧文件）
  - 写 planner/data/asset_manifest.json，限定 B 本轮只能选这些素材

用法：
    python facts_from_a.py                    # 自动找交付目录
    python facts_from_a.py "D:\\某交付目录"    # 手动指定
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "facts.json"
MANIFEST = HERE / "data" / "asset_manifest.json"
PROJECT = HERE.parent
ASSETS_DIR = PROJECT / "assets"

MAX_FACTS = 20
MAX_ELEMENTS = 15

# 事实原文里自带这些说法时，模型一引用就会被自查打回，干脆不提供给模型
RISKY = [
    r"(明清|唐宋|元明|汉代|唐代|宋代|明代|清代|民国)",
    r"[一二三四五六七八九十百千万两0-9]+年",
    r"[一二三四五六七八九十百千0-9]+(世纪|载)",
    r"公元\s*[0-9]+",
]


def is_risky(text):
    return any(re.search(p, text) for p in RISKY)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


DELIVERY_PATTERNS = (
    "fact.json", "facts.json",
    "*/*/fact.json", "*/*/facts.json",
    "*/*/*/fact.json", "*/*/*/facts.json",
)


def candidate_deliveries():
    """扫常见位置下的交付目录，兼容 A 改文件夹名的各种情况。"""
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


def dedup(items, limit=None):
    seen, out = set(), []
    for x in items:
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out[:limit] if limit else out


def fact_basis(item):
    """这条事实是原文正文、文章标题还是图注。"""
    et = str(item.get("evidence_type") or "").lower()
    if "title" in et:
        return "article_title"
    if "caption" in et:
        return "article_caption"
    return "source_text"


def asset_basis(asset):
    """这条描述是怎么来的：看过图、只读文字、或没说明。"""
    b = str(asset.get("description_basis") or "").lower()
    if "not_visually_verified" in b:
        return "video_unverified"
    if "visual" in b or "image_model" in b or "direct_image" in b:
        return "visual"
    if "title" in b or "article" in b:
        return "source_text"
    return ""


BASIS_LABEL = {
    "source_text": "来源原文",
    "article_title": "文章标题",
    "article_caption": "文章图注",
    "visual": "图片画面观察",
    "video_unverified": "仅文章文字，视频画面未核实",
    "": "素材描述",
}


def build(delivery):
    city = ""
    raw = []
    elements = []
    research_assets = []

    # research.json 先读：后面要用它的 source_url 补事实出处
    research = {}
    rp = delivery / "research.json"
    if rp.is_file():
        research = read_json(rp)
        city = research.get("city", "") or city
        # 新版字段是 assets，旧版是 media_assets，两种都收
        for a in (research.get("assets") or research.get("media_assets") or []):
            research_assets.append(a)
            elements += a.get("keywords") or []
            elements += a.get("suggested_themes") or []

    # 原始素材 id → 文件名 / 出处
    asset_file, asset_url = {}, {}
    for a in research_assets:
        if not a.get("id"):
            continue
        asset_url[a["id"]] = a.get("source_url", "")
        fp = a.get("file_path") or ""
        if fp:
            asset_file[a["id"]] = Path(fp).name

    # 1. fact.json（新）/ facts.json（旧）：有出处的原文陈述
    for name in ("fact.json", "facts.json"):
        fp = delivery / name
        if not fp.is_file():
            continue
        data = read_json(fp)
        city = data.get("city", "") or city
        for item in data.get("facts", []):
            text = (item.get("text") or item.get("evidence_quote") or "").strip()
            if not text:
                continue
            aids = item.get("asset_ids") or []
            source = item.get("source") or item.get("source_url", "")
            if not source:
                for aid in aids:
                    if asset_url.get(aid):
                        source = asset_url[aid]
                        break
            raw.append({
                "text": text,
                "source": source,
                "basis": fact_basis(item),
                "raw_id": item.get("id", ""),
                "files": [asset_file[x] for x in aids if x in asset_file],
            })
        # 旧 facts.json 把素材描述也放在这里
        for a in data.get("assets", []):
            desc = (a.get("description") or "").strip()
            if desc:
                raw.append({
                    "text": desc,
                    "source": a.get("media_url") or a.get("source_url", ""),
                    "basis": asset_basis(a),
                    "raw_id": "",
                    "files": [],
                })
            elements += a.get("suggested_themes") or []
            if a.get("category"):
                elements.append(a["category"])

    # 2. research.json 里的素材描述
    for a in research_assets:
        desc = (a.get("description") or "").strip()
        if not desc:
            continue
        fname = Path(a.get("file_path") or "").name
        raw.append({
            "text": desc,
            "source": a.get("source_url", ""),
            "basis": asset_basis(a),
            "raw_id": "",
            "files": [fname] if fname else [],
        })

    # 3. 更早格式兜底
    if not raw:
        for rv in delivery.rglob("ready_for_video.json"):
            data = read_json(rv)
            city = city or data.get("city", "")
            for a in data.get("assets", []):
                vd = a.get("visual_description") or {}
                desc = (vd.get("summary") or vd.get("description") or "").strip()
                if desc:
                    raw.append({
                        "text": desc,
                        "source": (a.get("download") or {}).get("page_url", ""),
                        "basis": "visual",
                        "raw_id": "",
                        "files": [],
                    })
                elements += vd.get("tags") or []

    if not raw:
        raise SystemExit("交付目录里没有可用描述：%s" % delivery)

    # 事实原文自带具体年数/朝代时，模型引用就会被 BANNED 打回，先剔掉
    clean = [f for f in raw if not is_risky(f["text"])]
    if clean and len(clean) < len(raw):
        print("  [过滤] %d 条事实含具体年数或朝代，不提供给模型" % (len(raw) - len(clean)))
        raw = clean

    # 去重编号，同时记录原始 id → 新编号
    merged, seen, raw_to_new, text_to_id = [], set(), {}, {}
    for f in raw:
        if f["text"] in seen:
            # 重复句子沿用已有编号，素材关联仍要记上
            f["new_id"] = text_to_id[f["text"]]
            if f["raw_id"]:
                raw_to_new[f["raw_id"]] = f["new_id"]
            continue
        seen.add(f["text"])
        new_id = "F%02d" % (len(merged) + 1)
        merged.append({
            "id": new_id,
            "text": f["text"],
            "source": f["source"],
            "basis": f["basis"],
        })
        f["new_id"] = new_id
        text_to_id[f["text"]] = new_id
        if f["raw_id"]:
            raw_to_new[f["raw_id"]] = new_id

    # 事实 → 素材（先算，后面按素材轮询采样要用）
    fact_files = {}
    for f in raw:
        nid = f.get("new_id")
        if nid and f.get("files"):
            fact_files.setdefault(nid, set()).update(f["files"])

    # 按素材轮询采样，保证每个素材都有事实可配，而不是只取前MAX_FACTS条
    by_asset, no_asset = {}, []
    for f in merged:
        files = fact_files.get(f["id"], set())
        if files:
            for fn in files:
                by_asset.setdefault(fn, []).append(f)
        else:
            no_asset.append(f)

    picked, seen_ids = [], set()
    while len(picked) < MAX_FACTS and any(by_asset.values()):
        for fn in list(by_asset):
            if len(picked) >= MAX_FACTS:
                break
            if by_asset[fn]:
                f = by_asset[fn].pop(0)
                if f["id"] not in seen_ids:
                    seen_ids.add(f["id"])
                    picked.append(f)
    for f in no_asset:
        if len(picked) >= MAX_FACTS:
            break
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            picked.append(f)

    merged = picked
    valid = {f["id"] for f in merged}

    # 素材 → 可配事实，供策划端强制绑定
    assets_out = []
    for a in research_assets:
        fname = Path(a.get("file_path") or "").name
        if not fname:
            continue
        related = set()
        for r in (a.get("fact_refs") or []):
            if r in raw_to_new:
                related.add(raw_to_new[r])
        for new_id, files in fact_files.items():
            if fname in files:
                related.add(new_id)
        assets_out.append({
            "file": fname,
            "type": a.get("media_type", "image"),
            "basis": asset_basis(a),
            "facts": sorted(x for x in related if x in valid),
            "hint": (a.get("description") or "").strip(),
        })

    return {
        "topic": city or "聊城",
        "city": city or "聊城",
        "source": "auto_from_a",
        "facts": merged,
        "assets": assets_out,
        "cultural_elements": dedup(elements, MAX_ELEMENTS),
    }, research_assets


def sync_assets(delivery, assets):
    """把本轮素材复制到 liaoyun/assets/。只新增或覆盖同名，不删除任何旧文件。"""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    delivery = Path(delivery)
    copied, missing = [], []

    names = []
    for a in assets:
        rel = a.get("file_path") or ""
        if not rel:
            continue
        src = delivery / rel
        if not src.is_file():
            missing.append(rel)
            continue
        dst = ASSETS_DIR / src.name
        shutil.copy2(src, dst)
        copied.append(src.name)
        names.append(src.name)

    # A 有时清单不全，目录里还有没登记的素材，一并带上
    for src in sorted((delivery / "assets").glob("*")):
        if src.is_file() and src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
            if src.name not in names:
                shutil.copy2(src, ASSETS_DIR / src.name)
                copied.append(src.name)
                names.append(src.name)

    return copied, missing


def main():
    delivery = Path(sys.argv[1]) if len(sys.argv) > 1 else find_delivery()
    if not delivery or not Path(delivery).is_dir():
        raise SystemExit("找不到 A 的交付目录，请手动传入路径")

    result, assets = build(Path(delivery))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    copied, missing = sync_assets(delivery, assets)
    MANIFEST.write_text(json.dumps({
        "delivery": str(delivery),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "assets": copied,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("已生成 %s" % OUT)
    print("  来源目录：%s" % delivery)
    print("  主题：%s" % result["topic"])
    print("  事实：%d 条" % len(result["facts"]))
    print("  元素：%s" % "、".join(result["cultural_elements"]))
    print("  本轮素材：%d 个 → %s" % (len(copied), ASSETS_DIR))
    for a in result.get("assets", []):
        print("    - %s｜可配事实：%s" % (a["file"], "、".join(a["facts"]) or "无"))
    if missing:
        print("  [警告] 清单里有 %d 个文件不存在：%s" % (len(missing), "、".join(missing)))
    print("  素材清单：%s" % MANIFEST)
    for f in result["facts"]:
        print("  - [%s] (%s) %s" % (f["id"], BASIS_LABEL.get(f["basis"], f["basis"]), f["text"]))


if __name__ == "__main__":
    main()
