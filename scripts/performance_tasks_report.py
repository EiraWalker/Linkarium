"""A task list with expandable stage timings. No application access or writes."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import csv
import html
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from performance_inventory import elapsed, timestamp
from performance_image_loading import summarize_images

NAMES = {
    'capture': '网页快照', 'metadata': '网页信息', 'classification': 'AI 分类', 'import': '书签导入',
    'ai_test': 'AI 连接测试', 'backup': '备份', 'restore': '恢复', 'desktop': '桌面', 'mobile': '手机',
    'cold': '首次加载', 'warm': '再次加载', 'library': '网站库加载', 'open_detail': '打开详情',
    'switch_device': '切换预览设备', 'scroll': '滚动', 'pagination': '下一页',
    'pagination_return': '返回上一页', 'folder': '进入文件夹', 'folder_return': '返回网站库',
    'settings': '进入设置', 'settings_return': '返回网站库', 'html_archive': '完整 HTML 快照加载',
    'device_budget': '设备测量预算', 'starting': '初始化', 'network_setup': '网络环境准备',
    'url_validation': '网址与 DNS 检查', 'browser_launch': '启动浏览器', 'navigation': '打开原网页',
    'readiness': '等待页面就绪', 'freeze': '冻结页面', 'rewrite': '重写 HTML/CSS',
    'archive': '归档与输出处理', 'replay': '加载归档', 'replay_check': '校验归档与结束处理',
    'preview.render': '生成预览图片', 'preview.backfill': '快照补图',
    'preview.verify_archive': '校验 HTML 文件', 'preview.child': '离线浏览器渲染',
    'preview.store': '写入图片', 'worker.job': '执行任务', 'worker.advance_imports': '推进导入',
    'ai.complete': 'AI 请求', 'ai.transport': 'AI 网络请求', 'ai.parse': '解析 AI 回复',
    'http.request': '处理请求', 'http.stream': '发送响应流',
    'capture.publish': '发布快照', 'capture.hash': '校验文件哈希',
    'capture.copy': '复制快照', 'capture.cleanup': '清理临时文件',
}


def name(value):
    return NAMES.get(value, value or '未命名')


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def key(value):
    return str(value or '').replace('-', '').lower()


def stage(label, duration=None, offset=None, depth=0, mode='区间'):
    return {'name': name(label), 'duration_ms': number(duration), 'offset_ms': number(offset), 'depth': depth, 'mode': mode}


def read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def sum_known(values):
    return sum(values) if values and all(number(item) is not None for item in values) else None


def in_window(value, started, finished):
    point = timestamp(value)
    return bool(point and started and finished and started <= point <= finished)


def span_stages(rows, origin):
    by_id = {key(row.get('span_id')): row for row in rows}
    stages = []
    for row in sorted(rows, key=lambda row: row.get('timestamp', '')):
        depth, parent, visited = 0, key(row.get('parent_span_id')), set()
        while parent in by_id and parent not in visited and depth < 8:
            visited.add(parent)
            depth += 1
            parent = key(by_id[parent].get('parent_span_id'))
        duration = number(row.get('duration_ms'))
        end = timestamp(row.get('timestamp'))
        start = end - timedelta(milliseconds=duration) if end and duration is not None else None
        offset = max(0, (start - origin).total_seconds()*1000) if start and origin else None
        stages.append(stage(row.get('operation'), duration, offset, depth, '观测区间'))
    return stages


def background_tasks(inventory, events, started, finished):
    attempts = defaultdict(list)
    for row in inventory.get('attempts', {}).get('details', []):
        attempts[key(row.get('job_id'))].append(row)
    jobs = {key(row.get('id')): row for row in inventory.get('jobs', {}).get('details', [])}
    # Earlier collections lack the raw job rows, but still have reliable attempts.
    for identity, items in attempts.items():
        jobs.setdefault(identity, {'id': items[0]['job_id'], 'kind': items[0].get('kind'), 'variant': items[0].get('variant')})
    capture = {key(row.get('attempt_id')): row for row in inventory.get('capture_diagnostics', {}).get('details', [])}
    by_attempt = defaultdict(list)
    for row in events:
        if row.get('record_type') == 'span' and row.get('attempt_id'):
            by_attempt[key(row['attempt_id'])].append(row)
    tasks = []
    for identity, job in jobs.items():
        items = sorted(attempts.get(identity, []), key=lambda row: row.get('attempt_no', 0))
        final = items[-1] if items else {}
        state = job.get('state') or final.get('state') or 'unknown'
        creation = job.get('created_at')
        if not creation and items:
            wait = number(items[0].get('initial_queue_ms'))
            first = timestamp(items[0].get('created_at'))
            creation = (first - timedelta(milliseconds=wait)).isoformat() if first and wait is not None else None
        end = final.get('finished_at') if state not in {'running', 'queued', 'retry_wait'} else None
        is_current = in_window(creation, started, finished) or any(in_window(row.get('created_at'), started, finished) for row in items)
        root_start = timestamp(creation)
        stages, waits = [], []
        for attempt in items:
            wait = number(attempt.get('initial_queue_ms', attempt.get('wait_since_previous_finish_ms')))
            waits.append(wait)
            attempt_start = timestamp(attempt.get('created_at'))
            offset = max(0, (attempt_start - root_start).total_seconds()*1000) if root_start and attempt_start else None
            stages.append(stage('第 ' + str(attempt.get('attempt_no', '?')) + ' 次执行', attempt.get('execution_ms'), offset, 0, '含子阶段'))
            evidence = capture.get(key(attempt.get('attempt_id')), {})
            for phase in evidence.get('phases', []):
                phase_offset = number(phase.get('start_ms'))
                stages.append(stage(phase.get('stage'), phase.get('duration_ms'),
                    offset + phase_offset if offset is not None and phase_offset is not None else None, 1))
            linked = span_stages(by_attempt.get(key(attempt.get('attempt_id')), []), root_start)
            for row in linked:
                row['depth'] += 1
            stages.extend(linked)
        duration = sum_known([number(row.get('execution_ms')) for row in items])
        # A cancelled job may have been cancelled long after its last attempt.
        # Do not invent cancellation time from the last attempt's end.
        total = elapsed(creation, end) if state in {'succeeded', 'failed'} else None
        tasks.append({'id': job.get('id', identity), 'name': name(job.get('kind')) + ' · ' + str(job.get('id', identity))[:8],
            'category': '后台任务', 'source': '本次' if is_current else '历史', 'variant': name(job.get('variant')) if job.get('variant') else '',
            'state': state, 'started_at': creation, 'queue_ms': sum_known(waits), 'execution_ms': duration,
            'total_ms': total, 'stages': stages, 'stage_count': sum(row['depth'] > 0 for row in stages), 'attempts': len(items)})
    return tasks


def browser_tasks(browser):
    tasks = []
    for index, row in enumerate(browser.get('runs', []) + browser.get('scenarios', []) + browser.get('html_samples', [])):
        title = ' · '.join(name(row.get(part)) for part in ('device', 'cache', 'scenario') if row.get(part))
        stages = []
        nav = row.get('navigation', {}) or {}
        if row.get('scenario') == 'library':
            ttfb, body = number(nav.get('ttfb_ms')), number(nav.get('response_end_ms'))
            stages.append(stage('等待 HTML 响应', ttfb, 0))
            stages.append(stage('下载 HTML', body - ttfb if body is not None and ttfb is not None and body >= ttfb else None, ttfb))
            stages.append(stage('DOM 就绪', offset=nav.get('dom_content_loaded_ms'), mode='时间点'))
        preview = row.get('preview', {}) or {}
        for field, title_label in [('first_ready_ms', '首张预览就绪'), ('first_paint_opportunity_ms', '首张预览绘制机会'),
                ('all_ready_ms', '全部预览就绪'), ('all_paint_opportunity_ms', '全部预览绘制机会')]:
            if field in preview:
                stages.append(stage(title_label, offset=preview[field], mode='时间点'))
        for image in preview.get('image_samples', []):
            label = '图片 ' + str(image.get('sample', '?'))
            headers, body = number(image.get('thumbnail_headers_ms')), number(image.get('thumbnail_body_end_ms'))
            decode, paint = number(image.get('decode_ms')), number(image.get('paint_opportunity_ms'))
            stages.append(stage(label + ' · 下载响应体', body-headers if body is not None and headers is not None and body >= headers else None, headers, 1))
            stages.append(stage(label + ' · body 完成至解码可用', decode-body if decode is not None and body is not None and decode >= body else None, body, 1))
            stages.append(stage(label + ' · 解码后至绘制机会', paint-decode if paint is not None and decode is not None and paint >= decode else None, decode, 1))
        for field, label in [('head_request_elapsed_ms', 'HEAD 校验'), ('iframe_load_ms', 'HTML iframe 加载'),
                ('outer_paint_opportunity_ms', 'HTML 绘制机会')]:
            if field in row:
                stages.append(stage(label, row[field]))
        duration = number(row.get('duration_ms', row.get('elapsed_ms')))
        task_id = 'browser-' + str(index+1)
        tasks.append({'id': task_id, 'name': title, 'category': '页面显示', 'source': '本次', 'variant': name(row.get('device')),
            'state': row.get('status', 'unknown'), 'started_at': row.get('started_at'), 'queue_ms': None,
            'execution_ms': duration, 'total_ms': duration, 'stages': stages, 'stage_count': len(stages),
            'error_code': row.get('error_code', row.get('error', row.get('reason'))),
            'ready_images': preview.get('success')})
        for group in row.get('network', []):
            if not group.get('route', '').startswith('/api/'):
                continue
            for sample_index, sample in enumerate(group.get('request_samples', [])):
                duration, headers = number(sample.get('duration_ms')), number(sample.get('headers_elapsed_ms'))
                parts = [stage('请求开始至响应头', headers, 0), stage('响应体传输',
                    duration-headers if duration is not None and headers is not None and duration >= headers else None, headers)]
                for metric, amount in sample.get('server_timing', {}).items():
                    parts.append(stage({'app': '服务端请求处理', 'sql': 'SQL 执行', 'db': '数据库'}.get(metric, metric), amount, mode='含子阶段'))
                tasks.append({'id': sample.get('request_id') or task_id+'-api-'+str(sample_index)+'-'+group['route'],
                    'request_id': sample.get('request_id'),
                    'name': group['route'] + ' · ' + title + ' · ' + str(sample_index+1),
                    'category': 'API 请求', 'source': '本次', 'state': 'failed' if sample.get('failed') or (number(sample.get('status')) or 0) >= 400 else 'complete',
                    'variant': name(row.get('device')), 'started_at': None, 'queue_ms': None,
                    'execution_ms': number(sample.get('server_timing', {}).get('app')), 'total_ms': duration, 'stages': parts, 'stage_count': len(parts)})
    return tasks


def observed_tasks(events, known_jobs):
    traces = defaultdict(list)
    for row in events:
        if row.get('record_type') == 'span' and key(row.get('job_id')) not in known_jobs:
            traces[key(row.get('trace_id')) or key(row.get('span_id'))].append(row)
    tasks = []
    for identity, rows in traces.items():
        roots = [row for row in rows if not row.get('parent_span_id')]
        root = max(roots or rows, key=lambda row: number(row.get('duration_ms')) or 0)
        # Routine polling without user work adds noise rather than task timings.
        if root.get('operation') == 'worker.advance_imports':
            continue
        duration = number(root.get('duration_ms'))
        at = timestamp(root.get('timestamp'))
        begin = at - timedelta(milliseconds=duration) if at and duration is not None else None
        stages = span_stages(rows, begin)
        if number(root.get('sql_ms')) is not None:
            stages.append(stage('SQL 执行', root['sql_ms'], mode='含子阶段'))
        tasks.append({'id': identity, 'name': name(root.get('operation')) + (' · '+root['route'] if root.get('route') else ''),
            'category': 'API 请求' if root.get('operation', '').startswith('http.') else '后台阶段记录', 'source': '本次',
            'variant': name(root.get('variant')) if root.get('variant') else '',
            'state': 'failed' if root.get('outcome') not in {'ok', None} else 'complete',
            'started_at': begin.isoformat() if begin else None, 'queue_ms': None, 'execution_ms': duration,
            'total_ms': duration, 'stages': stages, 'stage_count': len(stages)})
    return tasks


def merge_request_tasks(observed, browser):
    """Link client/server clocks by request ID without counting a request twice."""
    requests = {key(row.get('request_id')): row for row in browser if row.get('request_id')}
    remainder = []
    for server in observed:
        target = requests.get(key(server.get('id')))
        if not target:
            remainder.append(server)
            continue
        target['execution_ms'] = server['execution_ms']
        target['started_at'] = server.get('started_at')
        target['stages'] = [row for row in target['stages'] if row['name'] not in {'服务端请求处理', 'SQL 执行', '数据库'}]
        for row in server['stages']:
            target['stages'].append({**row, 'name': '服务端 · '+row['name'], 'clock': 'backend'})
        target['stage_count'] = len(target['stages'])
    return remainder + browser


def build_tasks(run_dir):
    run_dir = Path(run_dir)
    run = read(run_dir/'run.json', {})
    inventory = read(run_dir/'inventory-after.json', read(run_dir/'inventory.json', {}))
    events = []
    try:
        for line in (run_dir/'events.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    events.append(row)
            except ValueError:
                pass
    except OSError:
        pass
    tasks = background_tasks(inventory, events, timestamp(run.get('started_at')), timestamp(run.get('finished_at')))
    known = {key(task['id']) for task in tasks}
    browser = read(run_dir/'browser.json', {})
    tasks.extend(merge_request_tasks(observed_tasks(events, known), browser_tasks(browser)))
    tasks.sort(key=lambda row: (row['source'] != '本次', row.get('started_at') or ''), reverse=False)
    return {'schema_version': 1, 'generated_at': datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
        'started_at': run.get('started_at'), 'finished_at': run.get('finished_at'), 'tasks': tasks,
        'image_loading': [summarize_images(row) for row in browser.get('runs', [])],
        'counts': {'current': sum(row['source'] == '本次' for row in tasks), 'historical': sum(row['source'] == '历史' for row in tasks)},
        'measurement_mode': '临时 Web 实例' if read(run_dir.parent/'instrumented-runtime.json', {}).get('mode') == 'temporary_instrumented_web' else '当前服务',
        'truncated_jobs': inventory.get('jobs', {}).get('details_truncated', 0),
        'truncated_attempts': inventory.get('attempts', {}).get('details_truncated', 0)}


STYLE = '''
:root{color-scheme:light;font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;color:#1c293a;background:#f5f6f8}*{box-sizing:border-box}body{margin:0}main{max-width:1500px;margin:auto;padding:26px 22px}h1{font-size:24px;margin:0 0 6px}a{color:#1766db;text-decoration:none}.meta{color:#718096;font-size:12px}.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:20px 0 12px}.tools input{flex:1;min-width:180px}input,select,button{font:inherit;padding:7px 10px;border:1px solid #d8e0eb;border-radius:4px;background:#fff;color:inherit}button{cursor:pointer}button:disabled{opacity:.45;cursor:default}.table{overflow:auto;border:1px solid #dce3ed;border-radius:6px;background:#fff}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #e4e9f0;padding:10px 12px;text-align:left;vertical-align:top}th{background:#f0f3f7;white-space:nowrap;font-weight:600}td.number{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums}.task-name{min-width:200px;max-width:650px;overflow-wrap:anywhere}.task-name small{display:block;color:#7a8798;font-size:11px}.state{font-size:12px;white-space:nowrap}.bad{color:#c13939}.warn{color:#987326}.good{color:#27714a}.detail{background:#fbfcfe}.detail>td{padding:14px 20px}.stages{max-width:1100px}.stages th{font-size:12px}.stage-name{min-width:200px}.bar{height:5px;min-width:1px;max-width:200px;background:#2d79ee;border-radius:2px;margin-top:7px}.mode{font-size:11px;color:#8793a2}.pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:12px}.note{color:#7a8798;font-size:12px;margin:14px 0}.empty{text-align:center;color:#7a8798;padding:32px}.disclosure{border:0;background:transparent;color:#1766db;padding:0;white-space:nowrap}@media(max-width:650px){main{padding:18px 10px}h1{font-size:21px}.detail>td{padding:10px}td,th{padding:8px}.pager{justify-content:center}}
'''

STYLE += '''
.waterfall-group{margin-bottom:12px}.clock-label{font-size:12px;color:#65758b;margin:4px 0 7px}.waterfall-track{min-width:390px;width:430px}.waterfall-axis,.waterfall-row{display:block;width:420px}.waterfall-row{height:24px}.waterfall-axis text{font:10px system-ui;fill:#78869a}.waterfall-grid{stroke:#e4eaf2;stroke-width:1}.waterfall-bar{fill:#2877ec}.waterfall-bar.backend{fill:#2f9c96}.milestone{fill:#b47b20}.uncertain{fill:white;stroke:#8b98a9;stroke-width:1.5}.waterfall-row [tabindex]:focus,.timeline-bar:focus{outline:none;stroke:#152c4b;stroke-width:2}.chart-legend{font-size:11px;color:#78869a;white-space:nowrap;margin:7px 0}.chart-legend .blue{color:#2877ec}.chart-legend .teal{color:#2f9c96}.chart-legend .gold{color:#b47b20}.timeline-panel{border:1px solid #dce3ed;background:#fff;border-radius:6px;margin:0 0 14px;padding:14px}.timeline-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}.timeline-head strong{font-size:14px}.timeline-scroll{max-height:440px;overflow:auto}.timeline-svg{display:block;width:100%;min-width:950px}.timeline-svg text{font:11px system-ui;fill:#62738a}.timeline-svg .lane-label{fill:#26374c}.timeline-bar{fill:#2877ec}.timeline-bar.failed{fill:#bd5555}.timeline-line{stroke:#e7edf4}.timeline-toggle[aria-expanded="true"]{color:#1766db;background:#edf4ff;border-color:#9fbcf1}[hidden]{display:none!important}
'''


SCRIPT = r'''
const data=JSON.parse(document.querySelector('#data').textContent),body=document.querySelector('#rows');
let page=1;const size=50,opened=new Set();
const labels={succeeded:'完成',complete:'完成',failed:'失败',cancelled:'已取消',running:'执行中',queued:'排队中',retry_wait:'等待重试',skipped:'跳过',incomplete:'未完成',unknown:'未知'};
const fmt=v=>typeof v==='number'&&Number.isFinite(v)?v>=3600000?(v/3600000).toFixed(3)+' h':v>=60000?(v/60000).toFixed(3)+' min':v>=1000?(v/1000).toFixed(3)+' s':v.toFixed(1)+' ms':'—';
const local=v=>v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Taipei',hour12:false}):'';
const text=(tag,value,cls='')=>{const n=document.createElement(tag);n.textContent=value??'';if(cls)n.className=cls;return n};
const filter=()=>{const query=document.querySelector('#search').value.toLowerCase(),source=document.querySelector('#source').value,category=document.querySelector('#category').value;let rows=data.tasks.filter(t=>(!source||t.source===source)&&(!category||t.category===category)&&[t.name,t.id,t.variant,...t.stages.map(s=>s.name)].join(' ').toLowerCase().includes(query));if(document.querySelector('#sort').value==='duration')rows.sort((a,b)=>(b.execution_ms??-1)-(a.execution_ms??-1));else rows.sort((a,b)=>(b.started_at||'').localeCompare(a.started_at||''));return rows};
const known=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0;
function svgNode(tag,attrs={},content){const element=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attrs))element.setAttribute(key,String(value));if(content!==undefined)element.textContent=content;return element}
function chartScale(items){const offsets=items.filter(s=>known(s.offset_ms)).map(s=>s.offset_ms),origin=offsets.length?Math.min(...offsets):0;const end=Math.max(origin+1,...items.filter(s=>known(s.offset_ms)).map(s=>s.offset_ms+(known(s.duration_ms)?s.duration_ms:0)));return {origin,range:end-origin}}
function axis(scale){const svg=svgNode('svg',{viewBox:'0 0 420 28',class:'waterfall-axis','aria-hidden':'true'});for(let tick=0;tick<=4;tick++){const x=10+tick*100;svg.append(svgNode('text',{x,y:15,'text-anchor':tick===0?'start':tick===4?'end':'middle'},fmt(scale.range*tick/4)))}return svg}
function stageChart(stage,scale){if(!known(stage.offset_ms))return text('span','起点未记录','mode');const svg=svgNode('svg',{viewBox:'0 0 420 24',class:'waterfall-row',role:'img','aria-label':stage.name+'，开始后 '+fmt(stage.offset_ms)+'，耗时 '+fmt(stage.duration_ms)});for(let tick=0;tick<=4;tick++)svg.append(svgNode('line',{x1:10+tick*100,x2:10+tick*100,y1:0,y2:24,class:'waterfall-grid'}));const x=10+400*(stage.offset_ms-scale.origin)/scale.range;let shape;
 if(known(stage.duration_ms)){shape=svgNode('rect',{x,y:6,width:Math.max(.8,400*stage.duration_ms/scale.range),height:12,rx:2,class:'waterfall-bar'+(stage.clock==='backend'?' backend':''),'data-start-ms':stage.offset_ms,'data-duration-ms':stage.duration_ms})}
 else if(stage.mode==='时间点'){shape=svgNode('polygon',{points:`${x},5 ${x+5},12 ${x},19 ${x-5},12`,class:'milestone','data-start-ms':stage.offset_ms})}
 else{shape=svgNode('circle',{cx:x,cy:12,r:4,class:'uncertain','data-start-ms':stage.offset_ms})}
 shape.setAttribute('tabindex','0');shape.append(svgNode('title',{},stage.name+' · 开始后 '+fmt(stage.offset_ms)+(known(stage.duration_ms)?' · 结束 '+fmt(stage.offset_ms+stage.duration_ms)+' · 耗时 '+fmt(stage.duration_ms):stage.mode==='时间点'?' · 时间点':' · 结束未记录')));svg.append(shape);return svg}
function stageGroups(task,items){const groups=[];let group=null;for(const stage of items){const clock=stage.clock||'task',attempt=task.category==='后台任务'&&stage.depth===0&&/^第 .* 次执行$/.test(stage.name);if(!group||group.clock!==clock||attempt){group={clock,label:clock==='backend'?'服务端时间轴（独立起点）':attempt?stage.name:'任务时间轴',items:[]};groups.push(group)}group.items.push(stage)}return groups}
function stageTable(task,items){const container=text('div','','waterfall-groups');for(const group of stageGroups(task,items)){const scale=chartScale(group.items),section=text('div','','waterfall-group');section.append(text('div',group.label+(scale.origin>0?' · 轴起点：任务开始后 '+fmt(scale.origin):''),'clock-label'));const wrapper=text('div','','table stages'),table=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');for(const title of ['阶段','开始后','耗时'])hr.append(text('th',title));const ruler=text('th','','waterfall-track');ruler.append(axis(scale));hr.append(ruler);head.append(hr);table.append(head);const rows=document.createElement('tbody');
 for(const stage of group.items){const sr=document.createElement('tr'),name=text('td',stage.name,'stage-name');name.style.paddingLeft=(12+Math.min(stage.depth||0,8)*14)+'px';const offset=(stage.clock==='backend'?'服务端 + ':'')+fmt(stage.offset_ms);sr.append(name,text('td',offset,'number'),text('td',fmt(stage.duration_ms),'number'));const graphic=text('td','','waterfall-track');graphic.append(stageChart(stage,scale));sr.append(graphic);rows.append(sr)}table.append(rows);wrapper.append(table);section.append(wrapper);container.append(section)}return container}
let timelineOpen=false;
function absoluteIntervals(tasks){const result=[];for(const task of tasks){const origin=Date.parse(task.started_at);if(!Number.isFinite(origin))continue;if(task.category==='后台任务'){for(const stage of task.stages){if(stage.depth===0&&/^第 .* 次执行$/.test(stage.name)&&known(stage.offset_ms)&&known(stage.duration_ms))result.push({task,start:origin+stage.offset_ms,duration:stage.duration_ms,label:task.name+' · '+stage.name})}}else if(known(task.execution_ms)){result.push({task,start:origin,duration:task.execution_ms,label:task.name})}}return result}
function renderTimeline(filtered,visible){const panel=document.querySelector('#timeline');panel.hidden=!timelineOpen;if(!timelineOpen)return;panel.replaceChildren();let intervals=absoluteIntervals(visible),fallback=false;if(!intervals.length&&document.querySelector('#source').value==='本次'){intervals=absoluteIntervals(data.tasks.filter(t=>t.source==='本次'&&t.category==='API 请求')).sort((a,b)=>a.start-b.start).slice(0,50);fallback=true}const head=text('div','','timeline-head');head.append(text('strong',fallback?'同期 API 并发时间线':'任务并发时间线'),text('span','执行区间 · '+intervals.length+' 项'+(fallback?'（前 50 项）':'（当前页）'),'meta'));panel.append(head);if(!intervals.length){panel.append(text('p','这些记录没有可靠的绝对开始时间，无法对齐并发位置。','note'));return}
 const begin=Math.min(...intervals.map(i=>i.start)),end=Math.max(...intervals.map(i=>i.start+i.duration)),range=Math.max(1,end-begin),left=285,width=1080,plot=width-left-18,height=44+intervals.length*26;const scroll=text('div','','timeline-scroll'),svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,class:'timeline-svg',role:'img','aria-label':'任务按绝对开始时间对齐的执行时间线'});
 for(let tick=0;tick<=4;tick++){const x=left+plot*tick/4;svg.append(svgNode('line',{x1:x,x2:x,y1:26,y2:height,class:'timeline-line'}),svgNode('text',{x,y:16,'text-anchor':tick===0?'start':tick===4?'end':'middle'},'+'+fmt(range*tick/4)))}
 intervals.forEach((item,index)=>{const y=36+index*26;const label=svgNode('text',{x:0,y:y+10,class:'lane-label'},item.label.length>34?item.label.slice(0,33)+'…':item.label);label.append(svgNode('title',{},item.label));const rect=svgNode('rect',{x:left+plot*(item.start-begin)/range,y,width:Math.max(1,plot*item.duration/range),height:14,rx:2,class:'timeline-bar'+(item.task.state==='failed'?' failed':''),tabindex:0,'data-task-id':item.task.id,'data-start-ms':item.start,'data-duration-ms':item.duration,'aria-label':item.label+'，执行 '+fmt(item.duration)});rect.append(svgNode('title',{},item.label+' · '+new Date(item.start).toISOString()+' · 执行 '+fmt(item.duration)));svg.append(label,rect)});scroll.append(svg);panel.append(scroll)}
function detail(task){const tr=document.createElement('tr');tr.className='detail';const cell=document.createElement('td');cell.colSpan=7;
 if(!task.stages.length){cell.append(text('p','没有记录阶段计时。','note'));tr.append(cell);return tr}
 const images=task.category==='页面显示'?task.stages.filter(s=>s.depth===1):[],main=images.length?task.stages.filter(s=>s.depth!==1):task.stages;
 cell.append(text('div','阶段瀑布图 · 条形表示区间，◆ 表示时间点，○ 表示仅起点已知。','chart-legend'),stageTable(task,main));if(images.length){const disclosure=document.createElement('details');disclosure.append(text('summary','逐张图片细分'),stageTable(task,images));cell.append(disclosure)}
 if(task.error_code)cell.append(text('p',task.error_code,'note bad'));tr.append(cell);return tr}
function render(){const rows=filter(),total=Math.max(1,Math.ceil(rows.length/size));page=Math.min(page,total);body.replaceChildren();for(const task of rows.slice((page-1)*size,page*size)){const tr=document.createElement('tr');tr.dataset.task=task.id;const title=text('td','','task-name');title.append(text('div',task.name),text('small',[task.category,task.variant,task.source,local(task.started_at)].filter(Boolean).join(' · ')));tr.append(title);const state=text('td',labels[task.state]||task.state,'state '+(['succeeded','complete'].includes(task.state)?'good':['failed'].includes(task.state)?'bad':'warn'));tr.append(text('td',fmt(task.total_ms),'number'),state,text('td',fmt(task.execution_ms),'number'),text('td',fmt(task.queue_ms),'number'));tr.append(text('td',task.stage_count?task.stage_count+' 项':'—','number'));const action=document.createElement('td'),button=text('button',opened.has(task.id)?'收起':task.stage_count?'阶段耗时':'执行记录','disclosure');button.type='button';button.setAttribute('aria-expanded',String(opened.has(task.id)));button.onclick=()=>{opened.has(task.id)?opened.delete(task.id):opened.add(task.id);render()};action.append(button);tr.append(action);body.append(tr);if(opened.has(task.id))body.append(detail(task))}
 if(!rows.length){const tr=document.createElement('tr'),td=text('td','没有匹配的任务。','empty');td.colSpan=7;tr.append(td);body.append(tr)}
 document.querySelector('#count').textContent=`${rows.length} 项任务`;
 document.querySelector('#page').textContent=`${page} / ${total}`;document.querySelector('#prev').disabled=page<=1;document.querySelector('#next').disabled=page>=total;
 renderTimeline(rows,rows.slice((page-1)*size,page*size));
}
for(const id of ['search','source','category','sort'])document.querySelector('#'+id).addEventListener(id==='search'?'input':'change',()=>{page=1;if(id==='source')document.querySelector('#category').value='';render()});
document.querySelector('#prev').onclick=()=>{page--;render()};document.querySelector('#next').onclick=()=>{page++;render()};
document.querySelector('#timeline-toggle').onclick=()=>{timelineOpen=!timelineOpen;document.querySelector('#timeline-toggle').setAttribute('aria-expanded',String(timelineOpen));render()};
for(const category of [...new Set(data.tasks.map(t=>t.category))]){const option=text('option',category);option.value=category;document.querySelector('#category').append(option)}
if(data.tasks.some(t=>t.category==='页面显示'))document.querySelector('#category').value='页面显示';
const firstChart=filter().find(t=>t.stages.length);if(firstChart)opened.add(firstChart.id);
document.querySelector('#window').textContent=local(data.started_at)+' — '+local(data.finished_at)+' · '+data.measurement_mode;render();
'''


def render(report):
    payload = json.dumps(report, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    image_overview = ''
    if any(row.get('request_count') for row in report.get('image_loading', [])):
        def image_time(value):
            return f'{value/1000:.3f} s' if isinstance(value, (float, int)) else '—'
        rows = ''.join('<tr><td>'+html.escape(name(row.get('device'))+' · '+name(row.get('cache')))+'</td><td>'+str(row.get('image_count', 0))+'</td><td class="number"><strong>'+image_time(row.get('first_request_to_all_ready_ms'))+'</strong></td><td class="number">'+image_time(row.get('first_request_to_all_paint_ms'))+'</td><td class="number">'+str(row.get('peak_fetch_concurrency', 0))+'</td></tr>' for row in report.get('image_loading', []))
        image_overview = '<section style="margin-top:18px"><h2 style="font-size:17px;margin:0 0 8px">当前页图片加载时间</h2><p class="note">从第一张图片请求发出开始计时；前面的登录、列表数据等待不计入这一段。</p><div class="table"><table><thead><tr><th>场景</th><th>可用图片</th><th>全部图片就绪</th><th>全部显示准备完成</th><th>同时下载峰值</th></tr></thead><tbody>'+rows+'</tbody></table></div></section>'
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>任务与阶段耗时</title><style>'+STYLE+'</style></head><body><main><h1>任务与阶段耗时</h1><div class="meta"><span id="window"></span> · <a href="tasks.json">JSON</a> · <a href="task-stages.csv">阶段 CSV</a></div>' + image_overview + '<div class="tools"><input id="search" type="search" aria-label="搜索任务或阶段" placeholder="搜索任务或阶段"><select id="source" aria-label="记录来源"><option value="本次">本次实测</option><option value="历史">历史后台任务</option><option value="">全部记录</option></select><select id="category" aria-label="任务类型"><option value="">全部类型</option></select><select id="sort" aria-label="排序"><option value="latest">最近执行</option><option value="duration">执行耗时最长</option></select><span id="count" class="meta"></span><button id="timeline-toggle" type="button" class="timeline-toggle" aria-expanded="false" aria-controls="timeline">并发时间线</button></div><section id="timeline" class="timeline-panel" aria-label="任务并发时间线" hidden></section><div class="table"><table><thead><tr><th>任务</th><th>总耗时</th><th>状态</th><th>执行耗时</th><th>排队 / 重试等待</th><th>阶段</th><th></th></tr></thead><tbody id="rows"></tbody></table></div><div class="pager"><button id="prev" type="button">上一页</button><span id="page"></span><button id="next" type="button">下一页</button></div><p class="note">后台总耗时：创建至最后一次执行结束。阶段可包含子阶段；时间点表示开始后的时刻，不与区间相加。— 表示没有记录。</p></main><script type="application/json" id="data">'+payload+'</script><script>'+SCRIPT+'</script></body></html>'


def write_task_report(run_dir):
    run_dir = Path(run_dir)
    report = build_tasks(run_dir)
    (run_dir/'tasks.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    with (run_dir/'task-stages.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        fields = ['task_id', 'task', 'source', 'state', 'total_ms', 'execution_ms', 'stage', 'offset_ms', 'duration_ms', 'mode']
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for task in report['tasks']:
            for item in task['stages'] or [{}]:
                writer.writerow({'task_id': task['id'], 'task': task['name'], 'source': task['source'], 'state': task['state'],
                    'total_ms': task['total_ms'], 'execution_ms': task['execution_ms'], 'stage': item.get('name'),
                    'offset_ms': item.get('offset_ms'), 'duration_ms': item.get('duration_ms'), 'mode': item.get('mode')})
    (run_dir/'report.html').write_text(render(report), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    args = parser.parse_args()
    result = write_task_report(args.run_dir)
    print(json.dumps({'report': str(args.run_dir/'report.html'), 'counts': result['counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
