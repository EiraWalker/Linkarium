import hashlib
import os
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Error as PlaywrightError

from .models import CaptureError, CaptureRequest, CaptureResult
from .network import PublicURLPolicy
from .rewrite import DocumentRewriter, Resource
from .diagnostics import bind_event, event, host_only, metrics, mime_only
from .gif_frame import fetch_first_frame, open_gif_client
from .frame_content import inspect_replay_frames, wait_for_pdf
from .readiness import ApplicationReadiness
from .replay_document import ReplayDocument
from .preview import write_preview

REPLAY_CSP = (
    "sandbox; default-src 'none'; script-src 'none'; connect-src 'none'; "
    "style-src 'unsafe-inline' data:; img-src data:; font-src data:; "
    "media-src data:; frame-src data:; object-src 'none'; base-uri 'none'; form-action 'none'"
)

IMAGE_VISIBILITY = """image => {
    const r = image.getBoundingClientRect(), s = getComputedStyle(image);
    if (s.visibility !== 'visible' || r.width <= 0 || r.height <= 0) return false;
    let left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
    let top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
    for (let node = image; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || Number(style.opacity) === 0 ||
          style.contentVisibility === 'hidden') return false;
      if (node !== image) {
        const bounds = node.getBoundingClientRect();
        if (/(hidden|clip|scroll|auto)/.test(style.overflowX)) {
          left = Math.max(left, bounds.left); right = Math.min(right, bounds.right);
        }
        if (/(hidden|clip|scroll|auto)/.test(style.overflowY)) {
          top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom);
        }
      }
    }
    return right > left && bottom > top;
  }"""

IMAGE_DIAGNOSTICS = """() => {
  const visible = """ + IMAGE_VISIBILITY + """;
  const images = Array.from(document.images);
  const broken = images.filter(image => !image.complete || image.naturalWidth === 0);
  const visibleBroken = broken.filter(visible);
  return {total: images.length, broken: broken.length,
    pending: images.filter(image => !image.complete).length,
    visible_broken: visibleBroken.length,
    new_visible_broken: broken.filter(image =>
      (visible(image) || image.getAttribute('data-capture-source-visible') === 'true') &&
      image.getAttribute('data-capture-source-image') !== 'broken').length,
    empty_source_broken: broken.filter(image => !image.getAttribute('src')).length,
    data_source_broken: broken.filter(image => (image.currentSrc || image.src).startsWith('data:')).length};
}"""


def _challenge_provider(headers):
    """Use explicit provider markers, never guess from a 2xx status or page text."""
    if headers.get('x-amzn-waf-action', '').strip().lower() in ('challenge', 'captcha'):
        return 'aws-waf'
    if headers.get('cf-mitigated', '').strip().lower() == 'challenge':
        return 'cloudflare'
    return None


class _DocumentChanged(Exception):
    """Only a new main document may restart the bounded readiness/freeze pass."""


class _LateNavigation(CaptureError):
    def __init__(self, url):
        super().__init__('navigation_failed', 'Target page navigated after its state was frozen')
        # Private task memory only; diagnostics never persist the target URL.
        self.url = url


def _check_document_response(response):
    if response is None or (response.status >= 400 and not _challenge_provider(response.headers)):
        if response and response.status in (401, 403, 429):
            raise CaptureError('access_denied', 'Target page requires access or rate-limit recovery')
        if response and response.status in (404, 410):
            raise CaptureError('not_found', 'Target page does not exist')
        raise CaptureError('navigation_failed', 'Target page did not return a successful document')


def _monolith(request):
    candidate = request.monolith_path or os.environ.get('MONOLITH_PATH')
    if not candidate:
        root = Path(__file__).resolve().parents[2]
        filename = 'monolith.exe' if os.name == 'nt' else 'monolith'
        local = root / 'tools' / 'monolith' / filename
        candidate = str(local) if local.is_file() else shutil.which('monolith')
    if not candidate or not Path(candidate).is_file():
        raise CaptureError('tool_missing', 'Monolith is not installed; run the capture installer')
    return str(Path(candidate).resolve())


def capture(request: CaptureRequest) -> CaptureResult:
    request.validate()
    deadline = time.monotonic() + request.timeout_seconds
    budget = {'bytes': 0, 'requests': 0, 'error': None}

    def stable_capture(proxy=None):
        current = request
        for attempt in range(2):
            try:
                return _capture(current, proxy=proxy, deadline=deadline, budget=budget)
            except _LateNavigation as exc:
                if attempt == 1:
                    raise
                event('navigation_retry', attempt=attempt + 1, reason='document_changed_after_freeze', host=host_only(exc.url))
                current = replace(request, url=exc.url)

    from .proxy_config import request_proxy, HttpProxyConnector
    upstream = request_proxy(request)
    event('network_setup')
    if request.development or request.dns_mode == 'public-doh' or upstream:
        if os.environ.get('BOOKMARK_PUBLIC_DEPLOYMENT') == '1':
            raise CaptureError('isolation_required', 'Development capture is prohibited in a public deployment')
        from .doh_proxy import PublicDohResolver, PinnedPublicProxy
        from .network import SystemResolver
        resolver = (PublicDohResolver(deadline=deadline, proxy_url=upstream or None)
                    if request.dns_mode == 'public-doh' or upstream else SystemResolver(deadline))
        proxy = PinnedPublicProxy(resolver,request.max_bytes,deadline,request.deny_hosts,
                                  connector=HttpProxyConnector(upstream) if upstream else None,
                                  allow_test_hosts=request.allow_test_hosts).start()
        event('upstream_proxy', enabled=bool(upstream))
        try:
            return stable_capture(proxy)
        finally:
            # Closing threads must not turn an actionable capture error into TASK_FAILED.
            import sys
            pending_error = sys.exc_info()[0] is not None
            try:
                proxy.close()
            except Exception:
                if not pending_error:
                    raise
    return stable_capture()


def _capture(request: CaptureRequest, proxy=None, deadline=None, budget=None) -> CaptureResult:
    request.validate()
    local_browsers = Path(__file__).resolve().parents[2] / 'tools' / 'browsers'
    if local_browsers.is_dir() and 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(local_browsers)
    if not request.development and (os.name != 'posix' or os.environ.get('BOOKMARK_CAPTURE_ISOLATED') != '1'):
        raise CaptureError('isolation_required', 'Production captures must run through the verified isolated launcher')
    if request.development and os.environ.get('BOOKMARK_PUBLIC_DEPLOYMENT') == '1':
        raise CaptureError('isolation_required', 'Development capture is prohibited in a public deployment')
    policy = proxy.policy if proxy else PublicURLPolicy(request.allow_test_hosts, request.deny_hosts)
    event('url_validation')
    policy.validate(request.url)
    directory = Path(request.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    deadline = proxy.deadline if proxy else deadline if deadline is not None else time.monotonic() + request.timeout_seconds
    warnings = []
    if request.development:
        warnings.append('development-network-isolation-not-verified')
    if proxy:
        warnings.append('development-public-doh-pinned-ip-proxy' if request.dns_mode == 'public-doh' or request.proxy_url
                        else 'development-system-dns-pinned-ip-proxy')
    resources: dict[str, Resource] = {}
    response_stats = {}
    static_gifs = {}
    gif_client = open_gif_client(proxy) if proxy else None
    frozen_media_frames = set()
    # Keep URLs only in task memory to reconcile a challenged stylesheet with a
    # later successful response. Logs contain a bounded hostname and enums only.
    critical_challenges = set()
    response_event = bind_event()
    if budget is None:
        budget = {'bytes': 0, 'requests': 0, 'error': None}
    main_document = {'revision': 0, 'response': None, 'request': None, 'committed_request': None}
    width, height = (1366, 768) if request.variant == 'desktop' else (390, 844)
    safe_environment_keys = {'PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'LOCALAPPDATA', 'APPDATA',
                             'TEMP', 'TMP', 'HOME', 'LANG', 'LC_ALL', 'PLAYWRIGHT_BROWSERS_PATH'}
    subprocess_environment = {key: value for key, value in os.environ.items() if key in safe_environment_keys}

    def remaining_ms(cap=30000):
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise CaptureError('timeout', 'Capture exceeded its time budget')
        return min(remaining, cap)

    def intercept(route):
        budget['requests'] += 1
        if budget['requests'] > 2048:
            budget['error'] = CaptureError('request_budget', 'Capture request limit exceeded')
            route.abort()
            return
        try:
            if route.request.resource_type in ('websocket',):
                raise CaptureError('unsupported_resource', 'WebSocket resources are not captured')
            policy.validate(route.request.url)
            remaining_ms()
            if route.request.resource_type == 'media' and route.request.frame in frozen_media_frames:
                warnings.append('media-stream-stopped-after-freeze')
                route.abort()
                return
            if (proxy and route.request.method == 'GET' and route.request.resource_type == 'image'
                    and urlsplit(route.request.url).path.lower().endswith('.gif')):
                def charge_redirect():
                    budget['requests'] += 1
                    if budget['requests'] > 2048:
                        raise CaptureError('request_budget', 'Capture request limit exceeded')
                try:
                    if route.request.url not in static_gifs:
                        static_gifs[route.request.url] = fetch_first_frame(route.request.url, route.request.headers,
                                                                          proxy, charge_redirect, client=gif_client)
                    captured = static_gifs[route.request.url]
                except CaptureError as exc:
                    if exc.code != 'dns_failed':
                        budget['error'] = exc
                    raise
                if captured is not None:
                    body, headers = captured
                    warnings.append('animated-image-first-frame')
                    response_event('gif_first_frame', host=host_only(route.request.url), bytes=len(body))
                    route.fulfill(status=200, content_type='image/gif', body=body, headers=headers)
                    return
            route.continue_()
        except CaptureError as exc:
            warnings.append('blocked-request:' + exc.code)
            route.abort()

    def validate_request(browser_request):
        # Playwright's request events include every redirect hop, including hops
        # where a route handler is not repeated. The proxy validates literal
        # public destinations; a configured local upstream remains trusted to
        # honor them (its own sniffing/routing may re-resolve the hostname).
        try:
            if (urlsplit(browser_request.url).scheme in ('http', 'https')
                    and browser_request.is_navigation_request() and browser_request.frame == page.main_frame):
                main_document['revision'] += 1
                main_document['response'] = None
                main_document['request'] = browser_request
                response_event('document_navigation', phase='request', host=host_only(browser_request.url))
            if urlsplit(browser_request.url).scheme in ('http','https'):
                policy.validate(browser_request.url)
        except CaptureError as exc:
            warnings.append('blocked-request:' + exc.code)
            # Optional resource DNS failure need not discard a readable page.
            # Private-address attempts still fail closed.
            if exc.code != 'dns_failed' or (browser_request.is_navigation_request() and browser_request.frame == page.main_frame):
                budget['error'] = exc

    def collect(response):
        if budget['error']:
            return
        try:
            resource_type = response.request.resource_type
            headers = response.headers
            stats_key = (host_only(response.url), resource_type, mime_only(headers.get('content-type', '')))
            if stats_key in response_stats or len(response_stats) < 256:
                stats = response_stats.setdefault(stats_key, {'responses': 0, 'advertised_bytes': 0})
                stats['responses'] += 1
                try:
                    stats['advertised_bytes'] += max(0, int(headers.get('content-length', '0') or '0'))
                except ValueError:
                    pass
            provider = _challenge_provider(headers)
            main_frame = response.request.frame == page.main_frame
            if (main_frame and resource_type == 'document' and response.request.is_navigation_request()
                    and response.request == main_document['request']):
                main_document['response'] = response
            key = ('document', '') if resource_type == 'document' else (resource_type, response.url)
            if provider:
                response_event('resource_challenge', host=host_only(response.url), resource_type=resource_type,
                               http_status=response.status, provider=provider)
                warnings.append(f'resource-challenge:{provider}:{resource_type}')
                if main_frame and resource_type in ('document', 'stylesheet'):
                    critical_challenges.add(key)
                # A 202 empty HTML WAF response is not an image or stylesheet.
                return
            if response.status < 200 or response.status >= 300:
                return
            if main_frame and resource_type == 'document':
                # A new successful main document supersedes challenges from its
                # previous navigation; ad/child-frame documents cannot clear it.
                critical_challenges.clear()
            if resource_type in ('document', 'xhr', 'fetch'):
                return
            advertised = int(headers.get('content-length', '0') or '0')
            if resource_type == 'media':
                # A static archive freezes the available video frame/poster; it
                # never embeds the playback stream. response.body() would wait
                # for (and allocate) an entire movie that is not used below.
                # Browser traffic still passes through the proxy's aggregate
                # byte/request/deadline limits; images, scripts and styles keep
                # their separate per-resource bound.
                response_event('media_not_archived', host=host_only(response.url),
                               resource_type=resource_type, mime_type=mime_only(headers.get('content-type', '')),
                               advertised_bytes=advertised)
                warnings.append('media-stream-not-archived')
                return
            if advertised > 16 * 1024 * 1024:
                response_event('resource_limit', host=host_only(response.url), resource_type=resource_type,
                               mime_type=mime_only(headers.get('content-type', '')), advertised_bytes=advertised)
                budget['error'] = CaptureError('byte_budget', 'Resource exceeds per-resource byte budget')
                return
            body = response.body()
            budget['bytes'] += len(body)
            if len(body) > 16 * 1024 * 1024 or budget['bytes'] > request.max_bytes:
                budget['error'] = CaptureError('byte_budget', 'Capture resource byte budget exceeded')
                return
            content_type = headers.get('content-type', 'application/octet-stream')
            resource = Resource(body, content_type, response.url)
            resources[response.url] = resource
            recovered_stylesheet = (main_frame and resource_type == 'stylesheet'
                                    and content_type.split(';', 1)[0].strip().lower() == 'text/css')
            if recovered_stylesheet:
                critical_challenges.discard(key)
            previous = response.request.redirected_from
            while previous:
                resources[previous.url] = resource
                if recovered_stylesheet:
                    critical_challenges.discard(('stylesheet', previous.url))
                previous = previous.redirected_from
        except Exception:
            warnings.append('unreadable-resource-response')

    event('browser_launch')
    with sync_playwright() as playwright:
        launch_options = {'headless':True,'chromium_sandbox':True,'env':subprocess_environment,
            'args':['--disable-background-networking','--disable-features=MediaRouter','--disable-quic']}
        if proxy:
            launch_options['proxy'] = proxy.configuration
            launch_options['args'] += ['--proxy-bypass-list=<-loopback>',
                                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']
        browser = playwright.chromium.launch(**launch_options)
        options = {'viewport': {'width': width, 'height': height}, 'locale': 'zh-CN', 'timezone_id': 'Asia/Taipei',
                   'color_scheme': 'light', 'service_workers': 'block', 'accept_downloads': False}
        if request.variant == 'mobile':
            options.update({key: value for key, value in playwright.devices['Pixel 7'].items()
                            if key not in ('default_browser_type', 'viewport')})
            options['viewport'] = {'width': width, 'height': height}
            options['device_scale_factor'] = 2
        else:
            options['user_agent'] = (f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                f'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36')
        context = browser.new_context(**options)
        application_readiness = ApplicationReadiness(context)
        context.route('**/*', intercept)
        context.on('request',validate_request)
        def block_websocket(websocket):
            # A matching route never connects upstream unless connect_to_server
            # is called. Do not invoke synchronous close() from this dispatcher
            # callback: Playwright's Python sync adapter can deadlock there.
            warnings.append('blocked-request:unsupported_resource')
            response_event('blocked_websocket', host=host_only(websocket.url))

        context.route_web_socket('**/*', block_websocket)
        context.on('response', collect)
        context.add_init_script("""window.__captureShadowRoots = new WeakMap();
            const original = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(options) {
              const root = original.call(this, options); window.__captureShadowRoots.set(this, root); return root;
            };""")
        page = context.new_page()
        application_readiness.page = page
        def navigated(frame):
            if frame != page.main_frame:
                return
            # Playwright also emits this event for history/hash changes. Only
            # committing a new navigation request replaces the DOM context.
            response = main_document['response']
            if (response is not None and main_document['request'] != main_document['committed_request']
                    and urlsplit(response.url)._replace(fragment='') == urlsplit(frame.url)._replace(fragment='')):
                main_document['committed_request'] = main_document['request']
                main_document['revision'] += 1
                response_event('document_navigation', phase='commit', host=host_only(frame.url))

        page.on('framenavigated', navigated)
        context.on('page', lambda other: other.close() if other != page else None)
        page.on('download', lambda download: download.cancel())

        def check_current_document(revision, *, check_challenges=True):
            if budget['error']:
                raise budget['error']
            if proxy and proxy.error:
                raise proxy.error
            remaining_ms()
            if (main_document['revision'] != revision or main_document['response'] is None
                    or main_document['request'] != main_document['committed_request']):
                raise _DocumentChanged()
            policy.validate(page.url)
            _check_document_response(main_document['response'])
            if check_challenges and critical_challenges:
                raise CaptureError('challenge', 'Target page resources require human verification')

        try:
            event('navigation')
            response = page.goto(request.url, wait_until='domcontentloaded', timeout=remaining_ms(60000))
            if proxy and proxy.error:
                raise proxy.error
            event('document_response', http_status=response.status if response else None, host=host_only(page.url))
            if (response is not None and response.status in (502, 503, 504)
                    and not _challenge_provider(response.headers)):
                # Some public sites' temporary error documents navigate to the
                # real page themselves. Observe that normal browser behavior
                # once, without issuing retries or accepting an error document.
                event('transient_document_observation', http_status=response.status, milliseconds=2500)
                page.wait_for_timeout(min(2500, remaining_ms()))
                response = main_document['response'] or response
                if budget['error']:
                    raise budget['error']
                if proxy and proxy.error:
                    raise proxy.error
                remaining_ms()
                event('document_response', http_status=response.status, host=host_only(page.url))
            _check_document_response(response)
            policy.validate(page.url)
            freeze_function = Path(__file__).with_name('freeze.js').read_text(encoding='utf-8')
            freeze_script = '() => (' + freeze_function + ')(' + IMAGE_VISIBILITY + ')'
            # Shared by bounded navigation retries; never reset the stage budget.
            application_deadline = min(deadline, time.monotonic() + 10)
            # Client redirects/reloads may destroy the evaluation context after
            # goto succeeds. Restart only that case, at most twice, within the
            # original request deadline; do not swallow other browser errors.
            for readiness_attempt in range(3):
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=remaining_ms())
                    revision = main_document['revision']
                    event('readiness', attempt=readiness_attempt + 1)
                    # A site may naturally replace its challenged document or
                    # stylesheet while initializing. Enforce the final challenge
                    # gate after the bounded wait, not on its first poll.
                    application_readiness.wait(remaining_ms,
                                               lambda: check_current_document(revision, check_challenges=False),
                                               application_deadline)
                    check_current_document(revision)
                    response = main_document['response']
                    event('document_response', http_status=response.status, host=host_only(page.url))
                    page_title = page.title().strip().lower()
                    if page_title in ('just a moment...', 'attention required! | cloudflare', 'verify you are human'):
                        raise CaptureError('challenge', 'Target page requires human verification')
                    wait_for_pdf(page.main_frame, IMAGE_VISIBILITY, remaining_ms(10000))
                    source_images = page.evaluate(IMAGE_DIAGNOSTICS)
                    event('freeze')
                    frozen = page.evaluate(freeze_script)
                    check_current_document(revision)
                    break
                except (_DocumentChanged, PlaywrightError) as exc:
                    if isinstance(exc, PlaywrightError) and not any(signature in str(exc) for signature in (
                            'Execution context was destroyed', 'Cannot find context with specified id')):
                        raise
                    if budget['error']:
                        raise budget['error'] from exc
                    if proxy and proxy.error:
                        raise proxy.error from exc
                    remaining_ms()
                    if readiness_attempt == 2:
                        raise CaptureError('navigation_failed', 'Target page did not stabilize within bounded navigation retries') from exc
                    event('navigation_retry', attempt=readiness_attempt + 1,
                          reason='document_changed' if isinstance(exc, _DocumentChanged) else 'execution_context_destroyed',
                          host=host_only(page.url))
                    page.wait_for_timeout(min(100, remaining_ms()))
            event('source_images', **source_images)
            confirmed_broken = source_images['broken'] - source_images['pending']
            if confirmed_broken:
                warnings.append(f"source-broken-images:{confirmed_broken}")
            if source_images['pending']:
                warnings.append(f"source-pending-images:{source_images['pending']}")
            event('freeze_complete', html_bytes=len(frozen['html'].encode('utf-8')))
            warnings.extend(frozen['warnings'])
            result = CaptureResult(None, frozen['title'], frozen['description'], frozen['text_excerpt'], page.url,
                                   width, height, warnings)
            if request.mode == 'metadata':
                result.warnings = [value[:180] for value in list(dict.fromkeys(warnings))[:100]]
                return result
            rewriter = DocumentRewriter(resources, warnings, request.max_bytes)
            source_frames = {}

            def stop_frozen_media(frame):
                # The immutable clone already owns the actual frame/poster.
                # Stop playback in the source browser, which otherwise keeps
                # downloading loops while Monolith and offline replay run.
                frozen_media_frames.add(frame)
                try:
                    event('frame_media_stop', host=host_only(frame.url))
                    frame.evaluate("""() => {
                        for (const media of document.querySelectorAll('video,audio')) {
                            try { media.pause(); media.removeAttribute('src'); media.srcObject = null;
                                for (const source of media.querySelectorAll('source')) source.remove();
                                media.load(); } catch (_) {}
                        }
                    }""")
                    event('frame_media_stopped', host=host_only(frame.url))
                except PlaywrightError:
                    # Normal final document checks still reject/retry a real
                    # navigation; an optional detached iframe need not abort.
                    warnings.append('unavailable-frozen-media-frame')

            def freeze_frame(frame, data, depth=0, path=(), visible=True):
                if depth > 8:
                    raise CaptureError('frame_depth', 'Frame nesting exceeds budget')
                source_frames[path] = {'text': bool(data['text_excerpt'].strip()), 'visible': visible}
                stop_frozen_media(frame)
                nested = {}
                for child in frame.child_frames:
                    try:
                        event('frame_inspect', host=host_only(child.url), depth=depth + 1)
                        element = child.frame_element()
                        details = element.evaluate("element => ({...(window.__captureFrameKeys?.get(element) ?? {identifier: null}), visible: ("
                                                   + IMAGE_VISIBILITY + ")(element)})")
                        identifier = details['identifier']
                        if identifier is None:
                            # A new iframe is not part of the frozen parent DOM.
                            # Never attach it to another frame's old node index.
                            warnings.append('frame-added-after-freeze')
                            event('frame_skipped', host=host_only(child.url), reason='created_after_freeze')
                            continue
                        if details.get('hiddenReason'):
                            # Do not run third-party scripts in an iframe that
                            # had no rendered content in the frozen parent.
                            # Below-the-fold frames do not meet this condition.
                            nested[str(identifier)] = '<html><body></body></html>'
                            warnings.append('hidden-frame-not-archived')
                            event('frame_skipped', host=host_only(child.url),
                                  reason=details['hiddenReason'], depth=depth + 1)
                            continue
                        child_visible = visible and details['visible']
                        event('frame_freeze', host=host_only(child.url), depth=depth + 1, visible=details['visible'])
                        if child_visible:
                            wait_for_pdf(child, IMAGE_VISIBILITY, remaining_ms(10000))
                        child_data = child.evaluate(freeze_script)
                        event('frame_frozen', host=host_only(child.url), depth=depth + 1,
                              visible=details['visible'], html_bytes=len(child_data['html'].encode('utf-8')))
                        warnings.extend(child_data['warnings'])
                        nested[str(identifier)] = freeze_frame(child, child_data, depth + 1,
                            path + (str(identifier),), child_visible)
                    except (CaptureError, PlaywrightTimeout):
                        raise
                    except Exception:
                        warnings.append('unavailable-frame')
                if visible and any(state.get('visible') and not state.get('hiddenReason')
                                   and str(state['identifier']) not in nested
                                   for state in data.get('frames', [])):
                    # A frame removed/replaced after the parent clone was taken
                    # must not silently become an empty visible srcdoc archive.
                    raise CaptureError('replay_failed', 'Visible embedded content became unavailable during capture')
                return rewriter.html(data['html'], frame.url, nested)

            event('rewrite', resources=len(resources), resource_bytes=budget['bytes'], requests=budget['requests'])
            prepared = freeze_frame(page.main_frame, frozen)
            try:
                check_current_document(revision)
            except _DocumentChanged as exc:
                target = main_document['request'].url if main_document['request'] else page.url
                raise _LateNavigation(target) from exc
            event('rewrite_complete', html_bytes=len(prepared.encode('utf-8')))
            if len(prepared.encode('utf-8')) > request.max_bytes:
                raise CaptureError('byte_budget', 'Frozen HTML exceeds capture byte budget')
            event('archive')
            binary = _monolith(request)
            # All resources have already been inlined. Monolith may retrieve only this nonexistent
            # allowlisted domain, so missed references cannot cause arbitrary network fallback.
            env = subprocess_environment
            temporary = directory / (request.attempt_uuid + '.pending.html')
            version = subprocess.run([binary, '-V'], capture_output=True, timeout=5, env=env, check=True)
            result.monolith_version = version.stdout.decode('utf-8', 'replace').strip()
            try:
                process = subprocess.run([binary, '-j', '-M', '-d', 'capture.invalid', '-t', '1',
                    '-b', result.final_url, '-o', temporary.name, '-'], input=prepared.encode('utf-8'),
                    capture_output=True, timeout=max(1, remaining_ms(30000) / 1000), env=env, cwd=directory)
                if process.returncode != 0 or not temporary.is_file():
                    raise CaptureError('archive_failed', 'Monolith could not produce an archive')
                content = temporary.read_bytes()
                if len(content) > request.max_bytes:
                    raise CaptureError('byte_budget', 'Monolith output exceeds capture byte budget')
                # Apply tree sanitation to Monolith's actual output and validate that final artifact.
                final_html = rewriter.html(content.decode('utf-8'), result.final_url, restore_shadow=True)
                content = final_html.encode('utf-8')
                if len(content) > request.max_bytes:
                    raise CaptureError('byte_budget', 'Final HTML exceeds capture byte budget')
                event('replay', archive_bytes=len(content))
                # The isolated launcher's packet filter intentionally denies
                # loopback. Keep its in-memory delivery; only explicit local
                # development can use the faster single-document HTTP service.
                delivery = ReplayDocument(content, REPLAY_CSP, deadline) if request.development else nullcontext(None)
                with delivery as document:
                    replay_url = document.url if document else 'https://snapshot.invalid/'
                    replay_options = {'viewport': {'width': width, 'height': height}, 'service_workers': 'block'}
                    if document:
                        replay_options['proxy'] = {'server': document.proxy_url, 'bypass': '<-loopback>'}
                    replay = browser.new_context(**replay_options)
                    try:
                        replay_page = replay.new_page()
                        outside = []

                        def replay_route(route):
                            target = route.request
                            if (target.url == replay_url and target.resource_type == 'document'
                                    and target.is_navigation_request() and target.frame == replay_page.main_frame):
                                if document:
                                    route.continue_()
                                else:
                                    route.fulfill(status=200, body=content,
                                        headers={'Content-Type': 'text/html; charset=utf-8',
                                                 'Content-Security-Policy': REPLAY_CSP})
                            else:
                                outside.append(target.url)
                                route.abort()

                        replay.route('**/*', replay_route)
                        replay.route_web_socket('**/*', lambda _socket: outside.append('websocket'))
                        replay_response = replay_page.goto(replay_url, wait_until='load', timeout=remaining_ms())
                        if replay_response is None or replay_response.status != 200:
                            raise CaptureError('replay_failed', 'Archive replay did not return its expected document')
                        image_status, text_characters = inspect_replay_frames(
                            replay_page, IMAGE_DIAGNOSTICS, IMAGE_VISIBILITY, source_frames)
                        event('replay_images', **image_status)
                        event('replay_check', broken_images=image_status['broken'],
                              new_visible_broken=image_status['new_visible_broken'],
                              text_characters=text_characters, external_requests=len(outside))
                        if image_status['new_visible_broken']:
                            raise CaptureError('replay_failed', 'Archive introduced broken first-screen images')
                        offscreen_broken = image_status['broken'] - image_status['visible_broken']
                        if offscreen_broken:
                            warnings.append(f'offscreen-broken-images:{offscreen_broken}')
                        if image_status['visible_broken']:
                            warnings.append(f"source-broken-first-screen-images:{image_status['visible_broken']}")
                        if frozen['text_excerpt'].strip() and not text_characters:
                            raise CaptureError('replay_failed', 'Archive lost its readable page content')
                        if outside:
                            raise CaptureError('replay_failed', 'Archive attempted external network access')
                        if critical_challenges:
                            raise CaptureError('challenge', 'Target page resources require human verification')
                        if any(value.startswith('unreadable-media:') for value in warnings):
                            raise CaptureError('unsupported_visual_state', 'Visible media could not be frozen safely')
                        # Reuse the accepted offline document. The optional image
                        # cache must never replace or weaken HTML replay validation.
                        try:
                            event('preview')
                            # Leave time to publish the primary archive even if
                            # an optional screenshot stalls until its timeout.
                            preview_timeout = remaining_ms(11000) - 1000
                            if preview_timeout <= 0:
                                raise CaptureError('preview_budget', 'Insufficient time for optional preview')
                            preview = write_preview(replay_page, directory, width, height,
                                timeout_ms=preview_timeout)
                            result.preview_path = preview['path']
                            result.preview_sha256 = preview['sha256']
                            result.preview_byte_size = preview['byte_size']
                            event('preview_complete', preview_bytes=preview['byte_size'])
                        except (PlaywrightError, CaptureError, OSError):
                            warnings.append('preview-generation-failed')
                            event('preview_unavailable')
                        if outside:
                            raise CaptureError('replay_failed', 'Archive attempted external network access')
                    finally:
                        primary_error = sys.exc_info()[0]
                        try:
                            replay.close()
                        except PlaywrightError:
                            if primary_error is None:
                                raise
                # Requests can still finish while freezing/archiving. Never
                # publish if that background traffic exhausted a capture bound.
                if outside:
                    raise CaptureError('replay_failed', 'Archive attempted external network access')
                if critical_challenges:
                    raise CaptureError('challenge', 'Target page resources require human verification')
                if budget['error']:
                    raise budget['error']
                if proxy and proxy.error:
                    raise proxy.error
                remaining_ms()
                try:
                    check_current_document(revision)
                except _DocumentChanged as exc:
                    target = main_document['request'].url if main_document['request'] else page.url
                    raise _LateNavigation(target) from exc
                result.sha256 = hashlib.sha256(content).hexdigest()
                result.byte_size = len(content)
                artifact = directory / (result.sha256 + '.html')
                if artifact.exists():
                    if artifact.is_symlink() or hashlib.sha256(artifact.read_bytes()).hexdigest() != result.sha256:
                        raise CaptureError('artifact_conflict', 'Existing artifact hash mismatch')
                else:
                    with artifact.open('xb') as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                result.artifact_path = str(artifact)
                result.warnings = [value[:180] for value in list(dict.fromkeys(warnings))[:100]]
                return result
            except subprocess.TimeoutExpired as exc:
                raise CaptureError('timeout', 'Monolith exceeded capture time budget') from exc
            finally:
                temporary.unlink(missing_ok=True)
        except PlaywrightTimeout as exc:
            if proxy and proxy.error:
                raise proxy.error from exc
            raise CaptureError('timeout', 'Page did not become ready within its capture budget') from exc
        except PlaywrightError as exc:
            if proxy and proxy.error:
                raise proxy.error from exc
            if any(message in str(exc) for message in ('DOM exceeds capture node budget', 'DOM exceeds capture style budget')):
                raise CaptureError('dom_budget', 'Page DOM exceeds capture node budget') from exc
            raise CaptureError('browser_failed', 'Browser could not load or freeze the public page') from exc
        finally:
            if gif_client is not None:
                gif_client.close()
            metrics(requests=budget['requests'], resource_bytes=budget['bytes'],
                    network_bytes=proxy.bytes if proxy else None, connections=proxy.connections if proxy else None,
                    proxy_peak_active=proxy.server.peak_active_handlers if proxy else None,
                    proxy_wait_count=proxy.server.slot_wait_count if proxy else None,
                    proxy_wait_seconds=round(proxy.server.slot_wait_seconds, 3) if proxy else None,
                    response_types=[{'host': key[0], 'resource_type': key[1], 'mime_type': key[2], **values}
                                    for key, values in sorted(response_stats.items(),
                                        key=lambda item: item[1]['advertised_bytes'], reverse=True)[:16]])
            context.close()
            browser.close()
