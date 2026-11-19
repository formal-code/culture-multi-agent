"""Post-selection source quotations; missing facts never exclude an approved asset."""
import hashlib
import re

from culture.intent import finalize_intent
from culture.ranking import content_title


def extract_fact_quotes(title, body, caption, plan):
    plan = finalize_intent(plan)
    needles = plan["targets"] or [*plan["theme_keywords"], *plan["districts"]]
    excluded = plan["exclude_terms"]
    quotes, seen = [], set()

    def add(text, kind):
        text = str(text or "").strip()
        if (len(text) < 8 or len(text) > 600 or text in seen
                or any(term in text for term in excluded)
                or (needles and not any(term in text for term in needles))):
            return
        seen.add(text)
        quotes.append({"text": text, "evidence_type": kind, "evidence_quote": text})

    for sentence in re.split(r"(?<=[。！？!?；;])\s*|\n+", body or ""):
        sentence = re.split(r"(?:责任编辑|编辑|审核|校对|来源)\s*[:：]", sentence, maxsplit=1)[0].strip()
        # Some article containers include heading/date/font controls before the first paragraph.
        sentence = re.sub(r"^.{0,160}?(?:字号|字体)\s*[:：].{0,60}?打印本页\s*", "", sentence)
        parts = re.split(r"(?<=[，,])\s*", sentence) if len(sentence) > 600 else [sentence]
        for part in parts:
            add(part, "article_body")
    # Some video articles provide only a title and editorial credits.
    if not quotes:
        add(content_title(title), "article_title")
    add(caption, "media_caption")
    return quotes[:8]


def populate_fact_links(assets, sources, plan):
    """Annotate all approved assets, including those with no attributable statement."""
    by_source = {source["id"]: source for source in sources}
    facts = {}
    for asset in assets:
        refs = []
        for source_id in asset.get("source_ids", []):
            source = by_source.get(source_id)
            if source is None:
                continue
            body = source.get("article_text", "")
            if not body and asset.get("media_type") == "video":
                body = source.get("evidence", "")
            for quote in extract_fact_quotes(source.get("title", ""), body,
                                             asset.get("media_caption", ""), plan):
                identity = source.get("url", source_id) + "\n" + quote["evidence_type"] + "\n" + quote["text"]
                fact_id = "fact_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
                if fact_id not in facts:
                    facts[fact_id] = {"id": fact_id, **quote, "status": "source_text_unverified",
                        "source_ids": [source_id], "asset_ids": [],
                        "verification_note": "原文摘录，位置见evidence_type；未做独立事实核验，不代表已核实视频画面。"}
                if asset["id"] not in facts[fact_id]["asset_ids"]:
                    facts[fact_id]["asset_ids"].append(asset["id"])
                if fact_id not in refs:
                    refs.append(fact_id)
        asset["fact_refs"] = refs
        asset["fact_status"] = "source_statements_available" if refs else "no_source_statement"
    return list(facts.values())
