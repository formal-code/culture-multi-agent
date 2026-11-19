"""Offline regression tests. Model answers are simulated; never claim live model accuracy."""
import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from evaluation_agent.video_review.client import OmniClient, ReviewAPIError, atomic_json, media_item
from evaluation_agent.config import load_env
from evaluation_agent.video_review.config import ReviewSettings
from evaluation_agent.video_review.context import collect_context, source_record
from evaluation_agent.video_review.media import ffmpeg, probe, render_record, sha256, windows
from evaluation_agent.video_review.models import (AudioObservation, DIMENSIONS, VisualObservation,
                                                 check_audio, check_visual)
from evaluation_agent.video_review.pipeline import (build_evidence, review_video, understand_video,
                                                    validate_dimension)


class FakeClient:
    def __init__(self, invalid_dimension=None, failed_stage=None):
        self.stages = []
        self.invalid_dimension, self.failed_stage = invalid_dimension, failed_stage

    def ask(self, stage, instruction, schema, *, media=None, check=None):
        self.stages.append(stage)
        if self.failed_stage and self.failed_stage in stage:
            raise ReviewAPIError(stage, "PermissionDeniedError", 403, "insufficient_quota", "unit-test-id")
        if stage.startswith("画面"):
            result = {"accessed": True, "scenes": [{"start": 0, "end": .1,
                      "description": "[模拟测试] 蓝色背景", "on_screen_text": []}], "uncertainty": []}
        elif stage.startswith("音频"):
            result = {"accessed": True, "transcript": "", "sound_description": "[模拟测试] 连续测试音", "uncertainty": []}
        else:
            finding = {"conclusion": "[模拟测试] 判断依据存在；不代表真实模型表现",
                       "evidence_ids": ["V001"], "nature": "合理推断", "uncertainty": "模拟回答"}
            if stage.startswith("单维"):
                result = finding
            else:
                dimensions = {k: copy.deepcopy(finding) for k in DIMENSIONS}
                if self.invalid_dimension:
                    dimensions[self.invalid_dimension]["evidence_ids"] = ["DOES_NOT_EXIST"]
                result = {"overall": "[模拟测试] 完整流程结构测试，不是视频实际评价",
                          "dimensions": dimensions, "strengths": [], "limitations": ["模型已被替换为模拟对象"]}
        value = schema.model_validate(result)
        if check:
            check(value)
        return value.model_dump()


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="video_review_tests_")
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / "中文 成片.mp4"
        ffmpeg(["-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=1.2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=1.2",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", cls.video])
        cls.settings = ReviewSettings(api_key="unit-test-never-network", chunk_seconds=5)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def new_output(self):
        return Path(tempfile.mkdtemp(dir=self.root))

    def test_explicit_env_file_overrides_stale_process_values(self):
        env_file = self.new_output() / "c_agent.env"
        env_file.write_text(
            "DATAOPENAI_API_KEY=new-placeholder\nOMNI_MODEL=qwen3.8-omni-flash\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"DATAOPENAI_API_KEY": "stale-placeholder", "OMNI_MODEL": "old-model"}):
            load_env(env_file, override=True)
            settings = ReviewSettings.from_env()
            self.assertEqual(settings.api_key, "new-placeholder")
            self.assertEqual(settings.model, "qwen3.8-omni-flash")
            self.assertNotIn("new-placeholder", repr(settings))

    def test_actual_media_understanding_and_resume(self):
        before = sha256(self.video)
        fake, output = FakeClient(), self.new_output()
        directory, result = understand_video(self.video, output, self.settings, client=fake)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["video"]["has_audio_stream"])
        self.assertEqual(result["coverage"]["audio_chunks_read"], 1)
        self.assertFalse(probe(directory / "media/001/visual.mp4")["has_audio_stream"])
        import wave
        with wave.open(str(directory / "media/001/audio.wav")) as sound:
            self.assertEqual(sound.getframerate(), 16000)
            self.assertGreater(sound.getnframes(), 10000)
        self.assertEqual(before, sha256(self.video))
        calls = len(fake.stages)
        _, result2 = understand_video(self.video, output, self.settings, client=fake)
        self.assertEqual(result2["status"], "complete")
        self.assertEqual(calls, len(fake.stages))

    def test_quota_failure_never_fakes_report(self):
        fake = FakeClient(failed_stage="画面")
        directory, result = review_video(self.video, self.new_output(), self.settings, client=fake)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["dimensions"])
        self.assertEqual(result["api_error"]["code"], "insufficient_quota")
        self.assertEqual(len(fake.stages), 1)
        self.assertTrue((directory / "analysis_report.md").is_file())

    def test_invalid_dimension_retries_only_that_dimension_and_resume_is_stable(self):
        fake, output = FakeClient(invalid_dimension="pacing"), self.new_output()
        context = {"prompt": "给孩子看的观察游戏", "topic": "测试景点"}
        directory, result = review_video(self.video, output, self.settings, context=context, client=fake)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["dimensions"]), 6)
        self.assertEqual([s for s in fake.stages if s.startswith("单维")], ["单维修正_pacing"])
        calls = len(fake.stages)
        directory2, result2 = review_video(self.video, output, self.settings, context=context, client=fake)
        self.assertEqual(directory, directory2)
        self.assertEqual(calls, len(fake.stages))
        self.assertFalse(result2["prediction"]["enabled"])
        directory3, _ = review_video(self.video, output, self.settings, context={"prompt": "另一要求"}, client=fake)
        self.assertNotEqual(directory, directory3)

    def test_silent_video_is_not_audio_failure(self):
        silent = self.new_output() / "静音.mp4"
        ffmpeg(["-v", "error", "-i", self.video, "-an", "-c:v", "copy", "-y", silent])
        _, result = understand_video(silent, self.new_output(), self.settings, client=FakeClient())
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["coverage"]["audio_chunks_expected"], 0)
        self.assertEqual(result["chunks"][0]["audio"]["status"], "no_audio_stream")

    def test_source_topic_mismatch_cannot_reuse(self):
        directory = self.new_output()
        atomic_json(directory / "input_packet.json", {"topic": "光岳楼"})
        atomic_json(directory / "sources.json", [{"title": "旧主题", "excerpt": "旧资料", "retrieval_status": "success"}])
        _, evidence, notes = collect_context({"topic": "东昌府木版年画", "research_directory": str(directory)})
        self.assertFalse(any(k.startswith("S") for k in evidence))
        self.assertTrue(any("不匹配" in n for n in notes))
        _, evidence2, _ = collect_context({"topic": "光岳楼", "research_directory": str(directory)})
        self.assertEqual(evidence2["S1"]["kind"], "source_body")

    def test_source_snippet_not_read_body(self):
        row = {"search_snippet": "摘要", "retrieval_status": "failed"}
        self.assertEqual(source_record(row, "S", 1)["kind"], "search_snippet")
        finding = {"conclusion": "主张", "evidence_ids": ["S1"], "nature": "来源记载"}
        with self.assertRaises(ValueError):
            validate_dimension("local_culture", finding, {"S1": {"kind": "search_snippet"}})

    def test_unknown_evidence_and_invalid_times_rejected(self):
        with self.assertRaises(ValueError):
            validate_dimension("opening", {"conclusion": "x", "evidence_ids": ["X"], "nature": "合理推断"}, {})
        with self.assertRaisesRegex(ValueError, "旨在.*推断性表述"):
            validate_dimension("opening", {"conclusion": "这句话旨在形成悬念", "evidence_ids": ["V001"],
                                             "nature": "成片观察"},
                               {"V001": {"kind": "video_observation"}})
        with self.assertRaisesRegex(ValueError, "程序切片边界"):
            validate_dimension("pacing", {"conclusion": "音频在30秒处有较长停顿", "evidence_ids": ["A001"],
                                             "nature": "成片观察"},
                               {"A001": {"kind": "audio_observation", "start_seconds": 0, "end_seconds": 30},
                                "A002": {"kind": "audio_observation", "start_seconds": 30, "end_seconds": 60}})
        with self.assertRaises(ValueError):
            check_visual(VisualObservation(accessed=True, scenes=[{"start": 1, "end": 3, "description": "x"}]), 2)
        with self.assertRaises(ValueError):
            check_audio(AudioObservation(accessed=True))
        with self.assertRaises(ValidationError):
            VisualObservation(accessed=True, scenes=[{"start": float('nan'), "end": 1, "description": "x"}])

    def test_windows_preserve_tail_and_offsets(self):
        self.assertEqual(windows(60.03, 20), [(0, 20), (20, 40), (40, 60.03)])
        understanding = {"video": {}, "chunks": [{"index": 2, "start_seconds": 20, "end_seconds": 40,
            "visual": {"accessed": True, "scenes": [{"start": 1, "end": 3, "description": "x"}], "uncertainty": []}}]}
        _, evidence, _ = build_evidence(understanding, {})
        self.assertEqual(evidence["V002"]["scenes"][0]["start"], 21)

    def test_overlapping_audio_boundary_dedup_keeps_raw_observation(self):
        chunks = [
            {"index": 1, "start_seconds": 0, "end_seconds": 30, "audio": {
                "accessed": True, "transcript": "颜色？第", "sound_description": "女声；结尾处‘第’字后语音突然中断，疑似被截断。",
                "uncertainty": ["结尾‘第’字后内容缺失"]}},
            {"index": 2, "start_seconds": 30, "end_seconds": 60, "audio": {
                "accessed": True, "transcript": "第三章开始", "sound_description": "女声清楚。", "uncertainty": []}},
        ]
        understanding = {"video": {}, "chunks": chunks}
        _, evidence, _ = build_evidence(understanding, {})
        self.assertEqual(evidence["A001"]["text"], "颜色？")
        self.assertEqual(evidence["A002"]["text"], "第三章开始")
        self.assertEqual(chunks[0]["audio"]["transcript"], "颜色？第")
        self.assertNotIn("中断", evidence["A001"]["sound_description"])
        self.assertFalse(evidence["A001"]["uncertainty"])

    def test_generation_record_mismatch_not_used(self):
        directory = self.new_output()
        target = directory / "v.mp4"
        atomic_json(target.with_suffix(".render.json"), {"duration_seconds": 100})
        self.assertEqual(render_record(target, {"duration_seconds": 5})["status"], "mismatch_or_invalid")

    def test_media_request_format(self):
        file = self.new_output() / "中文.wav"
        file.write_bytes(b'not-a-real-audio-only-format-test')
        item = media_item(file, "audio")
        self.assertEqual(item["type"], "input_audio")
        self.assertTrue(item["input_audio"]["data"].startswith("data:;base64,"))

    def test_quota_error_sanitized(self):
        directory = self.new_output()
        client = OmniClient(self.settings, directory)
        error = RuntimeError('secret data must never be saved')
        error.status_code = 403
        error.body = {"code": "insufficient_quota", "message": 'secret data must never be saved'}
        error.request_id = 'test-request'
        with patch.object(client.sdk.chat.completions, 'create', side_effect=error):
            with self.assertRaises(ReviewAPIError) as raised:
                client.ask('test', 'test', AudioObservation)
        self.assertEqual(raised.exception.code, 'insufficient_quota')
        text = next(directory.glob('*_error.json')).read_text(encoding='utf-8')
        self.assertNotIn('secret data', text)
        self.assertNotIn(self.settings.api_key, text)

    def test_workflow_failure_keeps_video(self):
        from workflow import WorkflowState, run_video_review
        state = WorkflowState(prompt='x')
        state.videos['douyin'] = {'path': str(self.video)}
        original = copy.deepcopy(state.videos)
        with patch('team_interfaces.analyze_finished_video', side_effect=ValueError('test error')):
            with self.assertRaises(ValueError):
                run_video_review(state, 'douyin')
        self.assertEqual(state.videos, original)
        self.assertEqual(state.video_reviews['douyin']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
