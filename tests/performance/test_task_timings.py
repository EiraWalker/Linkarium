"""Per-task accounting, paired request stages and offline HTML integrity."""
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from performance_inventory import timestamp
from performance_tasks_report import background_tasks, browser_tasks, merge_request_tasks, render


class TaskTimingContracts(unittest.TestCase):
    def test_retry_waits_and_nested_phases_do_not_double_count_execution(self):
        inventory = {'jobs': {'details': [{'id': 'job-1', 'kind': 'capture', 'state': 'succeeded',
            'created_at': '2026-10-16T00:00:00+00:00'}]}, 'attempts': {'details': [
            {'job_id': 'job-1', 'attempt_id': 'attempt-1', 'attempt_no': 1, 'created_at': '2026-10-16T00:00:02+00:00',
             'finished_at': '2026-10-16T00:00:07+00:00', 'execution_ms': 5000, 'initial_queue_ms': 2000},
            {'job_id': 'job-1', 'attempt_id': 'attempt-2', 'attempt_no': 2, 'created_at': '2026-10-16T00:00:10+00:00',
             'finished_at': '2026-10-16T00:00:14+00:00', 'execution_ms': 4000, 'wait_since_previous_finish_ms': 3000}]},
            'capture_diagnostics': {'details': [{'attempt_id': 'attempt-1', 'phases': [
                {'stage': 'navigation', 'start_ms': 0, 'duration_ms': 2000},
                {'stage': 'archive', 'start_ms': 2000, 'duration_ms': 3000}]}]}}
        row = background_tasks(inventory, [], timestamp('2026-10-16T00:00:00+00:00'), timestamp('2026-10-16T00:01:00+00:00'))[0]
        self.assertEqual((row['queue_ms'], row['execution_ms'], row['total_ms']), (5000, 9000, 14000))
        self.assertEqual(row['stages'][1]['offset_ms'], 2000)
        self.assertEqual(row['source'], '本次')

    def test_cancelled_without_a_final_clock_preserves_missing_duration(self):
        row = background_tasks({'jobs': {'details': [{'id': 'cancelled', 'kind': 'capture', 'state': 'cancelled'}]}}, [], None, None)[0]
        self.assertIsNone(row['total_ms'])
        self.assertIsNone(row['execution_ms'])
        self.assertEqual(row['stages'], [])

    def test_browser_milestones_and_request_pairs_preserve_their_time_origins(self):
        rows = browser_tasks({'runs': [{'device': 'desktop', 'cache': 'cold', 'scenario': 'library', 'status': 'complete',
            'duration_ms': 700, 'preview': {'first_ready_ms': 200, 'image_samples': [{'sample': 1,
                'thumbnail_headers_ms': 180, 'thumbnail_body_end_ms': 185, 'decode_ms': 192, 'paint_opportunity_ms': 202}]},
            'network': [{'route': '/api/v1/bookmarks', 'request_samples': [
                {'duration_ms': 100, 'headers_elapsed_ms': 80, 'status': 200}]}]}]})
        image_parts = [row for row in rows[0]['stages'] if row['depth'] == 1]
        self.assertEqual([row['duration_ms'] for row in image_parts], [5, 7, 10])
        milestone = next(row for row in rows[0]['stages'] if row['name'] == '首张预览就绪')
        self.assertEqual(milestone['offset_ms'], 200)
        self.assertIsNone(milestone['duration_ms'])
        self.assertEqual([row['duration_ms'] for row in rows[1]['stages']], [80, 20])

    def test_json_payload_cannot_close_its_script_and_preserves_search_text(self):
        malicious = '</script><img src=x onerror=alert(1)>'
        report = {'tasks': [{'name': malicious}], 'started_at': None, 'finished_at': None}
        document = render(report)
        self.assertNotIn(malicious, document)
        payload = re.search(r'<script type="application/json" id="data">(.*?)</script>', document, re.S)[1]
        self.assertEqual(json.loads(payload)['tasks'][0]['name'], malicious)

    def test_client_server_request_ids_merge_without_duplicate_tasks_or_clocks(self):
        client = {'id': 'client', 'request_id': 'abcd-1234', 'total_ms': 100,
            'execution_ms': 50, 'stages': [{'name': '响应体传输', 'duration_ms': 20}]}
        server = {'id': 'abcd1234', 'execution_ms': 52, 'started_at': '2026-10-16T00:00:00+00:00',
            'stages': [{'name': 'SQL 执行', 'duration_ms': 8, 'offset_ms': 0}]}
        tasks = merge_request_tasks([server], [client])
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]['execution_ms'], tasks[0]['total_ms']), (52, 100))
        self.assertEqual(tasks[0]['stages'][-1]['clock'], 'backend')


if __name__ == '__main__':
    unittest.main()
