"""Keep empty research distinct from failures across the worker and workflow."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app_config
import team_ab_bridge as bridge
import team_ab_worker as worker
from team_interfaces import IntegrationPending, NoSuitableMaterials
from workflow import WorkflowState, run_content_workflow


class ResearchErrorTests(unittest.TestCase):
    def test_worker_marks_empty_research_with_a_stable_code(self):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('{}')), patch('sys.argv', ['worker', 'a']), \
             patch.object(worker, 'run_a', side_effect=NoSuitableMaterials('本轮无合格素材')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(worker.main(), 1)
        event = json.loads(output.getvalue().removeprefix(bridge.EVENT_PREFIX))
        self.assertEqual(event['kind'], 'error')
        self.assertEqual(event['code'], NoSuitableMaterials.code)

    def test_worker_does_not_mark_service_errors_as_empty_research(self):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('{}')), patch('sys.argv', ['worker', 'a']), \
             patch.object(worker, 'run_a', side_effect=RuntimeError('模型服务中断')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(worker.main(), 1)
        event = json.loads(output.getvalue().removeprefix(bridge.EVENT_PREFIX))
        self.assertNotIn('code', event)

    def run_worker_error(self, message, code=None):
        event = {'kind': 'error', 'message': message}
        if code is not None:
            event['code'] = code
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(bridge.EVENT_PREFIX + json.dumps(event) + '\n')
        process.poll.return_value = 1
        process.wait.return_value = 1
        with patch.object(bridge.subprocess, 'Popen', return_value=process):
            bridge.run_worker('a', {}, timeout=5)

    def test_parent_restores_empty_research_exception_from_worker_code(self):
        with self.assertRaises(NoSuitableMaterials):
            self.run_worker_error('本轮无合格素材', NoSuitableMaterials.code)

    def test_parent_does_not_classify_errors_using_message_keywords(self):
        with self.assertRaises(RuntimeError) as caught:
            self.run_worker_error('未找到合格素材：模型服务故障')
        self.assertNotIsInstance(caught.exception, NoSuitableMaterials)

    def test_empty_report_stops_before_reading_missing_research_files(self):
        with tempfile.TemporaryDirectory() as directory:
            delivery = Path(directory) / 'a_delivery'
            delivery.mkdir()
            bridge.write_json(delivery / 'collection_report.json', {'approved_counts': {'total': 0}})
            with patch.dict('os.environ', {'DATA_OPENAI_API_KEY': 'test', 'LLM_API_KEY': 'test'}), \
                 patch.object(bridge, 'run_worker', return_value={}):
                with self.assertRaises(NoSuitableMaterials):
                    bridge.collect_with_a('光岳楼', directory)

    def test_empty_assets_are_distinct_from_malformed_assets(self):
        with self.assertRaises(NoSuitableMaterials):
            bridge.normalize_research({'assets': []}, '.')
        with self.assertRaises(ValueError):
            bridge.normalize_research({'assets': 'invalid'}, '.')

    def test_unsupported_assets_are_reported_as_no_usable_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'asset.gif').write_bytes(b'test')
            with self.assertRaises(NoSuitableMaterials):
                bridge.normalize_research({'assets': [
                    {'id': 'a', 'media_type': 'image', 'file_path': 'asset.gif'}]}, directory)

    def test_workflow_reports_distinct_statuses_and_stops_downstream_calls(self):
        errors = [(NoSuitableMaterials('本轮无合格素材'), 'no_materials'),
                  (RuntimeError('模型中断'), 'error'),
                  (TimeoutError('采集超时'), 'error'),
                  (IntegrationPending('尚未接入'), 'waiting')]
        with tempfile.TemporaryDirectory() as directory:
            for error, status in errors:
                with self.subTest(status=status, error=type(error).__name__):
                    state = WorkflowState('光岳楼')
                    events = []
                    with patch.object(app_config, 'PIPELINE_MODE', 'agents'), \
                         patch.object(app_config, 'GENERATED_DIR', Path(directory)), \
                         patch('team_interfaces.collect_research', side_effect=error), \
                         patch('team_interfaces.plan_scripts') as plan:
                        with self.assertRaises(type(error)):
                            run_content_workflow(state, lambda *event: events.append(event))
                    self.assertEqual(state.stages['research'][0], status)
                    self.assertEqual(events[-1][1], status)
                    self.assertTrue(all(value[0] == 'pending' for key, value in state.stages.items()
                                        if key != 'research'))
                    plan.assert_not_called()

    def test_later_failure_keeps_completed_research(self):
        with tempfile.TemporaryDirectory() as directory:
            from team_interfaces import ResearchPackage
            research = {'schema_version': '1.0', 'reference_videos': [], 'media_assets': []}
            state = WorkflowState('光岳楼')
            with patch.object(app_config, 'PIPELINE_MODE', 'agents'), \
                 patch.object(app_config, 'GENERATED_DIR', Path(directory)), \
                 patch('team_interfaces.collect_research', return_value=ResearchPackage(research, Path(directory))), \
                 patch('team_interfaces.plan_scripts', side_effect=RuntimeError('策划失败')):
                with self.assertRaises(RuntimeError):
                    run_content_workflow(state, lambda *event: None)
            self.assertEqual(state.stages['research'][0], 'complete')
            self.assertEqual(state.stages['planning'][0], 'error')
            self.assertIs(state.research, research)


if __name__ == '__main__':
    unittest.main()
