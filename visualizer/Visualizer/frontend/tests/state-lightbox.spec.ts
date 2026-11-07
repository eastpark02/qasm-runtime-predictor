import {test, expect, type Page} from '@playwright/test';

async function openExample(page: Page, id: string) {
  await page.route('**/api/qbraid/**', route => route.fulfill({json: {profiles: [], jobs: []}}));
  await page.goto('/');
  await page.getByLabel('Start with a synthetic circuit').selectOption(id);
  await page.getByRole('button', {name: 'Open example', exact: true}).click();
  await expect(page.getByLabel('State selection size')).toBeVisible();
}

test('three selected GHZ states are manual and expand into an accessible lightbox', async ({page}) => {
  const calls: unknown[] = [];
  page.on('request', request => {if (/\/state$/.test(request.url())) calls.push(request.postDataJSON());});
  await openExample(page, 'ghz');
  await expect(page.getByLabel('State selection size')).toHaveValue('3');
  await expect(page.getByLabel('Selected state qubit 3')).toHaveValue('2');
  await page.getByLabel('Jump to step').fill('3');
  expect(calls).toHaveLength(0);
  await page.getByRole('button', {name: 'Calculate selected state', exact: true}).click();
  await expect(page.locator('.state-panel .qubit-card')).toHaveCount(3);
  expect(calls).toHaveLength(1);
  expect(calls[0]).toMatchObject({qubits: [0, 1, 2]});
  const cardBounds = await Promise.all((await page.locator('.state-panel .qubit-card').all()).map(card => card.boundingBox()));
  expect(cardBounds.every(box => box && Math.abs(box.y - cardBounds[0]!.y) < 2)).toBe(true);
  await expect(page.locator('.pair-detail')).toHaveCount(0);
  for (const card of await page.locator('.state-panel .qubit-card').all()) {
    await expect(card).toContainText('|0⟩ 50.0%');
    await expect(card).toContainText('|1⟩ 50.0%');
    await expect(card).toContainText('Maximally entangled with rest');
  }
  const expand = page.getByRole('button', {name: 'Expand Bloch sphere for qubit 1', exact: true});
  await expand.click();
  const dialog = page.getByRole('dialog', {name: 'Qubit q1', exact: true});
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Entanglement entropy');
  await expect(dialog).toContainText('1.000 bits');
  const cardBox = (await page.locator('.qubit-card').nth(1).locator('.bloch-viewport').first().boundingBox())!;
  const largeBox = (await dialog.locator('.bloch-viewport').boundingBox())!;
  expect(largeBox.height).toBeGreaterThan(cardBox.height);
  expect(largeBox.width).toBeGreaterThan(cardBox.width * 2);
  const close = dialog.getByRole('button', {name: 'Close expanded qubit view'});
  await expect(close).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
  await expect(expand).toBeFocused();
  await page.locator('.qubit-card').nth(0).locator('.bloch-viewport').click();
  await expect(page.getByRole('dialog', {name: 'Qubit q0', exact: true})).toBeVisible();
  await page.mouse.click(2, 2);
  await expect(page.getByRole('dialog')).not.toBeVisible();
  expect(calls).toHaveLength(1);
});

test('each of three qubit selectors changes independently and leaves results marked stale', async ({page}) => {
  const calls: unknown[] = [];
  page.on('request', request => {if (/\/state$/.test(request.url())) calls.push(request.postDataJSON());});
  await openExample(page, 'wide_ghz');
  await page.getByLabel('Selected state qubit 1').selectOption('31');
  await page.getByLabel('Selected state qubit 2').selectOption('15');
  await page.getByLabel('Selected state qubit 3').selectOption('7');
  await expect(page.getByLabel('Selected state qubit 2').locator('option[value="31"]')).toBeDisabled();
  await expect(page.getByLabel('Selected state qubit 3').locator('option[value="15"]')).toBeDisabled();
  expect(calls).toHaveLength(0);
  await page.getByRole('button', {name: 'Calculate selected state', exact: true}).click();
  await expect(page.locator('.qubit-card')).toHaveCount(3);
  expect(calls[0]).toMatchObject({qubits: [31, 15, 7]});
  await page.getByLabel('Selected state qubit 2').selectOption('14');
  await expect(page.locator('.result-banner')).toContainText('Previous result');
  await page.getByRole('button', {name: 'Expand Bloch sphere for qubit 15'}).click();
  await expect(page.getByRole('dialog')).toContainText('Previous result');
  await page.keyboard.press('Escape');
  expect(calls).toHaveLength(1);
  await page.getByLabel('State selection size').selectOption('1');
  await expect(page.getByLabel('Selected state qubit 1')).toHaveValue('31');
  await page.getByLabel('State selection size').selectOption('3');
  await expect(page.getByLabel('Selected state qubit 1')).toHaveValue('31');
  await expect(page.getByLabel('Selected state qubit 2')).toHaveValue('0');
  await expect(page.getByLabel('Selected state qubit 3')).toHaveValue('1');
  expect(calls).toHaveLength(1);
});

test('mobile lightbox retains exact projection when WebGL is unavailable', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(kind: string, ...args: unknown[]) {
      return kind.includes('webgl') ? null : (original as any).call(this, kind, ...args);
    } as typeof original;
  });
  await openExample(page, 'ghz');
  await page.getByRole('button', {name: 'Calculate selected state', exact: true}).click();
  await expect(page.locator('.qubit-card')).toHaveCount(3);
  await page.getByRole('button', {name: 'Expand Bloch sphere for qubit 2'}).click();
  const dialog = page.getByRole('dialog', {name: 'Qubit q2', exact: true});
  await expect(dialog.locator('.bloch-fallback')).toBeVisible();
  await expect(dialog).toContainText('Exact XZ projection · 3D unavailable');
  await expect(dialog).toContainText('XZ projection · Y = 0.000');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const rect = (await dialog.boundingBox())!;
  expect(rect.width).toBeLessThanOrEqual(390);
  await dialog.getByRole('button', {name: 'Close expanded qubit view'}).click();
  await expect(dialog).not.toBeVisible();
});
