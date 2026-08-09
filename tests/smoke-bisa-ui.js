'use strict';
const {chromium} = require('playwright');
const {spawn} = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

const root = path.resolve(__dirname,'..');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(),'bisa-ui-'));
const port = 18743;
const env = {...process.env,BISA_ENV:'development',BISA_DATA_DIR:tmp,BISA_DB_PATH:path.join(tmp,'bisa.sqlite3'),BISA_UPLOAD_DIR:path.join(tmp,'uploads'),BISA_BACKUP_DIR:path.join(tmp,'backups'),BISA_SEED_SAMPLE_DATA:'true',HOST:'127.0.0.1',PORT:String(port),PYTHONIOENCODING:'utf-8'};
const server=spawn('python',['bisa_server.py'],{cwd:root,env,stdio:['ignore','pipe','pipe']});
let serverErrors=''; server.stderr.on('data',chunk=>serverErrors+=chunk.toString());
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function ready(){for(let i=0;i<80;i++){try{const r=await fetch(`http://127.0.0.1:${port}/healthz`);if(r.ok)return;}catch{}await sleep(100);}throw new Error(`server_not_ready ${serverErrors}`);}

(async()=>{
  await ready(); const browser=await chromium.launch({headless:true});
  const sizes=[{width:320,height:760},{width:375,height:812},{width:390,height:844},{width:430,height:932}];
  const results=[];
  for(const viewport of sizes){
    const context=await browser.newContext({viewport}); const page=await context.newPage();
    page.setDefaultTimeout(10000);
    const consoleErrors=[]; page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});
    await page.goto(`http://127.0.0.1:${port}`,{waitUntil:'domcontentloaded',timeout:15000});
    await page.locator('.hero h1').waitFor();
    if(await page.locator('.demo-ad').count()<1)throw new Error('demo_ad_missing');
    if(await page.locator('.bundle-card').count()<1)throw new Error('demo_bundle_missing');
    if(await page.locator('.store-card:not(.bundle-card)').count()<1)throw new Error('demo_store_missing');
    const fit=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth,dir:document.documentElement.dir}));
    if(fit.scroll>fit.client+1)throw new Error(`horizontal_overflow_${viewport.width}_${fit.scroll}`);
    if(fit.dir!=='rtl')throw new Error('rtl_not_default');
    await page.locator('#languageButton').click();
    if(await page.locator('html').getAttribute('dir')!=='ltr')throw new Error('ltr_switch_failed');
    await page.locator('[data-view="explore"]').click();
    if(await page.locator('.product-card').count()<1)throw new Error('catalog_missing');
    if(consoleErrors.length)throw new Error(`console_errors:${consoleErrors.join('|')}`);
    results.push({width:viewport.width,fit:true,rtl:true,ltr:true}); await context.close();
  }
  const context=await browser.newContext({viewport:{width:390,height:844}}); const page=await context.newPage();
  const apiResponses=[]; page.on('response',async response=>{if(response.url().includes('/api/'))apiResponses.push({url:response.url(),status:response.status(),body:await response.text().catch(()=> '')});});
  page.setDefaultTimeout(10000);
  await page.goto(`http://127.0.0.1:${port}`,{waitUntil:'domcontentloaded',timeout:15000});
  await page.locator('[data-add-product]').first().click();
  await page.locator('#loginForm').waitFor();
  await page.locator('[name="phone"]').fill('96890000001'); await page.locator('[name="pin"]').fill('1234');
  await page.locator('#loginForm button[type="submit"]').click();
  await page.waitForTimeout(800);
  if(await page.locator('#sheetRoot').isVisible()){
    const diagnostics=await page.evaluate(()=>({toasts:[...document.querySelectorAll('.toast')].map(n=>n.textContent),storage:{token:localStorage.getItem('bisa.auth.token.v1'),account:localStorage.getItem('bisa.auth.account.v1')}})); diagnostics.responses=apiResponses;
    throw new Error(`login_sheet_did_not_close:${JSON.stringify(diagnostics)}`);
  }
  await page.locator('#sheetRoot').waitFor({state:'hidden'});
  await page.locator('.product-card').first().waitFor();
  await page.locator('[data-add-product]').first().click(); await page.locator('[data-view="cart"]').click();
  if(await page.locator('.cart-item').count()<1)throw new Error('shopper_cart_flow_failed');
  await page.locator('[data-view="home"]').click();
  await page.locator('[data-add-bundle]').first().click();
  if(await page.locator('#sheetRoot').isVisible())await page.locator('[data-confirm-replace]').click();
  await page.locator('[data-view="cart"]').click();
  if(await page.locator('.cart-item').count()<1)throw new Error('bundle_cart_flow_failed');
  await context.close(); await browser.close();
  console.log(JSON.stringify({ok:true,responsive:results,shopperFlow:true}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{server.kill('SIGTERM');setTimeout(()=>process.exit(process.exitCode||0),500);});
