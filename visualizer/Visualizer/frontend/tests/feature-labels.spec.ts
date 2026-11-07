import {test, expect, type Page} from '@playwright/test';

async function openFixture(page: Page, raw: Record<string, number>) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const selection = () => route.request().postDataJSON();
    if (path === '/api/health') return route.fulfill({json:{model:{available:true,explanation_available:true,thresholds:[256]},access:{scope:'synthetic',approved_files:1}}});
    if (path === '/api/examples') return route.fulfill({json:[{id:'partial',title:'Descriptive feature fixture',description:'Synthetic'}]});
    if (path === '/api/examples/partial') return route.fulfill({json:{id:'feature-labels',name:'Descriptive feature fixture',origin:'synthetic',num_qubits:4,num_clbits:4,total_steps:3,total_instructions:3,state_boundary:null,qubit_labels:['q0','q1','q2','q3'],parse_seconds:0,inspection_mode:'native_expanded',inspection_backend:'app_owned_cpp'}});
    if (path.endsWith('/instructions')) return route.fulfill({json:{steps:[1,2,3].map(step=>({step,name:'h',qubits:[0],clbits:[],parameters:[],angle_expression:'',source:'h q[0];',line:step,instances:1,body:null,state_boundary:false}))}});
    if (path.endsWith('/overview')) return route.fulfill({json:{total:3,step_kind:'expanded',buckets:[]}});
    if (path.endsWith('/features')) return route.fulfill({json:{...selection(),features:raw,derived_features:{cost_sel_per_2q:1.25}}});
    if (path.endsWith('/explain')) return route.fulfill({json:{...selection(),units:'log10(seconds)',base_value:0,output:.2,contributions:[{feature:'cost_sel_per_2q',value:1.25,contribution:.2}],note:'Synthetic'}});
    if (path.endsWith('/profiles')) return route.fulfill({json:{profiles:[]}});
    if (path.endsWith('/jobs')) return route.fulfill({json:{jobs:[]}});
    return route.fulfill({json:{available:false}});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'Open example',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Descriptive feature fixture',exact:true})).toBeVisible();
}

test('descriptive titles explain SHAP inputs and support searching while preserving identifiers', async ({page}) => {
  await openFixture(page,{st_ok:1,n_gates:9,n_1q:4,n_2q:3,n_3q_plus:2,n_measure:4,n_reset:1,n_barrier:2,n_nonclifford:2,n_param_gates:3,st_motif_entropy:.8});
  await page.getByRole('button',{name:'Inspect features here',exact:true}).click();
  const counts = page.getByRole('region',{name:'Gate counts at this checkpoint'});
  await expect(counts.locator('dl').first().locator('dd')).toHaveText(['9','4','3','2']);
  await expect(counts).toContainText('Measurements, resets and barriers are excluded.');
  await expect(counts).toContainText('expanded step 0');
  await page.getByLabel('Filter features').fill('pattern diversity');
  await expect(page.locator('.feature-table tbody tr')).toHaveCount(1);
  await expect(page.locator('.feature-table')).toContainText('Local gate-pattern diversity');
  await expect(page.locator('.feature-table')).toContainText('st_motif_entropy');
  await expect(page.locator('.feature-table')).toContainText('not quantum-state entropy');
  await page.getByRole('button',{name:'Explain this checkpoint',exact:true}).click();
  await expect(page.locator('.shap-row')).toContainText('Estimated MPS work per two-qubit gate');
  await expect(page.locator('.shap-row code')).toHaveText('cost_sel_per_2q');
  await expect(page.locator('.shap-row')).toContainText('minus log₁₀(1 + two-qubit gates)');
  await page.getByRole('button',{name:'Next step',exact:true}).click();
  await expect(counts).toContainText('previous checkpoint — inspect again to update');
});

test('unreported gate counts stay unavailable and source/structural totals are never added', async ({page}) => {
  await openFixture(page,{st_ok:1,n_gates:9,n_1q:4,st_n_gates:11,st_n_1q:6,st_n_2q:3,st_n_3q_plus:2});
  await page.getByRole('button',{name:'Inspect features here',exact:true}).click();
  const counts = page.getByRole('region',{name:'Gate counts at this checkpoint'});
  await expect(counts.locator('dl').first().locator('dd')).toHaveText(['9','4','Unavailable','Unavailable']);
  await expect(counts).toContainText('Extractors disagree: source total 9; structural total 11.');
});

test('failed structural extraction does not present zero-filled counters as measured gates', async ({page}) => {
  await openFixture(page,{st_ok:0,st_n_gates:0,st_n_1q:0,st_n_2q:0,st_n_3q_plus:0});
  await page.getByRole('button',{name:'Inspect features here',exact:true}).click();
  const counts = page.getByRole('region',{name:'Gate counts at this checkpoint'});
  await expect(counts).toContainText('Gate counts are unavailable in this extraction.');
  await expect(counts.locator('dl')).toHaveCount(0);
});
