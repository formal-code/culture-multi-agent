"""Exercise automatic review display without a model call or real generation service."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from evaluation_agent.video_review.media import ffmpeg
from evaluation_agent.video_review.models import DIMENSIONS
from workflow import WorkflowState


ROOT = Path(__file__).resolve().parents[1]


class VideoReviewUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='video_review_ui_')
        cls.root = Path(cls.temp.name)
        cls.clip = cls.root / 'clip.mp4'
        ffmpeg(['-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=64x96:r=10:d=0.5',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', cls.clip])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def make_state(self):
        state = WorkflowState(prompt='模拟页面测试：亲子观察视频')
        content = {'title': '页面测试', 'body': '模拟测试内容', 'hashtags': [], 'aspect_ratio': '9:16',
                   'total_duration': 0.5, 'segments': []}
        state.adaptations = {'platforms': {p: copy.deepcopy(content) for p in ('douyin', 'kuaishou', 'xiaohongshu')}}
        state.research = {}
        state.assets_base_dir = self.root
        return state

    def generate(self, *args, output_path, **kwargs):
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.clip, output)
        output.with_suffix('.render.json').write_text(json.dumps({'duration_seconds': 0.5, 'audio': 'silent'}), encoding='utf-8')
        return output, [], []

    def review(self, state, platform, progress):
        progress('模拟页面测试：读取声音与画面')
        result = {
            'status': 'complete', 'understanding_status': 'complete',
            'coverage': {'visual_chunks_read': 1, 'expected_chunks': 1, 'audio_chunks_read': 0, 'audio_chunks_expected': 0},
            'overall': '模拟页面测试：总体判断已展示',
            'dimensions': {key: {'conclusion': f'模拟页面测试：{label}结论', 'nature': '合理推断',
                                 'evidence_ids': ['V001'], 'uncertainty': '模拟测试不是实际评价'}
                           for key, label in DIMENSIONS.items()},
            'evidence': {'V001': {'kind': 'video_observation', 'start_seconds': 0, 'end_seconds': 0.5,
                                  'scenes': [{'start': 0, 'end': 0.5, 'description': '模拟测试画面',
                                              'on_screen_text': ['测试字幕']}] }},
            'strengths': [], 'limitations': ['这是模拟页面测试'],
        }
        directory = self.root / state.job_id / 'report'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'analysis_report.md').write_text(result['overall'], encoding='utf-8')
        (directory / 'analysis_report.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        state.video_reviews[platform] = {'status': 'complete', 'video_path': state.videos[platform]['path'],
                                         'directory': str(directory), 'result': result}
        return result

    def test_generation_automatically_shows_report_below_video_and_in_summary(self):
        state = self.make_state()
        app = AppTest.from_file(str(ROOT / 'streamlit_app.py'), default_timeout=15)
        app.session_state['workflow'] = state
        with patch('video_generator.generate_platform_video', side_effect=self.generate), \
             patch('workflow.run_video_review', side_effect=self.review) as review, \
             patch('app_config.GENERATED_DIR', self.root / 'generated'), \
             patch('app_config.VIDEO_REVIEW_AUTO', True):
            app.run()
            app.button(key=f'video_{state.job_id}_douyin').click().run()
            self.assertFalse(app.exception)
            review.assert_called_once()
            douyin = next(tab for tab in app.tabs if tab.label == '抖音')
            summary = next(tab for tab in app.tabs if tab.label == '测评与热度')
            for section in (douyin, summary):
                texts = [str(item.value) for item in section.markdown]
                self.assertIn('模拟页面测试：总体判断已展示', texts)
                for label in DIMENSIONS.values():
                    self.assertIn(f'模拟页面测试：{label}结论', texts)
            self.assertTrue(douyin.get('video'))
            self.assertEqual(len(douyin.get('download_button')), 3)
            self.assertEqual(len(summary.get('download_button')), 2)
            app.run()
            self.assertFalse(app.exception)
            review.assert_called_once()

    def test_analysis_failure_keeps_video_and_manual_retry_updates_both_views(self):
        state = self.make_state()
        app = AppTest.from_file(str(ROOT / 'streamlit_app.py'), default_timeout=15)
        app.session_state['workflow'] = state

        def fail_review(current, platform, progress):
            current.video_reviews[platform] = {'status': 'failed', 'video_path': current.videos[platform]['path'],
                                               'error': '模拟页面测试：暂时无法分析'}
            raise ValueError('simulated service failure')

        with patch('video_generator.generate_platform_video', side_effect=self.generate), \
             patch('workflow.run_video_review', side_effect=fail_review) as review, \
             patch('app_config.GENERATED_DIR', self.root / 'generated'), \
             patch('app_config.VIDEO_REVIEW_AUTO', True):
            app.run()
            app.button(key=f'video_{state.job_id}_douyin').click().run()
            self.assertFalse(app.exception)
            self.assertTrue(app.get('video'))
            review.assert_called_once()
            app.run()
            review.assert_called_once()
            review.side_effect = self.review
            app.button(key=f'review_evaluation_{state.job_id}_douyin').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(review.call_count, 2)
            for label in ('抖音', '测评与热度'):
                section = next(tab for tab in app.tabs if tab.label == label)
                self.assertIn('模拟页面测试：总体判断已展示', [str(item.value) for item in section.markdown])


if __name__ == '__main__':
    unittest.main()
