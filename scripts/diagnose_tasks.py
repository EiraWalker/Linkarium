"""Read-only task error export and optional isolated, non-publishing capture replay."""
import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from capture.diagnostics import host_only, error_evidence


@contextmanager
def read_database(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        yield connection
    finally:
        connection.close()


def collect(data_root, group_id=None):
    with read_database(data_root / 'state.sqlite3') as state:
        runtime = dict(state.execute('SELECT active_generation, library_epoch, worker_heartbeat FROM runtime_runtimestate WHERE id=1').fetchone())
    generation = runtime['active_generation']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', generation):
        raise ValueError('Invalid generation')
    with read_database(data_root / 'generations' / generation / 'library.sqlite3') as db:
        db.execute('BEGIN')
        if group_id:
            group = db.execute('SELECT * FROM library_jobgroup WHERE id=?', (UUID(group_id).hex,)).fetchone()
        else:
            group = db.execute("SELECT * FROM library_jobgroup WHERE kind='refresh' ORDER BY created_at DESC LIMIT 1").fetchone()
        if group is None:
            raise ValueError('No matching task group')
        rows = db.execute('''SELECT DISTINCT j.id, j.kind, j.target_id, j.variant, j.state, j.error_code,
            j.attempt_count, j.available_at, b.canonical_url FROM library_job j
            JOIN library_jobgroupmember m ON m.job_id=j.id
            LEFT JOIN library_bookmark b ON b.id=j.target_id WHERE m.group_id=? ORDER BY j.id''', (group['id'],)).fetchall()
        states = Counter(row['state'] for row in rows)
        errors = Counter(row['error_code'] for row in rows if row['error_code'])
        failures = []
        for row in rows:
            if not row['error_code']:
                continue
            item = dict(row)
            item['host'] = host_only(item.pop('canonical_url') or '')
            item['attempts'] = []
            for attempt in db.execute('SELECT id, attempt_no, state, error_code, created_at, finished_at, heartbeat_at, lease_expires_at FROM library_jobattempt WHERE job_id=? ORDER BY attempt_no', (row['id'],)):
                detail = dict(attempt)
                trace = data_root / 'diagnostics' / 'capture' / (str(UUID(attempt['id']))+'.json')
                detail['diagnostic_available'] = trace.is_file()
                if trace.is_file() and trace.stat().st_size < 128*1024:
                    try:
                        detail['diagnostic'] = json.loads(trace.read_text('utf-8'))
                        detail['diagnostic_incomplete'] = (detail['diagnostic'].get('state') == 'running' and attempt['state'] != 'running')
                    except (OSError, ValueError):
                        detail['diagnostic_available'] = False
                item['attempts'].append(detail)
            failures.append(item)
    return {'collected_at': datetime.now(timezone.utc).isoformat(), 'runtime': runtime,
            'group': dict(group), 'total_device_jobs': len(rows), 'states': dict(states),
            'errors': dict(errors), 'failed_jobs': failures}


def replay(data_root, group, job_id, output, dns_mode, proxy_url=None):
    from capture.models import CaptureRequest
    from capture.development import _run_capture_process
    runtime = group['runtime']
    with read_database(data_root / 'generations' / runtime['active_generation'] / 'library.sqlite3') as db:
        row = db.execute('SELECT j.id,j.kind,j.variant,b.canonical_url FROM library_job j JOIN library_bookmark b ON b.id=j.target_id WHERE j.id=?', (UUID(job_id).hex,)).fetchone()
    if row is None or row['kind'] not in ('capture', 'metadata'):
        raise ValueError('Replay requires an existing capture or metadata job')
    # This runs the same capture pipeline but never claims, retries or publishes a job.
    trace_path = (output / ('replay-' + str(UUID(job_id)) + '-' + uuid4().hex[:8] + '.json')).resolve()
    with tempfile.TemporaryDirectory(prefix='capture-replay-', dir=output) as scratch:
        request = CaptureRequest(attempt_uuid=str(uuid4()), generation=runtime['active_generation'],
            library_epoch=runtime['library_epoch'], url=row['canonical_url'], variant=row['variant'] or 'desktop',
            mode='metadata' if row['kind']=='metadata' else 'capture', output_dir=scratch,
            development=True, dns_mode=dns_mode, proxy_url=proxy_url or '', deny_hosts=('localhost', '127.0.0.1', '[::1]', 'testserver'))
        request_file = Path(scratch) / 'request.json'
        request_file.write_text(json.dumps(asdict(request)), encoding='utf-8')
        try:
            _run_capture_process([sys.executable, '-X', 'utf8', '-m', 'capture', '--request', str(request_file),
                '--diagnostics', str(trace_path)], Path(scratch), request.timeout_seconds, lambda: True)
            outcome = {'state': 'succeeded'}
        except Exception as error:
            outcome = {'state': 'failed', 'errors': error_evidence(error)}
    trace = json.loads(trace_path.read_text('utf-8')) if trace_path.exists() else {}
    trace['source_job_id'] = str(UUID(job_id))
    trace['replay_outcome'] = outcome
    if trace.get('state') == 'running':
        trace['state'] = 'terminated_by_parent'
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'job_id': job_id, 'state': outcome['state'], 'stage': trace.get('stage'), 'trace': str(trace_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=ROOT/'data')
    parser.add_argument('--group', help='Defaults to the newest refresh group')
    parser.add_argument('--output', type=Path, default=ROOT/'output'/'task-diagnostics'/datetime.now().strftime('%Y%m%d-%H%M%S'))
    parser.add_argument('--replay-job', action='append', default=[], help='Explicitly fetch this job again, without modifying the library')
    parser.add_argument('--dns-mode', choices=['system', 'public-doh'], default='public-doh')
    parser.add_argument('--proxy', help='Explicit loopback HTTP proxy for isolated replays')
    args = parser.parse_args()
    report = collect(args.data_root.resolve(), args.group)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# 后台任务诊断', '', f"采集时间（UTC）：{report['collected_at']}",
             f"批次：{report['group']['id']}", f"设备任务数：{report['total_device_jobs']}", '',
             '## 状态', '', *[f'- {key}: {value}' for key,value in report['states'].items()], '',
             '## 错误', '', *[f'- {key}: {value}' for key,value in report['errors'].items()], '',
             '详见 report.json：网站仅记录域名；历史任务未保存详细日志的，会明确标记 diagnostic_available=false。',
             '此导出不会修改任务。attempt 的 finished_at 才是执行结束时间，不能用 Job.updated_at 推断失败发生时间。']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'report': str(args.output/'report.json'), 'states': report['states'], 'errors': report['errors']}, ensure_ascii=False), flush=True)
    for job_id in args.replay_job:
        print(json.dumps(replay(args.data_root.resolve(), report, job_id, args.output, args.dns_mode, args.proxy), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
