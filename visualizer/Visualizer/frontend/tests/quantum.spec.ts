import {test, expect, type Page} from '@playwright/test';

const device = {id:'fixture:sim',name:'Fixture simulator',provider_name:'Fixture',route_name:'Fixture route',kind:'SIMULATOR',status:'ONLINE',qubits:30,queue_depth:0,average_queue_minutes:0,min_shots:1,max_shots:1000,billing_source:'qBraid credits',input_types:['qasm2']};
const wallet = {id:'wallet-a-verified',user_id:'fixture-user',organization_id:'fixture-org',credits:20,checked_at:Date.now()/1000,shared_with:[],auto_recharge:'disabled'};
async function setup(page: Page) {
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles:[{id:'a',label:'Member one',configured:true},{id:'b',label:'Member two',configured:true}]}}));
  await page.route('**/api/qbraid/profiles/*/devices', route => route.fulfill({json:{devices:[device],checked_at:Date.now()/1000}}));
  await page.route('**/api/qbraid/profiles/*/balance', route => route.fulfill({json:wallet}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid profile').selectOption('a');
  await page.getByLabel('qBraid device').selectOption(device.id);
}

test('main dashboard connects and browses without a circuit, then uploads through its qBraid button', async ({page, request}) => {
  const session = {id:'session_dashboard',label:'Local session · dashboard',session:true,configured:true};
  let connected = false;
  const executionCalls: string[] = [];
  page.on('request', r => {if (/\/qbraid\/(quote|submit)$/.test(r.url())) executionCalls.push(r.url());});
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles:connected ? [session] : []}}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.route('**/api/qbraid/profiles/*/devices', route => route.fulfill({json:{devices:[device],checked_at:Date.now()/1000}}));
  await page.route('**/api/qbraid/profiles/*/balance', route => route.fulfill({json:wallet}));
  await page.route('**/api/qbraid/connect', route => {
    expect(route.request().postDataJSON().api_key).toBe('fake-dashboard-key');
    connected = true;
    return route.fulfill({json:{profile:session,wallet}});
  });
  await page.goto('/');
  const connector = page.getByRole('region',{name:'qBraid connector',exact:true});
  await expect(connector).toBeVisible();
  await expect(connector.getByRole('heading',{name:'qBraid connector',exact:true})).toBeVisible();
  await expect(connector.locator('.quantum-checkpoint')).toContainText('No circuit selected');
  await expect(connector.getByRole('button',{name:'Get cost estimate',exact:true})).toBeDisabled();
  await connector.getByLabel('qBraid API key').fill('fake-dashboard-key');
  await connector.getByRole('button',{name:'Connect',exact:true}).click();
  await connector.getByLabel('qBraid device').selectOption(device.id);
  await expect(connector.locator('.quantum-wallet')).toContainText('20 credits');
  await expect(connector.getByRole('button',{name:'Get cost estimate',exact:true})).toBeDisabled();
  await expect(connector.getByLabel('qBraid API key')).toHaveCount(0);
  const source = await (await request.get('/api/examples/bell/source')).text();
  const chooseFile = page.waitForEvent('filechooser');
  await connector.getByRole('button',{name:'Upload QASM',exact:true}).click();
  await (await chooseFile).setFiles({name:'dashboard-bell.qasm',mimeType:'text/plain',buffer:Buffer.from(source)});
  await expect(page.getByRole('heading',{name:'dashboard-bell.qasm',exact:true})).toBeVisible();
  await expect(connector.locator('.quantum-checkpoint')).toContainText('Reviewing dashboard-bell.qasm');
  await expect(connector.getByLabel('qBraid device')).toHaveValue(device.id);
  await expect(connector.getByRole('button',{name:'Get cost estimate',exact:true})).toBeEnabled();
  expect(await connector.evaluate(el => !!document.querySelector('.workspace-grid') && !!(el.compareDocumentPosition(document.querySelector('.workspace-grid')!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await expect(page.locator('.quantum-panel')).toHaveCount(1);
  expect(executionCalls).toEqual([]);
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await connector.screenshot({path:'test-results/qbraid-dashboard-mobile.png'});
});
test('insufficient credit keeps selected profile and never submits or switches', async ({page}) => {
  const requests: string[] = [];
  page.on('request', r => {if (r.url().includes('/qbraid/')) requests.push(r.url());});
  await page.route('**/api/qbraid/quote', route => route.fulfill({json:{id:'fixture-quote',expires_at:Date.now()/1000+120,selection:route.request().postDataJSON(),device,wallet,estimated_cost:50,estimated_remaining:-30,pricing_available:true,requires_unknown_ack:false,insufficient:true,billing_source:'qBraid credits',note:'Estimate is not a charge cap.'}}));
  await setup(page);
  await page.getByRole('button',{name:'Get cost estimate'}).click();
  await expect(page.getByText(/Insufficient credits in this wallet/)).toBeVisible();
  await expect(page.getByLabel('qBraid profile')).toHaveValue('a');
  await expect(page.getByRole('button',{name:/Submit 100-shot/})).toBeDisabled();
  expect(requests.some(u=>u.includes('/profiles/b/'))).toBe(false);
  expect(requests.some(u=>u.endsWith('/submit'))).toBe(false);
  await page.getByLabel('qBraid profile').selectOption('b');
  await expect(page.locator('.quantum-quote')).toHaveCount(0);
});

test('unknown estimate needs acknowledgment and cloud result includes observed/ideal bars', async ({page}) => {
  let submitted = 0;
  await page.route('**/api/qbraid/quote', route => route.fulfill({json:{id:'fixture-quote',expires_at:Date.now()/1000+120,selection:route.request().postDataJSON(),device,wallet,estimated_cost:null,estimated_remaining:null,pricing_available:false,requires_unknown_ack:true,insufficient:false,billing_source:'qBraid credits',reason:'dynamic_pricing_unavailable',note:'No estimate available.'}}));
  await page.route('**/api/qbraid/submit', async route => {
    submitted++; const request=route.request().postDataJSON();
    await route.fulfill({json:{request_id:request.request_id,selection:request,profile_id:'a',device_id:device.id,remote_job_id:'fixture-job',shots:100,qubits:2,state:'COMPLETED',created_at:Date.now()/1000,returned_shots:100,counts:{'00':75,'11':25},comparison:[{bits:'00',count:75,observed:.75,ideal:.5},{bits:'11',count:25,observed:.25,ideal:.5}],measurement_mapping:'q[0] is rightmost',quote:{wallet,billing_source:'qBraid credits'},actual_cost:1}});
  });
  await setup(page); await page.getByRole('button',{name:'Get cost estimate'}).click();
  await expect(page.getByRole('button',{name:/Submit 100-shot/})).toBeDisabled();
  await page.getByRole('checkbox',{name:/I understand that pricing is unknown/}).check();
  await page.getByRole('button',{name:/Submit 100-shot/}).click();
  await expect(page.locator('.quantum-job-status')).toContainText('COMPLETED');
  expect(submitted).toBe(1);
  await expect(page.locator('.quantum-counts')).toContainText('75.00%');
  await expect(page.locator('.quantum-job progress').first()).toBeVisible();
  await page.locator('.quantum-panel').screenshot({path:'test-results/qbraid-fixture-preview.png'});
});

test('local key connection clears password, reads credits and QPU catalogue, and disconnect revokes review', async ({page}) => {
  const session = {id:'session_fixture',label:'Local session · fixture',session:true,configured:true};
  const qpu = {...device,id:'fixture:qpu',name:'Fixture hardware',kind:'QPU'};
  let profiles: typeof session[] = [], connects = 0, quotes = 0, submits = 0;
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles}}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.route('**/api/qbraid/profiles/*/devices', route => route.fulfill({json:{devices:[device,qpu],checked_at:Date.now()/1000}}));
  await page.route('**/api/qbraid/profiles/*/balance', route => route.fulfill({json:wallet}));
  await page.route('**/api/qbraid/connect', route => {
    connects++;
    const body = route.request().postDataJSON();
    expect(body.api_key).toBe('fake-local-test-key');
    expect(route.request().url()).not.toContain(body.api_key);
    if (connects === 2) expect(body.profile_id).toBe(session.id);
    profiles = [session];
    return route.fulfill({json:{profile:session,wallet}});
  });
  await page.route('**/api/qbraid/quote', route => {
    quotes++;
    return route.fulfill({json:{id:'fixture-quote',expires_at:Date.now()/1000+120,selection:route.request().postDataJSON(),device:qpu,wallet,estimated_cost:2,estimated_remaining:18,pricing_available:true,requires_unknown_ack:false,insufficient:false,billing_source:'qBraid credits',note:'Device and shot estimate in credits.'}});
  });
  await page.route('**/api/qbraid/submit', route => {submits++; return route.abort();});
  await page.route('**/api/qbraid/profiles/*/disconnect', route => {
    profiles = [{...session,configured:false}];
    return route.fulfill({json:{disconnected:true}});
  });
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  const key = page.getByLabel('qBraid API key');
  await expect(key).toHaveAttribute('type','password');
  await key.fill('fake-local-test-key');
  await page.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(page.getByRole('button',{name:'Disconnect',exact:true})).toBeVisible();
  await expect(page.locator('.quantum-wallet')).toContainText('20 credits');
  expect(quotes).toBe(0); expect(submits).toBe(0);
  await page.getByLabel('qBraid device').selectOption(qpu.id);
  await expect(page.getByLabel('qBraid device')).toContainText('Fixture hardware · Fixture via Fixture route · QPU');
  await page.getByRole('button',{name:'Get cost estimate'}).click();
  await expect(page.locator('.quantum-quote')).toContainText('18');
  expect(quotes).toBe(1); expect(submits).toBe(0);
  await page.getByRole('button',{name:'Disconnect',exact:true}).click();
  await expect(page.locator('.quantum-quote')).toHaveCount(0);
  await expect(key).toHaveValue('');
  await expect(page.getByText(/Saved local session is disconnected/)).toBeVisible();
  await page.reload(); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid profile').selectOption(session.id);
  await expect(key).toHaveValue('');
  await key.fill('fake-local-test-key');
  await page.getByRole('button',{name:'Reconnect saved profile'}).click();
  await expect(page.getByRole('button',{name:'Disconnect',exact:true})).toBeVisible();
  expect(connects).toBe(2); expect(submits).toBe(0);
  const stored = await page.evaluate(() => JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)]));
  expect(stored).not.toContain('fake-local-test-key');
});

test('rejected local key shows safe error and clears field without estimating', async ({page}) => {
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles:[]}}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.route('**/api/qbraid/connect', route => route.fulfill({status:502,json:{detail:'qBraid request failed for this profile. Check its credentials, permissions, network and credit balance.'}}));
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid API key').fill('fake-invalid-local-key');
  await page.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(page.getByLabel('qBraid API key')).toHaveValue('');
  await expect(page.getByRole('alert')).toContainText('qBraid request failed');
  await expect(page.locator('body')).not.toContainText('fake-invalid-local-key');
});

test('connection in progress locks profile selection until its result is bound', async ({page}) => {
  let release: () => void = () => {};
  const pending = new Promise<void>(resolve => {release = resolve;});
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles:[{id:'a',label:'Member one',configured:true}]}}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.route('**/api/qbraid/connect', async route => {
    await pending;
    return route.fulfill({status:502,json:{detail:'qBraid credentials were rejected.'}});
  });
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid API key').fill('fake-local-key');
  await page.getByRole('button',{name:'Connect',exact:true}).click();
  await expect(page.getByLabel('qBraid profile')).toBeDisabled();
  await expect(page.getByLabel('qBraid API key')).toHaveValue('');
  release();
  await expect(page.getByLabel('qBraid profile')).toBeEnabled();
  await expect(page.getByRole('alert')).toContainText('credentials were rejected');
});

test('catalogue separates IonQ hardware from Braket route and clears empty or failed refreshes', async ({page}) => {
  const forte = {...device,id:'aws:ionq:qpu:forte-1',name:'Forte 1 (AWS)',kind:'QPU',provider_name:'IonQ',route_name:'Amazon Braket',qubits:36,min_shots:100};
  let phase: 'loaded' | 'empty' | 'failed' = 'loaded', submissions = 0;
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles:[{id:'a',label:'Member one',configured:true}]}}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.route('**/api/qbraid/profiles/*/devices', route => phase === 'failed'
    ? route.fulfill({status:502,json:{detail:'Catalogue refresh failed for the selected profile.'}})
    : route.fulfill({json:{devices:phase === 'empty' ? [] : [forte],checked_at:Date.now()/1000}}));
  await page.route('**/api/qbraid/profiles/*/balance', route => route.fulfill({json:wallet}));
  await page.route('**/api/qbraid/quote', route => route.fulfill({status:502,json:{detail:'qBraid denied a cost estimate for this device and profile. Catalogue visibility and ONLINE status do not grant execution permission.'}}));
  await page.route('**/api/qbraid/submit', route => {submissions++; return route.abort();});
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid profile').selectOption('a');
  await page.getByLabel('qBraid device').selectOption(forte.id);
  await expect(page.getByLabel('qBraid device')).toContainText('IonQ via Amazon Braket');
  await expect(page.locator('.quantum-device')).toContainText('Hardware / simulator provider: IonQ');
  await expect(page.locator('.quantum-device')).toContainText('Submission route: Amazon Braket');
  await expect(page.getByText(/ONLINE describes the device/)).toContainText('not your account’s execution access');
  await page.getByRole('button',{name:'Get cost estimate'}).click();
  await expect(page.getByRole('alert')).toContainText('do not grant execution permission');
  await expect(page.locator('.quantum-quote')).toHaveCount(0);
  phase = 'empty';
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect(page.getByText(/No supported gate-model devices were returned/)).toBeVisible();
  await expect(page.getByLabel('qBraid device').locator('option')).toHaveCount(1);
  await expect(page.locator('.quantum-device')).toHaveCount(0);
  phase = 'loaded';
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await page.getByLabel('qBraid device').selectOption(forte.id);
  phase = 'failed';
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect(page.getByRole('alert')).toContainText('Catalogue refresh failed');
  await expect(page.getByLabel('qBraid device').locator('option')).toHaveCount(1);
  await expect(page.locator('.quantum-device')).toHaveCount(0);
  await expect(page.getByText(/qBraid catalogue checked/)).toHaveCount(0);
  expect(submissions).toBe(0);
});
