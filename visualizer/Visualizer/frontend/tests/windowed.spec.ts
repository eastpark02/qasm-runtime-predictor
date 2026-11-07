import {test, expect} from '@playwright/test';

// Synthetic HTTP fixtures only: proves the windowed-inspection UI contract
// without the native inspector or the backend windowed mode.
test('windowed inspection jumps between statement windows and keeps stale/cached labels', async ({page}) => {
  const TOTAL = 250000, SIZE = 100000;
  const windows = [0, 1, 2].map(index => ({index, start_step: index * SIZE + 1, end_step: Math.min(TOTAL, (index + 1) * SIZE), byte_offset: 120 + index * 1400000}));
  const stepRequests: {offset: number; limit: number}[] = [], computations: {action: string; checkpoint: Record<string, unknown>}[] = [], unexpected: string[] = [];
  const predicted = new Set<string>();
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname;
    if (path === '/api/health') return route.fulfill({json:{model:{available:true,explanation_available:true,thresholds:[256],fingerprint:'synthetic'},access:{approved_files:1},limits:{upload_bytes:256*1024**2,interactive_bytes:32*1024**2,inspection_operations:100000,inspection_qubits:2048,large_feature_timeout_seconds:180,large_feature_memory_bytes:3*1024**3,window_size:SIZE,windows_cached:0}}});
    if (path === '/api/examples') return route.fulfill({json:[]});
    if (path === '/api/importance') return route.fulfill({json:{available:false}});
    if (path === '/api/qbraid/profiles') return route.fulfill({json:{profiles:[]}});
    if (path === '/api/qbraid/jobs') return route.fulfill({json:{jobs:[]}});
    if (path === '/api/circuits') return route.fulfill({json:{id:'windowed-fixture',name:'windowed.qasm',origin:'synthetic',num_qubits:4,num_clbits:0,total_steps:TOTAL,total_instructions:null,state_boundary:TOTAL,qubit_labels:['q[0]','q[1]','q[2]','q[3]'],parse_seconds:.6,inspection_mode:'windowed',inspection_backend:'app_owned_cpp_windowed',window_size:SIZE,windows,fallback_reason:'ValueError: Source inspection limit at byte 1400000\n',warnings:['Windowed native inspection; only source-step navigation.','Cloud execution is unavailable in windowed mode.','Prefix runtime estimates are experimental.','No model prediction or state calculation has been run.']}});
    const action = path.split('/').at(-1)!;
    if (action === 'steps') {
      const offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
      stepRequests.push({offset, limit});
      const steps = Array.from({length: Math.max(0, Math.min(TOTAL, offset + limit) - offset)}, (_, i) => {const step = offset + i + 1; return {step, name:'h', qubits:[step % 4], clbits:[], parameters:[], angle_expression:'', source:`h q[${step % 4}];`, source_step:step, line:step + 3, instances:1, body:null, state_boundary:true};});
      return route.fulfill({json:{steps}});
    }
    if (action === 'overview') return route.fulfill({json:{buckets:Array.from({length:200}, (_, i) => ({start:i * TOTAL / 200, end:(i + 1) * TOTAL / 200, count:TOTAL / 200}))}});
    if (['features','predict','explain'].includes(action)) {
      const checkpoint = route.request().postDataJSON();
      computations.push({action, checkpoint});
      const common = {...checkpoint, features:{st_ok:1,n_gates:checkpoint.step}, derived_features:{}, prefix_hash:`prefix-${checkpoint.step}`, prefix_kind:'original_source_prefix'};
      if (action === 'explain') return route.fulfill({json:{...common,contributions:[{feature:'n_gates',value:checkpoint.step,contribution:.5}],contribution_count:1,contribution_sum:.5,units:'log10(seconds)',base_value:0,output:.5,note:'Synthetic explanation'}});
      const key = `${checkpoint.step}:${checkpoint.threshold}`, cached = action === 'predict' && predicted.has(key);
      if (action === 'predict') predicted.add(key);
      return route.fulfill({json:{...common,predicted_seconds:checkpoint.step / 1000,inference_seconds:.01,model_fingerprint:'synthetic',cached}});
    }
    unexpected.push(path);
    return route.fulfill({status:422,json:{detail:'Unavailable in windowed fixture'}});
  });
  await page.goto('/');
  await page.locator('input[type=file]').setInputFiles({name:'windowed.qasm',mimeType:'text/plain',buffer:Buffer.from('OPENQASM 2.0;\n')});
  await expect(page.getByRole('heading',{name:'windowed.qasm',exact:true})).toBeVisible();
  await expect(page.locator('.inspection-badge')).toHaveText('C++ windowed');
  await expect(page.locator('.circuit-stats')).toContainText('windowed native inspection · 250,000 statements');
  await expect(page.locator('.inspection-fallback')).toHaveText('Whole-file inspection stopped at its 100,000-statement limit (ValueError: Source inspection limit at byte 1400000); statements are inspected on demand in windows of 100,000.');
  await expect(page.getByRole('list',{name:'Windowed inspection limits'}).locator('li')).toHaveText(['Windowed native inspection; only source-step navigation.','Cloud execution is unavailable in windowed mode.']);
  const picker = page.getByLabel('Statement window',{exact:true});
  await expect(picker.locator('option')).toHaveText(['Statements 1–100,000','Statements 100,001–200,000','Statements 200,001–250,000']);
  await expect(picker).toHaveValue('0');
  await expect.poll(() => stepRequests.some(r => r.offset === 0 && r.limit === 200)).toBe(true);

  // Keyboard: focus the control and move to the second window.
  await picker.focus();
  const beforeJump = stepRequests.length;
  await picker.selectOption('1');
  await expect(page.getByLabel('Jump to step',{exact:true})).toHaveValue('100001');
  await expect.poll(() => stepRequests.some(r => r.offset === 100000 && r.limit === 200)).toBe(true);
  await expect(page.getByRole('button',{name:'Step 100001: H on q1',exact:true})).toBeVisible();
  // A jump fetches only the selected window: no padding page or nearby trace
  // reaches back into statements 1–100,000.
  expect(stepRequests.slice(beforeJump).every(r => r.offset >= 100000 && r.offset + r.limit <= 200000)).toBe(true);
  await expect(page.locator('.instruction-detail')).toContainText('SOURCE STEP 100001');

  await page.getByRole('button',{name:'Predict runtime here',exact:true}).click();
  await expect(page.getByText('Evaluated · step 100001 · threshold 256',{exact:true})).toBeVisible();
  await expect(page.locator('.shap-row')).toHaveCount(1);

  const beforeLast = stepRequests.length;
  await picker.selectOption('2');
  await expect(page.getByLabel('Jump to step',{exact:true})).toHaveValue('200001');
  await expect.poll(() => stepRequests.some(r => r.offset === 200000 && r.limit === 200)).toBe(true);
  expect(stepRequests.slice(beforeLast).every(r => r.offset >= 200000)).toBe(true);
  await expect(page.getByText('Run manually at this checkpoint',{exact:true})).toBeVisible();
  await expect(page.getByText(/Step 100001 · source · threshold 256 · previous checkpoint/)).toBeVisible();

  // The step input moves the highlighted window too.
  await page.getByLabel('Jump to step',{exact:true}).fill('100001');
  await expect(picker).toHaveValue('1');
  await page.getByRole('button',{name:'Predict runtime here',exact:true}).click();
  await expect(page.getByText('Cached · step 100001 · threshold 256',{exact:true})).toBeVisible();

  expect(computations.filter(c => c.action === 'predict').map(c => c.checkpoint)).toEqual([{step:100001,step_kind:'source',threshold:256},{step:100001,step_kind:'source',threshold:256}]);
  expect(stepRequests.every(r => r.offset >= 0 && r.offset < TOTAL && r.limit <= 200)).toBe(true);
  await page.setViewportSize({width:390, height:900});
  await expect(picker).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  expect(unexpected).toEqual([]);
});
