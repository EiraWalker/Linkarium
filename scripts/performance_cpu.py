"""Measure CPU versus elapsed image-loading time without changing the running app.

Uses live process counters, a disposable instrumented Web instance, and isolated
single-thread request batches. No capture, classification or preview generation.
"""
import argparse
import asyncio
from contextlib import ExitStack
import contextvars
import cProfile
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pstats
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'backend')]
os.environ.setdefault('BOOKMARK_DATA_ROOT', str(ROOT/'data'))
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT/'tools/browsers'))
from performance_browser import INSTRUMENT, NetworkProbe, measure_navigation, origin, route_template, temporary_owner_session
from performance_image_loading import summarize_images

ACTIVE = contextvars.ContextVar('cpu_probe_request', default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def sql_probe(execute, sql, params, many, context):
    # GET-side accidental writes must fail in the diagnostic process.
    keyword = sql.lstrip().split(None, 1)[0].upper()
    if keyword not in {'SELECT', 'PRAGMA', 'BEGIN', 'COMMIT', 'ROLLBACK', 'SAVEPOINT', 'RELEASE'}:
        raise RuntimeError('cpu_probe_write_blocked')
    start = time.perf_counter_ns()
    try:
        return execute(sql, params, many, context)
    finally:
        row = ACTIVE.get()
        if row is not None:
            row['sql_ms'] += (time.perf_counter_ns()-start)/1e6
            row['sql_count'] += 1


def install_hash_probe():
    original = hashlib.file_digest
    def measured(*args, **kwargs):
        start = time.perf_counter_ns()
        try:
            return original(*args, **kwargs)
        finally:
            row = ACTIVE.get()
            if row is not None:
                row['hash_ms'] += (time.perf_counter_ns()-start)/1e6
                row['hash_count'] += 1
    hashlib.file_digest = measured


def block_generation():
    from library import preview_cache
    def blocked(*args, **kwargs):
        raise RuntimeError('cpu_probe_missing_preview_generation_blocked')
    preview_cache.request_preview = blocked


def new_row():
    return {'sql_ms': 0., 'sql_count': 0, 'hash_ms': 0., 'hash_count': 0}


class CpuApplication:
    """Thread CPU excludes other request threads; streaming stays in the scope."""
    def __init__(self, application, target):
        self.application, self.target = application, target
        self.lock = threading.Lock()

    def __call__(self, environ, start_response):
        from django.db import connections
        row = new_row()
        row.update(route=route_template(environ.get('PATH_INFO', '/')), started_at=now(), status=None)
        if environ.get('REQUEST_METHOD') not in {'GET', 'HEAD'}:
            start_response('405 Method Not Allowed', [('Content-Length', '0')])
            return []
        token = ACTIVE.set(row)
        wall, cpu = time.perf_counter_ns(), time.thread_time_ns()
        stack = ExitStack()
        for alias in connections:
            stack.enter_context(connections[alias].execute_wrapper(sql_probe))
        def response(status, headers, exc_info=None):
            row['status'] = int(status.split()[0])
            row['request_id'] = next((v for k, v in headers if k.lower() == 'x-request-id'), None)
            row['construct_ms'] = (time.perf_counter_ns()-wall)/1e6
            return start_response(status, headers, exc_info)
        try:
            result = self.application(environ, response)
        except BaseException:
            stack.close()
            ACTIVE.reset(token)
            raise
        def iterate():
            try:
                yield from result
            finally:
                try:
                    if hasattr(result, 'close'):
                        result.close()
                finally:
                    stack.close()
                    row.update(thread_cpu_ms=(time.thread_time_ns()-cpu)/1e6,
                               app_wall_ms=(time.perf_counter_ns()-wall)/1e6)
                    ACTIVE.reset(token)
                    with self.lock, self.target.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(row, ensure_ascii=False)+'\n')
        return iterate()


def serve(directory):
    os.environ['BOOKMARK_PERF'] = '0'
    from config.wsgi import application
    from waitress import create_server
    install_hash_probe()
    block_generation()
    server = create_server(CpuApplication(application, directory/'requests.jsonl'),
                           host='127.0.0.1', port=0, threads=4)
    write(directory/'server-ready.json', {'pid': os.getpid(), 'origin': 'http://127.0.0.1:'+str(server.effective_port)})
    server.run()


def cpu_snapshot(pid, metrics):
    from ctypes import byref
    handle = metrics.kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise RuntimeError('web_process_not_readable')
    try:
        values = [metrics.w.FILETIME() for _ in range(4)]
        if not metrics.kernel.GetProcessTimes(handle, *(byref(v) for v in values)):
            raise RuntimeError('process_cpu_unavailable')
        return {'identity': metrics._ticks(values[0]),
                'cpu_ms': (metrics._ticks(values[2])+metrics._ticks(values[3]))/10000,
                'clock': time.perf_counter()}
    finally:
        metrics.kernel.CloseHandle(handle)


def cpu_delta(before, after):
    if before['identity'] != after['identity']:
        raise RuntimeError('web_pid_reused')
    elapsed = (after['clock']-before['clock'])*1000
    cpu = after['cpu_ms']-before['cpu_ms']
    if elapsed <= 0 or cpu < 0:
        raise RuntimeError('invalid_cpu_interval')
    return {'process_cpu_ms': cpu, 'window_wall_ms': elapsed,
            'one_core_percent': cpu/elapsed*100,
            'machine_percent': cpu/elapsed*100/(os.cpu_count() or 1)}


async def browser_runs(base, cookie, pid, repeats):
    from performance_resources import WindowsMetrics
    from playwright.async_api import async_playwright
    metrics = WindowsMetrics(ROOT)
    rows = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for device in ('desktop', 'mobile'):
                for repeat in range(repeats):
                    context = await browser.new_context(viewport={'width': 390 if device == 'mobile' else 1440,
                        'height': 844 if device == 'mobile' else 1000}, is_mobile=device == 'mobile',
                        has_touch=device == 'mobile', service_workers='block')
                    try:
                        await context.add_cookies([cookie])
                        await context.add_init_script(INSTRUMENT)
                        page = await context.new_page()
                        probe = NetworkProbe(base)
                        cdp = await probe.attach(context, page)
                        await cdp.send('Network.clearBrowserCache')
                        for cache in ('cold', 'warm'):
                            before = cpu_snapshot(pid, metrics)
                            row = await measure_navigation(page, probe, base+'/library?device='+device, device, cache, 20)
                            after = cpu_snapshot(pid, metrics)
                            row.update(repeat=repeat+1, process=cpu_delta(before, after), images=summarize_images(row))
                            rows.append(row)
                            print(json.dumps({'origin':base,'device':device,'cache':cache,'repeat':repeat+1,
                                'status':row['status'],'image_ms':row['images']['first_request_to_all_ready_ms'],
                                **row['process']},ensure_ascii=False), flush=True)
                    finally:
                        await context.close()
        finally:
            await browser.close()
    return rows


def micro(directory):
    """No HTTP server or other Python work in this process during batch clocks."""
    os.environ['BOOKMARK_PERF'] = '0'
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    from django.db import connections
    from django.test import Client
    from library.models import SnapshotVersion
    from library.preview_cache import image_path
    versions = list(SnapshotVersion.objects.select_related('artifact').order_by('-captured_at')[:200])
    paths = [f'/api/v1/snapshots/{v.id}/thumbnail?v=1' for v in versions if image_path(v).is_file()][:45]
    if len(paths) < 10:
        raise RuntimeError('insufficient_existing_previews')
    install_hash_probe()
    block_generation()
    report = {'started_at': now(), 'batches': [], 'profiles': []}
    state = {'deleted':False}
    with temporary_owner_session('http://127.0.0.1') as (cookie, state):
        client = Client()
        client.cookies[cookie['name']] = cookie['value']
        etags = {}
        def fetch(path, conditional=False):
            kwargs = {'HTTP_IF_NONE_MATCH': etags[path]} if conditional else {}
            response = client.get(path, **kwargs)
            if response.status_code not in (200, 304):
                raise RuntimeError('unexpected_response_'+str(response.status_code))
            if response.has_header('ETag'):
                etags[path] = response['ETag']
            if response.streaming:
                for chunk in response.streaming_content:
                    pass
            response.close()
        for path in paths:
            fetch(path)
        cases = [('bookmarks', ['/api/v1/bookmarks?variant=desktop&page_size=48'], 45, False),
                 ('folders', ['/api/v1/folders'], 60, False),
                 ('job_groups', ['/api/v1/job-groups?page=1&page_size=20'], 45, False),
                 ('thumbnail_200', paths, 180, False), ('thumbnail_304', paths, 180, True)]
        for label, targets, count, conditional in cases:
            # Warm imports, connections and filesystem cache before each batch.
            for target in targets[:2]:
                fetch(target, conditional)
            row = new_row()
            token = ACTIVE.set(row)
            with ExitStack() as stack:
                for alias in connections:
                    stack.enter_context(connections[alias].execute_wrapper(sql_probe))
                start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
                for i in range(count):
                    fetch(targets[i % len(targets)], conditional)
                row.update(label=label, count=count, wall_ms=(time.perf_counter_ns()-start_wall)/1e6,
                           cpu_ms=(time.process_time_ns()-start_cpu)/1e6)
            ACTIVE.reset(token)
            report['batches'].append(row)
            # Profiling is separate: call-stack instrumentation distorts timings.
            profiler = cProfile.Profile()
            profiler.enable()
            for i in range(min(count, 30)):
                fetch(targets[i % len(targets)], conditional)
            profiler.disable()
            stats = pstats.Stats(profiler)
            top = []
            for (filename, line, name), (cc, nc, tt, ct, callers) in stats.stats.items():
                try:
                    filename = str(Path(filename).resolve().relative_to(ROOT))
                except (ValueError, OSError):
                    filename = Path(filename).name
                name = re.sub(r' at 0x[0-9A-Fa-f]+', '', name)
                top.append({'file': filename, 'line': line, 'function': name,
                            'calls': nc, 'self_wall_ms':tt*1000, 'cumulative_wall_ms':ct*1000})
            report['profiles'].append({'label':label, 'count':min(count,30), 'timer':'elapsed; separate profiled batch',
                'top_self':sorted(top,key=lambda x:x['self_wall_ms'],reverse=True)[:15],
                'top_cumulative':sorted(top,key=lambda x:x['cumulative_wall_ms'],reverse=True)[:15]})
            print(json.dumps({'micro':label,**row}),flush=True)
    report.update(temporary_session_deleted=state['deleted'],finished_at=now())
    write(directory/'micro.json', report)


def collect(args):
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    os.environ['BOOKMARK_PERF'] = '0'
    result = {'started_at':now(), 'live_origin':args.base_url, 'logical_processors':os.cpu_count(),
              'application_strategy_changed':False, 'status':'incomplete',
              'clock':{'process':time.get_clock_info('process_time').implementation,
                       'thread':time.get_clock_info('thread_time').implementation,
                       'note':'Windows CPU accounting is quantized. Use aggregated batches, not zero CPU per short request.'}}
    child = None
    states = []
    try:
        from performance_resources import WindowsMetrics
        metrics = WindowsMetrics(ROOT)
        idle_start = cpu_snapshot(args.web_pid, metrics)
        time.sleep(3)
        result['idle'] = cpu_delta(idle_start, cpu_snapshot(args.web_pid, metrics))
        with temporary_owner_session(args.base_url) as (cookie, state):
            states.append(state)
            result['live_runs'] = asyncio.run(browser_runs(args.base_url, cookie, args.web_pid, args.repeats))
        with (directory/'server.log').open('w',encoding='utf-8') as log:
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            child = subprocess.Popen([sys.executable,'-X','utf8',__file__,'serve','--output',str(directory)],
                cwd=ROOT,env=dict(os.environ),stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags)
            deadline = time.monotonic()+20
            while not (directory/'server-ready.json').exists():
                if child.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('diagnostic_server_start_failed')
                time.sleep(.1)
            ready=read(directory/'server-ready.json')
            with temporary_owner_session(ready['origin']) as (cookie, state):
                states.append(state)
                result['instrumented_runs'] = asyncio.run(browser_runs(ready['origin'],cookie,ready['pid'],args.repeats))
        child.terminate()
        child.wait(timeout=10)
        result['temporary_web_closed']=True
        completed=subprocess.run([sys.executable,'-X','utf8',__file__,'micro','--output',str(directory)],
            cwd=ROOT,env=dict(os.environ),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,
            encoding='utf-8',timeout=120,creationflags=flags)
        (directory/'micro.log').write_text(completed.stdout,encoding='utf-8')
        if completed.returncode:
            raise RuntimeError('micro_batch_failed')
        result['micro']=read(directory/'micro.json')
        rows=result['live_runs']+result['instrumented_runs']
        result['status']='complete' if all(row['status']=='complete' and row['images']['status']=='complete' for row in rows) else 'incomplete'
    except Exception as error:
        result['error']=type(error).__name__+': '+str(error) if isinstance(error,RuntimeError) else type(error).__name__
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        result['temporary_web_closed']=child is None or child.poll() is not None
        result['temporary_sessions_deleted']=all(state['deleted'] for state in states)
        result['finished_at']=now()
        if (directory/'requests.jsonl').exists():
            result['requests']=[json.loads(line) for line in (directory/'requests.jsonl').read_text(encoding='utf-8').splitlines()]
        write(directory/'cpu.json',result)
    print(json.dumps({'status':result['status'],'output':str(directory),'temporary_web_closed':result['temporary_web_closed']}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['collect','serve','micro'])
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--base-url',type=origin,default='http://127.0.0.1:8001')
    parser.add_argument('--web-pid',type=int)
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    if args.mode=='collect':
        if not args.web_pid or not 1<=args.repeats<=5:
            parser.error('collect requires --web-pid and repeats 1..5')
        collect(args)
    elif args.mode=='serve':
        serve(args.output)
    else:
        micro(args.output)


if __name__=='__main__':
    main()
