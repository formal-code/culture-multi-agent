"""Verify both the visible progress card and the user-facing error notice."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import app_config
from team_interfaces import IntegrationPending, NoSuitableMaterials, ResearchPackage
from workflow import WorkflowState


ROOT = Path(__file__).resolve().parents[1]


class WorkflowErrorUITests(unittest.TestCase):
    def test_research_lists_images_and_videos_even_without_reference_video_records(self):
        state = WorkflowState('给我生成一个关于聊城东昌湖的视频，面向大众')
        state.research = {'reference_videos': [], 'media_assets': [
            {'media_type': 'image', 'title': '东昌湖湖景', 'source_url': 'https://example.org/lake.html'},
            {'media_type': 'image', 'title': '湖畔景观', 'source_url': 'https://example.org/lake.html'},
            {'media_type': 'video', 'title': '东昌湖游览', 'source_url': 'https://example.org/lake.html'}]}
        app = AppTest.from_file(str(ROOT / 'streamlit_app.py'), default_timeout=15)
        app.session_state['workflow'] = state
        app.run()
        self.assertFalse(app.exception)
        self.assertIn('已收集 3 份素材：图片 2 张，视频 1 段', [item.value for item in app.caption])
        self.assertIn('图片 · 东昌湖湖景', [item.value for item in app.markdown])
        self.assertIn('视频 · 东昌湖游览', [item.value for item in app.markdown])
        self.assertNotIn('暂无参考视频记录', [item.value for item in app.caption])

    def create_app(self):
        app = AppTest.from_file(str(ROOT / 'streamlit_app.py'), default_timeout=15)
        app.run()
        app.text_area[0].input('生成一条聊城光岳楼的视频')
        return app

    def stage_markup(self, app):
        return '\n'.join(str(item.value) for item in app.markdown if 'class="stage stage-' in str(item.value))

    def test_empty_research_shows_warning_and_amber_stage(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app_config, 'PIPELINE_MODE', 'agents'), \
             patch.object(app_config, 'GENERATED_DIR', Path(directory)), \
             patch('team_interfaces.collect_research', side_effect=NoSuitableMaterials('内部诊断')):
            app = self.create_app()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('未收集到合适素材' in item.value for item in app.warning))
            self.assertFalse(app.error)
            markup = self.stage_markup(app)
            self.assertIn('stage-no_materials', markup)
            self.assertIn('未收集到合适素材', markup)
            self.assertNotIn('内部诊断', markup)
            self.assertNotIn('stage-error', markup)
            self.assertEqual(app.session_state['workflow'].stages['planning'][0], 'pending')
            app.run()
            self.assertFalse(app.exception)
            self.assertIn('stage-no_materials', self.stage_markup(app))

    def test_service_failure_shows_error_and_red_stage(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app_config, 'PIPELINE_MODE', 'agents'), \
             patch.object(app_config, 'GENERATED_DIR', Path(directory)), \
             patch('team_interfaces.collect_research', side_effect=RuntimeError('private-service-diagnostic')):
            app = self.create_app()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('创作过程中出现异常' in item.value for item in app.error))
            self.assertFalse(app.warning)
            markup = self.stage_markup(app)
            self.assertIn('stage-error', markup)
            self.assertIn('当前步骤执行异常', markup)
            self.assertNotIn('private-service-diagnostic', markup)
            self.assertNotIn('stage-no_materials', markup)

    def test_pending_integration_keeps_info_notice(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app_config, 'PIPELINE_MODE', 'agents'), \
             patch.object(app_config, 'GENERATED_DIR', Path(directory)), \
             patch('team_interfaces.collect_research', side_effect=IntegrationPending('接口尚未接入')):
            app = self.create_app()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any(item.value == '接口尚未接入' for item in app.info))
            self.assertFalse(app.error)
            self.assertFalse(app.warning)
            self.assertIn('stage-waiting', self.stage_markup(app))

    def test_planning_error_preserves_completed_research(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app_config, 'PIPELINE_MODE', 'agents'), \
             patch.object(app_config, 'GENERATED_DIR', Path(directory)), \
             patch('team_interfaces.collect_research', return_value=ResearchPackage(
                 {'schema_version': '1.0', 'reference_videos': [], 'media_assets': []}, Path(directory))), \
             patch('team_interfaces.plan_scripts', side_effect=RuntimeError('策划中断')):
            app = self.create_app()
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any('已完成的结果会保留' in item.value for item in app.error))
            state = app.session_state['workflow']
            self.assertEqual(state.stages['research'][0], 'complete')
            self.assertEqual(state.stages['planning'][0], 'error')
            self.assertIsNotNone(state.research)


if __name__ == '__main__':
    unittest.main()
