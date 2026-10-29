// Theme behavior regression: synthetic API, temporary loopback HTTP, no real service or data.
const {chromium}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.env.PHOTO_WEBUI_HTML||path.resolve(__dirname,'../../photo_gateway/webui/index.html'),'utf8');
const key='photo_gateway.theme.v1',held=[];
async function settle(page){await page.evaluate(async()=>{await new Promise(requestAnimationFrame);await Promise.all(document.getAnimations().map(animation=>animation.finished.catch(()=>{})));});}
const photo={id:1,current_filename:'theme-sample.jpg',source_device:'demo',date_taken:'2026-10-29 12:00:00',file_size:100,mime_type:'image/jpeg',thumbnail_url:'/api/v1/photos/1/thumbnail',file_url:'/api/v1/photos/1/file',exif:{ImageWidth:600,ImageLength:400}};
const server=http.createServer((req,res)=>{
 const u=new URL(req.url,'http://localhost'),p=u.pathname;
 if(p==='/'){
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});
  if(u.searchParams.has('headOnly')){const end=html.indexOf('</head>')+7;res.write(html.slice(0,end));held.push(()=>res.end(html.slice(end)));}
  else res.end(html);return;
 }
 if(/\/(thumbnail|file)$/.test(p)){res.writeHead(200,{'Content-Type':'image/svg+xml'});res.end('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><rect width="600" height="400" fill="#c8d5c8"/></svg>');return;}
 let data;
 if(p.endsWith('/devices'))data={devices:[{id:'demo',name:'演示设备',enabled:true}]};
 else if(p.endsWith('/system/status'))data={storage:{total_bytes:1000,used_bytes:500,free_bytes:500,used_percent:50,level:'normal'},counts:{assets:1,pending_upload:0,processing:0},worker:{status:'running'},allowed_extensions:['jpg','png']};
 else if(p.endsWith('/upload-sessions/recent'))data={sessions:[{id:1,source_device:'demo',status:'COMPLETED',total_files:1}]};
 else if(p==='/api/v1/photos')data={photos:[photo],total:1};
 else if(p==='/api/v1/photos/1')data={photo};
 else if(p.endsWith('/sync/overview'))data={enabled:true,months:[],batches:[]};
 else if(p.endsWith('/sync/gates'))data={enabled:true,nas_alive:true,nas_ready:true,v1_idle:true,all_ok:true};
 else {res.writeHead(404);res.end();return;}
 res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({success:true,data}));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const url='http://127.0.0.1:'+server.address().port;
 let browser;
 const evidence=process.env.PHOTO_WEBUI_EVIDENCE_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'photo-theme-evidence-'));
 fs.mkdirSync(evidence,{recursive:true});let checks=0;const errors=[];
 try{
  browser=await chromium.launch({headless:true,...(process.env.PHOTO_WEBUI_CHROMIUM?{executablePath:process.env.PHOTO_WEBUI_CHROMIUM}:{})});
  const context=await browser.newContext({viewport:{width:1487,height:960}}),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto(url);await page.waitForSelector('#recentImports .recent-status');
  assert.equal(await page.getByLabel('配色',{exact:true}).count(),1,'visible labelled native theme selector is required');checks++;
  assert.equal(await page.locator('#themeSelect').evaluate(el=>el.tagName),'SELECT');
  assert.deepEqual(await page.locator('#themeSelect option').evaluateAll(opts=>opts.map(o=>[o.value,o.textContent])),[['pine','松林'],['blue','海蓝'],['plum','紫藤']]);
  assert.equal(await page.locator('#themeSelect').getAttribute('aria-describedby'),'themeStatus');
  assert.equal(await page.locator('#themeStatus').getAttribute('role'),'status');assert.equal(await page.locator('#themeStatus').getAttribute('aria-live'),'polite');
  assert.equal(await page.locator('#themeSelect').inputValue(),'pine');assert.match(await page.locator('#themeStatus').innerText(),/仅保存在此浏览器/);checks++;
  const accent=async expected=>{await page.waitForFunction(expected=>getComputedStyle(document.getElementById('bPre')).backgroundColor===expected,expected);return page.locator('#bPre').evaluate(el=>getComputedStyle(el).backgroundColor);};
  assert.equal(await accent('rgb(54, 91, 74)'),'rgb(54, 91, 74)');
  const preserved=()=>page.evaluate(()=>({body:getComputedStyle(document.body).backgroundImage,txt:getComputedStyle(document.body).color,logo:document.querySelector('.brandmark').innerHTML,bad:getComputedStyle(document.documentElement).getPropertyValue('--bad')}));
  const base=await preserved();
  await page.evaluate(()=>{localStorage.setItem('unrelated.test','keep');});
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwS8AAAAASUVORK5CYII=','base64');
  await page.locator('#files').setInputFiles({name:'selected.png',mimeType:'image/png',buffer:png});
  const requests=[];page.on('request',req=>{if(/^https?:/.test(req.url()))requests.push(req.url());});
  await page.locator('#themeSelect').selectOption('blue');
  assert.equal(await accent('rgb(49, 95, 134)'),'rgb(49, 95, 134)');assert.equal(await page.locator('#pickList li').count(),1);assert.equal(await page.locator('#v-upload').isVisible(),true);assert.deepEqual(await preserved(),base);checks++;
  assert.equal(await page.locator('.recent-status').evaluate(el=>getComputedStyle(el).color),'rgb(54, 91, 74)');checks++;
  assert.deepEqual(requests,[],'theme switch must not request anything');
  assert.equal(await page.evaluate(k=>localStorage.getItem(k),key),'blue');assert.equal(await page.evaluate(()=>localStorage.getItem('unrelated.test')),'keep');checks++;
  await page.reload();assert.equal(await page.locator('#themeSelect').inputValue(),'blue');assert.equal(await accent('rgb(49, 95, 134)'),'rgb(49, 95, 134)');checks++;
  await page.locator('nav [data-v="photos"]').click();await page.waitForSelector('#photoInfo .info-list');await page.locator('#photoInfo summary').click();
  const info=await page.locator('#photoInfo').innerText();
  await page.locator('#themeSelect').selectOption('plum');
  assert.equal(await page.locator('#v-photos').isVisible(),true);assert.equal(await page.locator('#photoInfo').innerText(),info);assert.equal(await page.locator('#photoInfo details').getAttribute('open'),'');assert.equal(await page.locator('.photo-tile.selected').count(),1);checks++;
  assert.equal(await accent('rgb(111, 78, 124)'),'rgb(111, 78, 124)');await settle(page);await page.screenshot({path:path.join(evidence,'plum-desktop.png'),fullPage:true});
  // Headless macOS Chromium does not operate native popup choices with arrow keys.
  // Prove keyboard reachability/focus, and exercise selection through its native select API.
  await page.locator('nav [data-v="log"]').focus();await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'themeSelect');
  assert.equal(await page.locator('#themeSelect').evaluate(el=>getComputedStyle(el).outlineWidth),'2px');
  await page.locator('#themeSelect').selectOption('blue');checks++;
  await page.locator('nav [data-v="sync"]').click();await page.waitForSelector('.is-ready .sync-gate-icon');
  const gates=()=>page.locator('.sync-gate-icon').evaluateAll(els=>els.map(el=>({color:getComputedStyle(el).color,background:getComputedStyle(el).backgroundColor})));
  const blueGates=await gates();assert.equal(blueGates[0].background,'rgb(54, 91, 74)');
  await page.locator('#themeSelect').selectOption('plum');assert.deepEqual(await gates(),blueGates);checks++;
  await page.locator('nav [data-v="system"]').click();await page.waitForSelector('.system-meter i');
  const statuses=()=>page.locator('.system-meter i,.live-status').evaluateAll(els=>els.map(el=>({color:getComputedStyle(el).color,background:getComputedStyle(el).backgroundColor})));
  const plumStatuses=await statuses();assert.equal(plumStatuses[0].background,'rgb(54, 91, 74)');assert.equal(plumStatuses[1].color,'rgb(54, 91, 74)');
  await page.locator('#themeSelect').selectOption('pine');assert.deepEqual(await statuses(),plumStatuses);assert.equal(await accent('rgb(54, 91, 74)'),'rgb(54, 91, 74)');checks++;
  for(const width of [375,390,768,1024,1487]){
   await page.setViewportSize({width,height:900});
   for(const theme of ['pine','blue','plum']){
    await page.locator('#themeSelect').selectOption(theme);
    for(const view of ['upload','photos','sync','system','log']){
     await page.locator(`nav [data-v="${view}"]`).click();
     assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`${width}/${theme}/${view} overflows`);
    }
    const boxes=await page.locator('header h1,header nav,.theme-control').evaluateAll(els=>els.map(el=>{const b=el.getBoundingClientRect();return {x:b.x,y:b.y,right:b.right,bottom:b.bottom};}));
    assert.ok(boxes.every(b=>b.x>=0&&b.right<=width),JSON.stringify(boxes));
    assert.ok(boxes[0].right<=boxes[2].x||boxes[2].right<=boxes[0].x||boxes[0].bottom<=boxes[2].y||boxes[2].bottom<=boxes[0].y,'brand/theme overlap');
    if(width<=390){assert.ok(boxes[1].y>=Math.max(boxes[0].bottom,boxes[2].bottom));await settle(page);await page.screenshot({path:path.join(evidence,`${theme}-${width}.png`),fullPage:true});}
    checks++;
   }
  }
  // Hold the response after </head>: preference must be restored before body is received.
  await page.evaluate(k=>localStorage.setItem(k,'blue'),key);
  const loading=page.goto(url+'/?headOnly', {waitUntil:'load'});
  await page.waitForFunction(()=>document.documentElement.dataset.theme==='blue'&&!document.body);
  checks++;held.shift()();await loading;await page.goto(url);
  for(const invalid of ['unknown','__proto__','constructor','toString','BLUE','', ' blue ']){
   await page.evaluate(([k,v])=>localStorage.setItem(k,v),[key,invalid]);await page.reload();
   assert.equal(await page.locator('#themeSelect').inputValue(),'pine',invalid);assert.equal(await accent('rgb(54, 91, 74)'),'rgb(54, 91, 74)');checks++;
  }
  const fresh=await browser.newContext(),freshPage=await fresh.newPage();await freshPage.goto(url);assert.equal(await freshPage.locator('#themeSelect').inputValue(),'pine');await fresh.close();checks++;
  for(const failure of ['read','write','access']){
   const bad=await browser.newContext();
   await bad.addInitScript(({failure,key})=>{
    if(failure==='access')Object.defineProperty(window,'localStorage',{get(){throw new DOMException('blocked','SecurityError');}});
    else{
     if(failure==='write')localStorage.setItem(key,'blue');
     const method=failure==='read'?'getItem':'setItem',original=Storage.prototype[method];
     Storage.prototype[method]=function(k,...args){if(k===key)throw new DOMException('blocked','SecurityError');return original.call(this,k,...args);};
    }
   },{failure,key});
   const badPage=await bad.newPage();badPage.on('pageerror',e=>errors.push(e.message));await badPage.goto(url);await badPage.waitForSelector('#device option',{state:'attached'});
   assert.equal(await badPage.locator('#themeSelect').inputValue(),failure==='write'?'blue':'pine');
   if(failure!=='write')assert.match(await badPage.locator('#themeStatus').innerText(),/无法读取配色偏好，本页使用默认配色/);
   await badPage.locator('#themeSelect').selectOption('plum');await badPage.waitForFunction(()=>getComputedStyle(document.getElementById('bPre')).backgroundColor==='rgb(111, 78, 124)');assert.equal(await badPage.locator('#bPre').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(111, 78, 124)');
   if(failure!=='read')assert.match(await badPage.locator('#themeStatus').innerText(),/配色已切换，但无法保存；刷新后可能恢复之前的配色/);
   else assert.match(await badPage.locator('#themeStatus').innerText(),/仅保存在此浏览器/);
   if(failure==='write')assert.equal(await badPage.evaluate(k=>localStorage.getItem(k),key),'blue','failed write must preserve previous preference');
   for(const width of [375,390,768,1024]){
    await badPage.setViewportSize({width,height:900});
    assert.ok(await badPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),failure+'/'+width);
    const status=await badPage.locator('#themeStatus').boundingBox(),nav=await badPage.locator('header nav').boundingBox();
    assert.ok(status.x>=0&&status.x+status.width<=width&&status.y>=nav.y+nav.height,'status overlaps nav');
    assert.equal(await badPage.locator('#themeStatus').isVisible(),true);
    if(width===375||width===1024){await settle(badPage);await badPage.screenshot({path:path.join(evidence,`storage-${failure}-${width}.png`),fullPage:true});}
   }
   await badPage.locator('nav [data-v="photos"]').click();await badPage.waitForSelector('#phGrid a');
   await settle(badPage);await badPage.screenshot({path:path.join(evidence,`storage-${failure}-feedback.png`),fullPage:true});
   await badPage.reload();assert.equal(await badPage.locator('#themeSelect').inputValue(),failure==='write'?'blue':'pine');checks++;
   await settle(badPage);await badPage.screenshot({path:path.join(evidence,`storage-${failure}.png`),fullPage:true});await bad.close();
  }
  assert.deepEqual(errors,[]);console.log(JSON.stringify({status:'PASS',checks,errors:errors.length,runtime:{node:process.version,platform:process.platform,arch:process.arch,browser:browser.version()},notRun:['native popup arrow selection in headed browsers'],evidence},null,2));
 }finally{for(const release of held.splice(0))release();if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
