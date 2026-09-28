const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(process.env.QWEN_IMAGE_URL || 'http://qwen-image:8188');
  await page.waitForFunction(() => Boolean(window.app?.graph && window.app?.canvas), null, { timeout: 90000 });
  await page.waitForFunction(
    () => window.app.graph.serialize().nodes.some(node => node.type === 'QwenImageDiTGPU'), null, { timeout: 30000 });
  assert.ok(await page.evaluate(
    () => !window.app.graph.serialize().nodes.some(node => node.type === 'UNETLoader')));
  const served = await (await page.request.get(new URL('/local-qwen-image/config', page.url()).href)).json();
  assert.equal(await page.evaluate(() => window.app.graph.serialize().nodes
    .find(node => node.type === 'QwenImageDiTGPU').widgets_values[0]), served.model_name);
  const workflow = JSON.parse(fs.readFileSync('/workflow.json', 'utf8'));
  const graph = await page.evaluate(async workflow => {
    const { app } = window;
    await app.loadGraphData(workflow);
    return (await app.graphToPrompt()).output;
  }, workflow);
  assert.equal(Object.keys(graph).length, 8);
  assert.equal(graph['5'].inputs.device, 'gpu');
  assert.equal(graph['4'].inputs.resolution, 1024);
  for (const [name, width, height] of [['desktop', 1600, 1000], ['mobile', 390, 844]]) {
    await page.setViewportSize({ width, height });
    await page.evaluate(() => {
      const { app } = window;
      app.canvas.ds.scale = Math.min(0.75, (window.innerWidth - 140) / 1850);
      app.canvas.ds.offset = [80, 180];
      app.canvas.setDirty(true, true);
    });
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `/screenshots/${name}.png`, fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.ok(await page.locator('canvas').first().isVisible());
  }
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('PASS: ComfyUI loads workflow, API graph is valid, desktop/mobile render without JS errors');
})().catch(error => { console.error(error); process.exit(1); });
