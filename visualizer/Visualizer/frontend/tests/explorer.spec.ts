import {test, expect} from '@playwright/test';

test('upload → select → predict → state; navigation stays manual and caches are labeled', async ({page, request}) => {
  const calls: string[] = [];
  page.on('request', r => {if (/\/(predict|state)$/.test(r.url())) calls.push(r.url());});
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Explore what happens between gates.'})).toBeVisible();
  // Seed the content cache under the example title; an upload must still show
  // the user's filename when it reuses that same parsed circuit.
  await page.getByRole('button', {name:'Open example', exact:true}).click();
  await expect(page.getByRole('heading', {name:'Partial entanglement + rotations', exact:true})).toBeVisible();
  await expect(page.getByText('C++ inspection · dev', {exact:true})).toBeVisible();
  const source = await (await request.get('/api/examples/partial/source')).text();
  await page.locator('input[type=file]').setInputFiles({name:'partial.qasm', mimeType:'text/plain', buffer:Buffer.from(source)});
  await expect(page.getByRole('heading', {name:'partial.qasm'})).toBeVisible();
  await page.getByRole('button', {name:'Step 2: CX on q0, q1', exact:true}).click();
  await expect(page.locator('.instruction-detail code')).toHaveText('cx q[0],q[1];');
  expect(calls).toHaveLength(0);
  await page.getByRole('button', {name:'Inspect features here'}).click();
  await expect(page.getByPlaceholder('Filter features…')).toBeVisible();
  expect(calls).toHaveLength(0);
  await page.getByRole('button', {name:'Predict runtime here'}).click();
  await expect(page.getByText(/^(Evaluated|Cached) · step 2 · threshold 256$/)).toBeVisible();
  await page.getByRole('button', {name:'Calculate state here'}).click();
  await expect(page.getByText('Pairwise concurrence', {exact:true})).toBeVisible();
  await expect(page.getByText('0.811', {exact:false}).first()).toBeVisible();
  expect(calls).toHaveLength(2);
  await page.screenshot({path:'test-results/checkpoint-desktop.png', fullPage:true});
  await page.getByRole('button', {name:'Next step', exact:true}).click();
  await expect(page.getByText('Previous result — calculate to update', {exact:false})).toBeVisible();
  expect(calls).toHaveLength(2);
  await page.getByRole('button', {name:'Previous step', exact:true}).click();
  await page.getByRole('button', {name:'Calculate state here'}).click();
  await expect(page.getByText('Cached result', {exact:true})).toBeVisible();
  await page.getByRole('button', {name:'Predict runtime here'}).click();
  await expect(page.getByText('Cached · step 2 · threshold 256')).toBeVisible();
  await page.getByLabel('Simulator threshold').selectOption('64');
  await expect(page.getByText('Run manually at this checkpoint', {exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:'test-results/checkpoint-mobile.png',fullPage:true});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('unknown content rejected and measurement boundary shown', async ({page, request}) => {
  await page.goto('/');
  await page.locator('input[type=file]').setInputFiles({name:'bell.qasm',mimeType:'text/plain',buffer:Buffer.from('OPENQASM 2.0; qreg q[1]; x q[0];')});
  await expect(page.getByRole('alert')).toContainText('not on the development/synthetic allowlist');
  await page.getByLabel('Start with a synthetic circuit').selectOption('custom');
  await page.getByRole('button',{name:'Open example'}).click();
  await expect(page.getByText('C++ inspection · dev', {exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await expect(page.locator('.instruction-detail code')).toContainText('ry(');
  await page.getByRole('button',{name:'Calculate state here'}).click();
  await expect(page.getByText('Pairwise concurrence', {exact:true})).toBeVisible();
  await expect(page.locator('.pair-detail strong').first()).toHaveText('0.000');
  await page.getByText('Inspect custom gate body', {exact:false}).click();
  await expect(page.locator('pre')).toContainText('gate pair');
  await page.getByLabel('Jump to step').fill('3');
  await expect(page.getByRole('button',{name:'Calculate state here'})).toBeDisabled();
  await expect(page.getByText('State inspection stops before measurement/reset', {exact:false})).toBeVisible();
  const source = await (await request.get('/api/examples/bell/source')).text();
  expect(source).toContain('cx q[0],q[1]');
});

test('worker timeout is visible and controls recover', async ({page}) => {
  await page.goto('/');
  await page.getByRole('button',{name:'Open example'}).click();
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await page.route('**/api/circuits/*/state', async route => {await route.fulfill({status:408,contentType:'application/json',body:JSON.stringify({detail:'State exceeded 10 seconds; the worker was terminated'})});});
  await page.getByRole('button',{name:'Calculate state here'}).click();
  await expect(page.getByRole('alert')).toContainText('worker was terminated');
  await expect(page.getByRole('button',{name:'Calculate state here'})).toBeEnabled();
});
