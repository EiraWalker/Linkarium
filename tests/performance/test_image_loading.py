"""Image clocks must begin at the first request; parallel work cannot be added."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from performance_image_loading import summarize_images


def sample(start, end, headers=None):
    row = {'start_ms': start, 'body_end_ms': end, 'transfer_bytes': 300, 'encoded_bytes': 1000}
    if headers is not None:
        row['headers_ms'] = headers
    return row


def run(samples):
    return {'device': 'mobile', 'cache': 'warm',
            'resources': [{'route': '/api/v1/snapshots/:id/thumbnail', 'samples': samples}],
            'preview': {'available': 2, 'success': 2, 'pending': 0, 'errors': 0,
                        'all_ready_ms': 170, 'all_paint_opportunity_ms': 190}}


class ImageLoadingTests(unittest.TestCase):
    def test_serial_clock_and_decomposition(self):
        result = summarize_images(run([sample(100, 120, 115), sample(130, 160, 150)]))
        self.assertEqual(result['first_request_to_all_ready_ms'], 70)
        self.assertEqual(result['first_request_to_all_paint_ms'], 90)
        self.assertEqual(result['peak_fetch_concurrency'], 1)
        self.assertEqual(result['request_sum_ms'], 50)
        self.assertEqual(result['serial_request_gaps_ms'], 10)
        self.assertEqual(result['last_body_to_all_ready_ms'], 10)
        self.assertEqual(result['response_body_sum_ms'], 15)
        self.assertEqual(result['last_request_start_ms'], 30)
        self.assertEqual(result['requests'][0]['start_ms'], 0)

    def test_parallel_intervals_have_no_serial_gap_sum(self):
        result = summarize_images(run([sample(100, 150, 140), sample(120, 160, 150)]))
        self.assertEqual(result['peak_fetch_concurrency'], 2)
        self.assertIsNone(result['serial_request_gaps_ms'])
        self.assertGreater(result['request_sum_ms'], result['first_request_to_all_ready_ms'])

    def test_missing_clocks_or_incomplete_images_never_fabricate_zero(self):
        data = run([sample(100, 120), sample(130, 160)])
        result = summarize_images(data)
        self.assertIsNone(result['response_body_sum_ms'])
        data['preview']['success'] = 1
        result = summarize_images(data)
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['first_request_to_all_ready_ms'])
        self.assertIsNone(result['first_request_to_all_paint_ms'])
        result = summarize_images({})
        self.assertIsNone(result['first_request_to_all_ready_ms'])
        self.assertIsNone(result['request_sum_ms'])

    def test_invalid_samples_ignored_and_ambiguous_image_join_not_guessed(self):
        data = run([sample(100, 120), sample(100, 120), sample(150, 140), sample(float('nan'), 180)])
        image = {'thumbnail_body_end_ms': 120, 'observed_ms': 125}
        data['preview']['image_samples'] = [image, deepcopy(image)]
        result = summarize_images(data)
        self.assertEqual(result['request_count'], 2)
        self.assertTrue(all(row['observed_ready_ms'] is None for row in result['requests']))


if __name__ == '__main__':
    unittest.main()
