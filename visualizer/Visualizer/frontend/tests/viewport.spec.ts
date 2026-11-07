import {test, expect, type Page} from '@playwright/test';

function instruction(step: number) {
  return {step, source_step: step, name: 'h', qubits: [step % 256], clbits: [], parameters: [], angle_expression: '', source: `h q[${step % 256}];`, primitive_source: `h q[${step % 256}];`, line: step + 4, instances: 1, body: null, state_boundary: false};
}
async function wideCircuit(page: Page) {
  await page.route('**/api/examples/partial', route => route.fulfill({json:{id:'large-fixture', name:'Large synthetic fixture', origin:'synthetic', num_qubits:256, num_clbits:0, total_steps:33152, total_instructions:33152, state_boundary:null, expanded_state_boundary:null, parse_seconds:.02, inspection_mode:'native_expanded', inspection_backend:'app_owned_cpp', qubit_labels:[]}}));
  await page.route('**/api/circuits/large-fixture/instructions?*', route => {
    const url = new URL(route.request().url()), offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
    return route.fulfill({json:{offset,total:33152,steps:Array.from({length:Math.min(limit,33152-offset)},(_,i)=>instruction(offset+i+1))}});
  });
  await page.route('**/api/circuits/large-fixture/overview?*', route => route.fulfill({json:{total:33152,buckets:[{start:1,end:33152,count:33152}]}}));
  await page.goto('/');
  await page.getByRole('button',{name:'Open example',exact:true}).click();
}

test('whole-circuit paging, zoom, fullscreen and selection never compute', async ({page}) => {
  const computations: string[] = [], pages: number[] = [];
  page.on('request', request => {
    if (/\/(predict|state|explain|stepper|submit)$/.test(request.url())) computations.push(request.url());
    if (request.url().includes('/instructions?')) pages.push(Number(new URL(request.url()).searchParams.get('offset')));
  });
  await wideCircuit(page);
  await expect(page.getByText('C++ inspection · dev')).toBeVisible();
  await expect(page.getByRole('button',{name:'Run C++ stepper here'})).toHaveCount(0);
  await page.getByLabel('Jump to step').fill('1.5');
  await expect(page.getByLabel('Jump to step')).toHaveValue('1');
  await page.getByLabel('Jump to step').fill('21001');
  await expect(page.locator('.instruction-detail code')).toContainText('h q[9]');
  expect(pages.some(p => p > 20000)).toBe(true);
  await page.getByRole('button',{name:'Focus selected',exact:true}).click();
  await page.getByRole('button',{name:'Zoom in',exact:true}).click();
  const scale = await page.locator('.zoom-readout').textContent();
  await page.getByRole('button',{name:'View circuit fullscreen',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'Fullscreen circuit explorer'})).toBeVisible();
  await expect(page.getByLabel('Jump to step')).toHaveValue('21001');
  await expect(page.locator('.zoom-readout')).toHaveText(scale!);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'View circuit fullscreen'})).toBeFocused();
  await page.getByRole('button',{name:'Fit circuit',exact:true}).click();
  await expect(page.locator('.zoom-readout')).toHaveText('Overview');
  const requestsBefore = pages.length;
  await page.waitForTimeout(150);
  expect(pages.length).toBe(requestsBefore);
  expect(await page.locator('.circuit-viewport .gate').count()).toBe(0);
  await page.getByRole('button',{name:'Explore circuit overview'}).press('Enter');
  await expect(page.locator('.zoom-readout')).not.toHaveText('Overview');
  expect(await page.locator('.circuit-viewport .gate').count()).toBeLessThan(80);
  expect(computations).toEqual([]);
});

test('fullscreen overlay works without browser fullscreen API and mobile stays contained', async ({page}) => {
  await page.addInitScript(() => {Object.defineProperty(Element.prototype,'requestFullscreen',{value:undefined,configurable:true});});
  await wideCircuit(page);
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'View circuit fullscreen'}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button',{name:'Zoom in',exact:true}).click();
  await page.getByRole('button',{name:'Exit fullscreen'}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('dragging and pinching gate columns moves the camera without selecting gates', async ({page}) => {
  await wideCircuit(page);
  await expect(page.locator('.circuit-viewport .gate').first()).toBeVisible();
  const original = await page.locator('.circuit-viewport .gate').first().getAttribute('aria-label');
  const rect = (await page.locator('.circuit-viewport').boundingBox())!;
  await page.mouse.move(rect.x + 450, rect.y + 140);
  await page.mouse.down();
  await page.mouse.move(rect.x + 150, rect.y + 140, {steps:10});
  await page.mouse.up();
  await expect(page.getByLabel('Jump to step')).toHaveValue('0');
  await expect(page.locator('.circuit-viewport .gate').first()).not.toHaveAttribute('aria-label',original!);
  // First overscan gate can be clipped at the left edge; click an interior gate.
  await page.locator('.circuit-viewport .gate').nth(2).click();
  await expect(page.getByLabel('Jump to step')).not.toHaveValue('0');
  const selected = await page.getByLabel('Jump to step').inputValue();
  const session = await page.context().newCDPSession(page);
  await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{id:1,x:rect.x+300,y:rect.y+150},{id:2,x:rect.x+450,y:rect.y+150}]});
  await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{id:1,x:rect.x+240,y:rect.y+150},{id:2,x:rect.x+510,y:rect.y+150}]});
  await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await expect(page.locator('.zoom-readout')).not.toHaveText('100%');
  await expect(page.getByLabel('Jump to step')).toHaveValue(selected);
});

test('prediction launches signed SHAP, keeps all inputs, and labels postprocessing', async ({page}) => {
  const features = {st_ok:.75, count:4, decreases:2};
  await page.route('**/api/circuits/*/predict', route => {const s = route.request().postDataJSON(); return route.fulfill({json:{...s,features,derived_features:{threshold_log:2.4},predicted_seconds:21600,inference_seconds:.002,model_fingerprint:'b'.repeat(64),cached:false,prefix_kind:'generated_primitives'}});});
  const contributions = Array.from({length:14},(_,i)=>({feature:`feature_${i}`,value:i,contribution:(i%2?-1:1)*(14-i)/10}));
  await page.route('**/api/circuits/*/explain', route => route.fulfill({json:{...route.request().postDataJSON(),units:'log10(seconds)',base_value:1,output:2,contribution_sum:1,contributions,postprocessing_applied:true,note:'fixture'}}));
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await page.getByRole('button',{name:'Predict runtime here',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(10);
  await expect(page.locator('.shap-row').first()).toContainText('feature_0');
  await expect(page.locator('.shap-bar.negative')).toHaveCount(5);
  await expect(page.getByText('Python fallback · complete',{exact:true})).toBeVisible();
  await expect(page.getByText(/timeout rule or runtime cap changed/)).toBeVisible();
  await page.getByRole('button',{name:'Derived inputs (1)'}).click();
  await expect(page.locator('.feature-table')).toContainText('threshold_log');
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(0);
  await page.getByRole('button',{name:'Previous step',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(10);
});

test('late SHAP stays attached to its checkpoint after navigation', async ({page}) => {
  let release!: () => void;
  const pending = new Promise<void>(resolve => {release = resolve;});
  await page.route('**/api/circuits/*/predict', route => route.fulfill({json:{...route.request().postDataJSON(),features:{st_ok:1},predicted_seconds:1,inference_seconds:.001,model_fingerprint:'a'.repeat(64),cached:false}}));
  await page.route('**/api/circuits/*/explain', async route => {await pending; await route.fulfill({json:{...route.request().postDataJSON(),units:'log10(seconds)',base_value:0,output:0,contributions:[{feature:'delayed',value:1,contribution:.3}],note:'fixture'}});});
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await page.getByRole('button',{name:'Predict runtime here',exact:true}).click();
  await expect(page.getByText(/Calculating explanation for step 1/)).toBeVisible();
  await page.getByRole('button',{name:'Next step',exact:true}).click(); release();
  await expect(page.locator('.shap-row')).toHaveCount(0);
  await page.getByRole('button',{name:'Previous step',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(1);
});

test('opening another circuit clears computations that finish for the previous circuit', async ({page}) => {
  let releaseOpen!: () => void, releasePrediction!: () => void;
  const opening = new Promise<void>(resolve => {releaseOpen=resolve;});
  const predicting = new Promise<void>(resolve => {releasePrediction=resolve;});
  await page.route('**/api/examples/bell', async route => {const response=await route.fetch(); await opening; await route.fulfill({response});});
  await page.route('**/api/circuits/*/predict', async route => {await predicting; await route.fulfill({json:{...route.request().postDataJSON(),features:{st_ok:1},predicted_seconds:1,inference_seconds:.001,model_fingerprint:'a'.repeat(64),cached:false}});});
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await expect(page.getByLabel('Jump to step')).toBeVisible();
  await page.getByLabel('Start with a synthetic circuit').selectOption('bell');
  await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByRole('button',{name:'Predict runtime here',exact:true}).click();
  await expect(page.getByRole('button',{name:'Predicting…',exact:true})).toBeDisabled();
  releaseOpen();
  await expect(page.getByRole('button',{name:'Predict runtime here',exact:true})).toBeEnabled();
  releasePrediction();
  await expect(page.getByText('Run manually at this checkpoint', {exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Open example',exact:true})).toBeEnabled();
});
