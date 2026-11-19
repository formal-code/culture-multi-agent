import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import config
from culture.model import ModelClient, review_asset
from culture.types import Article, Budget, CollectionError, Media, ModelUnavailable


REVIEW = {"description": "古建筑楼阁立于城中。", "decision": "keep", "reason": "符合文旅用途",
          "category": "古建筑", "tags": ["光岳楼"], "requirement_status": "match",
          "requirement_evidence": "光岳楼", "uncertainties": [], "is_museum_display": False}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {config.API_KEY_ENV: "offline-test-placeholder"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session_patch = patch("culture.model.requests.Session")
        self.session = self.session_patch.start().return_value
        self.addCleanup(self.session_patch.stop)
        response = self.session.post.return_value
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(REVIEW, ensure_ascii=False)}}]}
        self.budget = Budget()
        self.client = ModelClient(self.budget)
        self.addCleanup(self.client.close)

    def test_image_sends_actual_file_and_article_context_together(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tower.jpg"
            path.write_bytes(b"offline-image-fixture")
            result = review_asset(self.client, "image", path,
                Article("https://example.org/tower.html", "光岳楼", "光岳楼的来源说明。", []),
                Media("https://example.org/tower.jpg", "image", "光岳楼夜景"),
                {"targets": ["光岳楼"]})
        self.assertEqual(result["decision"], "keep")
        user = self.session.post.call_args.kwargs["json"]["messages"][1]["content"]
        context = json.loads(user[0]["text"])
        self.assertEqual(context["article_title"], "光岳楼")
        self.assertEqual(context["media_caption"], "光岳楼夜景")
        image = user[1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(image.split(",", 1)[1]), b"offline-image-fixture")
        self.assertEqual(self.budget.calls, 1)

    def test_missing_local_file_does_not_count_or_send_model_request(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(CollectionError):
                self.client.complete("system", "article text", image_path=Path(temp) / "missing.jpg")
        self.session.post.assert_not_called()
        self.assertEqual(self.budget.calls, 0)

    def test_museum_display_exclusion_overrides_keep_for_every_request(self):
        for targets in ([], ["婚俗博物馆"]):
            with self.subTest(targets=targets):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW,
                    description="传统婚服静态展品。", is_museum_display=True))
                result = review_asset(client, "image", Path("unused.jpg"),
                    Article("u", "婚俗博物馆", "传统文化介绍。", []), Media("u.jpg", "image"),
                    {"targets": targets})
                self.assertEqual(result["decision"], "reject")
                self.assertIn("博物馆陈列", result["reason"])

    def test_obvious_museum_display_description_overrides_false_flag(self):
        for description in ("玻璃展柜陈列契约文书。", "人偶复原传统婚俗场景。", "展厅内展板介绍民俗历史。"):
            with self.subTest(description=description):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW, description=description))
                result = review_asset(client, "image", Path("unused.jpg"),
                    Article("u", "婚俗博物馆", "介绍当地文化。", []), Media("u.jpg", "image"), {})
                self.assertEqual(result["decision"], "reject")

    def test_museum_exterior_and_live_craft_are_not_rejected_as_displays(self):
        for description in ("博物馆外观为灰砖木构建筑，檐下挂红灯笼。", "手艺人在桌前剪纸，游人在旁观看。"):
            with self.subTest(description=description):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW, description=description))
                result = review_asset(client, "image", Path("unused.jpg"),
                    Article("u", "婚俗博物馆", "介绍当地文化。", []), Media("u.jpg", "image"), {})
                self.assertEqual(result["decision"], "keep")

    def test_missing_display_classification_cannot_pass_image_review(self):
        client = Mock()
        value = dict(REVIEW)
        del value["is_museum_display"]
        client.complete.return_value = json.dumps(value)
        result = review_asset(client, "image", Path("unused.jpg"),
            Article("u", "传统文化", "", []), Media("u.jpg", "image"), {})
        self.assertEqual(result["decision"], "review")

    def test_explicit_display_request_still_respects_model_quality_decision(self):
        plan = {"raw_request": "聊城博物馆展品照片", "museum_display_requested": True,
                "museum_display_request_evidence": "博物馆展品照片"}
        for decision in ("keep", "reject", "review"):
            with self.subTest(decision=decision):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW, decision=decision,
                    description="展柜内陈列陶器。", is_museum_display=True))
                result = review_asset(client, "image", Path("unused.jpg"),
                    Article("u", "聊城博物馆", "馆藏介绍。", []), Media("u.jpg", "image"), plan)
                self.assertEqual(result["decision"], decision)

    def test_generic_museum_mention_or_unverified_permission_still_rejects_displays(self):
        for raw, evidence in (("聊城博物馆", "博物馆"),
                              ("聊城博物馆外观", "博物馆展品照片"),
                              ("聊城不要博物馆陈列", "不要博物馆陈列")):
            with self.subTest(raw=raw):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW, is_museum_display=True))
                result = review_asset(client, "image", Path("unused.jpg"),
                    Article("u", "博物馆", "", []), Media("u.jpg", "image"),
                    {"raw_request": raw, "museum_display_requested": True,
                     "museum_display_request_evidence": evidence})
                self.assertEqual(result["decision"], "reject")

    def test_video_review_uses_only_article_text(self):
        with patch("culture.model.Path.read_bytes", side_effect=AssertionError("must not read video frames")):
            result = review_asset(self.client, "video", Path("unused.mp4"),
                Article("https://example.org/tower.html", "光岳楼", "光岳楼视频介绍。", []),
                Media("https://example.org/tower.mp4", "video"), {"targets": ["光岳楼"]})
        user = self.session.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual([part["type"] for part in user], ["text"])
        self.assertTrue(result["visuals_unverified"])

    def test_network_error_types_are_distinguished_without_retry(self):
        for error, label in [(requests.ConnectTimeout(), "连接超时"),
                             (requests.ReadTimeout(), "回复读取超时"),
                             (requests.ConnectionError(), "连接中断")]:
            with self.subTest(error=type(error).__name__):
                self.session.post.reset_mock()
                self.session.post.side_effect = error
                with self.assertRaisesRegex(ModelUnavailable, label):
                    self.client.complete("system", "text")
                self.session.post.assert_called_once()

    def test_lake_video_can_match_one_target_without_visual_review(self):
        article = Article("https://example.org/race.html",
            "2023聊城龙舟公开赛在东昌湖金凤广场水域举办", "东昌湖举办龙舟公开赛。", [])
        client = Mock()
        client.complete.return_value = json.dumps(dict(REVIEW,
            description="据来源文章，该视频主题为东昌湖龙舟公开赛。",
            requirement_evidence="东昌湖举办龙舟公开赛"))
        result = review_asset(client, "video", Path("unused.mp4"), article,
            Media("https://example.org/race.mp4", "video"),
            {"targets": ["东昌湖", "光岳楼"]})
        self.assertEqual(result["decision"], "keep")
        self.assertTrue(result["visuals_unverified"])
        prompt, payload = client.complete.call_args.args
        context = json.loads(payload)
        self.assertEqual(context["target_match_mode"], "any")
        self.assertIn("不要求它同时出现光岳楼", prompt)
        self.assertIn("不得仅因未观看视频", prompt)
        self.assertIsNone(client.complete.call_args.kwargs["image_path"])

    def test_video_keep_requires_verbatim_source_evidence(self):
        for evidence in ("", None, ["光岳楼"], "光岳楼夜间灯光表演"):
            with self.subTest(evidence=evidence):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW, requirement_evidence=evidence))
                result = review_asset(client, "video", Path("unused.mp4"),
                    Article("u", "光岳楼", "光岳楼古建筑介绍。", []),
                    Media("u.mp4", "video"), {"targets": ["光岳楼"]})
                self.assertEqual(result["decision"], "review")
                self.assertIn("原文依据", result["reason"])

    def test_video_review_or_reject_is_not_automatically_promoted(self):
        for decision in ("review", "reject"):
            with self.subTest(decision=decision):
                client = Mock()
                client.complete.return_value = json.dumps(dict(REVIEW, decision=decision))
                result = review_asset(client, "video", Path("unused.mp4"),
                    Article("u", "光岳楼", "光岳楼介绍。", []), Media("u.mp4", "video"),
                    {"targets": ["东昌湖", "光岳楼"]})
                self.assertEqual(result["decision"], decision)


if __name__ == "__main__":
    unittest.main()
