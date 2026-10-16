"""Build a CPU diagnosis from a completed, local-only performance_cpu run."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def summarize(data):
    live = []
    for device in ('desktop', 'mobile'):
        for cache in ('cold', 'warm'):
            rows = [r for r in data['live_runs'] if r['device'] == device and r['cache'] == cache]
            if not rows:
                continue
            cpu = sum(r['process']['process_cpu_ms'] for r in rows)
            wall = sum(r['process']['window_wall_ms'] for r in rows)
            live.append({'device':device, 'cache':cache, 'count':len(rows),
                'image_ms':statistics.mean(r['images']['first_request_to_all_ready_ms'] for r in rows),
                'window_ms':wall/len(rows), 'cpu_ms':cpu/len(rows), 'one_core_percent':100*cpu/wall,
                'machine_percent':100*cpu/wall/data['logical_processors']})
    requests = {r['request_id']:r for r in data['requests'] if r.get('request_id')}
    serial = []
    for cache in ('cold', 'warm'):
        runs = [r for r in data['instrumented_runs'] if r['device']=='mobile' and r['cache']==cache]
        measured = []
        for run in runs:
            network = [r for group in run['network'] if group['route']=='/api/v1/snapshots/:id/thumbnail'
                       for r in group['request_samples']]
            matched = [requests[r['request_id']] for r in network if r.get('request_id') in requests]
            images = run['images']
            if (len(matched)!=images['request_count'] or images['status']!='complete'
                    or images['peak_fetch_concurrency']!=1 or images['serial_request_gaps_ms'] is None):
                continue
            app = sum(r['app_wall_ms'] for r in matched)
            other = images['request_sum_ms']-app
            if other < 0:
                continue
            measured.append({'image_ms':images['first_request_to_all_ready_ms'], 'server_ms':app,
                'sql_ms':sum(r['sql_ms'] for r in matched), 'hash_ms':sum(r['hash_ms'] for r in matched),
                'http_other_ms':other, 'queue_ms':images['serial_request_gaps_ms'],
                'tail_ms':images['last_body_to_all_ready_ms'], 'images':len(matched)})
        if measured:
            averaged={key:statistics.mean(r[key] for r in measured) for key in measured[0]}
            serial.append({'cache':cache,'count':len(measured),**averaged,
                'server_share_percent':100*sum(r['server_ms'] for r in measured)/sum(r['image_ms'] for r in measured)})
    batches = [{**r, 'cpu_per_request_ms':r['cpu_ms']/r['count'],
                'wall_per_request_ms':r['wall_ms']/r['count'],
                'sql_per_request_ms':r['sql_ms']/r['count'],
                'hash_per_request_ms':r['hash_ms']/r['count'],
                'sql_per_request':r['sql_count']/r['count']} for r in data['micro']['batches']]
    return {'started_at':data['started_at'],'finished_at':data['finished_at'], 'status':data['status'],
            'live':live,'serial':serial,'batches':batches,'idle':data.get('idle'),
            'logical_processors':data['logical_processors'],'profiles':data['micro']['profiles'],
            'request_counts':dict(Counter(r['route'] for r in data['requests'])),
            'temporary_web_closed':data['temporary_web_closed'],
            'temporary_sessions_deleted':data['temporary_sessions_deleted'] and data['micro']['temporary_session_deleted']}


STYLE = '''
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;font-size:14px;color:#243046;background:#f4f6f8}*{box-sizing:border-box}body{margin:0}
main{max-width:1150px;padding:28px 22px 48px;margin:auto}h1{font-size:25px;margin:0 0 8px}h2{font-size:18px;margin:0 0 12px}h3{font-size:15px}
p{line-height:1.8;margin:8px 0}.muted{color:#68758b;font-size:13px}section{background:#fff;border:1px solid #dae1ea;border-radius:6px;padding:20px;margin-top:18px}
table{width:100%;border-collapse:collapse;white-space:nowrap}td,th{padding:10px 12px;text-align:left;border-bottom:1px solid #e6ebf2}th{background:#f7f9fb;color:#53627a;font-size:13px}.num{text-align:right;font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}
.breakdown{display:flex;height:28px;overflow:hidden;border-radius:4px;margin:18px 0 12px}.breakdown span{min-width:1px}.key{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px}
.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}select{font:inherit;border:1px solid #ccd5e2;border-radius:4px;padding:7px 12px;background:white;color:inherit}
.bar{height:10px;border-radius:2px;background:#3579dc;margin-top:6px;max-width:100%}a{color:#1563dd}details{margin-top:12px}summary{cursor:pointer;color:#53627a}
.finding{display:grid;grid-template-columns:1fr 1fr;gap:20px}.finding p{margin:0}.finding strong{display:block;margin-bottom:4px}
@media(max-width:650px){main{padding:18px 12px}section{padding:14px}h1{font-size:22px}.finding{grid-template-columns:1fr}}
'''

SCRIPT = r'''
const d=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const ms=n=>Number.isFinite(n)?`${n.toFixed(1)} ms`:'—',pct=n=>`${n.toFixed(1)}%`;
const scene=r=>(r.device==='desktop'?'桌面':'手机')+' · '+(r.cache==='cold'?'首次加载':'再次加载');
const names={bookmarks:'网站列表（48 条）',folders:'文件夹与数量统计',job_groups:'后台任务批次列表',thumbnail_200:'图片首次响应 · 200',thumbnail_304:'图片缓存验证 · 304'};
$('date').textContent=`${new Date(d.started_at).toLocaleString('zh-CN')} · 正式服务 8001 · ${d.logical_processors} 个逻辑处理器`;
$('rounds').textContent=`桌面、手机分别测冷暖加载，每个场景 ${d.live.map(r=>r.count).join(' / ')} 次，表内为均值。每页加载 45 张可用图片。`;
$('live').innerHTML=d.live.map(r=>`<tr><td>${scene(r)}</td><td class="num"><strong>${ms(r.image_ms)}</strong></td><td class="num">${ms(r.window_ms)}</td><td class="num">${ms(r.cpu_ms)}</td><td class="num">${pct(r.one_core_percent)}</td><td class="num">${pct(r.machine_percent)}</td></tr>`).join('');
$('micro').innerHTML=d.batches.map(r=>`<tr><td>${names[r.label]}</td><td class="num">${r.count}</td><td class="num"><strong>${ms(r.cpu_per_request_ms)}</strong></td><td class="num">${ms(r.wall_per_request_ms)}</td><td class="num">${r.sql_per_request.toFixed(0)}</td><td class="num">${ms(r.sql_per_request_ms)}</td><td class="num">${ms(r.hash_per_request_ms)}</td></tr>`).join('');
const max=Math.max(...d.batches.map(r=>r.cpu_per_request_ms));
$('cpu-bars').innerHTML=d.batches.map(r=>`<p>${names[r.label]}：${ms(r.cpu_per_request_ms)} CPU / 请求<span class="bar" style="display:block;width:${r.cpu_per_request_ms/max*100}%"></span></p>`).join('');
const thumb=d.batches.find(r=>r.label==='thumbnail_304');
if(thumb)$('budget').textContent=`以本次独立批量测试估算，45 次缓存验证约需 ${(45*thumb.cpu_per_request_ms).toFixed(0)} ms 的 Web 进程 CPU 工作量。它包含 Python、SQLite、哈希和系统调用，不能全算作解释器开销。`;
const folderProfile=d.profiles.find(p=>p.label==='folders'),descendants=folderProfile?.top_self.find(r=>r.function==='descendant_ids');
if(descendants)$('folder-calls').textContent=(descendants.calls/folderProfile.count).toFixed(0);
const jobBatch=d.batches.find(r=>r.label==='job_groups');if(jobBatch)$('job-cpu').textContent=jobBatch.cpu_per_request_ms.toFixed(1);
const navCount=d.live.reduce((n,r)=>n+r.count,0),initialCounts=['/api/v1/bookmarks','/api/v1/folders','/api/v1/job-groups'].map(k=>d.request_counts[k]??0);
$('duplicates').textContent=`${navCount} 次临时页面观测中，网站列表、文件夹和任务列表分别请求 ${initialCounts.join(' / ')} 次。`+(initialCounts.some(n=>n>navCount)?'存在额外请求，需要结合后台完成事件与初始化流程判断。':'未观察到这些接口初始化重复加载。');
if(!d.serial.length)$('serial-section').hidden=true;
if(d.idle)$('idle').textContent=`未发起探测的 3 秒参考窗口：Web 进程约占一个核的 ${pct(d.idle.one_core_percent)}（整机 ${pct(d.idle.machine_percent)}）。实际页面加载时明显高于此值；仍可能包含用户页面自己的轮询。`;
const select=$('serial-select');
d.serial.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.cache==='cold'?'手机首次加载':'手机再次加载';select.append(o)});
function draw(){const r=d.serial[Number(select.value)];if(!r)return;
 const phases=[['服务端应用处理',r.server_ms,'#3579dc'],['其余请求链路与调度',r.http_other_ms,'#80a9e4'],['前端逐张推进间隔',r.queue_ms,'#b9c5d5'],['末张下载后到全部就绪',r.tail_ms,'#27a8ad']];
 $('serial-total').textContent=`${ms(r.image_ms)}，其中服务端应用处理 ${ms(r.server_ms)}，约占 ${pct(r.server_share_percent)}`;
 $('serial-bar').innerHTML=phases.map(([label,t,color])=>`<span title="${label} ${ms(t)}" style="background:${color};width:${t/r.image_ms*100}%"></span>`).join('');
 $('serial-rows').innerHTML=phases.map(([label,t,color])=>`<tr><td><span class="key" style="background:${color}"></span>${label}</td><td class="num">${ms(t)}</td></tr>`).join('');
 $('nested').textContent=`服务端处理内，SQL 驱动调用合计 ${ms(r.sql_ms)}，JPEG 哈希合计 ${ms(r.hash_ms)}。它们已包含在蓝色区间内，不再相加。这些是区间耗时，尚未分别测得纯 CPU。`;
}
select.value=String(Math.max(0,d.serial.findIndex(r=>r.cache==='warm')));select.addEventListener('change',draw);draw();
$('profiles').innerHTML=d.profiles.map(p=>`<h3>${names[p.label]} · 独立分析 ${p.count} 次</h3><div class="scroll"><table><thead><tr><th>函数</th><th>来源</th><th class="num">调用次数</th><th class="num">独占区间耗时</th></tr></thead><tbody>${p.top_self.slice(0,6).map(r=>`<tr><td></td><td></td><td class="num">${r.calls}</td><td class="num">${ms(r.self_wall_ms)}</td></tr>`).join('')}</tbody></table></div>`).join('');
// Function names are text, never HTML: profiles can include Python built-in reprs.
let rows=[...document.querySelectorAll('#profiles tbody tr')],index=0;
for(const p of d.profiles)for(const r of p.top_self.slice(0,6)){rows[index].cells[0].textContent=r.function;rows[index++].cells[1].textContent=r.file+':'+r.line}
'''


def render(report):
    payload=json.dumps(report,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Python 后端 CPU 对加载速度的影响</title><style>'''+STYLE+'''</style></head><body><main>
<h1>Python 后端 CPU 对加载速度的影响</h1><p id="date" class="muted"></p>
<p><strong>将后端 CPU 工作量与图片加载时间分别测量。</strong>不能把所有等待都归因于图片体积，也不能仅凭整机 CPU 占用低就排除后端成本。</p>
<section><h2>正式服务实测：图片加载与 CPU 工作量</h2><p id="rounds" class="muted"></p>
<div class="scroll"><table><thead><tr><th>场景</th><th class="num">首图请求 → 全部就绪</th><th class="num">整次观测窗口</th><th class="num">窗口内 Web CPU</th><th class="num">单核占用</th><th class="num">整机占用</th></tr></thead><tbody id="live"></tbody></table></div>
<p class="muted">CPU 时间是进程实际运行的累计时间，观测窗口还包含图片开始前的接口和浏览器等待。CPU 列不能除以“图片加载”列推算贡献率；多线程 CPU 时间也可能超过窗口长度。</p><p id="idle" class="muted"></p></section>
<section><h2>一次请求实际消耗多少后端 CPU</h2><p>在独立 Python 进程中使用同一 Django 中间件、鉴权和真实数据，预热后成批执行 GET，排除浏览器、HTTP 网络和其他请求线程的 CPU。另行进行函数分析，避免把分析器开销算入这里。</p>
<div class="scroll"><table><thead><tr><th>接口</th><th class="num">次数</th><th class="num">CPU / 次</th><th class="num">区间耗时 / 次</th><th class="num">SQL / 次</th><th class="num">SQL 耗时 / 次</th><th class="num">JPEG 哈希 / 次</th></tr></thead><tbody id="micro"></tbody></table></div>
<div id="cpu-bars"></div><p id="budget"></p>
<p class="muted">Windows GetProcessTimes / GetThreadTimes 的记账存在粒度，因此采用批次累计平均，不将短请求的 0 ms 当成零成本。批次使用代表性的 45 份已有预览，已预热文件系统缓存；不能当作冷磁盘基准。SQL 计时不含取回后的 ORM 对象构造，SQL/哈希列均为区间耗时，并非各自独占 CPU。</p></section>
<section id="serial-section"><h2>这对整页图片加载影响多大（仅串行样本）</h2><div class="toolbar"><label for="serial-select">同次请求关联</label><select id="serial-select"></select></div>
<p id="serial-total"></p><div id="serial-bar" class="breakdown"></div><table><tbody id="serial-rows"></tbody></table><p id="nested" class="muted"></p>
<p class="muted">来自临时 Web 实例的 3 轮平均，逐请求按 Request ID 关联。手机一次只加载 1 张，请求互不重叠，以上区间才可以相加。服务端处理含 CPU、I/O 与调度等待，约占比不是“换语言就能提速的比例”。桌面有 2 张并发，不套用这项加法。</p>
<p class="muted">其余请求链路包含 WSGI 外排队、HTTP 传递、浏览器接收与探测开销，尚未进一步拆分。临时实例增加了时钟、SQL 与请求关联探针，因此这里与正式服务的绝对值分别呈现。</p></section>
<section><h2>本次观测</h2><div class="finding"><p><strong>缩略图与缓存验证</strong>图片响应包含鉴权、SQL、路径检查、文件读取与 ETag 计算。304 省去图片响应体，验证成本仍存在。具体次数与耗时见上方批量测量。</p>
<p><strong>目录处理</strong>本次函数明细中，每次请求调用 descendant_ids <span id="folder-calls">—</span> 次；未出现在热点列表时显示“—”。目录算法是否仍存在重复遍历，需要结合当时源码判断。</p>
<p><strong>后台任务列表</strong>单次请求约 <span id="job-cpu">—</span> ms CPU。轮询频率和参与统计的历史数据量会共同影响页面加载时的资源争用。</p>
<p><strong>页面初始化：接口重复加载</strong><span id="duplicates"></span></p></div></section>
<section><h2>测量边界</h2><p>应按本次 CPU、请求次数和函数明细判断优化目标。当前为并发请求时，不把各请求的服务端耗时直接相加为整页加载耗时；没有串行样本时，上方整页分段不显示。</p>
<p>当前证据不能给出 Rust 重写的提速倍数。SQLite、文件系统和浏览器阶段不会因为替换 Python 自动消失；并发、重复请求与数据量不变时，重写也保留这些等待。</p>
<p class="muted">此处的“图片渲染”是已有 JPEG 在网站库中的加载显示。浏览器负责 JPEG 解码与绘制，Python 负责接口和图片响应；没有启动新的网页抓取或快照生成。正式服务未重启，鉴权、缓存、并发和业务代码没有修改。</p></section>
<details><summary>函数耗时明细（单独分析样本）</summary><p class="muted">cProfile 的独占区间耗时用于定位函数；受分析器影响，不能替代上方未经分析器的 CPU 数值，也不能将父子累计耗时相加。</p><div id="profiles"></div></details>
<p class="muted"><a href="cpu.json">原始测量</a> · <a href="cpu-summary.json">摘要</a> · <a href="../image-loading-20261016-090122/report.html">逐张图片加载瀑布图</a></p>
</main><script id="data" type="application/json">'''+payload+'''</script><script>'''+SCRIPT+'''</script></body></html>'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    report=summarize(json.loads((args.directory/'cpu.json').read_text(encoding='utf-8')))
    (args.directory/'cpu-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.directory/'report.html').write_text(render(report),encoding='utf-8')
    print(json.dumps({'report':str(args.directory/'report.html'),'status':report['status']}))


if __name__=='__main__':
    main()
