import json
import struct
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest.mock import patch

import requests

import config
from culture.collector import collect
from culture.web import Response
from culture.types import ModelUnavailable


class PipelineTests(unittest.TestCase):
    def test_request_to_home_article_image_review_and_delivery_offline(self):
        self._run_pipeline("keep")

    def test_pending_review_file_is_deleted_and_not_counted_as_approved(self):
        self._run_pipeline("review")

    def test_rejected_file_not_in_approved_or_pending_review_directory(self):
        self._run_pipeline("reject")

    def test_museum_display_cannot_be_delivered_even_when_model_says_keep(self):
        self._run_pipeline("reject", museum_display=True)

    def test_timeout_keeps_approved_files_without_retaining_pending_files(self):
        self._run_pipeline("keep", fail_after_success=True)

    def _run_pipeline(self, decision, fail_after_success=False, museum_display=False):
        site = {"id": "fixture", "name": "离线官网", "url": "https://example.org/",
                "hosts": ["example.org"], "search_enabled": False}
        plan = {"city": "聊城", "districts": ["东昌府区"], "targets": ["光岳楼"],
                "themes": [], "exclude_terms": [], "media_type": "all"}
        review = {"description": "古建筑楼阁矗立于城中。", "decision": "keep" if museum_display else decision, "reason": "主体清晰",
                  "category": "古建筑", "requirement_status": "match", "requirement_evidence": "光岳楼",
                  "uncertainties": [], "tags": ["光岳楼"], "is_museum_display": museum_display}
        article_url = "https://example.org/20261119/tower.html"
        html = ("<h1>光岳楼</h1><div class='article-content'><p>光岳楼坐落在聊城古城，"
                "文章介绍这座古建筑的楼阁、屋檐与建筑轮廓，为图片提供明确地点来源线索。</p>"
                "<img src='/tower.jpg' alt='光岳楼'></div>")
        if fail_after_success:
            html = html.replace("</div>", "<img src='/pending.jpg'><img src='/timeout.jpg'></div>")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "OUTPUT_DIR", Path(temp) / "delivery"), \
                patch.object(config, "OFFICIAL_SITES", [site]), \
                patch.dict("os.environ", {config.API_KEY_ENV: "offline-test-placeholder"}), \
                patch("culture.model.requests.Session") as session, \
                patch("culture.collector.WebClient") as web_factory:
            response = session.return_value.post.return_value
            response.status_code = 200
            response.json.side_effect = [
                {"choices": [{"message": {"content": json.dumps(plan, ensure_ascii=False)}}]},
                {"choices": [{"message": {"content": json.dumps(review, ensure_ascii=False)}}]},
            ]
            if fail_after_success:
                pending = dict(review, decision="review", requirement_status="uncertain")
                response.json.side_effect = [
                    {"choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}
                    for value in (plan, review, pending)]
                session.return_value.post.side_effect = [response, response, response, requests.ReadTimeout()]
            web = web_factory.return_value
            web.html.side_effect = [(site["url"], "<a href='/20261119/tower.html'>光岳楼</a>"), (article_url, html)]

            def download(url, dest, media_type, referer):
                # A valid PNG fixture built with stdlib; no optional image library needed.
                def chunk(kind, data):
                    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
                header = struct.pack("!2I5B", 640, 480, 8, 2, 0, 0, 0)
                pixels = (b"\x00" + b"\x64\x96\xc8" * 640) * 480
                Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                    + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
                return Response(url, b"", "image/jpeg", None)

            web.download.side_effect = download
            if fail_after_success:
                with self.assertRaisesRegex(ModelUnavailable, "回复读取超时"):
                    collect("聊城东昌府区光岳楼", progress=lambda _: None)
                out = config.OUTPUT_DIR
                report = json.loads((out / "collection_report.json").read_text(encoding="utf-8"))
                research = json.loads((out / "research.json").read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "模型停止")
                self.assertEqual(report["model_calls"], 4)
                self.assertEqual(report["selected_asset_ids"], [row["id"] for row in research["assets"]])
                self.assertEqual(len(research["assets"]), 1)
                self.assertFalse((out / "review").exists())
                self.assertEqual(len(list((out / "assets").iterdir())), 1)
                for row in research["assets"]:
                    self.assertTrue((out / row["file_path"]).is_file())
                for name in ("fact.json", "sources.json"):
                    self.assertTrue((out / name).is_file())
                self.assertEqual(session.return_value.post.call_count, 4)
                return
            report = collect("聊城东昌府区光岳楼", progress=lambda _: None)
            self.assertEqual(report["status"], "采集完成")
            self.assertEqual(report["pages_read"], 2)
            self.assertEqual(report["model_calls"], 2)
            self.assertEqual(report["candidates_attempted"], 1)
            out = config.OUTPUT_DIR
            self.assertEqual(len(report["selected_asset_ids"]), int(decision == "keep"))
            self.assertFalse((out / "review").exists())
            if decision == "review":
                self.assertTrue(any(row.get("decision") == "review" for row in report["failures"]))
            if decision != "keep":
                research = json.loads((out / "research.json").read_text(encoding="utf-8"))
                self.assertEqual(research['assets'], [])
                self.assertEqual(json.loads((out / "fact.json").read_text(encoding="utf-8"))['facts'], [])
                self.assertEqual(json.loads((out / "sources.json").read_text(encoding="utf-8"))['sources'], [])
                self.assertEqual(list((out / "assets").iterdir()), [])
                self.assertFalse(list(out.rglob("*.jpg")))
                return
            research = json.loads((out / "research.json").read_text(encoding="utf-8"))
            asset = research["assets"][0]
            self.assertTrue((out / asset["file_path"]).is_file())
            self.assertEqual(asset["source_url"], article_url)
            self.assertEqual(asset["request_match"]["status"], "match")
            for name in ("fact.json", "sources.json", "collection_report.json"):
                self.assertTrue((out / name).is_file())
            request = session.return_value.post.call_args.kwargs["json"]
            self.assertEqual(request["messages"][1]["content"][1]["type"], "image_url")


if __name__ == "__main__":
    unittest.main()
