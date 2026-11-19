"""Verify collection continues without image or video minimums, entirely offline."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import config
from culture.collector import _collect_plan
from culture.types import Budget, CollectionError, ModelUnavailable
from culture.web import Response


class VideoGoalTests(unittest.TestCase):
    def run_fixture(self, outcomes=("keep", "keep"), *, media_type="all", max_pages=10,
                    max_candidates=500, image_count=6):
        site = {"id": "fixture", "name": "离线官网", "url": "https://example.org/",
                "hosts": ["example.org"], "search_enabled": False}
        plan = {"raw_request": "聊城光岳楼东昌湖", "city": "聊城", "targets": ["光岳楼", "东昌湖"],
                "themes": [], "media_type": media_type}
        images_url = "https://example.org/20261119/images.html"
        videos_url = "https://example.org/20261119/videos.html"
        pages = {
            site["url"]: "<a href='/20261119/images.html'>光岳楼东昌湖风光</a>"
                         "<a href='/20261119/videos.html'>光岳楼视频</a>",
            images_url: "<h1>光岳楼东昌湖风光</h1><div class='article-content'><p>光岳楼景观。</p>"
                        + "".join(f"<img src='/image{i}.jpg'>" for i in range(image_count)) + "</div>",
            videos_url: "<h1>光岳楼视频</h1><div class='article-content'><p>光岳楼文旅视频。</p>"
                        + "<img src='/extra.jpg'>"
                        + "".join(f"<video src='/clip{i}.mp4' title='clip{i}'></video>"
                                  for i in range(len(outcomes))) + "</div>",
        }
        budget = Budget(max_pages=max_pages, max_candidates=max_candidates)
        web, model = Mock(), Mock()
        web.html.side_effect = lambda url, **kwargs: (url, pages[url])

        def download(url, dest, media_type, referer):
            if media_type == "video":
                index = int(Path(url).stem.removeprefix("clip"))
                if outcomes[index] == "failed":
                    raise CollectionError("离线模拟：视频下载失败")
            Path(dest).write_bytes(b"offline-fixture")
            return Response(url, b"", "video/mp4" if media_type == "video" else "image/jpeg", None)

        def complete(prompt, user, *, image_path=None):
            budget.take("calls")
            decision = "keep"
            if image_path is None:
                index = int(json.loads(user)["media_caption"].removeprefix("clip"))
                if outcomes[index] == "timeout":
                    raise ModelUnavailable("离线模拟：模型读取超时")
                decision = outcomes[index]
            return json.dumps({"description": "光岳楼的文旅素材。", "decision": decision,
                "reason": "离线审核结果", "requirement_status": "match",
                "requirement_evidence": "光岳楼", "uncertainties": [], "tags": [],
                "is_museum_display": False})

        web.download.side_effect = download
        model.complete.side_effect = complete
        messages = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "OFFICIAL_SITES", [site]), \
                patch.object(config, "OUTPUT_DIR", Path(temp) / "delivery"), \
                patch("culture.collector._check_media"):
            if "timeout" in outcomes:
                with self.assertRaises(ModelUnavailable):
                    _collect_plan(plan, budget, model, web, messages.append)
                report = json.loads((config.OUTPUT_DIR / "collection_report.json").read_text(encoding="utf-8"))
            else:
                report = _collect_plan(plan, budget, model, web, messages.append)
            research = json.loads((config.OUTPUT_DIR / "research.json").read_text(encoding="utf-8"))
            self.assertEqual(len(research["assets"]), report["approved_counts"]["total"])
            self.assertEqual(sum(row["media_type"] == "video" for row in research["assets"]),
                             report["approved_counts"]["video"])
            self.assertTrue(all((config.OUTPUT_DIR / row["file_path"]).is_file() for row in research["assets"]))
            self.assertFalse((config.OUTPUT_DIR / "review").exists())
        return report, web, messages

    def test_all_images_and_later_videos_are_collected_without_early_stop(self):
        report, web, _ = self.run_fixture()
        self.assertEqual(report["status"], "采集完成")
        self.assertEqual(report["approved_counts"], {"total": 9, "image": 7, "video": 2})
        self.assertFalse(report["minimum_counts_enabled"])
        self.assertNotIn("shortfall", report)
        self.assertEqual(report["pages_read"], 3)
        downloads = [call.args[0] for call in web.download.call_args_list]
        self.assertTrue(any("image5.jpg" in url for url in downloads))
        self.assertTrue(any("extra.jpg" in url for url in downloads))

    def test_failed_review_and_rejected_videos_do_not_count(self):
        report, _, _ = self.run_fixture(("failed", "review", "reject", "keep"))
        self.assertEqual(report["status"], "采集完成")
        self.assertEqual(report["approved_counts"], {"total": 8, "image": 7, "video": 1})
        self.assertEqual(report["candidates_attempted"], 11)
        self.assertNotIn("shortfall", report)

    def test_page_limit_stops_and_publishes_images_without_video_requirement(self):
        report, _, _ = self.run_fixture(max_pages=2)
        self.assertEqual(report["status"], "预算停止")
        self.assertEqual(report["approved_counts"], {"total": 6, "image": 6, "video": 0})
        self.assertIn("达到网页总额度", report["reason"])

    def test_candidate_limit_stops_without_video_requirement(self):
        report, _, _ = self.run_fixture(("failed", "keep", "keep"), max_candidates=6)
        self.assertEqual(report["status"], "预算停止")
        self.assertEqual(report["candidates_attempted"], 6)
        self.assertEqual(report["approved_counts"]["video"], 0)

    def test_model_failure_saves_partial_assets(self):
        report, _, _ = self.run_fixture(("keep", "timeout"))
        self.assertEqual(report["status"], "模型停止")
        self.assertEqual(report["approved_counts"]["video"], 1)
        self.assertNotIn("shortfall", report)

    def test_image_only_request_collects_all_images(self):
        report, _, _ = self.run_fixture(media_type="image")
        self.assertEqual(report["status"], "采集完成")
        self.assertEqual(report["approved_counts"], {"total": 7, "image": 7, "video": 0})
        self.assertNotIn("min_video_count", report)

    def test_video_only_request_collects_all_videos_beyond_five(self):
        report, _, _ = self.run_fixture(("keep",) * 6, media_type="video")
        self.assertEqual(report["status"], "采集完成")
        self.assertEqual(report["approved_counts"], {"total": 6, "image": 0, "video": 6})

    def test_five_hundred_candidate_limit_is_enforced_and_all_approved_are_saved(self):
        report, web, _ = self.run_fixture(media_type="image", image_count=501)
        self.assertEqual(report["status"], "预算停止")
        self.assertEqual(report["candidates_attempted"], 500)
        self.assertEqual(report["approved_counts"], {"total": 500, "image": 500, "video": 0})
        self.assertEqual(web.download.call_count, 500)
        self.assertEqual(report["limits"]["candidates"], 500)

    def test_one_video_is_complete_when_queue_is_exhausted(self):
        report, _, _ = self.run_fixture(("keep",), media_type="video")
        self.assertEqual(report["status"], "采集完成")
        self.assertEqual(report["approved_counts"], {"total": 1, "image": 0, "video": 1})
        self.assertFalse(report["minimum_counts_enabled"])


if __name__ == "__main__":
    unittest.main()
