"""Image loading clocks, relative to the first thumbnail request, not navigation."""
import math
import statistics


def value(number):
    return float(number) if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number) and number >= 0 else None


def difference(end, start):
    end, start = value(end), value(start)
    return end-start if end is not None and start is not None and end >= start else None


def summarize_images(run):
    resources = []
    for group in run.get('resources', []):
        if '/snapshots/' not in group.get('route', '') or '/thumbnail' not in group.get('route', ''):
            continue
        for item in group.get('samples', []):
            start, end = value(item.get('start_ms')), value(item.get('body_end_ms'))
            if start is not None and end is not None and end >= start:
                resources.append(item)
    resources.sort(key=lambda item: item['start_ms'])
    preview = run.get('preview', {})
    first = resources[0]['start_ms'] if resources else None
    last = max((item['body_end_ms'] for item in resources), default=None)
    events = sorted([(row['start_ms'], 1) for row in resources if row['body_end_ms'] > row['start_ms']] +
                    [(row['body_end_ms'], -1) for row in resources if row['body_end_ms'] > row['start_ms']])
    current = peak = 0
    for _, change in events:
        current += change
        peak = max(peak, current)
    complete = bool(resources and preview.get('available', 0) > 0 and preview.get('success') == preview.get('available')
                    and not preview.get('errors') and not preview.get('pending'))
    gaps = [difference(b['start_ms'], a['body_end_ms']) for a, b in zip(resources, resources[1:])]
    gaps_total = sum(gaps) if peak == 1 and all(gap is not None for gap in gaps) else None
    # Resource names are deliberately redacted. Only join an unambiguous timestamp.
    observations, ambiguous = {}, set()
    for image in preview.get('image_samples', []):
        if value(image.get('thumbnail_body_end_ms')) is None:
            continue
        key = round(image['thumbnail_body_end_ms'], 6)
        if key in observations:
            ambiguous.add(key)
        observations[key] = image
    for key in ambiguous:
        observations.pop(key, None)
    body_durations = [difference(row['body_end_ms'], row.get('headers_ms')) for row in resources]
    request_details = []
    for index, row in enumerate(resources):
        image = observations.get(round(row['body_end_ms'], 6), {})
        request_details.append({'number': index+1, 'start_ms': difference(row['start_ms'], first),
            'headers_ms': difference(row.get('headers_ms'), first), 'end_ms': difference(row['body_end_ms'], first),
            'duration_ms': difference(row['body_end_ms'], row['start_ms']),
            'observed_ready_ms': difference(image.get('observed_ms'), first),
            'body_to_observed_ready_ms': difference(image.get('observed_ms'), row['body_end_ms'])})
    return {'device': run.get('device'), 'cache': run.get('cache'), 'status': 'complete' if complete else 'incomplete',
        'image_count': preview.get('success', 0), 'available_images': preview.get('available', 0),
        'request_count': len(resources), 'first_request_from_navigation_ms': first,
        'first_request_to_all_ready_ms': difference(preview.get('all_ready_ms'), first) if complete else None,
        'first_request_to_all_paint_ms': difference(preview.get('all_paint_opportunity_ms'), first) if complete else None,
        'first_request_to_last_body_ms': difference(last, first),
        'last_request_start_ms': difference(resources[-1]['start_ms'], first) if resources else None,
        'peak_fetch_concurrency': peak, 'request_sum_ms': sum(row['duration_ms'] for row in request_details) if resources else None,
        'serial_request_gaps_ms': gaps_total,
        'last_body_to_all_ready_ms': difference(preview.get('all_ready_ms'), last) if complete else None,
        'median_request_ms': statistics.median(row['duration_ms'] for row in request_details) if request_details else None,
        'response_body_sum_ms': sum(body_durations) if resources and all(part is not None for part in body_durations) else None,
        'transfer_bytes': sum(row.get('transfer_bytes', 0) for row in resources),
        'encoded_bytes': sum(row.get('encoded_bytes', 0) for row in resources), 'requests': request_details}
