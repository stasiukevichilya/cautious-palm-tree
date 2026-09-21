const { chromium } = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = process.env.SDXL_URL || 'http://sdxl:8080';
  await page.goto(base);
  await page.waitForFunction(() => document.querySelector('#connection').dataset.ready === 'true');
  await page.fill('#prompt', 'A red ceramic teapot on a white table, studio photography');
  await page.fill('#steps', '30');
  await page.fill('#seed', '42');
  const submitted = page.waitForResponse(response => response.url().endsWith('/api/generations') && response.request().method() === 'POST');
  await page.click('#generate');
  const response = await submitted;
  assert.equal(response.status(), 202);
  const created = await response.json();
  await page.waitForFunction(id => !document.querySelector('#image').hidden &&
    document.querySelector('#image').getAttribute('src') === `/api/generations/${id}/image`, created.id, {timeout:180000});
  await page.waitForFunction(() => document.querySelector('#image').naturalWidth === 1024);
  assert.equal(await page.locator('#error').isVisible(), false);
  const job = await page.evaluate(() => localStorage.getItem('sdxl-job'));
  assert.ok(job);
  await page.reload();
  await page.waitForFunction(() => !document.querySelector('#image').hidden);
  assert.equal(await page.evaluate(() => localStorage.getItem('sdxl-job')), job);
  for (const [name, width, height] of [['desktop',1440,1000],['mobile',390,844]]) {
    await page.setViewportSize({width,height});
    await page.screenshot({path:`/screenshots/${name}.png`,fullPage:true});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    assert.ok(await page.locator('#image').evaluate(img => img.complete && img.naturalWidth === 1024));
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('PASS: generation, reload, image, desktop/mobile layout, no browser errors');
})().catch(error => { console.error(error); process.exit(1); });
