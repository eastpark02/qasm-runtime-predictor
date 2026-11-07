import {test, expect, type Page} from '@playwright/test';

async function openScrollableCircuit(page: Page) {
  const computations: string[] = [];
  page.on('request', request => {
    if (/\/(features|predict|state|explain|stepper|submit)$/.test(new URL(request.url()).pathname)) computations.push(request.url());
  });
  await page.route('**/api/examples/partial', route => route.fulfill({json:{id:'gesture-fixture',name:'Scrollable circuit',origin:'synthetic',num_qubits:64,num_clbits:0,total_steps:512,total_instructions:512,state_boundary:null,expanded_state_boundary:null,parse_seconds:.01,inspection_mode:'native_expanded',inspection_backend:'app_owned_cpp',qubit_labels:[]}}));
  await page.route('**/api/circuits/gesture-fixture/instructions?*', route => {
    const url = new URL(route.request().url()), offset = Number(url.searchParams.get('offset')), limit = Number(url.searchParams.get('limit'));
    return route.fulfill({json:{offset,total:512,steps:Array.from({length:Math.min(limit,512-offset)},(_,i) => {
      const step = offset+i+1;
      return {step,source_step:step,name:'h',qubits:[step%64],clbits:[],parameters:[],source:`h q[${step%64}];`,primitive_source:`h q[${step%64}];`,line:step+4,instances:1,body:null,state_boundary:false};
    })}});
  });
  await page.route('**/api/circuits/gesture-fixture/overview?*', route => route.fulfill({json:{buckets:[{start:1,end:512,count:512}]}}));
  await page.goto('/');
  await page.getByRole('button',{name:'Open example',exact:true}).click();
  await expect(page.locator('.circuit-viewport .gate').first()).toBeVisible();
  await page.locator('.circuit-viewport').scrollIntoViewIfNeeded();
  return computations;
}

async function hoverCircuit(page: Page) {
  const rect = (await page.locator('.circuit-viewport').boundingBox())!;
  await page.mouse.move(rect.x+350,rect.y+200);
  return rect;
}

test('trackpad scroll pans qubits and gates; ctrl-wheel pinch alone changes zoom', async ({page}) => {
  const computations = await openScrollableCircuit(page);
  await hoverCircuit(page);
  const scrollY = await page.evaluate(() => window.scrollY);
  await page.mouse.wheel(0,320);
  await expect(page.getByLabel('First visible qubit')).toHaveValue('5');
  await expect(page.locator('.zoom-readout')).toHaveText('100%');
  const first = await page.locator('.circuit-viewport .gate').first().getAttribute('aria-label');
  await page.mouse.wheel(600,0);
  await expect(page.locator('.circuit-viewport .gate').first()).not.toHaveAttribute('aria-label',first!);
  await expect(page.getByLabel('First visible qubit')).toHaveValue('5');
  await expect(page.locator('.zoom-readout')).toHaveText('100%');
  expect(await page.evaluate(() => window.scrollY)).toBe(scrollY);
  await page.keyboard.down('Control');
  await page.mouse.wheel(0,-160);
  await page.keyboard.up('Control');
  await expect(page.locator('.zoom-readout')).toHaveText('127%');
  await expect(page.getByLabel('Jump to step')).toHaveValue('0');
  expect(computations).toEqual([]);
});

test('wheel units, shift-scroll and camera limits stay bounded without scrolling the page', async ({page}) => {
  const computations = await openScrollableCircuit(page);
  const viewport = page.locator('.circuit-viewport');
  await viewport.dispatchEvent('wheel',{deltaY:4,deltaMode:1,bubbles:true,cancelable:true});
  await expect(page.getByLabel('First visible qubit')).toHaveValue('1');
  await viewport.dispatchEvent('wheel',{deltaY:1,deltaMode:2,bubbles:true,cancelable:true});
  await expect(page.getByLabel('First visible qubit')).toHaveValue('7');
  await viewport.dispatchEvent('wheel',{deltaY:600,shiftKey:true,bubbles:true,cancelable:true});
  await expect(page.locator('.circuit-viewport .step-number').first()).toHaveText('6');
  await expect(page.getByLabel('First visible qubit')).toHaveValue('7');
  await hoverCircuit(page);
  const scrollY = await page.evaluate(() => window.scrollY);
  await page.mouse.wheel(1_000_000,1_000_000);
  await expect(page.getByLabel('First visible qubit')).toHaveValue('57');
  await expect(page.locator('.circuit-viewport .gate').last()).toHaveAttribute('aria-label',/^Step 512:/);
  await page.mouse.wheel(-1_000_000,-1_000_000);
  await expect(page.getByLabel('First visible qubit')).toHaveValue('0');
  await expect(page.locator('.circuit-viewport .step-number').first()).toHaveText('1');
  expect(await page.evaluate(() => window.scrollY)).toBe(scrollY);
  await expect(page.locator('.zoom-readout')).toHaveText('100%');
  await page.keyboard.down('Control');
  await page.mouse.wheel(0,-10_000);
  await expect(page.locator('.zoom-readout')).toHaveText('300%');
  await page.mouse.wheel(0,10_000);
  await page.keyboard.up('Control');
  await expect(page.locator('.zoom-readout')).toHaveText('40%');
  expect(computations).toEqual([]);
});

test('two-finger touch translates and pinches while drag, keyboard selection and fullscreen still work', async ({page}) => {
  const computations = await openScrollableCircuit(page);
  const rect = await hoverCircuit(page);
  await page.mouse.wheel(300,300);
  await expect(page.getByLabel('First visible qubit')).toHaveValue('4');
  const gate = page.getByRole('button',{name:'Step 3: H on q3',exact:true}).locator('.step-number');
  const x = Number(await gate.getAttribute('x'));
  const session = await page.context().newCDPSession(page);
  const touch = (type: string, points: {id:number;x:number;y:number}[]) => session.send('Input.dispatchTouchEvent',{type,touchPoints:points.map(p=>({...p,x:rect.x+p.x,y:rect.y+p.y}))});
  await touch('touchStart',[{id:1,x:350,y:250},{id:2,x:550,y:250}]);
  await touch('touchMove',[{id:1,x:290,y:150},{id:2,x:490,y:150}]);
  await touch('touchEnd',[]);
  await expect(page.locator('.zoom-readout')).toHaveText('100%');
  await expect(page.getByLabel('First visible qubit')).toHaveValue('6');
  expect(Number(await gate.getAttribute('x'))).toBeCloseTo(x-60,4);
  await expect(page.getByLabel('Jump to step')).toHaveValue('0');
  await touch('touchStart',[{id:1,x:290,y:200},{id:2,x:490,y:200}]);
  await touch('touchMove',[{id:1,x:240,y:200},{id:2,x:540,y:200}]);
  await touch('touchEnd',[]);
  await expect(page.locator('.zoom-readout')).toHaveText('150%');
  await expect(page.getByLabel('Jump to step')).toHaveValue('0');
  await page.mouse.move(rect.x+450,rect.y+140);
  await page.mouse.down();
  await page.mouse.move(rect.x+250,rect.y+140,{steps:8});
  await page.mouse.up();
  await expect(page.getByLabel('Jump to step')).toHaveValue('0');
  const selectable = page.locator('.circuit-viewport .gate').nth(2);
  const step = (await selectable.getAttribute('aria-label'))!.match(/^Step (\d+):/)![1];
  await selectable.press('Enter');
  await expect(page.getByLabel('Jump to step')).toHaveValue(step);
  await page.getByRole('button',{name:'View circuit fullscreen',exact:true}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await hoverCircuit(page);
  await page.mouse.wheel(0,128);
  await expect(page.locator('.zoom-readout')).toHaveText('150%');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button',{name:'View circuit fullscreen',exact:true})).toBeFocused();
  expect(computations).toEqual([]);
});
