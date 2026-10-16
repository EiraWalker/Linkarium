"""Opt-in public-site evidence using the development-only pinned-IP DoH proxy.

HTML files are the delivered snapshots. PNG files only document visual comparisons.
Public pages can change between requests, so this reports differences without claiming
that one sample proves arbitrary websites are visually identical.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from playwright.sync_api import sync_playwright

from capture import CaptureError, CaptureRequest, CaptureResult, capture
from capture.doh_proxy import PinnedPublicProxy, PublicDohResolver
from capture.runner import REPLAY_CSP


def compare_pixels(page, before, after):
    return page.evaluate('''async ([left,right]) => {
      const images=await Promise.all([left,right].map(src=>new Promise((resolve,reject)=>{
        const img=new Image();img.onload=()=>resolve(img);img.onerror=reject;img.src='data:image/png;base64,'+src;
      })));
      const canvas=document.createElement('canvas');canvas.width=images[0].width;canvas.height=images[0].height;
      const ctx=canvas.getContext('2d');ctx.drawImage(images[0],0,0);const a=ctx.getImageData(0,0,canvas.width,canvas.height).data;
      ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(images[1],0,0);const b=ctx.getImageData(0,0,canvas.width,canvas.height).data;
      let changed=0,totalError=0;for(let n=0;n<a.length;n+=4){let delta=0;for(let c=0;c<3;c++)delta+=Math.abs(a[n+c]-b[n+c]);totalError+=delta;if(delta>30)changed++;}
      return {changed_pixel_ratio:changed/(a.length/4),mean_rgb_error:totalError/(a.length/4*3)};
    }''', [base64.b64encode(before).decode(),base64.b64encode(after).decode()])


def replay_only(variant,result,output,prefix):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(chromium_sandbox=True)
        options = {'viewport':{'width':result.viewport_width,'height':result.viewport_height},
                   'locale':'zh-CN','timezone_id':'Asia/Taipei','color_scheme':'light','service_workers':'block'}
        if variant == 'mobile':
            options.update({key:value for key,value in playwright.devices['Pixel 7'].items()
                            if key not in ('default_browser_type','viewport')})
            options['viewport'] = {'width':390,'height':844}
            options['device_scale_factor'] = 2
        context = browser.new_context(**options)
        outside = []
        def route_saved(route):
            if route.request.url == 'https://snapshot.invalid/':
                route.fulfill(status=200,body=Path(result.artifact_path).read_bytes(),headers={
                    'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':REPLAY_CSP})
            else:
                outside.append(route.request.url)
                route.abort()
        context.route('**/*',route_saved)
        page = context.new_page()
        page.goto('https://snapshot.invalid/',wait_until='load',timeout=10000)
        page.screenshot(path=str(output/f'{prefix}-{variant}-offline-replay.png'),scale='css')
        state = page.evaluate("""() => ({title:document.title,text_length:(document.body?.innerText||'').trim().length,
            broken_images:Array.from(document.images).filter(image=>!image.complete||!image.naturalWidth).length})""")
        browser.close()
    state['external_requests'] = outside
    if state['broken_images'] or outside:
        raise CaptureError('replay_failed','Saved public archive is not self-contained')
    return state


def visual_check(url, variant, result, output, prefix):
    deadline = time.monotonic()+90
    proxy = PinnedPublicProxy(PublicDohResolver(deadline=deadline),64*1024*1024,deadline).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True,proxy=proxy.configuration,
                args=['--proxy-bypass-list=<-loopback>','--disable-quic',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
            options = {'viewport':{'width':result.viewport_width,'height':result.viewport_height},
                       'locale':'zh-CN','timezone_id':'Asia/Taipei','color_scheme':'light','service_workers':'block'}
            if variant == 'mobile':
                options.update({key:value for key,value in playwright.devices['Pixel 7'].items()
                                if key not in ('default_browser_type','viewport')})
                options['viewport'] = {'width':390,'height':844}
                options['device_scale_factor'] = 2
            original = browser.new_context(**options)
            blocked = []
            def original_route(route):
                try:
                    proxy.policy.validate(route.request.url)
                    route.continue_()
                except CaptureError as error:
                    blocked.append(error.code)
                    route.abort()
            original.route('**/*',original_route)
            original.route_web_socket('**/*',lambda websocket:websocket.close())
            page = original.new_page()
            page.goto(url,wait_until='domcontentloaded',timeout=30000)
            page.evaluate('''async () => {
              const work=[document.fonts.ready];for(const image of document.images){
                const r=image.getBoundingClientRect();if(r.top<innerHeight&&r.bottom>=0)work.push(image.decode().catch(()=>{}));
              }await Promise.race([Promise.all(work),new Promise(resolve=>setTimeout(resolve,4000))]);
            }''')
            page.wait_for_timeout(1200)
            before = page.screenshot(path=str(output/f'{prefix}-{variant}-original.png'),scale='css')
            live_title = page.title()
            live_text = page.locator('body').inner_text()
            original.close()
            replay = browser.new_context(**options)
            external_requests = []
            def replay_route(route):
                if route.request.url == 'https://snapshot.invalid/':
                    route.fulfill(status=200,body=Path(result.artifact_path).read_bytes(),headers={
                        'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':REPLAY_CSP})
                else:
                    external_requests.append(route.request.url)
                    route.abort()
            replay.route('**/*',replay_route)
            saved = replay.new_page()
            saved.goto('https://snapshot.invalid/',wait_until='load',timeout=30000)
            saved.wait_for_timeout(500)
            after = saved.screenshot(path=str(output/f'{prefix}-{variant}-archive.png'),scale='css')
            replay_status = saved.evaluate("""() => ({title:document.title,text:(document.body?.innerText||'').trim(),
                broken_images:Array.from(document.images).filter(image=>!image.complete||!image.naturalWidth).length})""")
            trusted = browser.new_page()
            difference = compare_pixels(trusted,before,after)
            browser.close()
        if proxy.error:
            raise proxy.error
        return {'pixel_comparison':difference,'live_title':live_title,'live_text_length':len(live_text),
                'replay_title':replay_status['title'],'replay_text_length':len(replay_status['text']),
                'broken_images':replay_status['broken_images'],'external_requests':external_requests,
                'proxy_network_bytes':proxy.bytes,'blocked_live_requests':blocked}
    finally:
        proxy.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url',default='https://example.com/')
    parser.add_argument('--prefix',default='example')
    parser.add_argument('--reuse-desktop',action='store_true')
    parser.add_argument('--offline-existing',action='store_true',help='Replay an existing report without public requests')
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(root/'tools'/'browsers'))
    output = Path(__file__).parent/'results'/'public-doh'
    output.mkdir(parents=True,exist_ok=True)
    if arguments.offline_existing:
        report = json.loads((output/f'{arguments.prefix}-report.json').read_text(encoding='utf-8'))
        for record in report:
            if 'result' in record:
                record['offline_replay_check'] = replay_only(record['variant'],CaptureResult(**record['result']),output,arguments.prefix)
        (output/f'{arguments.prefix}-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps([{'variant':record['variant'],'offline_replay_check':record.get('offline_replay_check')} for record in report]))
        return
    report = []
    for variant in ('desktop','mobile'):
        record = {'variant':variant,'url':arguments.url}
        try:
            if variant == 'desktop' and arguments.reuse_desktop:
                result = CaptureResult(**json.loads((output/'first-desktop.json').read_text(encoding='utf-8'))['result'])
            else:
                result = capture(CaptureRequest(str(uuid4()),'doh-p0',1,arguments.url,str(output),
                    variant=variant,development=True,dns_mode='public-doh'))
            record['result'] = result.to_dict()
            # Persist the completed HTML even when a subsequent live comparison fails.
            (output/f'{arguments.prefix}-{variant}-result.json').write_text(
                json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
            try:
                record['visual_check'] = visual_check(arguments.url,variant,result,output,arguments.prefix)
            except Exception as error:
                record['visual_check_error'] = {'code':getattr(error,'code','comparison_failed'),
                                               'message':str(error)[:500]}
        except Exception as error:
            record['error'] = {'code':getattr(error,'code','capture_failed'),'message':str(error)[:500]}
        report.append(record)
        (output/f'{arguments.prefix}-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'variant':variant,'artifact_path':record.get('result',{}).get('artifact_path'),
            'visual_check':record.get('visual_check'),'error':record.get('error') or record.get('visual_check_error')},
            ensure_ascii=True),flush=True)


if __name__ == '__main__':
    main()
