import {test, expect, type Page} from '@playwright/test';

// All qBraid routes are intercepted before opening the application. No provider call or spend.
const device = {id:'qbraid:qbraid:sim:gpu-tn',name:'Checker synthetic MPS',provider_name:'qBraid',route_name:'qBraid',kind:'SIMULATOR',status:'ONLINE',qubits:30,queue_depth:0,average_queue_minutes:0,min_shots:1,max_shots:1000,billing_source:'qBraid credits',input_types:['qasm2']};
const wallet = {id:'checker-wallet',user_id:'checker-user',organization_id:'checker-org',credits:100,checked_at:Date.now()/1000,shared_with:[]};
const prepared = {program:'OPENQASM 2.0; include "qelib1.inc"; qreg q[2]; creg c[2]; h q[0]; cx q[0],q[1]; measure q -> c;',program_hash:'a'.repeat(64),source_hash:'b'.repeat(64),scope:'full',classical_bits:2,measurement_mapping:'q[0] → c[0]; q[1] → c[1]; c[0] is the rightmost count bit',measurement_note:'Original terminal measurements preserved.',ideal_checkpoint:{step:2,step_kind:'source'},ideal_available:false};
const quote = (selection:object) => ({id:'checker-quote',selection,prepared,device,wallet,expires_at:Date.now()/1000+120,estimated_cost:1,estimated_remaining:99,pricing_available:true,requires_unknown_ack:false,insufficient:false,billing_source:'qBraid credits',note:'Synthetic fixture only; no job sent.'});
const selection = {circuit_id:'checker-circuit',scope:'full',step:0,step_kind:'source',profile_id:'a',device_id:device.id,shots:100,runtime_options:{}};
const stats = {qubits:[{qubit:0,clbit:0,p1:.5,p1_standard_error:.05,z:0},{qubit:1,clbit:1,p1:.5,p1_standard_error:.05,z:0}],pairs:[{qubits:[0,1],zz:1,standard_error:0}],note:'Classical measurement statistics; not entanglement.'};
const job = (id:string, shots=100) => ({request_id:id,selection:{...selection,shots},scope:'full',profile_id:'a',device_id:device.id,remote_job_id:'synthetic-only',shots,qubits:2,classical_bits:2,state:'COMPLETED',created_at:Date.now()/1000,returned_shots:shots,counts:{'00':shots/2,'11':shots/2},execution_seconds:2.5,elapsed_seconds:60,actual_cost:1,measurement_mapping:prepared.measurement_mapping,measurement_note:prepared.measurement_note,program:prepared.program,program_hash:prepared.program_hash,source_hash:prepared.source_hash,quote:quote(selection),measurement_statistics:stats,result_details:{simulationMethod:'matrix_product_state',precision:'double',maxBondDim:64,relativeCutoff:1e-5,absoluteCutoff:1e-5},runtime_options:{},returned_runtime_options:{}});

async function mockCloud(page:Page, jobs:ReturnType<typeof job>[] = []) {
  const execution: {path:string,body:any}[] = [];
  await page.route('**/api/qbraid/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (/\/(quote|submit|compare)$/.test(path)) execution.push({path,body:route.request().postDataJSON()});
    if (path.endsWith('/profiles')) return route.fulfill({json:{profiles:[{id:'a',label:'Synthetic checker',configured:true}]}});
    if (path.endsWith('/devices')) return route.fulfill({json:{devices:[device,{...device,id:'fixture:qpu',name:'Synthetic hardware',kind:'QPU'}],checked_at:Date.now()/1000}});
    if (path.endsWith('/balance')) return route.fulfill({json:wallet});
    if (path.endsWith('/jobs')) return route.fulfill({json:{jobs}});
    if (path.endsWith('/quote')) return route.fulfill({json:quote(route.request().postDataJSON())});
    if (path.endsWith('/compare')) return route.fulfill({json:{program_hash:prepared.program_hash,jobs,pairs:[{request_ids:jobs.map(j=>j.request_id),total_variation_distance:0}],warnings:['Returned shot counts differ; compare normalized probabilities.'],outcomes:[{bits:'00',probabilities:[.5,.5]},{bits:'11',probabilities:[.5,.5]}],outcomes_total:2}});
    if (path.endsWith('/programs')) return route.fulfill({json:{...jobs[0],provider_programs:{}}});
    if (path.endsWith('/submit')) return route.fulfill({status:500,json:{detail:'Unexpected test submission blocked.'}});
    return route.fulfill({status:404,json:{detail:'Unconfigured synthetic route; no provider request made.'}});
  });
  return execution;
}

async function openAndSelect(page:Page) {
  await page.goto('/');
  await page.getByRole('button',{name:'Open example',exact:true}).click();
  await page.getByLabel('qBraid profile').selectOption('a');
  await page.getByLabel('qBraid device').selectOption(device.id);
}

test('checker: default full circuit and explicit checkpoint zero safeguard stay manual',async ({page}) => {
  const calls = await mockCloud(page);
  await openAndSelect(page);
  await expect(page.getByRole('radio',{name:'Run full circuit',exact:true})).toBeChecked();
  await expect(page.locator('.quantum-checkpoint')).toContainText('Full circuit · all gates');
  expect(calls).toEqual([]);
  await page.getByRole('button',{name:'Get cost estimate',exact:true}).click();
  expect(calls[0].body).toMatchObject({scope:'full',step:0,step_kind:'source'});
  await expect(page.getByText('Prepared execution review',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Choose checkpoint',exact:true}).click();
  await expect(page.getByRole('radio',{name:'Run selected checkpoint',exact:true})).toBeChecked();
  await expect(page.locator('#circuit-checkpoint')).toBeFocused();
  await expect(page.getByRole('button',{name:'Get cost estimate',exact:true})).toBeDisabled();
  await expect(page.getByRole('checkbox',{name:/Checkpoint 0 has zero gates/})).not.toBeChecked();
  await expect(page.locator('.quantum-quote')).toHaveCount(0);
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await expect(page.getByRole('button',{name:'Get cost estimate',exact:true})).toBeEnabled();
  expect(calls).toHaveLength(1);
  await page.getByRole('button',{name:'Get cost estimate',exact:true}).click();
  expect(calls[1].body).toMatchObject({scope:'checkpoint',step:1});
  expect(calls.some(c=>c.path.endsWith('/submit'))).toBe(false);
});

test('checker: runtime option changes invalidate reviewed quote and matching ideal is explicit',async ({page}) => {
  const calls = await mockCloud(page);
  const states:any[]=[];
  await page.route('**/api/circuits/*/state',route=>{states.push(route.request().postDataJSON());return route.fulfill({json:{joint:[],qubits:[]}});});
  await openAndSelect(page);
  await page.getByRole('button',{name:'Get cost estimate',exact:true}).click();
  expect(states).toEqual([]);
  await page.getByRole('button',{name:'Calculate matching ideal',exact:true}).click();
  expect(states).toEqual([{step:2,step_kind:'source'}]);
  await page.getByLabel('Cloud runtime profile').selectOption('mps256');
  await expect(page.locator('.quantum-quote')).toHaveCount(0);
  await page.getByLabel('Cloud sampling seed').fill('0');
  await page.getByRole('button',{name:'Get cost estimate',exact:true}).click();
  expect(calls.at(-1)?.body.runtime_options).toEqual({method:'mps',precision:'double',maxBondDim:256,relativeCutoff:1e-8,absoluteCutoff:1e-8,seed:0});
  expect(calls.filter(c=>c.path.endsWith('/submit'))).toEqual([]);
});

test('checker: results display measured statistics timing provenance and comparison without submitting',async ({page}) => {
  const jobs = [job('11111111-1111-4111-8111-111111111111'),job('22222222-2222-4222-8222-222222222222',200)];
  const calls = await mockCloud(page,jobs);
  await page.goto('/');
  await page.getByLabel('Cloud job history').selectOption(jobs[0].request_id);
  await expect(page.getByRole('heading',{name:'Per measured qubit',exact:true})).toBeVisible();
  await expect(page.locator('.quantum-job')).toContainText('2.500 s');
  await expect(page.locator('.quantum-job')).toContainText('60.000 s');
  await expect(page.locator('.quantum-job')).toContainText('matrix_product_state');
  await expect(page.locator('.quantum-job')).toContainText('does not quantify entanglement');
  await page.getByText('Executed program and measurement provenance',{exact:true}).click();
  await page.getByRole('button',{name:'Fetch provider submitted / compiled program',exact:true}).click();
  await expect(page.getByText('No provider program available through this SDK/device.',{exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Measured outcomes · 100 returned shots',exact:true})).toBeVisible();
  const picker = page.locator('.quantum-comparison-picker');
  await picker.getByRole('checkbox').nth(0).check();
  await picker.getByRole('checkbox').nth(1).check();
  await page.getByRole('button',{name:'Compare selected runs',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Comparison of 2 saved runs',exact:true})).toBeVisible();
  await expect(page.locator('.quantum-comparison')).toContainText('TVD 0.0000');
  await expect(page.getByLabel('Saved run distribution comparison')).toBeVisible();
  expect(calls.filter(c=>c.path.endsWith('/compare'))).toHaveLength(1);
  expect(calls.some(c=>/\/(quote|submit)$/.test(c.path))).toBe(false);
  await page.locator('.quantum-comparison').screenshot({path:'../.local/checker-comparison-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await page.screenshot({path:'../.local/checker-results-mobile.png',fullPage:true});
});

test('checker: incomplete ideal response stays unavailable and another device needs a fresh quote',async ({page}) => {
  const jobs=[job('33333333-3333-4333-8333-333333333333')];
  const calls=await mockCloud(page,jobs);
  await page.route('**/api/circuits/*/state',route=>route.fulfill({json:{joint:[{bits:'00',probability:.5}],joint_total:4}}));
  await openAndSelect(page);
  await page.getByRole('button',{name:'Get cost estimate',exact:true}).click();
  await page.getByRole('button',{name:'Calculate matching ideal',exact:true}).click();
  await expect(page.getByText('The state view returned an incomplete distribution; an ideal baseline is unavailable.',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Calculate matching ideal',exact:true})).toBeEnabled();
  await expect(page.getByText('Matching complete Qiskit ideal distribution available; it will be projected through this classical mapping.',{exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'Reuse reviewed circuit for another device',exact:true}).click();
  await expect(page.locator('.quantum-quote')).toHaveCount(0);
  expect(calls).toHaveLength(1);
  await page.getByLabel('qBraid device').selectOption('fixture:qpu');
  expect(calls).toHaveLength(1);
  await page.getByRole('button',{name:'Get cost estimate',exact:true}).click();
  expect(calls[1].body).toMatchObject({circuit_id:'checker-circuit',scope:'full',device_id:'fixture:qpu'});
  expect(calls.some(c=>c.path.endsWith('/submit'))).toBe(false);
});
