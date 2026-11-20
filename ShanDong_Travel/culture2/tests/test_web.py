"""Distinguish robots availability from actual media denial, without network access."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import config
from culture.types import Budget, BudgetExceeded, CollectionError
from culture.web import WebClient


class WebRulesTests(unittest.TestCase):
    def setUp(self):
        self.session_patch = patch("culture.web.requests.Session")
        self.session = self.session_patch.start().return_value
        self.session.headers = {}
        self.addCleanup(self.session_patch.stop)
        host_patch = patch("culture.web.public_host")
        host_patch.start()
        self.addCleanup(host_patch.stop)
        sleep_patch = patch("culture.web.time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.messages = []
        self.budget = Budget()
        self.web = WebClient(self.budget, progress=self.messages.append)
        self.addCleanup(self.web.close)

    def response(self, status, body=b"", content_type="text/plain"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.status_code = status
        response.is_redirect = response.is_permanent_redirect = False
        response.headers = {"Content-Type": content_type}
        response.encoding = "utf-8"
        response.iter_content.return_value = [body]
        return response

    def download(self, directory, name="clip.mp4"):
        return self.web.download("https://media.example/" + name, Path(directory) / name,
                                 "video", "https://news.example/article.html")

    def test_cumulative_download_limit_cleans_partial_file_and_excludes_html(self):
        self.budget.max_download_bytes = 5
        self.session.get.side_effect = [self.response(404),
            self.response(200, b"<html>page content</html>", "text/html"),
            self.response(200, b"abc", "video/mp4"),
            self.response(200, b"def", "video/mp4")]
        self.web.html("https://media.example/article.html")
        self.assertEqual(self.budget.download_bytes, 0)
        with tempfile.TemporaryDirectory() as temp:
            self.download(temp, "first.mp4")
            self.assertEqual(self.budget.download_bytes, 3)
            with self.assertRaisesRegex(BudgetExceeded, "媒体下载总量上限"):
                self.download(temp, "second.mp4")
            self.assertFalse((Path(temp) / "second.mp4").exists())
            self.assertEqual((Path(temp) / "first.mp4").read_bytes(), b"abc")

    def test_failed_download_bytes_still_count_toward_total(self):
        response = self.response(200, content_type="video/mp4")
        import requests
        def interrupted_stream(*args):
            yield b"abc"
            raise requests.ConnectionError("offline interruption")
        response.iter_content.side_effect = interrupted_stream
        self.session.get.side_effect = [self.response(404), response]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(CollectionError):
                self.download(temp)
            self.assertFalse((Path(temp) / "clip.mp4").exists())
        self.assertEqual(self.budget.download_bytes, 3)

    def test_robots_403_does_not_prevent_available_public_video_download(self):
        self.session.get.side_effect = [self.response(403, b"Forbidden", "text/html"),
                                       self.response(200, b"offline video fixture", "video/mp4")]
        with tempfile.TemporaryDirectory() as temp:
            self.download(temp)
            self.assertEqual((Path(temp) / "clip.mp4").read_bytes(), b"offline video fixture")
        self.assertEqual(self.budget.requests, 2)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("规则文件不可用", self.messages[0])

    def test_robots_403_does_not_bypass_actual_media_403_or_retry(self):
        self.session.get.side_effect = [self.response(403), self.response(403)]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CollectionError, "媒体文件返回 HTTP 403"):
                self.download(temp)
            self.assertFalse((Path(temp) / "clip.mp4").exists())
        self.assertEqual(self.session.get.call_count, 2)

    def test_explicit_disallow_still_blocks_media_without_requesting_it(self):
        self.session.get.side_effect = [self.response(200, b"User-agent: *\nDisallow: /\n")]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CollectionError, "站点规则不允许"):
                self.download(temp)
            self.assertFalse((Path(temp) / "clip.mp4").exists())
        self.assertEqual(self.session.get.call_count, 1)

    def test_configured_video_path_can_download_despite_robots_disallow(self):
        self.session.get.side_effect = [self.response(200, b"User-agent: *\nDisallow: /\n"),
                                       self.response(200, b"video fixture", "video/mp4")]
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "clip.mp4"
            self.web.download("https://api.lcxw.cn/upload/mp4/2026/clip.mp4", dest,
                              "video", "https://www.lcxw.cn/article.html")
            self.assertEqual(dest.read_bytes(), b"video fixture")
        self.assertEqual(self.session.get.call_count, 2)
        self.assertTrue(any("robots禁止声明仅作提示" in message for message in self.messages))

    def test_exception_does_not_apply_to_articles_images_or_other_paths_and_hosts(self):
        cases = [("https://api.lcxw.cn/upload/mp4/clip.mp4", ""),
                 ("https://api.lcxw.cn/upload/mp4/photo.jpg", "image"),
                 ("https://api.lcxw.cn/upload/mp4/photo.jpg", "video"),
                 ("https://api.lcxw.cn/other/clip.mp4", "video"),
                 ("https://sub.api.lcxw.cn/upload/mp4/clip.mp4", "video")]
        for url, kind in cases:
            with self.subTest(url=url, kind=kind):
                self.web.robots.clear()
                self.session.get.reset_mock()
                self.session.get.side_effect = [self.response(200, b"User-agent: *\nDisallow: /\n")]
                with self.assertRaisesRegex(CollectionError, "站点规则不允许"):
                    self.web.allowed(url, media_type=kind)
                self.assertEqual(self.session.get.call_count, 1)

    def test_video_exception_can_be_disabled(self):
        self.session.get.side_effect = [self.response(200, b"User-agent: *\nDisallow: /\n")]
        with patch.object(config, "VIDEO_ROBOTS_WARNING_ONLY", {}):
            with self.assertRaisesRegex(CollectionError, "站点规则不允许"):
                self.web.allowed("https://api.lcxw.cn/upload/mp4/clip.mp4", media_type="video")

    def test_media_denial_remains_effective_inside_exception(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.web.robots.clear()
                self.session.get.reset_mock()
                self.session.get.side_effect = [self.response(200, b"User-agent: *\nDisallow: /\n"),
                                               self.response(status)]
                with tempfile.TemporaryDirectory() as temp:
                    dest = Path(temp) / "clip.mp4"
                    with self.assertRaisesRegex(CollectionError, f"媒体文件返回 HTTP {status}"):
                        self.web.download("https://api.lcxw.cn/upload/mp4/clip.mp4", dest,
                                          "video", "https://www.lcxw.cn/article.html")
                    self.assertFalse(dest.exists())
                self.assertEqual(self.session.get.call_count, 2)

    def test_exception_does_not_allow_denied_redirect_target(self):
        redirect = self.response(302)
        redirect.is_redirect = True
        redirect.headers["Location"] = "https://restricted.example/clip.mp4"
        self.session.get.side_effect = [self.response(200, b"User-agent: *\nDisallow: /\n"),
                                       redirect, self.response(200, b"User-agent: *\nDisallow: /\n")]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CollectionError, "站点规则不允许"):
                self.web.download("https://api.lcxw.cn/upload/mp4/clip.mp4", Path(temp) / "clip.mp4",
                                  "video", "https://www.lcxw.cn/article.html")
        self.assertNotIn("https://restricted.example/clip.mp4",
                         [call.args[0] for call in self.session.get.call_args_list])

    def test_robots_server_error_html_and_timeout_remain_unconfirmed(self):
        import requests
        for response in (self.response(503), self.response(200, b"<html>Error</html>", "text/html"),
                         requests.ReadTimeout()):
            with self.subTest(response=response):
                self.web.robots.clear()
                self.session.get.reset_mock()
                transient = isinstance(response, requests.ReadTimeout) or response.status_code == 503
                self.session.get.side_effect = [response] * (2 if transient else 1)
                with tempfile.TemporaryDirectory() as temp:
                    with self.assertRaisesRegex(CollectionError, "站点访问规则暂时无法确认"):
                        self.download(temp)
                self.assertEqual(self.session.get.call_count, 2 if transient else 1)

    def test_robots_403_is_cached_and_not_retried_for_every_video(self):
        self.session.get.side_effect = [self.response(403), self.response(200, b"a", "video/mp4"),
                                       self.response(200, b"b", "video/mp4")]
        with tempfile.TemporaryDirectory() as temp:
            self.download(temp, "one.mp4")
            self.download(temp, "two.mp4")
        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(len(self.messages), 1)

    def test_redirect_target_rules_checked_before_following_media_redirect(self):
        redirect = self.response(302)
        redirect.is_redirect = True
        redirect.headers["Location"] = "https://restricted.example/clip.mp4"
        self.session.get.side_effect = [self.response(403), redirect,
            self.response(200, b"User-agent: *\nDisallow: /\n")]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CollectionError, "站点规则不允许"):
                self.download(temp)
        urls = [call.args[0] for call in self.session.get.call_args_list]
        self.assertNotIn("https://restricted.example/clip.mp4", urls)


if __name__ == "__main__":
    unittest.main()
