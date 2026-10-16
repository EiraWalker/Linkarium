"""Actual SVG geometry: start offsets, independent clocks and concurrency."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from performance_tasks_report import render


def task(identity, category, start, duration, stages):
    return {'id': identity, 'name': identity, 'category': category, 'source': '本次',
        'variant': '', 'state': 'complete', 'started_at': start, 'execution_ms': duration,
        'total_ms': duration, 'queue_ms': None, 'stages': stages, 'stage_count': len(stages)}


def phase(label, offset, duration, mode='区间', **extra):
    return {'name': label, 'offset_ms': offset, 'duration_ms': duration,
        'depth': 0, 'mode': mode, **extra}


class TimingChartIntegration(unittest.TestCase):
    def test_waterfall_geometry_and_absolute_timeline_do_not_invent_clocks(self):
        from playwright.sync_api import sync_playwright
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT/'tools/browsers'))
        data = {'started_at': '2026-10-16T00:00:00+00:00', 'finished_at': '2026-10-16T00:00:01+00:00',
            'measurement_mode': '隔离图表测试', 'tasks': [
                task('page', '页面显示', None, 60, [phase('first', 0, 10), phase('second', 20, 20),
                    phase('paint', 60, None, '时间点'), phase('unknown start', None, 5), phase('unknown end', 40, None)]),
                task('api-a', 'API 请求', '2026-10-16T00:00:00+00:00', 100, [phase('网络请求', 0, 120),
                    phase('处理请求', 0, 100, clock='backend'), phase('SQL 执行', None, 10, clock='backend')]),
                task('api-b', 'API 请求', '2026-10-16T00:00:00.050+00:00', 100, [phase('请求', 0, 100)])]}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'charts.html'
            target.write_text(render(data), encoding='utf-8')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    errors, remote = [], []
                    page.on('pageerror', lambda error: errors.append(type(error).__name__))
                    page.on('request', lambda request: remote.append(request.method) if request.url.startswith(('http:', 'https:')) else None)
                    page.goto(target.as_uri())
                    bar = page.locator('.waterfall-bar[data-start-ms="20"]')
                    self.assertAlmostEqual(float(bar.get_attribute('x')), 10+400*20/60)
                    self.assertAlmostEqual(float(bar.get_attribute('width')), 400*20/60)
                    self.assertEqual(page.locator('.milestone[data-start-ms="60"]').count(), 1)
                    self.assertEqual(page.locator('.uncertain[data-start-ms="40"]').count(), 1)
                    self.assertEqual(page.locator('.waterfall-bar[data-duration-ms="5"]').count(), 0)
                    self.assertIn('起点未记录', page.locator('.detail').inner_text())
                    page.locator('#timeline-toggle').click()
                    self.assertEqual(page.locator('#timeline .timeline-bar').count(), 2)
                    first = page.locator('.timeline-bar[data-task-id="api-a"]')
                    second = page.locator('.timeline-bar[data-task-id="api-b"]')
                    first_x, second_x = float(first.get_attribute('x')), float(second.get_attribute('x'))
                    self.assertGreater(second_x, first_x)
                    self.assertLess(second_x, first_x+float(first.get_attribute('width')))
                    page.select_option('#category', 'API 请求')
                    page.locator('[data-task="api-a"] button').click()
                    self.assertEqual(page.locator('.detail .waterfall-group').count(), 2)
                    self.assertIn('服务端时间轴（独立起点）', page.locator('.detail').inner_text())
                    self.assertEqual(page.locator('.detail .waterfall-bar[data-duration-ms="10"]').count(), 0)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'))
                    self.assertEqual(errors, [])
                    self.assertEqual(remote, [])
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()
