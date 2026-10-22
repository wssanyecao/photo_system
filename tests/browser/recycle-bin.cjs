// Isolated V1.4 UI regression: only synthetic data through a loopback fixture.
const {chromium}=require('playwright');
const http=require('http'),fs=require('fs'),path=require('path'),assert=require('node:assert/strict');
const file=path.resolve(__dirname,'../../photo_gateway/webui/index.html');
const photo={id:1,current_filename:'sample.jpg',date_taken:'2026-10-22 12:00:00',created_at:'2026-10-22 15:00:00',
 file_size:400,mime_type:'image/jpeg',thumbnail_url:'/api/v1/photos/1/thumbnail',
 file_url:'/api/v1/photos/1/file',archive_path:'Photos/2026/202610/sample.jpg'};
const trashPhotos=Array.from({length:47},(_,i)=>({
 ...photo,id:i+1,current_filename:'sample-'+(i+1)+'.'+(i%2?'png':'jpg'),
 date_taken: i%3===0?'2026-10-21 10:00:00':'2026-10-22 10:00:00',
 created_at:i%3===0?'2026-10-21 16:00:00':'2026-10-22 16:00:00',
 source_device: i%2?'other':'camera',
 mime_type:i%2?'image/png':'image/jpeg',
 deleted_at:'2026-10-22 17:00:00',expires_at:'2026-11-21 17:00:00',is_recycled:true
}));
let trashed=false,reviewing=false,decision=null;
let lastRecycleQuery=null;
function response(res,data,status=200){res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify({success:status<400,data}));}
function listTrash(u){
 lastRecycleQuery=u.searchParams;
 let photos=trashPhotos.slice();
 const q=u.searchParams,field=q.get('field')==='uploaded'?'created_at':'date_taken';
 const df=q.get('date_from'),dt=q.get('date_to');
 if(df)photos=photos.filter(x=>x[field]>=df);
 if(dt)photos=photos.filter(x=>x[field]<dt);
 if(q.get('source_device'))photos=photos.filter(x=>x.source_device===q.get('source_device'));
 if(q.get('mime_type'))photos=photos.filter(x=>x.mime_type===q.get('mime_type'));
 const order=q.get('order')==='asc'?1:-1;
 photos.sort((a,b)=>a[field].localeCompare(b[field])*order || (a.id-b.id)*order);
 const limit=Number(q.get('limit')||12),offset=Number(q.get('offset')||0);
 const total=photos.length;
 return {photos:photos.slice(offset,offset+limit).map(x=>({
   ...x, file_url:'/api/v1/recycle-bin/'+x.id+'/preview',
   preview_url:'/api/v1/recycle-bin/'+x.id+'/preview',
   thumbnail_url:'/api/v1/recycle-bin/'+x.id+'/thumbnail',
 })),total,limit,offset};
}
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1'),p=url.pathname,m=req.method;
 if(p==='/'){res.writeHead(200,{'Content-Type':'text/html;charset=utf-8'});res.end(fs.readFileSync(file));return;}
 if(/\/photos\/\d+\/(thumbnail|file)$/.test(p)||/\/recycle-bin\/\d+\/(thumbnail|preview)$/.test(p)){
   res.writeHead(200,{'Content-Type':'image/svg+xml'});
   res.end('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><rect width="600" height="400" fill="#c8d5c8"/></svg>');return;
 }
 if(p==='/api/v1/devices')return response(res,{devices:[{id:'camera',name:'Camera',enabled:true},{id:'other',name:'Other',enabled:true}]});
 if(p==='/api/v1/system/status')return response(res,{storage:null,counts:{assets:1,pending_upload:0,processing:0},allowed_extensions:['jpg','png']});
 if(p==='/api/v1/upload-sessions/recent')return response(res,{sessions:[]});
 if(p==='/api/v1/photos'&&m==='GET')return response(res,{photos:trashed?[]:[photo],total:trashed?0:1,limit:Number(url.searchParams.get('limit')||12)});
 if(p==='/api/v1/photos/1'&&m==='GET')return response(res,{photo});
 if(p==='/api/v1/photos/1/trash'&&m==='POST'){trashed=true;return response(res,{state:'TRASHED'});}
 if(p==='/api/v1/recycle-bin'&&m==='GET')return response(res,listTrash(url));
 const detail=p.match(/^\/api\/v1\/recycle-bin\/(\d+)$/);
 if(detail&&m==='GET'){
   const ph=trashPhotos.find(x=>x.id===Number(detail[1]));
   return ph?response(res,{photo:ph}):response(res,{},404);
 }
 if(p==='/api/v1/recycle-bin/1/restore'&&m==='POST'){
   trashed=false;trashPhotos.splice(0,1);return response(res,{state:'ACTIVE'});
 }
 if(p==='/api/v1/upload-sessions'&&m==='POST')return response(res,{session:{id:'mock-session'}});
 if(p.endsWith('/items/precheck'))return response(res,{items:[{item_id:7,precheck_status:'NO_MATCH'}]});
 if(p==='/api/v1/upload-items/7/file'&&m==='POST'){reviewing=true;return response(res,{status:'UPLOADED'});}
 if(p.endsWith('/deleted-reviews'))return response(res,{items:reviewing&&decision===null?[{item_id:7,asset_id:1,original_filename:'sample.jpg',lifecycle_state:'TRASHED'}]:[]});
 if(p==='/api/v1/upload-items/7/deleted-decision'&&m==='POST'){decision='keep_deleted';return response(res,{item_id:7});}
 if(p==='/api/v1/upload-sessions/mock-session')return response(res,{session:{id:'mock-session',status:decision?'COMPLETED':'UPLOADING',total_files:1,uploaded_files:1,duplicate_files:decision?1:0,skipped_files:0,failed_files:0}});
 res.writeHead(404);res.end('not found '+p);
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({headless:true,...(process.env.PHOTO_WEBUI_CHROMIUM?{executablePath:process.env.PHOTO_WEBUI_CHROMIUM}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1200,height:850}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
  await page.goto('http://127.0.0.1:'+server.address().port);
  await page.locator('nav [data-v="photos"]').click();
  await page.locator('#phGrid .photo-tile').first().click();
  assert.equal(await page.locator('#lbxDelete').innerText(),'移入回收站');
  await page.locator('#lbxDelete').click();
  await page.waitForSelector('#phGrid .empty-state');
  assert.equal(trashed,true);

  await page.locator('#bRecycle').click();
  await page.waitForFunction(()=>document.querySelector('#galleryTitle').textContent==='回收站');
  assert.equal(await page.locator('#gallerySubtitle').innerText(),
    '浏览本地回收站的照片，保留期内可恢复，回收站文件不参与 NAS 同步');
  assert.equal(await page.locator('#bPh').innerText(),'返回照片');
  assert.equal(await page.locator('#bRecycle').isVisible(),false);
  assert.equal(await page.locator('.photo-workspace').isVisible(),true);
  await page.waitForFunction(()=>document.querySelectorAll('#phGrid .photo-tile').length===30);
  assert.equal(await page.locator('#pgNext').isEnabled(),true);

  // Same pagination controls, including user-configurable page size.
  await page.locator('#pgSize').fill('10');
  await page.locator('#pgSize').dispatchEvent('change');
  await page.waitForFunction(()=>document.querySelector('#pgSize').value==='10'&&document.querySelectorAll('#phGrid .photo-tile').length===10);
  await page.locator('#pgNext').click();
  await page.waitForFunction(()=>document.querySelector('#pgInfo').textContent.startsWith('2 /'));
  assert.equal(lastRecycleQuery.get('offset'),'10');

  // Same sorting and all filters; selection/zoom works like regular photos.
  await page.locator('[data-order="asc"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-order="asc"]').classList.contains('active'));
  assert.equal(lastRecycleQuery.get('order'),'asc');
  await page.locator('[data-field="uploaded"]').click();
  await page.waitForFunction(()=>document.querySelector('#pgBar').textContent.includes('共 47 张'));
  assert.equal(lastRecycleQuery.get('field'),'uploaded');
  await page.locator('#photoDevice').selectOption('other');
  await page.waitForFunction(()=>document.querySelector('#pgBar').textContent.includes('共 23 张'));
  assert.equal(lastRecycleQuery.get('source_device'),'other');
  await page.locator('#photoMime').selectOption('image/png');
  await page.waitForFunction(()=>document.querySelector('#pgBar').textContent.includes('共 23 张'));
  assert.equal(lastRecycleQuery.get('mime_type'),'image/png');
  await page.locator('#df').fill('2026-10-22');
  await page.locator('#dt').fill('2026-10-22');
  await page.locator('#bFilter').click();
  await page.waitForFunction(()=>document.querySelector('#pgBar').textContent.includes('共 15 张'));
  assert.equal(lastRecycleQuery.get('date_from'),'2026-10-22');
  assert.equal(lastRecycleQuery.get('date_to'),'2026-10-23');

  // Reset filters to recover first asset; check enlarged modal uses restore action.
  await page.locator('#photoDevice').selectOption('');
  await page.locator('#photoMime').selectOption('');
  await page.locator('#df').fill('');
  await page.locator('#dt').fill('');
  await page.locator('[data-field="taken"]').click();
  await page.locator('[data-order="desc"]').click();
  await page.locator('#pgSize').fill('200');
  await page.locator('#pgSize').dispatchEvent('change');
  await page.waitForFunction(()=>document.querySelectorAll('#phGrid .photo-tile').length===47);
  await page.locator('#phGrid .photo-tile[data-id="1"]').click();
  await page.waitForSelector('#lbxImg:visible');
  assert.equal(await page.locator('#lbxDelete').innerText(),'恢复照片');
  assert.equal(await page.locator('#photoInfo .info-list').isVisible(),true);
  await page.locator('#lbxDelete').click();
  await page.waitForFunction(()=>document.querySelectorAll('#phGrid .photo-tile').length===46);
  assert.equal(trashed,false);
  assert.equal(await page.locator('#galleryTitle').innerText(),'回收站');

  // Mobile layout and lightbox retain their responsive constraints.
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.locator('#phGrid .photo-tile').first().click();
  await page.waitForSelector('#lbxImg:visible');
  const modal=await page.locator('#lbxFrame').boundingBox();
  assert.ok(modal.x>=0 && modal.x+modal.width<=390);
  assert.ok(modal.y>=0 && modal.y+modal.height<=844);
  assert.equal(await page.locator('#lbxDelete').innerText(),'恢复照片');
  await page.locator('#lbxClose').click();
  await page.setViewportSize({width:1200,height:850});

  await page.locator('#bPh').click();
  await page.waitForFunction(()=>document.querySelector('#galleryTitle').textContent==='照片');
  assert.equal(await page.locator('#gallerySubtitle').innerText(),'按拍摄时间浏览已归档照片');
  assert.equal(await page.locator('#bPh').innerText(),'刷新');
  assert.equal(await page.locator('#bRecycle').isVisible(),true);
  await page.waitForSelector('#phGrid .photo-tile');

  // Deleted-upload review remains independent from gallery mode.
  trashed=true;
  await page.locator('nav [data-v="upload"]').click();
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jwS8AAAAASUVORK5CYII=','base64');
  await page.locator('#files').setInputFiles({name:'sample.jpg',mimeType:'image/jpeg',buffer:png});
  await page.locator('#bPre').click();
  await page.waitForSelector('#Pdeleted:not(.hidden)',{timeout:12000});
  await page.locator('#deletedReviews button').first().click();
  await page.waitForSelector('#Pres:not(.hidden)',{timeout:12000});
  assert.equal(decision,'keep_deleted');
  assert.deepEqual(errors,[]);
  console.log('PASS recycle gallery: mode, filters, pagination, zoom, restore, upload decision');
 }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
