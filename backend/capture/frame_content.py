"""Bounded readiness and offline validation for embedded page content."""
from .diagnostics import event, host_only
from .models import CaptureError


def wait_for_pdf(frame, visibility_script, timeout_ms):
    # PDF.js creates the viewer before its document/worker has rendered. A
    # document load event or an allocated canvas is not rendering completion.
    # Poll synchronously with Playwright's host timeout, never await page promises.
    predicate = """() => {
      const viewer = document.querySelector('.pdfViewer');
      if (!viewer) return 'ordinary';
      const visible = """ + visibility_script + """;
      if (!visible(viewer)) {
        // An empty PDF.js viewer has zero height until its document loads.
        // That is pending content, not a CSS-hidden reader we may ignore.
        if (getComputedStyle(viewer).visibility !== 'visible') return 'ordinary';
        for (let node = viewer; node; node = node.parentElement || node.getRootNode()?.host) {
          const style = getComputedStyle(node);
          if (style.display === 'none' || Number(style.opacity) === 0 ||
              style.contentVisibility === 'hidden') return 'ordinary';
        }
        const bounds = viewer.getBoundingClientRect();
        if ((bounds.width > 0 && bounds.height > 0) || bounds.top >= innerHeight ||
            bounds.left >= innerWidth || bounds.bottom < 0 || bounds.right < 0) return 'ordinary';
      }
      const app = window.PDFViewerApplication;
      if (!app?.pdfDocument || !app.pdfViewer?.getPageView) return false;
      const pages = [...viewer.querySelectorAll('.page')].filter(visible);
      if (!pages.length) return false;
      return pages.every(page => {
        const index = Number(page.getAttribute('data-page-number')) - 1;
        const view = Number.isInteger(index) && index >= 0 ? app.pdfViewer.getPageView(index) : null;
        const canvas = page.querySelector('canvas');
        return view?.renderingState === 3 && canvas?.width > 0 && canvas?.height > 0;
      }) ? 'pdf' : false;
    }"""
    handle = frame.wait_for_function(predicate, polling=100, timeout=timeout_ms)
    try:
        is_pdf = handle.json_value() == 'pdf'
    finally:
        handle.dispose()
    if is_pdf:
        event('pdf_ready', host=host_only(frame.url))
    return is_pdf


def inspect_replay_frames(page, image_script, visibility_script, source_frames):
    """Inspect nested srcdoc content too, respecting hidden/offscreen parents."""
    totals = dict(total=0, broken=0, visible_broken=0, new_visible_broken=0,
                  empty_source_broken=0, data_source_broken=0)
    visited = set()

    def inspect(frame, path=(), visible=True):
        visited.add(path)
        source = source_frames.get(path, {})
        check_visible = visible or source.get('visible', False)
        images = frame.evaluate(image_script)
        if not check_visible:
            images['visible_broken'] = images['new_visible_broken'] = 0
        for key in totals:
            totals[key] += images[key]
        text = frame.evaluate("() => (document.body?.innerText || '').trim().length")
        for child in frame.child_frames:
            element = child.frame_element()
            identifier = element.get_attribute('data-capture-frame')
            child_visible = visible and element.evaluate(visibility_script)
            text += inspect(child, path + (identifier,), child_visible)
        if path and check_visible and source.get('text') and not text:
            raise CaptureError('replay_failed', 'Archive lost its readable page content')
        return text

    text = inspect(page.main_frame)
    if any(path and row.get('visible') and row.get('text') and path not in visited
           for path, row in source_frames.items()):
        raise CaptureError('replay_failed', 'Archive lost its readable page content')
    return totals, text
