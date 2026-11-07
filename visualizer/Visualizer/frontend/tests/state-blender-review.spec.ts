import {test, expect, type Page} from '@playwright/test';

const harness = 'http://127.0.0.1:4174/';

async function blockCloud(page: Page) {
  await page.route('**/api/qbraid/**', route => route.fulfill({json: {profiles: [], jobs: []}}));
}

async function contextCounter(page: Page, fail = false) {
  await page.addInitScript(({fail}) => {
    const original = HTMLCanvasElement.prototype.getContext;
    const seen = new Set<HTMLCanvasElement>();
    (window as any).__checkerGL = seen;
    HTMLCanvasElement.prototype.getContext = function(kind: string, ...args: any[]) {
      if (kind.includes('webgl')) {
        if (fail) return null;
        seen.add(this);
        (window as any).__checkerGLCanvas = this;
      }
      return (original as any).call(this, kind, ...args);
    } as any;
  }, {fail});
}

test('checker Blender: axes radius mixed center camera invariance and one context for 25 cards', async ({page}) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await contextCounter(page);
  await page.goto(harness);
  const view = page.locator('.inspector .bloch-viewport');
  await expect(view).toHaveAttribute('data-renderer', '3d');
  const fixtures: [string, number[]][] = [
    ['z-plus', [0, 1, 0]], ['z-minus', [0, -1, 0]],
    ['x-plus', [1, 0, 0]], ['x-minus', [-1, 0, 0]],
    ['y-plus', [0, 0, -1]], ['y-minus', [0, 0, 1]],
    ['arbitrary', [.6123724356957945, .5, -.6123724356957945]],
    ['partial-q0', [0, .5, 0]], ['bell-q0', [0, 0, 0]], ['ghz-q0', [0, 0, 0]],
  ];
  for (const [id, tip] of fixtures) {
    await page.getByRole('combobox').selectOption(id);
    await expect.poll(async () => JSON.parse((await view.getAttribute('data-tip')) || '[]')).toEqual(tip.map(n => expect.closeTo(n, 8)));
    const state = JSON.parse((await view.getAttribute('data-state'))!);
    expect(Math.hypot(...tip)).toBeCloseTo(state.length, 8);
    await expect(view).toHaveAttribute('data-zero', String(state.length < 1e-12));
    await expect(view).toHaveAccessibleName(/Vector x .* y .* z .* Length/);
    await expect(view.locator('.bloch-axis-labels span')).toHaveCount(6);
  }
  await page.getByRole('combobox').selectOption('arbitrary');
  await expect(view).toHaveAttribute('data-zero', 'false');
  const before = await view.getAttribute('data-state');
  const tip = await view.getAttribute('data-tip');
  const camera = await view.getAttribute('data-camera');
  const resetCamera = async () => expect.poll(async () => JSON.parse((await view.getAttribute('data-camera'))!)).toEqual(JSON.parse(camera!).map((n: number) => expect.closeTo(n, 10)));
  const box = (await view.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down(); await page.mouse.move(box.x + box.width / 2 + 55, box.y + box.height / 2 + 25, {steps: 8}); await page.mouse.up();
  await expect(view).not.toHaveAttribute('data-camera', camera!);
  await expect(view).toHaveAttribute('data-state', before!);
  await expect(view).toHaveAttribute('data-tip', tip!);
  await page.locator('.inspector').getByRole('button', {name: 'Reset view of qubit 0'}).click();
  await resetCamera();
  await view.focus(); await page.keyboard.press('ArrowLeft'); await page.keyboard.press('+');
  await expect(view).not.toHaveAttribute('data-camera', camera!);
  await expect(view).toHaveAttribute('data-state', before!);
  await page.keyboard.press('r'); await resetCamera();
  expect(await page.evaluate(() => (window as any).__checkerGL.size)).toBe(1);
  await expect(page.locator('.bloch-viewport')).toHaveCount(25);
  const pixels = await view.locator('canvas').evaluate(canvas => {
    const data = (canvas as HTMLCanvasElement).getContext('2d')!.getImageData(0, 0, (canvas as HTMLCanvasElement).width, (canvas as HTMLCanvasElement).height).data;
    let drawn = 0, mint = 0;
    for (let i = 0; i < data.length; i += 4) {if (data[i+3] > 0) drawn++; if (data[i+1] > 180 && data[i+1] > data[i] * 1.25) mint++;}
    return {drawn, mint};
  });
  expect(pixels.drawn).toBeGreaterThan(10000); expect(pixels.mint).toBeGreaterThan(20);
  await view.screenshot({path: '../.local/checker-blender-desktop.png'});
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await view.screenshot({path: '../.local/checker-blender-mobile.png'});
  expect(errors).toEqual([]);
});

test('checker Blender: missing asset retains exact labeled projection including Y', async ({page}) => {
  await page.route('**/assets/bloch-sphere.glb', route => route.abort());
  await page.goto(harness);
  await page.getByRole('combobox').selectOption('y-plus');
  const card = page.locator('.inspector .bloch-studio');
  await expect(card).toContainText('Exact XZ projection · 3D unavailable');
  await expect(card).toContainText('XZ projection · Y = 1.000');
  await expect(card.locator('.bloch-fallback circle').last()).toHaveAttribute('cx', '130');
  await expect(card.locator('.bloch-fallback circle').last()).toHaveAttribute('cy', '105');
  await expect(card.getByRole('button')).toBeDisabled();
});

test('checker Blender: unavailable WebGL and lost context recover to projection', async ({page}) => {
  await contextCounter(page, true);
  await page.goto(harness);
  await expect(page.locator('.inspector .bloch-viewport')).toHaveAttribute('data-renderer', 'fallback');
  await expect(page.locator('.inspector .bloch-fallback')).toBeVisible();
});

test('checker Blender: context loss never leaves a blank visualization', async ({page}) => {
  await contextCounter(page);
  await page.goto(harness);
  const view = page.locator('.inspector .bloch-viewport');
  await expect(view).toHaveAttribute('data-renderer', '3d');
  await page.evaluate(() => (window as any).__checkerGLCanvas.dispatchEvent(new Event('webglcontextlost', {cancelable: true})));
  await expect(view).toHaveAttribute('data-renderer', 'fallback');
  await expect(view.locator('.bloch-fallback')).toBeVisible();
});

test('checker selected state: wide GHZ and MPS are manual honest and interactive', async ({page}) => {
  await blockCloud(page);
  const calls: any[] = [];
  page.on('request', request => {if (/\/state$/.test(request.url())) calls.push(request.postDataJSON());});
  await page.goto('/');
  for (const [id, qubit, method] of [['wide_ghz', 31, 'Stabilizer'], ['wide_mps', 23, 'MPS']] as const) {
    await page.getByLabel('Start with a synthetic circuit').selectOption(id);
    await page.getByRole('button', {name: 'Open example', exact: true}).click();
    await expect(page.getByRole('heading', {name: id === 'wide_ghz' ? /32-qubit GHZ/ : /24-qubit rotation chain/})).toBeVisible();
    await expect(page.locator('.state-panel .qubit-card')).toHaveCount(0);
    const jump = page.getByLabel('Jump to step');
    await jump.fill((await jump.getAttribute('max'))!);
    await page.getByLabel('Selected state qubit 1').selectOption(String(qubit));
    await expect(page.getByRole('checkbox', {name: /Include full joint/})).toBeDisabled();
    const before = calls.length;
    await expect(page.locator('.state-panel .qubit-card')).toHaveCount(0);
    await page.getByRole('button', {name: 'Calculate selected state', exact: true}).click();
    await expect(page.locator('.state-panel .qubit-card')).toHaveCount(1);
    expect(calls.length).toBe(before + 1);
    expect(calls.at(-1)).toMatchObject({qubits: [qubit], method: 'auto', include_joint: false});
    await expect(page.locator('.state-panel')).toContainText(`${method} · ${method === 'MPS' ? 'Approximate' : 'Exact'}`);
    if (method === 'MPS') await expect(page.locator('.state-panel')).toContainText('No certified error bound');
    await expect(page.locator('.joint-detail')).toHaveCount(0);
    await expect(page.locator('.bloch-viewport')).toHaveAttribute('data-renderer', '3d');
    await page.getByLabel('Selected state qubit 1').selectOption('0');
    await expect(page.locator('.result-banner')).toContainText('Previous result');
    expect(calls.length).toBe(before + 1);
  }
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator('.state-panel').screenshot({path: '../.local/checker-state-mobile.png'});
});

test('checker Blender: lazy module resolving after unmount creates no stale renderer', async ({page}) => {
  await blockCloud(page);
  // This race checks component lifecycle only; the preceding test uses real physics.
  await page.route('**/api/circuits/*/state', route => route.fulfill({json: {
    step: 0, step_kind: 'source', selected_qubits: [0], requested_method: 'auto',
    method: 'stabilizer', accuracy: 'exact', include_joint: false, global_joint_complete: false,
    requested_settings: {mps_max_bond: 64, mps_cutoff: 1e-10}, mps_settings: null,
    causal_cone: {simulated_qubits: 1, original_qubits: 2, retained_operations: 0},
    qubits: [{index: 0, p0: 1, p1: 0, bloch: [0, 0, 1], length: 1, entropy: 0, purity: 1}],
    pair: null, joint: [], joint_total: 0, calculation_seconds: .01, cached: false,
  }}));
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  let requested = false;
  let release!: () => void;
  const gate = new Promise<void>(resolve => {release = resolve;});
  await page.route('**/assets/bloch3d-*.js', async route => {
    requested = true;
    await gate;
    await route.continue();
  });
  await page.goto('/');
  await page.getByRole('button', {name: 'Open example', exact: true}).click();
  await page.getByRole('button', {name: 'Calculate selected state', exact: true}).click();
  await expect.poll(() => requested).toBe(true);
  await expect(page.locator('.bloch-fallback')).toBeVisible();
  await page.getByRole('button', {name: 'Open example', exact: true}).click();
  await expect(page.locator('.bloch-viewport')).toHaveCount(0);
  release();
  await page.getByRole('button', {name: 'Calculate selected state', exact: true}).click();
  await expect(page.locator('.bloch-viewport')).toHaveAttribute('data-renderer', '3d');
  await expect(page.locator('.bloch-axis-labels span')).toHaveCount(6);
  expect(errors).toEqual([]);
});
