"""Offline image-loading diagnosis, using existing measurements only."""
import argparse
import json
from pathlib import Path
import statistics

from performance_image_loading import summarize_images


def build_report(directory):
    directory = Path(directory)
    baseline = json.loads((directory/'baseline-browser.json').read_text(encoding='utf-8'))
    experiment = json.loads((directory/'concurrency-experiment.json').read_text(encoding='utf-8'))
    comparison = []
    for device in ('desktop', 'mobile'):
        arms = {}
        for condition, recorded in (('original', 'original'), ('cap6', 'browser_only_6')):
            runs = [run for run in experiment['runs'] if run['device'] == device and run['condition'] == recorded]
            complete = [run for run in runs if run.get('status') == 'complete'
                        and run['summary'].get('all_ready_span_ms') is not None]
            samples = [run['summary']['all_ready_span_ms'] for run in complete]
            arms[condition] = {'samples_ms': samples, 'median_ms': statistics.median(samples) if samples else None,
                              'completed': len(complete), 'total': len(runs)}
        control, changed = arms['original']['median_ms'], arms['cap6']['median_ms']
        comparison.append({'device': device, **arms,
                           'reduction_percent': 100*(control-changed)/control if control and changed is not None else None})
    return {'started_at': baseline['started_at'], 'finished_at': experiment['finished_at'],
            'metric': 'first_thumbnail_request_to_all_available_current_page_images_ready',
            'baseline': [summarize_images(run) for run in baseline.get('runs', [])],
            'comparison': comparison,
            'experiment_status': experiment.get('status'),
            'application_files_modified': experiment.get('application_files_modified'),
            'temporary_session_deleted': experiment.get('temporary_session_deleted'),
            'browser_version': baseline.get('browser_version')}


STYLE = '''
:root{font-family:system-ui,"Microsoft YaHei",sans-serif;color:#1f2937;background:#f4f6f8;font-size:14px}
*{box-sizing:border-box}body{margin:0}main{max-width:1160px;margin:auto;padding:28px 22px 48px}
h1{font-size:26px;margin:0 0 8px}h2{font-size:18px;margin:0 0 12px}h3{font-size:14px;margin:16px 0 8px}
p{line-height:1.75;margin:8px 0}.muted{color:#637086;font-size:13px}.lead{font-size:16px}
section{background:white;border:1px solid #dce2e9;border-radius:6px;padding:20px;margin-top:20px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid #e8edf2}
th{background:#f7f9fb;font-size:13px;color:#526178}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
strong{font-weight:650}a{color:#1563dd}select{font:inherit;padding:7px 12px;border:1px solid #ccd5e2;border-radius:4px;background:white;color:inherit}
.chart-tools{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:#526178}
.key:before{content:"";display:inline-block;width:11px;height:11px;border-radius:2px;background:var(--color);margin-right:5px;vertical-align:-1px}
svg{display:block;width:100%;height:auto}svg text{font:12px system-ui,"Microsoft YaHei",sans-serif;fill:#526178}.grid{stroke:#e8edf2;stroke-width:1}
.request-wait{fill:#3579dc}.request-body{fill:#27a8ad}.request-client{fill:#bac5d3}.decomp{height:26px;display:flex;border-radius:4px;overflow:hidden;margin:16px 0}
.decomp span{min-width:2px}.chip{display:inline-block;font-size:12px;background:#eef4ff;color:#1563dd;padding:3px 7px;border-radius:4px}
.explain{display:grid;grid-template-columns:1fr 1fr;gap:20px}.explain p{margin:0}.explain strong{display:block;margin-bottom:4px}
details{margin-top:10px}summary{cursor:pointer;color:#526178}.kpi{font-size:30px;color:#1563dd;font-weight:650;margin:10px 0}
.bar-sample{height:8px;background:#3579dc;border-radius:2px;margin:5px 0}.bar-sample.changed{background:#27a8ad}
@media(max-width:650px){main{padding:18px 12px}section{padding:14px}h1{font-size:23px}.explain{grid-template-columns:1fr}th,td{padding:9px 10px}.muted{font-size:12px}}
'''


SCRIPT = r'''
const data=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
const fmt=n=>Number.isFinite(n)?`${(n/1000).toFixed(3)} 秒`:'未完成 / 无记录';
const ms=n=>Number.isFinite(n)?`${n.toFixed(1)} ms`:'无记录';
const device=n=>n==='desktop'?'桌面':'手机';
const cache=n=>n==='cold'?'首次加载':'再次加载';
const size=n=>n>=1e6?`${(n/1e6).toFixed(2)} MB`:`${(n/1000).toFixed(1)} KB`;
$('date').textContent=`实测时间：${new Date(data.started_at).toLocaleString('zh-CN')} · 本机 8001 · Chromium / 手机尺寸模拟`;
$('baseline').innerHTML=data.baseline.map(r=>`<tr><td>${device(r.device)} · ${cache(r.cache)}</td><td class="num">${r.image_count} / ${r.available_images}</td><td class="num"><strong>${fmt(r.first_request_to_all_ready_ms)}</strong></td><td class="num">${fmt(r.first_request_to_all_paint_ms)}</td><td class="num">${r.peak_fetch_concurrency}</td><td class="num">${size(r.transfer_bytes)}</td></tr>`).join('');
const mobile=data.baseline.find(r=>r.device==='mobile'&&r.cache==='warm');
if(mobile && Number.isFinite(mobile.serial_request_gaps_ms) && Number.isFinite(mobile.first_request_to_all_ready_ms)){
 const phases=[['各张图片的请求过程',mobile.request_sum_ms,'#3579dc'],['上一张结束到下一张发出的间隔',mobile.serial_request_gaps_ms,'#bac5d3'],['最后一张到全部就绪',mobile.last_body_to_all_ready_ms,'#27a8ad']];
 $('serial-total').textContent=fmt(mobile.first_request_to_all_ready_ms);
 $('serial-bar').innerHTML=phases.map(([name,n,color])=>`<span style="width:${n/mobile.first_request_to_all_ready_ms*100}%;background:${color}" title="${name}：${ms(n)}"></span>`).join('');
 $('serial-table').innerHTML=phases.map(([name,n])=>`<tr><td>${name}</td><td class="num">${ms(n)}</td></tr>`).join('');
}
$('comparison').innerHTML=data.comparison.map(r=>`<tr><td>${device(r.device)}</td><td class="num">${fmt(r.original.median_ms)}</td><td class="num">${fmt(r.cap6.median_ms)}</td><td class="num"><strong>${r.reduction_percent?.toFixed(0)??'—'}%</strong></td><td class="num">${r.original.completed} / ${r.original.total}；${r.cap6.completed} / ${r.cap6.total}</td></tr>`).join('');
$('samples').innerHTML=data.comparison.map(r=>`<p>${device(r.device)}原策略：${r.original.samples_ms.map(ms).join(' / ')}；并发 6 张：${r.cap6.samples_ms.map(ms).join(' / ')}</p>`).join('');
const select=$('scene');
data.baseline.forEach((r,i)=>{const opt=document.createElement('option');opt.value=i;opt.textContent=`${device(r.device)} · ${cache(r.cache)}`;select.appendChild(opt)});
const NS='http://www.w3.org/2000/svg';
function el(tag,attrs,parent,text){const n=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;parent.appendChild(n);return n}
function draw(){
 const r=data.baseline[Number(select.value)],svg=$('waterfall');svg.replaceChildren();
 const end=Math.max(r.first_request_to_all_paint_ms??0,...r.requests.map(x=>x.end_ms));
 if(!r.request_count||!(end>0)){$('chart-note').textContent='没有可用图片请求记录';return}
 const width=1000,left=78,right=24,top=34,rowHeight=23,height=top+r.requests.length*rowHeight+28;
 svg.setAttribute('viewBox',`0 0 ${width} ${height}`);const x=t=>left+(width-left-right)*t/end;
 for(let i=0;i<=5;i++){const time=end*i/5;el('line',{x1:x(time),x2:x(time),y1:top-8,y2:height-20,class:'grid'},svg);el('text',{x:x(time),y:16,'text-anchor':i===0?'start':i===5?'end':'middle'},svg,`${time.toFixed(0)} ms`)}
 r.requests.forEach((q,i)=>{
  const y=top+i*rowHeight;
  el('text',{x:6,y:y+11},svg,`图片 ${q.number}`);
  const g=el('g',{'data-image':q.number},svg);
  el('title',{},g,`图片 ${q.number}：${ms(q.start_ms)} 发出，${ms(q.end_ms)} 下载结束；请求过程 ${ms(q.duration_ms)}；结束到前端观察就绪 ${ms(q.body_to_observed_ready_ms)}`);
  const rect=(start,finish,cls)=>{if(Number.isFinite(start)&&Number.isFinite(finish)&&finish>=start)el('rect',{x:x(start),y,width:Math.max(.7,x(finish)-x(start)),height:14,rx:2,class:cls,'data-start-ms':start,'data-end-ms':finish},g)};
  if(Number.isFinite(q.headers_ms)&&q.headers_ms>=q.start_ms&&q.headers_ms<=q.end_ms){rect(q.start_ms,q.headers_ms,'request-wait');rect(q.headers_ms,q.end_ms,'request-body')}
  else rect(q.start_ms,q.end_ms,'request-wait');
  rect(q.end_ms,q.observed_ready_ms,'request-client');
 });
 $('chart-note').textContent=`0 ms 是第一张图片开始请求。最后一张在 ${ms(r.last_request_start_ms)} 才发出；全部就绪 ${fmt(r.first_request_to_all_ready_ms)}。悬停可看每张耗时。`;
}
select.value=String(Math.max(0,data.baseline.findIndex(r=>r.device==='mobile'&&r.cache==='warm')));
select.addEventListener('change',draw);draw();
'''


def render(report):
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>图片加载耗时与原因</title><style>'''+STYLE+'''</style></head><body><main>
<h1>图片为什么加载慢</h1><p id="date" class="muted"></p>
<p class="lead">已验证的主要原因：<strong>前端限制桌面同时加载 2 张、手机只加载 1 张</strong>，当前页 45 张预览需要多轮排队。再次加载仍逐张向后端验证缓存。</p>
<section><h2>第一张开始请求 → 当前页全部图片就绪</h2>
<p class="muted">这段计时不包含登录、获取网站列表和开始加载图片前的等待。当前页 48 张卡片，其中 45 张有可用快照；另外 3 张不计入加载目标。</p>
<div class="scroll"><table><thead><tr><th>场景</th><th class="num">完成图片</th><th class="num">全部就绪</th><th class="num">显示准备完成</th><th class="num">下载并发峰值</th><th class="num">实际传输量</th></tr></thead><tbody id="baseline"></tbody></table></div>
<p class="muted">“就绪”已等待图片有效且解码完成；“显示准备完成”再等待两次动画帧回调，是绘制机会的近似值，不能证明每个像素已显示到屏幕。以上数值均是这一段的耗时。</p></section>
<section><h2>45 张图片如何排队</h2><div class="chart-tools"><label for="scene">场景</label><select id="scene"></select></div>
<div class="legend"><span class="key" style="--color:#3579dc">请求至响应头</span><span class="key" style="--color:#27a8ad">下载响应体</span><span class="key" style="--color:#bac5d3">下载后至前端观察到就绪</span></div>
<p id="chart-note" class="muted"></p><svg id="waterfall" role="img" aria-label="图片请求瀑布图"></svg>
<p class="muted">空白表示尚未发出请求；灰色包含浏览器回调、图片处理和监测观察间隔，不能全部视为解码耗时。这里展示请求与显示阶段，没有函数调用栈数据。</p></section>
<section><h2>手机再次加载：时间花在哪里</h2><div id="serial-total" class="kpi"></div><div id="serial-bar" class="decomp"></div>
<table><tbody id="serial-table"></tbody></table>
<p class="muted">手机请求串行，以上三段可以相加。桌面请求有重叠，不能直接相加。再次加载仅传输约 13.5 KB，仍然较慢，表明单纯压缩图片不能解决这段等待。</p></section>
<section><h2>只改变并发数的对照验证</h2><p>在隔离测试浏览器内将并发临时改为 6 张，每组测 3 次。<strong>正式网站的加载策略没有改动。</strong></p>
<div class="scroll"><table><thead><tr><th>设备</th><th class="num">原策略中位数</th><th class="num">并发 6 张中位数</th><th class="num">耗时减少</th><th class="num">完成样本（原 / 6 张）</th></tr></thead><tbody id="comparison"></tbody></table></div>
<details><summary>查看各次用时与实验条件</summary><div id="samples" class="muted"></div><p class="muted">两组均使用新浏览器上下文与相同请求拦截，HTTP 缓存均关闭；只替换浏览器内存中的并发分支。绝对值不能直接与上方正常缓存测试比较。样本较少，仅用于验证因果；6 张尚非经过全面验证的最优值，手机为桌面 Chromium 模拟。</p></details></section>
<section><h2>原因与下一步</h2><div class="explain"><p><strong>① 前端排队是已验证的主要限制</strong>队列名额一直占用到图片加载完成，再推进下一张。手机 45 张串行，逐次请求和逐次推进的等待会累积；提高并发的对照实验明显缩短了时间。</p><p><strong>② 命中缓存仍有每张验证成本</strong>缩略图要求每次验证缓存。后端还会鉴权、查询快照并读取文件计算 ETag；浏览器刷新后仍要访问这 45 个接口。其独占耗时尚未分别测量，不能直接归因于 Python 慢。</p></div>
<p>优先测试更合适的图片并发数并保留首屏优先；再细分缓存验证、文件哈希与浏览器解码的耗时。鉴权和退出登录后的缓存隔离需要保留。</p>
<p class="muted">查看图片时加载的是已有 JPEG 预览，不会重新抓取原网站。当前证据没有显示持续的浏览器长任务主导这段加载时间。</p></section>
<p class="muted">原始数据：<a href="baseline-browser.json">正常加载测量</a> · <a href="concurrency-experiment.json">并发对照</a> · <a href="image-loading-summary.json">耗时摘要</a></p>
</main><script type="application/json" id="data">'''+payload+'''</script><script>'''+SCRIPT+'''</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    report = build_report(args.directory)
    (args.directory/'image-loading-summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    (args.directory/'report.html').write_text(render(report), encoding='utf-8')
    print(json.dumps({'report': str(args.directory/'report.html')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
