import {test, expect, type Page, type Route} from '@playwright/test';

const circuit = {id:'shap-fixture',name:'Synthetic SHAP fixture',origin:'synthetic',num_qubits:2,num_clbits:0,total_steps:2,total_instructions:2,state_boundary:null,qubit_labels:['q0','q1'],parse_seconds:0,inspection_mode:'native_expanded',inspection_backend:'app_owned_cpp'};
const features = {features:{st_ok:1,raw_feature:3},derived_features:Object.fromEntries(Array.from({length:7}, (_,i)=>[`derived_${i}`,i]))};
function explanation(count: number, selection: Record<string, unknown>) {
  const contributions = Array.from({length:count}, (_,i)=>({feature:`input_${i}`,value:i,contribution:(i%2 ? -1 : 1)*(i+1)/100}));
  return {...selection,units:'log10(seconds)',base_value:0,output:contributions.reduce((s,c)=>s+c.contribution,0),contribution_count:count,contribution_sum:contributions.reduce((s,c)=>s+c.contribution,0),contributions,note:'Synthetic test explanation'};
}
async function setup(page: Page) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/health') return route.fulfill({json:{model:{available:true,explanation_available:true,thresholds:[64,256],fingerprint:'synthetic'},access:{scope:'synthetic',approved_files:1}}});
    if (path === '/api/examples') return route.fulfill({json:[{id:'partial',title:circuit.name,description:'Synthetic'}]});
    if (path === '/api/examples/partial') return route.fulfill({json:circuit});
    if (path.endsWith('/instructions')) return route.fulfill({json:{steps:[1,2].map(step=>({step,name:'h',qubits:[0],clbits:[],parameters:[],angle_expression:'',source:'h q[0];',line:step,instances:1,body:null,state_boundary:false}))}});
    if (path.endsWith('/overview')) return route.fulfill({json:{total:2,step_kind:'expanded',buckets:[]}});
    if (path.endsWith('/features')) return route.fulfill({json:{...features,...route.request().postDataJSON()}});
    if (path.endsWith('/predict')) return route.fulfill({json:{...features,...route.request().postDataJSON(),predicted_seconds:.2,inference_seconds:.001,cached:false,model_fingerprint:'synthetic'}});
    if (path.endsWith('/profiles')) return route.fulfill({json:{profiles:[]}});
    if (path.endsWith('/jobs')) return route.fulfill({json:{jobs:[]}});
    return route.fulfill({json:{available:false}});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'Open example',exact:true}).click();
  await expect(page.getByRole('heading',{name:circuit.name,exact:true})).toBeVisible();
}

for (const count of [0,1,7,10,13]) {
  test(`shows ${Math.min(count,10)} genuine SHAP rows from ${count} inputs`, async ({page}) => {
    await setup(page);
    await page.route('**/api/circuits/*/explain', route=>route.fulfill({json:explanation(count,route.request().postDataJSON())}));
    await page.getByRole('button',{name:'Predict runtime here'}).click();
    await expect(page.locator('.shap-row')).toHaveCount(Math.min(10,count));
    await expect(page.getByText(`Showing ${Math.min(count,10)} of ${count} model inputs.`,{exact:false})).toBeVisible();
    await expect(page.getByRole('button',{name:'Derived inputs (7)',exact:true})).toBeVisible();
    if (!count) await expect(page.getByText('No SHAP contributions were returned for this checkpoint.')).toBeVisible();
    if (count) await expect(page.locator('.shap-label code').first()).toHaveText(`input_${count-1}`);
    if (count < 10) await expect(page.getByText(/All available contributions are shown/)).toBeVisible();
  });
}

test('selection changes keep previous inputs labeled and allow a fresh explanation without prediction', async ({page}) => {
  await setup(page);
  const selections: Record<string,unknown>[] = [];
  let predictions = 0;
  page.on('request',r=>{if(r.url().endsWith('/predict')) predictions++;});
  await page.route('**/api/circuits/*/explain', route=>{const selection=route.request().postDataJSON();selections.push(selection);return route.fulfill({json:explanation(7,selection)});});
  await page.getByRole('button',{name:'Predict runtime here'}).click();
  await expect(page.locator('.shap-row')).toHaveCount(7);
  await page.getByLabel('Simulator threshold').selectOption('64');
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(0);
  await expect(page.getByText('No explanation for this selection yet.',{exact:false})).toBeVisible();
  await expect(page.getByText(/Step 0 · expanded · threshold 256 · previous checkpoint/)).toBeVisible();
  await page.getByRole('button',{name:'Inspect features here'}).click();
  await expect(page.getByText('Step 1 · expanded · threshold 64',{exact:true})).toBeVisible();
  expect(selections).toHaveLength(1);
  await page.getByRole('button',{name:'Explain this checkpoint',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(7);
  await expect(page.locator('.shap-chart')).toContainText('Step 1 · expanded · threshold 64');
  expect(selections[1]).toEqual({step:1,step_kind:'expanded',threshold:64});
  expect(predictions).toBe(1);
});

test('failed explanations retry independently; late responses never replace a new circuit', async ({page}) => {
  await setup(page);
  let attempts=0;
  let release: (()=>void) | undefined;
  let pending: Route | undefined;
  await page.route('**/api/circuits/*/explain', async route=>{
    attempts++;
    if(attempts===1) return route.fulfill({status:422,json:{detail:'Synthetic explanation failure'}});
    if(attempts===3) {pending=route; await new Promise<void>(resolve=>{release=resolve;});}
    await route.fulfill({json:explanation(attempts===3 ? 1 : 7,route.request().postDataJSON())});
  });
  await page.getByRole('button',{name:'Explain this checkpoint',exact:true}).click();
  await expect(page.getByText(/Synthetic explanation failure/)).toBeVisible();
  await page.getByRole('button',{name:'Retry explanation',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(7);
  await expect(page.getByText(/Synthetic explanation failure/)).toHaveCount(0);
  await page.getByRole('button',{name:'Refresh explanation',exact:true}).click();
  await expect(page.getByText(/Calculating explanation for step 0/)).toBeVisible();
  await expect.poll(()=>!!pending).toBe(true);
  await page.getByRole('button',{name:'Open example',exact:true}).click();
  await expect(page.getByRole('button',{name:'Explain this checkpoint',exact:true})).toBeEnabled();
  release!();
  await expect.poll(()=>pending!.request().response().then(r=>r?.status())).toBe(200);
  await expect(page.locator('.shap-row')).toHaveCount(0);
  await expect(page.getByText('No explanation for this selection yet.',{exact:false})).toBeVisible();
});
