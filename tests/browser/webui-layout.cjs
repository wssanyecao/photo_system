// Isolated browser regression: in-memory API fixtures, synthetic images, loopback HTTP only.
// Requires Node.js + Playwright; never connects to a running Photo Gateway.
const {chromium}=require('playwright');
const http=require('http'),fs=require('fs'),assert=require('assert/strict');
const path=require('path'),os=require('os');
const root=path.resolve(__dirname,'../..');
const htmlPath=process.env.PHOTO_WEBUI_HTML||path.join(root,'photo_gateway/webui/index.html');
const dimensions=[[600,400],[400,600],[800,600],[800,450]];
const photos=Array.from({length:60},(_,i)=>({id:i+1,current_filename:`IMG_${i+1}.jpg`,date_taken:'2026-10-15 12:30:00',file_size:5000000,mime_type:'image/jpeg',source_device:'demo',thumbnail_url:`/api/v1/photos/${i+1}/thumbnail`,file_url:`/api/v1/photos/${i+1}/file`,archive_path:`Photos/2026/202610/IMG_${i+1}.jpg`,exif:{ImageWidth:dimensions[i%4][0],ImageLength:dimensions[i%4][1]},sha256:'abc'}));
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');let data;
 if(url.pathname==='/'){res.setHeader('Content-Type','text/html; charset=utf-8');res.end(fs.readFileSync(htmlPath));return;}
 if(/\/(thumbnail|file)$/.test(url.pathname)){const id=Number(url.pathname.split('/')[4]),[w,h]=dimensions[(id-1)%4];res.setHeader('Content-Type','image/svg+xml');res.end(`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="100%" height="100%" fill="#b8c8ba"/><circle cx="${w/2}" cy="${h/2}" r="80" fill="#eff1e9"/></svg>`);return;}
 if(url.pathname.endsWith('/devices'))data={devices:[{id:'demo',name:'演示设备',enabled:true}]};
 else if(url.pathname.endsWith('/system/status'))data={storage:{total_bytes:107374182400,used_bytes:64424509440,free_bytes:41875931136,used_percent:60,level:'normal',thresholds:{warning_percent:80,critical_percent:90,stop_percent:95}},counts:{assets:60,assets_missing:0,pending_upload:0,processing:0,sessions_total:12},worker:{status:'running',mode:'threaded-v1-v2'},allowed_extensions:['jpg','png','heic']};
 else if(url.pathname.endsWith('/upload-sessions/recent'))data={sessions:Array.from({length:12},(_,i)=>({id:i,source_device:'demo',created_at:'2026-10-15 12:00:00',total_files:24,uploaded_files:24,duplicate_files:0,failed_files:0,status:'COMPLETED'}))};
 else if(url.pathname==='/api/v1/photos')data={photos:photos.slice(Number(url.searchParams.get('offset')||0),Number(url.searchParams.get('offset')||0)+Number(url.searchParams.get('limit')||48)),total:photos.length};
 else if(/\/photos\/\d+$/.test(url.pathname))data={photo:photos[Number(url.pathname.split('/').pop())-1]};
 else if(url.pathname.endsWith('/sync/overview'))data={enabled:false,months:[],batches:[]};
 else {res.writeHead(404);res.end();return;}
 res.setHeader('Content-Type','application/json');res.end(JSON.stringify({success:true,data}));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const port=server.address().port;
 const browser=await chromium.launch({headless:true,...(process.env.PHOTO_WEBUI_CHROMIUM?{executablePath:process.env.PHOTO_WEBUI_CHROMIUM}:{})});const page=await browser.newPage({viewport:{width:1487,height:960}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let uploadScenario="success", uploadedExifTimes=[];
 await page.route(/\/api\/v1\/upload-(sessions|items)(\/|$)/,async route=>{
  const req=route.request(),u=new URL(req.url());if(u.pathname.endsWith('/recent'))return route.continue();
  let data,status=200;
  if(req.method()==='POST'&&u.pathname.endsWith('/upload-sessions'))data={session:{id:'demo-session'}};
  else if(u.pathname.endsWith('/precheck'))data={items:[{item_id:1,precheck_status:['duplicate','cancel-failure'].includes(uploadScenario)?'POSSIBLE_DUPLICATE':'NO_MATCH'}]};
  else if(u.pathname.endsWith('/confirmation')){
   if(uploadScenario==='cancel-failure'&&JSON.parse(req.postData()).confirmation===2)status=503;
   data={};
  }
  else if(u.pathname.endsWith('/deleted-reviews'))data={items:[]};
  else if(u.pathname.endsWith('/file')){
   const body=req.postData()||'';
   const match=body.match(/name="exif_time"\r?\n\r?\n([^\r\n]+)/);
   if(match)uploadedExifTimes.push(match[1]);
   if(uploadScenario==='failure')status=503;data={};
  }
  else data={session:{id:'demo-session',status:'COMPLETED',total_files:1,uploaded_files:uploadScenario==='duplicate'?0:1,duplicate_files:0,skipped_files:uploadScenario==='duplicate'?1:0,failed_files:0}};
  await route.fulfill({status,contentType:'application/json',body:JSON.stringify(status===200?{success:true,data}:{success:false,error:{code:'TEST_UNAVAILABLE',message:'合成网络故障'}})});
 });
 const evidence=process.env.PHOTO_WEBUI_EVIDENCE_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'photo-ui-evidence-'));fs.mkdirSync(evidence,{recursive:true});
 try{
 await page.goto(`http://127.0.0.1:${port}`);await page.waitForSelector('#recentImports tr:nth-child(12)');
 const aligned=async()=>{const a=await page.locator('.upload-main').boundingBox(),b=await page.locator('.upload-flow').boundingBox();assert.ok(Math.abs(a.y-b.y)<1,JSON.stringify({a,b}));assert.ok(Math.abs(a.height-b.height)<1,JSON.stringify({a,b}));return {leftHeight:a.height,rightHeight:b.height};};
 const empty=await aligned();await page.screenshot({path:evidence+'/upload-empty.png',fullPage:true});
 const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwS8AAAAASUVORK5CYII=','base64');
 await page.locator('#files').setInputFiles(Array.from({length:24},(_,i)=>({name:`demo-${i}.png`,mimeType:'image/png',buffer:png})));
 await page.waitForSelector('#pickList li:nth-child(24)');const selected=await aligned();
 const picker=await page.locator('#picker').boundingBox(),icon=await page.locator('#picker .pk-ico').boundingBox();assert.ok(picker.height>=icon.height+24);assert.equal(await page.locator('#bPre svg').count(),1);
 // Append files through the real file input, without uploading any content.
 await page.locator('#files').setInputFiles({name:'additional.png',mimeType:'image/png',buffer:png});assert.equal(await page.locator('#pickList li').count(),25);
 async function checkScroll(selector){await page.locator(selector).scrollIntoViewIfNeeded();const b=await page.locator(selector).boundingBox();await page.mouse.move(b.x+b.width/2,b.y+Math.min(60,b.height/2));const before=await page.evaluate(()=>scrollY);await page.mouse.wheel(0,260);await page.waitForTimeout(200);const top=await page.locator(selector).evaluate(el=>el.scrollTop);assert.ok(top>0,selector+' did not scroll');assert.equal(await page.evaluate(()=>scrollY),before,selector+' scrolled outer page');await page.locator(selector).evaluate(el=>el.scrollTop=el.scrollHeight);await page.mouse.wheel(0,260);await page.waitForTimeout(200);assert.equal(await page.evaluate(()=>scrollY),before,selector+' chained at bottom');return top;}
 const scrolls={files:await checkScroll('#pickListWrap'),recent:await checkScroll('.recent-wrap')};
 await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:evidence+'/upload-selected.png',fullPage:true});
 // Follow the actual button/API flow; all requests are intercepted in this isolated browser.
 await page.locator('#bClearFiles').click();
 for(const scenario of ['success','duplicate','failure','success']){
  uploadScenario=scenario;await page.locator('#files').setInputFiles({name:'stage.png',mimeType:'image/png',buffer:png});await page.locator('#bPre').click();
  if(scenario==='duplicate'){
   await page.waitForSelector('#Pdup:not(.hidden)');assert.equal(await page.locator('.upload-main #Pdup').count(),1);assert.equal(await page.locator('#picker').isVisible(),false);await page.screenshot({path:evidence+'/upload-duplicates.png',fullPage:true});await page.locator('#bSkipAll').click();
  }
  await page.waitForSelector('#Pexif:not(.hidden)');
  assert.equal(await page.locator('#exifTabs .exif-tab').count(),2);
  if(scenario!=='duplicate'){
    assert.match(await page.locator('#exifTabs').innerText(),/待处理 1/);
    await page.locator('#exifBatchKeep').click();
    assert.match(await page.locator('#exifTabs').innerText(),/待处理 0/);
  }
  await page.locator('#exifContinue').click();
  if(scenario!=='failure'){
   await page.waitForFunction(()=>document.querySelector('#txTitle').textContent==='等待后台归档');assert.equal(await page.locator('.upload-main #Ptx').count(),1);assert.equal(await page.locator('#picker').isVisible(),false);await aligned();await page.screenshot({path:evidence+'/upload-processing.png',fullPage:true});
  }
  await page.waitForSelector('#Pres:not(.hidden)');assert.equal(await page.locator('.upload-main #Pres').count(),1);assert.equal(await page.locator('#picker').isVisible(),false);
  assert.equal(await page.locator('#failList').isVisible(),scenario==='failure');await aligned();await page.screenshot({path:evidence+'/upload-report-'+scenario+'.png',fullPage:true});
  await page.locator('#bMore').click();assert.equal(await page.locator('#picker').isVisible(),true);assert.equal(await page.locator('#Pres').isVisible(),false);
 }
 // EXIF time review: confirm filename suggestion and move the same photo between two tabs.
 uploadScenario='success';
 await page.locator('#files').setInputFiles({name:'微信图片_20260304112233_演示.jpg',
   mimeType:'image/jpeg',buffer:Buffer.from([0xff,0xd8,0xff,0xd9])});
 await page.locator('#bPre').click();
 await page.waitForSelector('#Pexif:not(.hidden)');
 assert.match(await page.locator('#exifTabs').innerText(),/待处理 1/);
 assert.match(await page.locator('#exifRows').innerText(),/DateTimeOriginal、CreateDate、ModifyDate 均缺失/);
 assert.equal(await page.locator('[data-exif-date]').inputValue(),'2026-03-04T11:22:33');
 await page.locator('[data-exif-accept]').click();
 assert.match(await page.locator('#exifTabs').innerText(),/待处理 0/);
 assert.match(await page.locator('#exifTabs').innerText(),/已处理 1/);
 await page.locator('[data-exif-tab="processed"]').click();
 assert.match(await page.locator('#exifRows').innerText(),/已采纳时间/);
 await page.locator('[data-exif-reopen]').click();
 assert.match(await page.locator('#exifTabs').innerText(),/待处理 1/);
 await page.locator('[data-exif-accept]').click();
 assert.match(await page.locator('#exifTabs').innerText(),/已处理 1/);
 await page.locator('[data-exif-tab="pending"]').click();
 assert.equal(await page.locator('#exifRows .exif-item').count(),0);
 await page.locator('#exifContinue').click();
 await page.waitForSelector('#Pres:not(.hidden)');
 await page.locator('#bMore').click();
 // R3: Native datetime-local drops :00 seconds; acceptance must normalize it.
 for(const iso of ['2026-03-04T11:22:00','2026-03-04T00:00:00','2026-03-04T11:22:09']){
  await page.locator('#files').setInputFiles({name:'no-time-photo.jpg',mimeType:'image/jpeg',
    buffer:Buffer.from([0xff,0xd8,0xff,0xd9])});
  await page.locator('#bPre').click();
  await page.waitForSelector('#Pexif:not(.hidden)');
  const normalized=await page.locator('[data-exif-date]').evaluate((el,value)=>{
    el.value=value;el.dispatchEvent(new Event('change',{bubbles:true}));
    return el.value;
  },iso);
  if(iso.endsWith(':00'))assert.equal(normalized,iso.slice(0,-3));
  assert.equal(await page.evaluate(()=>files[0].exif.value),iso);
  await page.locator('[data-exif-accept]').click();
  assert.match(await page.locator('#exifTabs').innerText(),/待处理 0/);
  await page.locator('#exifContinue').click();
  await page.waitForSelector('#Pres:not(.hidden)');
  assert.equal(uploadedExifTimes.at(-1),iso);
  await page.locator('#bMore').click();
 }
 assert.equal(await page.evaluate(()=>exifValidISO('2026-02-30T11:22:00')),false);
 assert.equal(await page.evaluate(()=>exifValidISO('2026-03-04T25:00:00')),false);
 // Failed EXIF cancellation must become a visible failure, never a successful skip.
 uploadScenario='cancel-failure';
 await page.locator('#files').setInputFiles({name:'pending-time.heic',mimeType:'image/heic',buffer:Buffer.from('synthetic-heic')});
 await page.locator('#bPre').click();
 await page.waitForSelector('#Pdup:not(.hidden)');
 await page.locator('#bReAll').click();
 await page.waitForSelector('#Pexif:not(.hidden)');
 await page.locator('[data-exif-skip]').click();
 await page.locator('#exifContinue').click();
 await page.waitForSelector('#Pres:not(.hidden)');
 assert.equal(await page.locator('#failList').isVisible(),true);
 assert.match(await page.locator('#failList').innerText(),/暂不上传确认失败/);
 assert.equal(await page.locator('#resH').innerText(),'上传失败');
 await page.locator('#bMore').click();
 uploadScenario='success';
 // Leave a long local list for the later mobile regression.
 await page.locator('#files').setInputFiles(Array.from({length:24},(_,i)=>({name:`mobile-${i}.png`,mimeType:'image/png',buffer:png})));
 await page.locator('nav [data-v="photos"]').click();await page.waitForSelector('#photoInfo .info-list');assert.equal(await page.locator('#photoInfo img,#photoInfo video').count(),0);
 await page.locator('#photoInfo summary').click();await page.locator('#phGrid a').nth(1).hover();assert.equal(await page.locator('#photoInfo details').getAttribute('open'),'');
 for(let i=0;i<4;i++){
  await page.locator('#phGrid a').nth(i).click();await page.locator('#lbxImg').waitFor({state:'visible'});await page.waitForFunction(w=>document.querySelector('#lbxImg').naturalWidth===w,dimensions[i][0]);await page.waitForTimeout(50);
  const frame=await page.locator('#lbxFrame').boundingBox(),img=await page.locator('#lbxImg').boundingBox(),bar=await page.locator('.lbx-bar').boundingBox();assert.ok(Math.abs(frame.width-img.width-18)<2);assert.ok(bar.y<img.y);assert.ok(frame.y>=0&&frame.y+frame.height<=960);await page.locator('#lbxImg').click();assert.equal(await page.locator('#lbx').isVisible(),true);assert.equal(await page.locator('#lbxVid').isVisible(),false);await page.locator('#lbxFrame').click({position:{x:3,y:3}});assert.equal(await page.locator('#lbx').isVisible(),true);await page.screenshot({path:evidence+`/lightbox-${i}.png`});await page.mouse.click(4,4);assert.equal(await page.locator('#lbx').isVisible(),false);
 }
 await page.locator('#phGrid a').first().click();await page.locator('#lbxClose').click();assert.equal(await page.locator('#lbx').isVisible(),false);
 await page.locator('#phGrid a').first().click();await page.keyboard.press('Escape');assert.equal(await page.locator('#lbx').isVisible(),false);
 await page.locator('nav [data-v="system"]').click();await page.waitForSelector('.storage-total');assert.match(await page.locator('.storage-total').innerText(),/100 GB/);assert.match(await page.locator('.system-storage-value').innerText(),/已使用.*60 GB.*总计.*100 GB/);assert.match(await page.locator('.storage-available').innerText(),/39 GB/);
 const storageBox=await page.locator('.system-storage').boundingBox(),workerBox=await page.locator('.system-processing').boundingBox(),photoBox=await page.locator('.system-photo-data').boundingBox();
 assert.ok(Math.abs(storageBox.width-workerBox.width-photoBox.width-20)<2,JSON.stringify({storageBox,workerBox,photoBox}));assert.ok(Math.abs(workerBox.y-photoBox.y)<1);const workerLabel=await page.locator('.system-processing dt').first().boundingBox(),workerValue=await page.locator('.system-processing dd').first().boundingBox();assert.ok(workerValue.x-workerLabel.x<160);assert.ok((await page.locator('.system-storage-meta .live-status').boundingBox()).width<150);assert.ok(Math.abs(photoBox.x+photoBox.width-storageBox.x-storageBox.width)<1);
 const capacity=await page.evaluate(()=>[fmtCapacity(0),fmtCapacity(512*1024**2),fmtCapacity(1024**3),fmtCapacity(null)]);assert.deepEqual(capacity,['0 MB','512 MB','1 GB','—']);await page.screenshot({path:evidence+'/system.png',fullPage:true});
 await page.locator('nav [data-v="log"]').click();assert.equal(await page.locator('.help-step-arrow').count(),4);
 for(const arrow of await page.locator('.help-step-arrow').all()){const b=await arrow.boundingBox();assert.ok(b.width>=30);const pathWidth=await arrow.locator('path').evaluate(el=>el.getBBox().width);assert.ok(pathWidth>=60);}
 await page.screenshot({path:evidence+'/help-arrows.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.locator('nav [data-v="upload"]').click();await page.waitForTimeout(80);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));const mobilePicker=await page.locator('#picker').boundingBox();assert.ok(mobilePicker.height>=64);assert.ok(mobilePicker.width>=300);assert.ok((await page.locator('.upload-main').boundingBox()).width>=350);await page.screenshot({path:evidence+'/upload-mobile.png',fullPage:true});
 await page.locator('nav [data-v="system"]').click();await page.waitForSelector('.storage-available');const mobileSystem=await page.locator('.system-processing').boundingBox();assert.ok(mobileSystem.width>=350);await page.screenshot({path:evidence+'/system-mobile.png',fullPage:true});await page.locator('nav [data-v="log"]').click();assert.equal(await page.locator('.help-step-arrow:visible').count(),0);await page.screenshot({path:evidence+'/help-mobile.png',fullPage:true});await page.locator('nav [data-v="photos"]').click();await page.locator('#phGrid a').nth(1).click();await page.waitForTimeout(80);const mobileFrame=await page.locator('#lbxFrame').boundingBox();assert.ok(mobileFrame.x>=0&&mobileFrame.x+mobileFrame.width<=390&&mobileFrame.y>=0&&mobileFrame.y+mobileFrame.height<=844);await page.mouse.click(4,4);assert.equal(await page.locator('#lbx').isVisible(),false);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({status:'PASS',empty,selected,scrolls,capacity,evidence},null,2));
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
