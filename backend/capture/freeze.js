(imageVisible) => {
  const warnings = [];
  // DOM positions can change after this immutable clone was taken (including
  // when media is stopped). Keep element identity out of page attributes.
  const frameKeys = new WeakMap();
  const frameStates = [];
  window.__captureFrameKeys = frameKeys;
  let frameSequence = 0;
  // Record visibility with the immutable parent clone. A source script can
  // later hide/show an iframe without changing what this snapshot contained.
  // Offscreen frames are still rendered content and must not be discarded.
  const hiddenFrameReason = frame => {
    if (getComputedStyle(frame).visibility !== 'visible') return 'css_visibility';
    for (let node = frame; node; node = node.parentElement || node.getRootNode()?.host) {
      const style = getComputedStyle(node);
      if (style.display === 'none') return 'display_none';
      if (Number(style.opacity) === 0) return 'transparent';
      if (style.contentVisibility === 'hidden') return 'content_hidden';
    }
    const bounds = frame.getBoundingClientRect();
    return bounds.width <= 0 || bounds.height <= 0 ? 'empty_bounds' : null;
  };
  // outerHTML does not escape raw-text <style> contents. Preserve CSS strings
  // and URLs with a CSS escape; split bare delimiter tokens with a comment.
  const serializeStyleText = css => css.replace(
    /\\[\s\S]|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'|\/\*[\s\S]*?\*\/|(?<![-\w])url\(\s*(?:\\[\s\S]|[^"'()\\\s])*\s*\)|<\/style/gi,
    token => /^<\/style/i.test(token) ? '<' + '/**/' + token.slice(1) :
      token.replace(/<\/style/gi, endTag => '\\3c ' + endTag.slice(1)));
  const rasterCandidates = new Map();
  let rasterPixels = 0;
  for (const animation of document.getAnimations()) {
    try {
      const sampledTime = animation.currentTime;
      animation.pause();
      // pause() schedules work for the next animation frame. Seeking to the
      // sampled time completes that pending pause synchronously, so computed
      // styles and the source page stay on the same frame during serialization.
      if (sampledTime !== null) animation.currentTime = sampledTime;
    } catch (_) { warnings.push('animation-freeze-incomplete'); }
  }
  const serializeRoot = root => {
    const cloned = root instanceof ShadowRoot ? document.createElement('div') : root.cloneNode(true);
    if (root instanceof ShadowRoot) for (const child of root.childNodes) cloned.appendChild(child.cloneNode(true));
    const originalNodes = Array.from(root.querySelectorAll('*'));
    const cloneNodes = Array.from(cloned.querySelectorAll('*'));
    if (originalNodes.length > 100000) throw new Error('DOM exceeds capture node budget');
    // Large reference indexes can contain tens of thousands of useful links.
    // Keep the entire DOM and original CSS, but bound expensive computed-style
    // sampling to the first screen (our preview contract) and its ancestors.
    let sampledNodes = null;
    if (originalNodes.length > 30000) {
      sampledNodes = new Set();
      for (const node of originalNodes) {
        const rect = node.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 &&
            rect.top < innerHeight && rect.left < innerWidth) {
          for (let ancestor = node; ancestor && ancestor !== root; ancestor = ancestor.parentElement) {
            if (sampledNodes.has(ancestor)) break;
            sampledNodes.add(ancestor);
          }
          // Offscreen definitions may supply a visible SVG's paint or geometry.
          if (node instanceof SVGSVGElement) for (const child of node.querySelectorAll('*')) sampledNodes.add(child);
        }
      }
      if (sampledNodes.size > 30000) throw new Error('DOM exceeds capture style budget');
      warnings.push('large-dom-first-screen-styles');
    }
    // Share captured declarations instead of repeating hundreds of properties
    // on every node. A first, anonymous cascade layer preserves the priority of
    // the frozen !important values over the page's own styles and named layers.
    const styles = [], styleLookup = new Map();
    const rememberStyle = (dest, computed, attribute, source = null) => {
      const values = new Map(Array.from(computed, name => [name, computed.getPropertyValue(name)]));
      if (source?.computedStyleMap) {
        const typed = source.computedStyleMap();
        if (String(typed.get('height')) === 'auto') {
          values.set('height', 'auto'); values.set('block-size', 'auto');
        }
        if (String(typed.get('width')) === 'auto') {
          values.set('width', 'auto'); values.set('inline-size', 'auto');
        }
      }
      // The sampled transform/color/etc. already represents the animation frame.
      // Replaying the animation or transition would override that frozen state.
      values.set('animation-name', 'none');
      values.set('transition-property', 'none');
      const key = JSON.stringify(Array.from(values));
      let entry = styleLookup.get(key);
      if (!entry) {
        entry = {id: String(styles.length), values, attributes: new Set(), uses: 0};
        styles.push(entry); styleLookup.set(key, entry);
      }
      entry.attributes.add(attribute); entry.uses++;
      dest.setAttribute(attribute, entry.id);
    };
    originalNodes.forEach((source, index) => {
      let dest = cloneNodes[index];
      if (!dest) return;
      dest.removeAttribute('data-capture-source-image');
      dest.removeAttribute('data-capture-source-visible');
      dest.removeAttribute('data-capture-raster-src');
      for (const name of ['data-capture-style', 'data-capture-before', 'data-capture-after']) dest.removeAttribute(name);
      const sampleStyle = !sampledNodes || sampledNodes.has(source);
      // Explicit computed values freeze dynamic styles, CSS media state, transitions, and viewport units.
      const nativeControl = /^(INPUT|SELECT|OPTION|TEXTAREA|BUTTON)$/.test(source.tagName);
      const metadataNode = /^(HEAD|META|LINK|STYLE|TITLE|SCRIPT)$/.test(source.tagName);
      if (sampleStyle && !nativeControl && !metadataNode) {
        rememberStyle(dest, getComputedStyle(source), 'data-capture-style', source);
        // Inline !important would outrank the generated layer. All its resolved
        // values have already been captured, including custom properties.
        dest.removeAttribute('style');
      }
      dest.removeAttribute('autofocus');
      const id = `capture-${index}`;
      dest.setAttribute('data-capture-node', id);
      for (const which of sampleStyle ? ['::before', '::after'] : []) {
        const pseudo = getComputedStyle(source, which);
        if (pseudo.content !== 'none' && pseudo.content !== 'normal') {
          rememberStyle(dest, pseudo, which === '::before' ? 'data-capture-before' : 'data-capture-after');
        }
      }
      if (source instanceof HTMLInputElement) {
        dest.setAttribute('value', source.type === 'password' ? '' : source.value);
        if (source.checked) dest.setAttribute('checked', ''); else dest.removeAttribute('checked');
      }
      if (source instanceof HTMLTextAreaElement) dest.textContent = source.value;
      if (source instanceof HTMLOptionElement) {
        if (source.selected) dest.setAttribute('selected', ''); else dest.removeAttribute('selected');
      }
      if (source instanceof HTMLImageElement) {
        // Carry each image's baseline through rewriting and Monolith. Always
        // overwrite page-supplied markers; counts alone cannot identify damage.
        dest.setAttribute('data-capture-source-image',
          !source.complete ? 'pending' : source.naturalWidth > 0 ? 'loaded' : 'broken');
        dest.setAttribute('data-capture-source-visible', String(imageVisible(source)));
        if (source.currentSrc) {
          dest.setAttribute('src', source.currentSrc);
          dest.removeAttribute('srcset');
          dest.removeAttribute('sizes');
          dest.removeAttribute('loading');
        }
        // Keep HTML and real image elements, but avoid embedding multi-megapixel
        // originals for tiny, fixed preview regions. The rewriter adopts this
        // lossless PNG candidate only if it is smaller than the fetched asset.
        const src = source.currentSrc || source.src;
        const imageStyle = getComputedStyle(source);
        if (source.complete && source.naturalWidth * source.naturalHeight >= 8000000 &&
            imageStyle.objectFit !== 'none' && imageStyle.imageRendering === 'auto' &&
            /(?:\.(?:png|jpe?g|webp|avif|bmp)(?:[?#]|$)|^data:image\/(?:png|jpeg|webp|avif|bmp)[;,])/i.test(src)) {
          const rect = source.getBoundingClientRect();
          const scale = Math.max(rect.width * devicePixelRatio / source.naturalWidth,
            rect.height * devicePixelRatio / source.naturalHeight);
          const width = Math.ceil(source.naturalWidth * scale), height = Math.ceil(source.naturalHeight * scale);
          const pixels = width * height;
          if (rect.width > 0 && rect.height > 0 && scale <= 0.5 && pixels <= 2000000) {
            const key = `${src}\n${width}x${height}`;
            if (!rasterCandidates.has(key) && rasterPixels + pixels <= 8000000) {
              rasterPixels += pixels;
              try {
                const canvas = document.createElement('canvas');
                canvas.width = width; canvas.height = height;
                canvas.getContext('2d').drawImage(source, 0, 0, width, height);
                rasterCandidates.set(key, canvas.toDataURL('image/png'));
              } catch (_) {
                // Cross-origin images without CORS remain their original asset.
                rasterCandidates.set(key, null);
              }
            }
            const candidate = rasterCandidates.get(key);
            if (candidate?.startsWith('data:image/png;base64,')) dest.setAttribute('data-capture-raster-src', candidate);
          }
        }
      }
      if (source instanceof HTMLAudioElement) {
        // Static previews retain native controls and layout, but not playback.
        // Empty/missing source data can otherwise keep sandboxed replay waiting
        // for its load event indefinitely, even with all page images ready.
        dest.removeAttribute('src');
        dest.removeAttribute('autoplay');
        dest.setAttribute('preload', 'none');
        for (const child of dest.querySelectorAll('source,track')) child.remove();
        warnings.push('audio-not-archived');
      }
      if (source instanceof HTMLCanvasElement || source instanceof HTMLVideoElement) {
        let bitmap, mediaWarning;
        try {
          if (source instanceof HTMLCanvasElement) bitmap = source.toDataURL('image/png');
          else if (source.readyState >= 2) {
            const canvas = document.createElement('canvas');
            canvas.width = source.videoWidth; canvas.height = source.videoHeight;
            canvas.getContext('2d').drawImage(source, 0, 0); bitmap = canvas.toDataURL('image/png');
          }
        } catch (_) { /* A tainted canvas/video must not discard the rest of the page. */ }
        if (!bitmap || bitmap === 'data:,') {
          if (source instanceof HTMLVideoElement && source.poster) {
            bitmap = source.poster;
            mediaWarning = 'media-poster:VIDEO';
          } else {
            const width = Math.max(1, source.clientWidth || 300), height = Math.max(1, source.clientHeight || 150);
            const label = source instanceof HTMLVideoElement ? '视频内容未缓存' : '画布内容未缓存';
            // Local SVG is a bounded, explicit placeholder for this media region,
            // never a screenshot replacement for the HTML document.
            const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#202124"/><svg x="${width / 2 - 20}" y="${height / 2 - 20}" width="40" height="40" viewBox="0 0 40 40"><rect x="5" y="8" width="30" height="24" rx="3" fill="none" stroke="#9aa0a6" stroke-width="2"/><path d="M17 14L27 20L17 26Z" fill="#9aa0a6"/></svg></svg>`;
            bitmap = 'data:image/svg+xml,' + encodeURIComponent(svg);
            mediaWarning = `media-placeholder:${source.tagName}`;
            dest.setAttribute('alt', label);
          }
        }
        const img = document.createElement('img');
        for (const attr of dest.attributes) img.setAttribute(attr.name, attr.value);
        for (const name of ['poster', 'autoplay', 'controls', 'loop', 'muted', 'preload']) img.removeAttribute(name);
        img.src = bitmap;
        img.setAttribute('data-capture-media', mediaWarning ? 'fallback' : 'frame');
        img.setAttribute('data-capture-source-visible', String(imageVisible(source)));
        if (mediaWarning) warnings.push(mediaWarning);
        dest.replaceWith(img); dest = img;
      }
      const shadow = source.shadowRoot || window.__captureShadowRoots?.get(source);
      if (shadow) {
        // Monolith 2.10.1 drops template contents. Carry them through as ordinary
        // parsed DOM, then restore declarative shadow templates after archiving.
        const expanded = document.createElement('div');
        expanded.setAttribute('data-capture-shadow', 'declarative-placeholder');
        const frozen = serializeRoot(shadow);
        expanded.innerHTML = frozen.html;
        dest.prepend(expanded);
        warnings.push('shadow-dom-declarative-restored');
        warnings.push(...frozen.warnings);
      }
      if (source instanceof HTMLIFrameElement) {
        const key = String(frameSequence++);
        const state = {identifier: key, hiddenReason: hiddenFrameReason(source), visible: imageVisible(source)};
        frameKeys.set(source, state);
        frameStates.push(state);
        dest.setAttribute('data-capture-frame', key);
      }
    });
    if (styles.length) {
      const counts = new Map();
      for (const entry of styles) for (const [name, value] of entry.values) {
        let alternatives = counts.get(name);
        if (!alternatives) { alternatives = new Map(); counts.set(name, alternatives); }
        alternatives.set(value, (alternatives.get(value) || 0) + entry.uses);
      }
      const common = new Map();
      for (const [name, alternatives] of counts) {
        // Do not invent custom properties on nodes where they were absent.
        if (!styles.every(entry => entry.values.has(name))) continue;
        common.set(name, Array.from(alternatives).sort((a, b) => b[1] - a[1])[0][0]);
      }
      const declarations = values => Array.from(values, ([name, value]) => `${name}:${value}!important;`).join('');
      const selector = (attribute, id = null) => `[${attribute}${id === null ? '' : `="${id}"`}]` +
        (attribute === 'data-capture-before' ? '::before' : attribute === 'data-capture-after' ? '::after' : '');
      const attributes = ['data-capture-style', 'data-capture-before', 'data-capture-after'];
      let css = attributes.map(attribute => selector(attribute)).join(',') + `{${declarations(common)}}`;
      const differencesByEntry = new Map();
      const repeated = new Map();
      for (const entry of styles) {
        const differences = new Map(Array.from(entry.values).filter(([name, value]) => common.get(name) !== value));
        differencesByEntry.set(entry, differences);
        for (const [name, value] of differences) {
          // Scoped custom properties (often whole theme palettes or SVG data)
          // cannot use the universal common rule. Share declarations only
          // among the exact elements/pseudos where they were sampled.
          if (!name.startsWith('--') && value.length < 256) continue;
          let alternatives = repeated.get(name);
          if (!alternatives) { alternatives = new Map(); repeated.set(name, alternatives); }
          let entries = alternatives.get(value);
          if (!entries) { entries = []; alternatives.set(value, entries); }
          entries.push(entry);
        }
      }
      const sharedGroups = new Map();
      for (const [name, alternatives] of repeated) for (const [value, entries] of alternatives) {
        if (entries.length < 2) continue;
        const key = entries.map(entry => entry.id).join(',');
        let group = sharedGroups.get(key);
        if (!group) { group = {entries, values: [], savings: 0}; sharedGroups.set(key, group); }
        group.values.push([name, value]);
        group.savings += (name.length + value.length + 12) * (entries.length - 1);
      }
      const shared = [];
      for (const group of sharedGroups.values()) {
        const selectors = group.entries.flatMap(entry => Array.from(entry.attributes, attribute => selector(attribute, entry.id))).join(',');
        if (group.savings <= selectors.length + 2) continue;
        shared.push(`${selectors}{${declarations(group.values)}}`);
        for (const entry of group.entries) for (const [name] of group.values) differencesByEntry.get(entry).delete(name);
      }
      for (const [entry, differences] of differencesByEntry) {
        if (differences.size) css += Array.from(entry.attributes, attribute => selector(attribute, entry.id)).join(',') + `{${declarations(differences)}}`;
      }
      css += shared.join('');
      const frozenStyle = document.createElement('style');
      frozenStyle.textContent = serializeStyleText(`@layer{${css}}`);
      (cloned.querySelector('head') || cloned).prepend(frozenStyle);
    }
    // CSSOM and adopted stylesheets may never have appeared in DOM text.
    for (const sheet of root.adoptedStyleSheets || []) {
      try {
        const style = document.createElement('style');
        style.textContent = serializeStyleText(Array.from(sheet.cssRules, rule => rule.cssText).join('\n'));
        cloned.appendChild(style);
      } catch (_) { warnings.push('unreadable-adopted-stylesheet'); }
    }
    for (let index = 0; index < originalNodes.length; index++) {
      const source = originalNodes[index], dest = cloneNodes[index];
      if (source.tagName === 'STYLE' && source.sheet && dest) {
        try { dest.textContent = serializeStyleText(Array.from(source.sheet.cssRules, rule => rule.cssText).join('\n')); }
        catch (_) { warnings.push('unreadable-stylesheet'); }
      }
    }
    return {html: root instanceof ShadowRoot ? cloned.innerHTML : (cloned.outerHTML || cloned.innerHTML), warnings: []};
  };
  const frozen = serializeRoot(document.documentElement);
  return {html: '<!doctype html>' + frozen.html, warnings, frames: frameStates,
    title: document.title.slice(0, 1000),
    description: (document.querySelector('meta[name="description"]')?.content || '').slice(0, 2000),
    text_excerpt: (document.body?.innerText || '').slice(0, 6000)};
}
