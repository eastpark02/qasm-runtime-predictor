import {test, expect, type Page} from '@playwright/test';

const device = {id:'fixture:sim',name:'Fixture simulator',kind:'SIMULATOR',status:'ONLINE',qubits:30,queue_depth:0,average_queue_minutes:0,min_shots:1,max_shots:1000,billing_source:'qBraid credits',input_types:['qasm2']};
const wallet = {id:'wallet-a-verified',user_id:'fixture-user',organization_id:'fixture-org',credits:20,checked_at:Date.now()/1000,shared_with:[],auto_recharge:'disabled'};
async function setup(page: Page) {
  await page.route('**/api/qbraid/profiles', route => route.fulfill({json:{profiles:[{id:'a',label:'Member one',configured:true},{id:'b',label:'Member two',configured:true}]}}));
  await page.route('**/api/qbraid/profiles/*/devices', route => route.fulfill({json:{devices:[device]}}));
  await page.route('**/api/qbraid/profiles/*/balance', route => route.fulfill({json:wallet}));
  await page.route('**/api/qbraid/jobs', route => route.fulfill({json:{jobs:[]}}));
  await page.goto('/'); await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid profile').selectOption('a');
  await page.getByLabel('qBraid device').selectOption(device.id);
}
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
