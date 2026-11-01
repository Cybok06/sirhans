const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function browser(fetch){
  const config={cart:[{phone:'0240000000',serviceId:'mtn',amount:30}],place_url:'/admin/receipt/place',return_url:'/admin/failed-receipts',csrf_token:'csrf',token:'ticket'};
  const state={textContent:''}, redirects=[];
  const context={window:{location:{assign:url=>redirects.push(url)}},document:{getElementById:()=>state},fetch};
  const html=fs.readFileSync(path.join(__dirname,'../templates/receipt_recovery_overlay.html'),'utf8');
  const script=html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('{{ receipt_recovery|tojson }}',JSON.stringify(config));
  vm.runInNewContext(script,context);
  return {context,state,redirects};
}
test('source cart loads before authenticated placement and returns to admin',async()=>{
  let cart,requests=0;
  const app=browser(async(url,options)=>{
    requests++;
    assert.equal(cart[0].phone,'0240000000');
    assert.equal(url,'/admin/receipt/place');
    assert.deepEqual(JSON.parse(options.body),{csrf_token:'csrf',token:'ticket'});
    return {json:async()=>({success:true,message:'Order placed'})};
  });
  await app.context.window.runReceiptRecovery(value=>{cart=value;});
  assert.deepEqual(app.redirects,['/admin/failed-receipts']);
  await app.context.window.runReceiptRecovery(()=>assert.fail('must not place twice'));
  assert.equal(requests,1);
});
test('failed placement returns to admin where its unresolved result is saved',async()=>{
  const app=browser(async()=>({json:async()=>({success:false,message:'Service unavailable'})}));
  await app.context.window.runReceiptRecovery(()=>{});
  assert.equal(app.state.textContent,'Service unavailable');
  assert.deepEqual(app.redirects,['/admin/failed-receipts']);
});
test('interrupted network never automatically resends an uncertain order',async()=>{
  let requests=0;
  const app=browser(async()=>{requests++;throw new Error('interrupted');});
  await app.context.window.runReceiptRecovery(()=>{});
  await app.context.window.runReceiptRecovery(()=>{});
  assert.equal(requests,1);
  assert.equal(app.redirects.length,0);
  assert.match(app.state.textContent,/Check placement/);
});
