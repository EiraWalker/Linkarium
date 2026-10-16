"""Summarize saved before/after load probes without accessing the application."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def summarize(directory):
    documents={key:read(directory/key/'cpu.json') for key in ('before','after')}
    rows=[]
    for device in ('desktop','mobile'):
        for cache in ('cold','warm'):
            row={'device':device,'cache':cache}
            for condition,document in documents.items():
                runs=[r for r in document['live_runs'] if r['device']==device and r['cache']==cache]
                if len(runs)!=3 or any(r['status']!='complete' or r['images']['status']!='complete' for r in runs):
                    raise ValueError('three completed samples required per condition')
                row[condition]={'image_ms':statistics.median(r['images']['first_request_to_all_ready_ms'] for r in runs),
                    'navigation_ms':statistics.median(r['preview']['all_ready_ms'] for r in runs),
                    'cpu_ms':statistics.median(r['process']['process_cpu_ms'] for r in runs),
                    'wall_ms':statistics.median(r['process']['window_wall_ms'] for r in runs),
                    'image_samples_ms':[r['images']['first_request_to_all_ready_ms'] for r in runs],
                    'navigation_samples_ms':[r['preview']['all_ready_ms'] for r in runs],
                    'image_counts':[r['images']['image_count'] for r in runs],
                    'encoded_bytes':[r['images']['encoded_bytes'] for r in runs]}
            if row['before']['image_counts']!=row['after']['image_counts'] or row['before']['encoded_bytes']!=row['after']['encoded_bytes']:
                raise ValueError('before/after images or encoded bytes changed')
            row['reduction_percent']=100*(1-row['after']['image_ms']/row['before']['image_ms'])
            rows.append(row)
    requests={}
    for condition,d in documents.items():
        counts=Counter()
        for r in d['live_runs']:
            for group in r['network']:
                if group['route'].startswith('/api/v1/'):
                    counts[group['route']]+=group['request_count']
        requests[condition]=dict(counts)
    paired=read(directory/'paired'/'comparison.json')
    sweep=read(directory/'concurrency-check.json')
    concurrency=[]
    for device in ('desktop','mobile'):
        for cap in (4,6,8):
            selected=[r for r in sweep['runs'] if r['device']==device and r['cap']==cap]
            concurrency.append({'device':device,'cap':cap,'count':len(selected),
                'image_ms':statistics.median(r['image_ms'] for r in selected),
                'cpu_ms':statistics.median(r['process_cpu_ms'] for r in selected)})
    return {'started_at':documents['before']['started_at'],'finished_at':sweep['finished_at'],
            'rows':rows,'requests':requests,'cpu_comparison':paired['comparison'],
            'concurrency':concurrency,'selected_concurrency':6,
            'cleanup_verified':all(d['temporary_web_closed'] and d['temporary_sessions_deleted'] and d['micro']['temporary_session_deleted'] for d in documents.values()) and paired['temporary_session_deleted'] and sweep['temporary_session_deleted'],
            'all_samples_complete':all(d['status']=='complete' for d in documents.values()) and paired['status']=='complete' and sweep['status']=='complete'}


STYLE='''
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f4f6f8;color:#243046;font-size:14px}*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:auto;padding:28px 22px 48px}
h1{font-size:25px;margin:0 0 10px}h2{font-size:18px;margin:0 0 12px}p{line-height:1.8;margin:8px 0}.muted{font-size:13px;color:#69768b}section{background:white;border:1px solid #dce2e9;border-radius:6px;padding:20px;margin-top:18px}
table{width:100%;border-collapse:collapse;white-space:nowrap}td,th{padding:10px 12px;text-align:left;border-bottom:1px solid #e6ebf1}th{background:#f7f9fb;color:#53627a;font-size:13px}.num{text-align:right;font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}select{font:inherit;border:1px solid #ccd5e2;border-radius:4px;background:white;padding:7px 12px;color:inherit}
.chart-row{display:grid;grid-template-columns:130px 1fr;align-items:center;margin-top:22px}.track{height:19px;margin:5px 0;background:#f4f6f8;border-radius:2px}.bar{height:100%;border-radius:2px;min-width:1px;background:#b9c5d5}.bar.after{background:#3579dc}
.legend{display:flex;gap:18px;font-size:12px;color:#69768b;margin:12px 0}.key{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:5px}.changes{display:grid;grid-template-columns:1fr 1fr;gap:18px}.changes strong{display:block}a{color:#1563dd}summary{cursor:pointer;color:#53627a}details{margin-top:14px}
@media(max-width:650px){main{padding:18px 12px}section{padding:14px}h1{font-size:22px}.chart-row{grid-template-columns:90px 1fr}.changes{grid-template-columns:1fr}}
'''


SCRIPT=r'''
const d=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const ms=n=>`${n.toFixed(1)} ms`,pct=n=>`${n.toFixed(1)}%`;
const name=r=>(r.device==='desktop'?'桌面':'手机')+' · '+(r.cache==='cold'?'首次加载':'再次加载');
$('date').textContent=`${new Date(d.started_at).toLocaleString('zh-CN')} — ${new Date(d.finished_at).toLocaleTimeString('zh-CN')} · 本机服务 8001 · 每场景 3 次，取中位数`;
function draw(){const metric=$('clock').value,max=Math.max(...d.rows.flatMap(r=>[r.before[metric],r.after[metric]]));
 $('times').innerHTML=d.rows.map(r=>`<tr><td>${name(r)}</td><td class="num">${ms(r.before[metric])}</td><td class="num"><strong>${ms(r.after[metric])}</strong></td><td class="num">${pct(100*(1-r.after[metric]/r.before[metric]))}</td></tr>`).join('');
 $('bars').innerHTML=d.rows.map(r=>`<div class="chart-row"><span>${name(r)}</span><div><div class="track" title="优化前 ${ms(r.before[metric])}"><div class="bar" style="width:${r.before[metric]/max*100}%"></div></div><div class="track" title="优化后 ${ms(r.after[metric])}"><div class="bar after" style="width:${r.after[metric]/max*100}%"></div></div></div></div>`).join('');
}
$('clock').addEventListener('change',draw);draw();
const routes=[['网站列表','/api/v1/bookmarks'],['文件夹','/api/v1/folders'],['任务批次列表','/api/v1/job-groups'],['整组任务明细','/api/v1/job-groups/:id'],['缩略图','/api/v1/snapshots/:id/thumbnail']];
$('requests').innerHTML=routes.map(([title,route])=>`<tr><td>${title}</td><td class="num">${d.requests.before[route]??0}</td><td class="num">${d.requests.after[route]??0}</td></tr>`).join('');
const names={bookmarks:'网站列表（未改，作为波动参考）',folders:'文件夹统计',job_groups:'任务批次列表',desktop_200:'桌面图片 · 200',desktop_304:'桌面缓存验证 · 304',mobile_200:'手机图片 · 200',mobile_304:'手机缓存验证 · 304'};
$('cpu').innerHTML=d.cpu_comparison.map(r=>`<tr><td>${names[r.label]}</td><td class="num">${ms(r.before.cpu_per_request_ms)}</td><td class="num">${ms(r.after.cpu_per_request_ms)}</td><td class="num">${pct(r.cpu_reduction_percent)}</td></tr>`).join('');
$('process').innerHTML=d.rows.map(r=>`<tr><td>${name(r)}</td><td class="num">${ms(r.before.cpu_ms)}</td><td class="num">${ms(r.after.cpu_ms)}</td><td class="num">${pct(100*(r.after.cpu_ms/r.before.cpu_ms-1))}</td></tr>`).join('');
$('caps').innerHTML=d.concurrency.map(r=>`<tr><td>${r.device==='desktop'?'桌面':'手机'}</td><td class="num">${r.cap}</td><td class="num">${ms(r.image_ms)}</td><td class="num">${ms(r.cpu_ms)}</td></tr>`).join('');
$('samples').innerHTML=d.rows.map(r=>`<p>${name(r)}：优化前 ${r.before.image_samples_ms.map(ms).join(' / ')}；优化后 ${r.after.image_samples_ms.map(ms).join(' / ')}</p>`).join('');
'''


def render(data):
    payload=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>加载速度优化对照</title><style>'''+STYLE+'''</style></head><body><main>
<h1>加载速度优化对照</h1><p id="date" class="muted"></p>
<p><strong>已应用：提高图片并发、合并预览布局读写、消除初始化重复请求、按需加载任务明细，并减少后端目录与任务统计。</strong></p>
<section><h2>45 张图片加载时间</h2><div class="toolbar"><label for="clock">计时范围</label><select id="clock"><option value="image_ms">首张图片开始请求 → 本页全部图片就绪</option><option value="navigation_ms">打开页面 → 本页全部图片就绪</option></select></div>
<p class="muted">同一批图片、相同总字节数。48 张卡片中有 45 张可用快照；缺图未算作成功。手机为 Chromium 尺寸模拟。</p>
<div class="scroll"><table><thead><tr><th>场景</th><th class="num">优化前</th><th class="num">优化后</th><th class="num">耗时减少</th></tr></thead><tbody id="times"></tbody></table></div>
<div class="legend"><span><i class="key" style="background:#b9c5d5"></i>优化前</span><span><i class="key" style="background:#3579dc"></i>优化后</span></div><div id="bars"></div>
<details><summary>查看逐次加载用时</summary><div id="samples" class="muted"></div></details></section>
<section><h2>页面少做了哪些请求</h2><p class="muted">以下为各阶段 12 次正式服务页面观测中的请求总数。任务弹窗关闭，轻量摘要仍然自动更新。</p>
<table><thead><tr><th>请求</th><th class="num">优化前</th><th class="num">优化后</th></tr></thead><tbody id="requests"></tbody></table></section>
<section><h2>后端 CPU：同进程交替对照</h2><p>使用同一会话、真实库和预览文件，交替执行 Git 基线与当前 GET 实现，重复 3 轮。表内为每次请求 CPU 的中位数；图片内容及 ETag 校验一致。</p>
<div class="scroll"><table><thead><tr><th>接口</th><th class="num">优化前 CPU / 次</th><th class="num">优化后 CPU / 次</th><th class="num">CPU 减少</th></tr></thead><tbody id="cpu"></tbody></table></div>
<p class="muted">Windows CPU 记账有粒度，且测试期间存在运行环境波动；未修改的网站列表也产生差值，不能将其算作代码优化收益。目录遍历与 ORM 对象数量的减少另外由回归测试验证，不只依赖短时计时。</p>
<details><summary>整次页面观测窗口的 Web CPU</summary><table><thead><tr><th>场景</th><th class="num">优化前</th><th class="num">优化后</th><th class="num">变化</th></tr></thead><tbody id="process"></tbody></table>
<p class="muted">这是导航、初始化接口和图片加载整个观测窗口的进程 CPU，不是单独图片阶段。瞬时并发提高会改变线程调度与 CPU 使用；本次手机首次加载的累计 CPU 增加，不能声称所有场景 CPU 都降低。正式服务还可能接收其他页面请求。</p></details></section>
<section><h2>并发上限的补充对照</h2><p>在优化后的服务上，仅于隔离浏览器内临时设置 4 / 6 / 8 张并发，轮换顺序，各测 3 次。</p>
<table><thead><tr><th>设备</th><th class="num">并发</th><th class="num">图片加载</th><th class="num">整次 Web CPU</th></tr></thead><tbody id="caps"></tbody></table>
<p class="muted">三组均使用新上下文和相同请求拦截，HTTP 缓存均关闭，绝对值与上方正常缓存测试分别呈现。正式配置使用 6 张，保留首屏优先和整页加载。</p></section>
<section><h2>本次实现与验证</h2><div class="changes"><p><strong>图片加载</strong>桌面 2 / 手机 1 改为最多 6 张；先测量全部卡片，再更新预览位置，每个滚动容器只测量一次起点。下载、版本区分和退出登录清理保留。</p>
<p><strong>目录统计</strong>从有收藏的文件夹向上累加，复用已计算的父目录关系，替换对每个文件夹分别扫描整棵树。</p>
<p><strong>任务列表</strong>数据库先选择历史页和当前任务，再计算详细进度；当前摘要不受历史翻页影响。弹窗打开时才下载整组任务明细。</p>
<p><strong>缩略图响应</strong>复用请求中已读取的库代次，省去一次额外状态数据库连接。每张图片仍先鉴权，再进行缓存验证。</p></div>
<p class="muted">已验证全部可用图片、跨页保留、历史版本、缺图重试、超时释放、401 清理、任务进度自动更新及历史选择保持。Web 已重载；原采集 worker 保持运行。没有修改图片质量、业务数据或数据库结构。</p></section>
<p class="muted">原始数据：<a href="before/cpu.json">优化前</a> · <a href="after/cpu.json">优化后</a> · <a href="paired/comparison.json">CPU 交替样本</a> · <a href="concurrency-check.json">并发对照</a> · <a href="comparison.json">摘要</a></p>
</main><script type="application/json" id="data">'''+payload+'''</script><script>'''+SCRIPT+'''</script></body></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    data=summarize(args.directory)
    (args.directory/'comparison.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.directory/'report.html').write_text(render(data),encoding='utf-8')
    print(json.dumps({'report':str(args.directory/'report.html'),'complete':data['all_samples_complete'],'cleanup':data['cleanup_verified']}))


if __name__=='__main__':
    main()
