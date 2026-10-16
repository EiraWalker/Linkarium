async page => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto('http://127.0.0.1:18765/library');
  await page.getByRole('button', { name: '查看 Example 公共网页验收', exact: true }).click();
  const preview = page.locator('[aria-label="网站详情"] iframe');
  await preview.waitFor();
  await page.waitForTimeout(16000);
  if (await page.getByText('预览未加载', { exact: true }).count())
    throw new Error('A successfully loaded HTML preview was marked as timed out');
  await page.getByText('快照提示（1）', { exact: true }).click();
  await page.getByText('部分图片、字体等资源未保存，页面可能不完整。', { exact: true }).waitFor();
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-desktop.png', fullPage: true });
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-public-desktop.png', fullPage: true });
  const desktop = await preview.evaluate(frame => ({ width: frame.style.width, height: frame.style.height, sandbox: frame.getAttribute('sandbox') }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '手机', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('iframe').length === 1 && document.querySelector('iframe')?.style.width === '390px');
  await page.frameLocator('[aria-label="网站详情"] iframe').locator('body').filter({ hasText: 'This domain is for use in documentation examples' }).waitFor();
  const mobile = await preview.evaluate(frame => ({ width: frame.style.width, height: frame.style.height, sandbox: frame.getAttribute('sandbox') }));
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile-detail.png' });
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-public-mobile-detail.png' });
  await page.getByRole('button', { name: '关闭详情', exact: true }).click();
  await page.getByRole('button', { name: '手机快照', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('iframe').length === 4 && [...document.querySelectorAll('iframe')].every(frame => frame.style.width === '390px'));
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile.png', fullPage: true });
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile-viewport.png' });
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-public-mobile.png' });
  await page.setViewportSize({ width: 320, height: 760 });
  await page.screenshot({ path: 'D:/website/output/playwright/frontend-mobile-320.png', fullPage: true });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  if (overflow) throw new Error('Mobile layout overflows horizontally');
  return { desktop, mobile, contentWarningShown: true, previewTimeout: false, mobileIframeCount: await page.locator('iframe').count(), overflow };
}
