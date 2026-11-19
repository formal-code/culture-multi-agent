import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
from culture.collector import _publish
from culture.facts import extract_fact_quotes, populate_fact_links
from culture.types import Article, Media


PLAN = {"raw_request": "聊城的风土人情", "city": "聊城", "targets": [],
        "districts": [], "themes": ["风土人情"], "exclude_terms": []}


class FactTests(unittest.TestCase):
    def test_page_font_controls_are_not_included_in_source_statement(self):
        body = "婚俗博物馆 时间：2022-09-14 字号： 大 中 小 打印本页 当地婚俗展览介绍了传统婚礼礼仪。"
        facts = extract_fact_quotes("婚俗介绍", body, "", PLAN)
        self.assertEqual(facts[0]["text"], "当地婚俗展览介绍了传统婚礼礼仪。")
        self.assertIn(facts[0]["evidence_quote"], body)

    def test_theme_only_request_extracts_related_source_body(self):
        body = "当地婚俗展览介绍了传统婚礼礼仪。编辑：某某 审核：某某"
        facts = extract_fact_quotes("当地文化", body, "", PLAN)
        self.assertEqual(facts[0]["evidence_type"], "article_body")
        self.assertEqual(facts[0]["text"], "当地婚俗展览介绍了传统婚礼礼仪。")
        self.assertTrue(all(row["evidence_quote"] in body for row in facts))
        self.assertFalse(any("编辑" in row["text"] for row in facts))

    def test_title_only_video_has_explicitly_labelled_title_statement(self):
        title = "烟台市级非遗传承人宋玉良，携八仙葫芦亮相聊城葫芦文化节。"
        facts = extract_fact_quotes(title, "编辑：某某 审核：某某", "", PLAN)
        self.assertEqual(facts, [{"text": title, "evidence_type": "article_title", "evidence_quote": title}])

    def test_named_target_does_not_gain_unrelated_theme_claims(self):
        plan = dict(PLAN, targets=["光岳楼"])
        self.assertEqual(extract_fact_quotes("婚俗博物馆展览介绍", "民俗展览介绍传统服饰。", "", plan), [])

    def test_model_description_is_never_used_as_source_fact(self):
        assets = [{"id": "a", "media_type": "image", "source_ids": ["s"],
                   "description": "这里的非遗已有千年历史。"}]
        sources = [{"id": "s", "url": "https://example.org/photo", "title": "图片", "article_text": ""}]
        self.assertEqual(populate_fact_links(assets, sources, PLAN), [])
        self.assertEqual(assets[0]["fact_status"], "no_source_statement")
        self.assertEqual(assets[0]["fact_refs"], [])

    def test_shared_source_is_deduplicated_and_all_assets_remain(self):
        assets = [{"id": aid, "media_type": "image", "source_ids": ["s"]} for aid in ("a", "b")]
        sources = [{"id": "s", "url": "https://example.org/marriage", "title": "婚俗介绍",
                    "article_text": "当地婚俗展览介绍了传统婚礼礼仪。"}]
        plan_before = copy.deepcopy(PLAN)
        facts = populate_fact_links(assets, sources, PLAN)
        self.assertEqual(PLAN, plan_before)
        self.assertEqual(len(assets), 2)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["asset_ids"], ["a", "b"])
        self.assertEqual(assets[0]["fact_refs"], assets[1]["fact_refs"])
        self.assertEqual(facts[0]["status"], "source_text_unverified")

    def test_missing_facts_do_not_remove_approved_asset_from_published_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / "stage"
            stage.mkdir()
            approved = []
            for number, body in enumerate(("当地婚俗展览介绍了传统婚礼礼仪。", "")):
                download = root / f"photo{number}.jpg"
                download.write_bytes(b"approved offline fixture")
                approved.append({"id": f"asset{number}", "download": download,
                    "article": Article(f"https://www.lcxw.cn/20261119/{number}.html", "图片记录", body, []),
                    "media": Media(f"https://img.example/{number}.jpg", "image"),
                    "review": {"description": "现场的建筑画面。", "requirement_status": "match", "uncertainties": []}})
            report = {"status": "采集完成", "visited_pages": [], "selected_asset_ids": ["asset0", "asset1"]}
            with patch.object(config, "OUTPUT_DIR", root / "delivery"):
                _publish(approved, PLAN, report, stage, lambda _: None)
                research = json.loads((config.OUTPUT_DIR / "research.json").read_text(encoding="utf-8"))
                facts = json.loads((config.OUTPUT_DIR / "fact.json").read_text(encoding="utf-8"))["facts"]
                sources = json.loads((config.OUTPUT_DIR / "sources.json").read_text(encoding="utf-8"))["sources"]
                self.assertEqual([a["id"] for a in research["assets"]], report["selected_asset_ids"])
                self.assertEqual(len(list((config.OUTPUT_DIR / "assets").iterdir())), 2)
                self.assertEqual(research["assets"][1]["fact_status"], "no_source_statement")
                self.assertEqual(research["assets"][1]["fact_refs"], [])
                self.assertEqual(len(facts), 1)
                self.assertEqual(sources[0]["publisher"], "聊城新闻网")
                self.assertIn(facts[0]["text"], sources[0]["article_text"])


if __name__ == "__main__":
    unittest.main()
