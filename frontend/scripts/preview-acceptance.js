async page => {
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.goto('http://127.0.0.1:18765/library');
  await page.getByRole('button', { name: '查看 动态页面预览 12', exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('iframe').length === 8);
  await page.evaluate(() => {
    window.previewMetrics = { desktopMax: 0, mobileMax: 0, violations: [] };
    const check = () => {
      const count = document.querySelectorAll('iframe').length;
      const mobile = matchMedia('(max-width:767px)').matches;
      const cap = mobile ? 4 : 8;
      const key = mobile ? 'mobileMax' : 'desktopMax';
      window.previewMetrics[key] = Math.max(window.previewMetrics[key], count);
      if (count > cap) window.previewMetrics.violations.push({ count, cap });
    };
    window.previewObserver = new MutationObserver(check);
    window.previewObserver.observe(document.body, { subtree: true, childList: true });
    check();
  });
  const top = await page.locator('iframe').evaluateAll(frames => frames.map(frame => frame.src));
  await page.locator('main').evaluate(element => element.scrollTop = element.scrollHeight);
  await page.waitForFunction(old => [...document.querySelectorAll('iframe')].some(frame => !old.includes(frame.src)), top);
  const bottom = await page.locator('iframe').evaluateAll(frames => frames.map(frame => frame.src));
  await page.locator('main').evaluate(element => element.scrollTop = 0);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: '查看 动态页面预览 12', exact: true }).click();
  await page.locator('[aria-label="网站详情"] iframe').waitFor();
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-desktop.png', fullPage: true });
  const dimensions = await page.locator('iframe').evaluateAll(frames => frames.map(frame => ({
    width: frame.style.width, height: frame.style.height, sandbox: frame.getAttribute('sandbox'),
  })));
  if (dimensions.some(frame => frame.width !== '1366px' || frame.height !== '768px' || frame.sandbox !== ''))
    throw new Error('Desktop viewport or sandbox is incorrect');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => document.querySelectorAll('iframe').length === 1);
  await page.getByRole('button', { name: '手机', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('iframe')?.style.width === '390px');
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile-detail.png', fullPage: true });
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('iframe').length === 4);
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 320, height: 760 });
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile-320.png', fullPage: true });
  const result = await page.evaluate(() => ({
    metrics: window.previewMetrics,
    overflow: document.documentElement.scrollWidth > innerWidth,
    columns: getComputedStyle(document.querySelector('article').parentElement).gridTemplateColumns,
  }));
  if (result.metrics.violations.length) throw new Error(JSON.stringify(result.metrics));
  if (result.overflow) throw new Error('Mobile page overflows horizontally');
  return { dimensions, offscreenReclaimed: top.some(src => !bottom.includes(src)), ...result };
}
