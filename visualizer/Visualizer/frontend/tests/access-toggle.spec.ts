import {test, expect} from '@playwright/test';
import type {Page} from '@playwright/test';

// The switch is global backend state: every test leaves it OFF, even on failure or timeout.
test.afterEach(async ({request}) => {
  expect((await request.post('/api/access', {data: {allow_unlisted: false}})).ok()).toBe(true);
});

async function expectOffLabels(page: Page) {
  await expect(page.locator('.audit-label')).toHaveText('Audit uploads blocked');
  await expect(page.locator('.source-note')).toHaveText('Approved development files only');
  await expect(page.locator('.welcome-guard')).toHaveText('3 approved file contents · audit labels never loaded');
  await expect(page.locator('.welcome-intro>p')).toContainText('Open a synthetic example or upload an allowlisted development circuit.');
}

// Synthetic HTTP fixtures only: proves the footer switch renders from /api/health,
// POSTs /api/access on change, refreshes health, and swaps the access labels.
test('footer switch renders from health, posts on flip and swaps access labels', async ({page}) => {
  let allow = false;
  const posts: unknown[] = [], unexpected: string[] = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const access = {scope: allow ? 'unrestricted' : 'development_only', allow_unlisted: allow, default_from_env: false, approved_files: 3, source: 'synthetic manifest'};
    if (path === '/api/health') return route.fulfill({json: {model: {available: true, thresholds: [256], fingerprint: 'synthetic'}, access}});
    if (path === '/api/access' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      posts.push(body); allow = body.allow_unlisted;
      return route.fulfill({json: {...access, scope: allow ? 'unrestricted' : 'development_only', allow_unlisted: allow}});
    }
    if (path === '/api/circuits' && route.request().method() === 'POST') return route.fulfill({status: 403, json: {detail: 'Upload rejected: synthetic content is not on the development allowlist'}});
    if (path === '/api/examples') return route.fulfill({json: []});
    if (path === '/api/importance') return route.fulfill({json: {available: false}});
    if (path === '/api/qbraid/profiles') return route.fulfill({json: {profiles: []}});
    if (path === '/api/qbraid/jobs') return route.fulfill({json: {jobs: []}});
    unexpected.push(path);
    return route.fulfill({status: 404, json: {detail: 'Unconfigured synthetic route'}});
  });
  await page.goto('/');
  const toggle = page.getByRole('switch', {name: 'Allow unlisted uploads'});
  await expect(toggle).toBeVisible();
  await expect(toggle).not.toBeChecked();
  await expectOffLabels(page);
  await expect(page.getByText('Unlisted uploads allowed')).toHaveCount(0);

  await toggle.click();
  await expect(toggle).toBeChecked();
  expect(posts).toEqual([{allow_unlisted: true}]);
  await expect(page.locator('.audit-label')).toHaveText('Unlisted uploads allowed');
  await expect(page.locator('.app-header .lucide-shield-alert')).toBeVisible();
  await expect(page.locator('.source-note')).toHaveText('Any UTF-8 .qasm file (unlisted uploads allowed)');
  await expect(page.locator('.welcome-guard')).toContainText('unlisted uploads allowed: audit exclusion is not guaranteed');
  await expect(page.locator('.welcome-intro>p')).toContainText('upload any .qasm file (unlisted files are labelled; audit exclusion is not guaranteed for them).');
  await expect(page.locator('.welcome-intro>p')).not.toContainText('allowlisted development circuit');
  // Warning colour differs from the muted off state, in both themes.
  const colors = () => page.evaluate(() => ['.source-note', 'footer>span:first-child', '.welcome-guard', '.version-label'].map(s => getComputedStyle(document.querySelector(s)!).color));
  const seen: string[] = [];
  for (const theme of ['Switch to light mode', 'Switch to dark mode']) {
    await page.getByTitle(theme).click();
    await expect(toggle).toBeVisible();
    const [note, footer, guard, muted] = await colors();
    expect(note).toBe(footer);
    expect(guard).toBe(note);
    expect(note).not.toBe(muted);
    seen.push(note);
  }
  expect(seen[0]).not.toBe(seen[1]);  // Light and dark use their own --warn token.

  // Keyboard operable: Space flips it back off and everything renders as before.
  await toggle.focus();
  await page.keyboard.press('Space');
  await expect(toggle).not.toBeChecked();
  expect(posts).toEqual([{allow_unlisted: true}, {allow_unlisted: false}]);
  await expectOffLabels(page);

  // Flipping the switch leaves an unrelated error banner in place.
  await page.locator('input[type=file]').setInputFiles({name: 'unknown.qasm', mimeType: 'text/plain', buffer: Buffer.from('OPENQASM 2.0;\nqreg q[1];\n')});
  const banner = page.locator('.error-banner');
  await expect(banner).toContainText('synthetic content is not on the development allowlist');
  await toggle.click();
  await expect(toggle).toBeChecked();
  await expect(banner).toContainText('synthetic content is not on the development allowlist');
  expect(unexpected).toEqual([]);
});

// Live backend: proves the real switch accepts an unlisted synthetic upload and
// that turning it off revokes that circuit and rejects a fresh upload.
test('live: switch on accepts an unlisted upload; switch off revokes it', async ({page, request}) => {
  expect((await (await request.get('/api/access')).json()).allow_unlisted).toBe(false);
  const marker = `access-toggle-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const source = `OPENQASM 2.0;\ninclude "qelib1.inc";\n// ${marker}\nqreg q[2];\nh q[0];\ncx q[0],q[1];\n`;
  await page.goto('/');
  const toggle = page.getByRole('switch', {name: 'Allow unlisted uploads'});
  await expect(toggle).not.toBeChecked();
  await expect(page.locator('.audit-label')).toHaveText('Audit uploads blocked');

  await toggle.click();
  await expect(toggle).toBeChecked();
  await expect(page.locator('.audit-label')).toHaveText('Unlisted uploads allowed');
  expect((await (await request.get('/api/health')).json()).access).toMatchObject({scope: 'unrestricted', allow_unlisted: true});

  const uploaded = page.waitForResponse(r => new URL(r.url()).pathname === '/api/circuits' && r.request().method() === 'POST');
  await page.locator('input[type=file]').setInputFiles({name: `${marker}.qasm`, mimeType: 'text/plain', buffer: Buffer.from(source)});
  const response = await uploaded;
  expect(response.status()).toBe(200);
  const circuit = await response.json();
  expect(circuit.origin).toBe('unlisted');
  expect(circuit.warnings[0]).toBe('Unlisted upload: this file is not on the development allowlist, so audit exclusion is not guaranteed for it.');
  await expect(page.getByRole('heading', {name: `${marker}.qasm`, exact: true})).toBeVisible();
  await expect(page.locator('.tag-warn')).toHaveText('Unlisted upload');
  expect((await request.get(`/api/circuits/${circuit.id}/steps`)).status()).toBe(200);

  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect(page.locator('.audit-label')).toHaveText('Audit uploads blocked');
  expect((await (await request.get('/api/access')).json())).toMatchObject({scope: 'development_only', allow_unlisted: false});
  expect([403, 404]).toContain((await request.get(`/api/circuits/${circuit.id}/steps`)).status());
  expect([403, 404]).toContain((await request.post(`/api/circuits/${circuit.id}/features`, {data: {step: 1}})).status());
  const fresh = await request.post(`/api/circuits?name=${marker}.qasm`, {data: Buffer.from(source), headers: {'Content-Type': 'text/plain'}});
  expect(fresh.status()).toBe(403);
});
