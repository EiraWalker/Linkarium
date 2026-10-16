"""Real-browser fixture fidelity evidence. PNGs are diagnostics, never archive artifacts."""
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import sync_playwright
from capture import CaptureRequest, capture
from capture.runner import REPLAY_CSP
from test_capture import fixture_server


def main():
    root = Path(__file__).resolve().parents[2]
    output = Path(__file__).parent / 'results'
    output.mkdir(exist_ok=True)
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(root / 'tools' / 'browsers'))
    report = []
    with fixture_server() as url:
        for variant in ('desktop', 'mobile'):
            result = capture(CaptureRequest(str(uuid4()), 'p0', 1, url, str(output), variant=variant,
                development=True, allow_test_hosts=('127.0.0.1',)))
            with sync_playwright() as p:
                browser = p.chromium.launch(chromium_sandbox=True)
                options = {'viewport': {'width': result.viewport_width, 'height': result.viewport_height},
                           'locale': 'zh-CN', 'timezone_id': 'Asia/Taipei', 'color_scheme': 'light',
                           'service_workers': 'block'}
                if variant == 'mobile':
                    options.update({key: value for key, value in p.devices['Pixel 7'].items()
                                    if key not in ('default_browser_type', 'viewport')})
                    options['viewport'] = {'width': 390, 'height': 844}
                    options['device_scale_factor'] = 2
                original = browser.new_context(**options)
                page = original.new_page()
                page.goto(url, wait_until='load')
                page.wait_for_timeout(1500)
                before = page.screenshot(path=str(output / f'{variant}-original.png'), scale='css')
                replay = browser.new_context(**options)
                replay.route('**/*', lambda route: route.fulfill(status=200,
                    body=Path(result.artifact_path).read_bytes(), headers={'Content-Type':'text/html', 'Content-Security-Policy':REPLAY_CSP})
                    if route.request.url == 'https://snapshot.invalid/' else route.abort())
                saved = replay.new_page()
                saved.goto('https://snapshot.invalid/', wait_until='load')
                saved.wait_for_timeout(500)
                after = saved.screenshot(path=str(output / f'{variant}-archive.png'), scale='css')
                # Pixel comparison runs inside a separate trusted blank browser document.
                compare = browser.new_page()
                difference = compare.evaluate('''async ([left,right]) => {
                  const images=await Promise.all([left,right].map(src=>new Promise((resolve,reject)=>{
                    const img=new Image(); img.onload=()=>resolve(img);img.onerror=reject;img.src='data:image/png;base64,'+src;
                  })));
                  const canvas=document.createElement('canvas'); canvas.width=images[0].width;canvas.height=images[0].height;
                  const ctx=canvas.getContext('2d');ctx.drawImage(images[0],0,0);const a=ctx.getImageData(0,0,canvas.width,canvas.height).data;
                  ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(images[1],0,0);const b=ctx.getImageData(0,0,canvas.width,canvas.height).data;
                  let changed=0,totalError=0;for(let n=0;n<a.length;n+=4){let delta=0;for(let c=0;c<3;c++)delta+=Math.abs(a[n+c]-b[n+c]);totalError+=delta;if(delta>30)changed++;}
                  return {changed_pixel_ratio:changed/(a.length/4),mean_rgb_error:totalError/(a.length/4*3)};
                }''', [base64.b64encode(before).decode(), base64.b64encode(after).decode()])
                report.append({'variant': variant, 'result': result.to_dict(), 'pixel_comparison': difference})
                browser.close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{'variant': item['variant'], 'pixel_comparison': item['pixel_comparison'],
                      'bytes': item['result']['byte_size'], 'warnings': item['result']['warnings']} for item in report], indent=2))
    if any(item['pixel_comparison']['changed_pixel_ratio'] > 0.01 for item in report):
        raise SystemExit('Controlled fixture exceeds 1% changed-pixel acceptance threshold')


if __name__ == '__main__':
    main()
