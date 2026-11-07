import {test, expect} from '@playwright/test';

test('large upload offers only the exact Full circuit checkpoint and manual prediction with SHAP', async ({page}) => {
  const computations: {action: string; checkpoint: Record<string, unknown>}[] = [];
  const unexpected: string[] = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/health') return route.fulfill({json:{model:{available:true,explanation_available:true,thresholds:[256],fingerprint:'synthetic'},access:{approved_files:1},limits:{upload_bytes:256*1024**2,interactive_bytes:32*1024**2,inspection_operations:100000,inspection_qubits:2048,large_feature_timeout_seconds:180,large_feature_memory_bytes:3*1024**3}}});
    if (path === '/api/examples') return route.fulfill({json:[]});
    if (path === '/api/importance') return route.fulfill({json:{available:false}});
    if (path === '/api/qbraid/profiles') return route.fulfill({json:{profiles:[]}});
    if (path === '/api/qbraid/jobs') return route.fulfill({json:{jobs:[]}});
    if (path === '/api/circuits') return route.fulfill({json:{id:'large-approved-fixture',name:'large.qasm',origin:'synthetic',num_qubits:null,num_clbits:null,total_steps:1,total_instructions:null,state_boundary:null,qubit_labels:[],parse_seconds:0,inspection_mode:'full_source_only',inspection_backend:'not_run_large_file',full_source_only:true,source_bytes:218359802}});
    const action = path.split('/').at(-1)!;
    if (['features','predict','explain'].includes(action)) {
      const checkpoint = route.request().postDataJSON();
      computations.push({action,checkpoint});
      const common = {...checkpoint,features:{st_ok:1,n_gates:3211172},derived_features:{},input_hash:'exact-file-sha',prefix_hash:'exact-file-sha',prefix_kind:'original_full_source',checkpoint_label:'Full circuit'};
      if (action === 'explain') return route.fulfill({json:{...common,contributions:[{feature:'n_gates',value:3211172,contribution:.5}],contribution_count:1,contribution_sum:.5,units:'log10(seconds)',base_value:0,output:.5,note:'Synthetic explanation'}});
      return route.fulfill({json:{...common,predicted_seconds:3,inference_seconds:.01,model_fingerprint:'synthetic',cached:false}});
    }
    unexpected.push(path);
    return route.fulfill({status:422,json:{detail:'Unavailable in full-file mode'}});
  });
  await page.goto('/');
  await expect(page.getByText(/Uploads up to 256 MiB/)).toBeVisible();
  await page.locator('input[type=file]').setInputFiles({name:'large.qasm',mimeType:'text/plain',buffer:Buffer.from('OPENQASM 2.0;\n')});
  await expect(page.getByRole('heading',{name:'large.qasm',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Predict the full circuit',exact:true})).toBeVisible();
  await expect(page.getByText(/Syntax|Circuit syntax/)).toContainText('have not been validated');
  await expect(page.getByText('3 GiB memory budget.',{exact:false})).toBeVisible();
  await expect(page.getByLabel('Next step',{exact:true})).toHaveCount(0);
  await expect(page.locator('.circuit-viewport')).toHaveCount(0);
  await expect(page.getByRole('region',{name:'qBraid connector',exact:true})).toBeVisible();
  await expect(page.locator('.quantum-checkpoint')).toContainText('Cloud execution disabled because syntax and safe measurement mapping have not been inspected');
  await expect(page.getByRole('button',{name:'Get cost estimate',exact:true})).toBeDisabled();
  await expect(page.locator('.quantum-checkpoint')).not.toContainText('0 qubits');
  expect(computations).toEqual([]);
  await page.getByRole('button',{name:'Predict runtime here',exact:true}).click();
  await expect(page.locator('.shap-row')).toHaveCount(1);
  expect(computations.map(row=>row.action)).toEqual(['predict','explain']);
  for (const row of computations) expect(row.checkpoint).toEqual({step:1,step_kind:'source',threshold:256});
  expect(unexpected).toEqual([]);
});
