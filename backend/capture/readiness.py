"""Bounded observation of natural application rendering, without changing its state."""
import time
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .diagnostics import event


INSTALL = """(() => {
  const state = {changed: performance.now(), targets: new Set(), overflow: false};
  new MutationObserver(records => {
    for (const record of records) {
      if (state.targets.size >= 128) {state.overflow = true; break;}
      const targets = record.type === 'childList' && record.addedNodes.length && !record.removedNodes.length
        ? record.addedNodes : [record.target];
      for (const target of targets) {
        if (state.targets.size >= 128) {state.overflow = true; break;}
        state.targets.add(target.nodeType === 1 ? target : target.parentElement);
      }
    }
  }).observe(document, {subtree: true, childList: true, characterData: true,
    attributes: true, attributeFilter: ['class', 'style', 'src', 'srcset', 'hidden']});
  window.__captureReadiness = state;
})();"""

SETTLED = """() => {
  const state = window.__captureReadiness;
  if (!state) return false;
  const inViewport = node => {
    if (!node?.isConnected) return true;
    if (getComputedStyle(node).visibility !== 'visible') return false;
    const r = node.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.top < innerHeight && r.bottom > 0 &&
      r.left < innerWidth && r.right > 0;
  };
  if (state.overflow || [...state.targets].some(inViewport)) state.changed = performance.now();
  state.targets.clear(); state.overflow = false;
  if (performance.now() - state.changed < 600 || document.fonts.status === 'loading') return false;
  // Include transparent image placeholders: their app's onLoad may reveal them.
  return [...document.images].every(image => !inViewport(image) || image.complete);
}"""


class ApplicationReadiness:
    def __init__(self, context):
        self.page = None
        self.pending = set()
        self.last_finished = None
        context.add_init_script(INSTALL)
        context.on('request', self.started)
        context.on('requestfinished', self.finished)
        context.on('requestfailed', self.finished)

    def started(self, request):
        if self.page is None or request.frame != self.page.main_frame:
            return
        if request.is_navigation_request():
            # Old-document callbacks can only discard their own Request object.
            self.pending.clear()
            self.last_finished = None
            return
        if request.resource_type not in ('script', 'xhr', 'fetch'):
            return
        current, target = urlsplit(self.page.url), urlsplit(request.url)
        if (current.scheme, current.netloc) == (target.scheme, target.netloc):
            self.pending.add(request)

    def finished(self, request):
        if request in self.pending:
            self.pending.remove(request)
            self.last_finished = time.monotonic()

    def wait(self, remaining_ms, check_document, deadline):
        started = time.monotonic()
        extended = False
        while True:
            if time.monotonic() >= deadline:
                # An unfinished application bundle differs from an ongoing data
                # poll: freezing now can preserve the unhydrated SSR skeleton.
                # Permit one extra bounded script window, still under the same
                # original capture deadline. Never extend for XHR/fetch alone.
                if not extended and any(request.resource_type == 'script' for request in self.pending):
                    deadline = min(deadline + 10, time.monotonic() + remaining_ms(10000) / 1000)
                    extended = True
                    event('application_script_wait', pending_scripts=sum(
                        request.resource_type == 'script' for request in self.pending))
                else:
                    break
            check_document()
            timeout = min(remaining_ms(500), max(1, int((deadline - time.monotonic()) * 1000)))
            try:
                handle = self.page.wait_for_function(SETTLED, polling=100, timeout=timeout)
                handle.dispose()
                now = time.monotonic()
                network_quiet = not self.pending and (self.last_finished is None or now - self.last_finished >= 2)
                if network_quiet and now - started >= 1.2:
                    event('application_ready', observation_seconds=round(now - started, 3))
                    return
            except PlaywrightTimeout:
                pass
            check_document()
            delay = min(100, remaining_ms(), max(0, int((deadline - time.monotonic()) * 1000)))
            if delay:
                self.page.wait_for_timeout(delay)
        # Same-origin polling and animated UI can continue on otherwise readable
        # pages. Stop observing, retain diagnostics and let normal replay checks
        # decide integrity. A blocked renderer still fails a host-side timeout.
        event('readiness_limit', pending_application_requests=len(self.pending))
        handle = self.page.wait_for_function("() => document.readyState !== 'loading'",
                                             polling=100, timeout=remaining_ms(2000))
        handle.dispose()
