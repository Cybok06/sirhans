const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const services = [
  {id:'mtn',name:'MTN',image_url:'/mtn.png',offers:[{label:'5GB',amount:30},{label:'10GB',amount:60}]},
  {id:'telecel',name:'Telecel',image_url:'/telecel.png',offers:[{label:'2GB',amount:12}]}
];
function element(){
  return {children:[],value:'',disabled:false,events:{},classList:{add(){}},
    addEventListener(name, fn){this.events[name]=fn;},
    appendChild(child){this.children.push(child.element || child);},
    replaceChildren(...children){this.children=children; this.value='';},
    setAttribute(name, value){this[name]=value;},
    add(child){this.children.push(child);},focus(){this.focused=true;}};
}
function browser(response){
  const ids = Object.fromEntries(['failedReceiptForm','failedReceiptModal','failedReceiptTitle','receiptTypeHelp','receiptEntries','addReceiptEntry','submitReceiptEntries','receiptFeedback'].map(id => [id,element()]));
  ids.alerts=[];
  ids.failedReceiptForm.reportValidity=()=>true;
  function row(){
    const fields={};
    for(const key of ['entry-title','remove','phone','offer','phone-label','offer-label','service-label','service-trigger','service-menu','service-caption','service']) fields[`.receipt-${key}`]=element();
    const node={querySelector(selector){return fields[selector];}};
    node.remove=()=>{ids.receiptEntries.children=ids.receiptEntries.children.filter(item=>item!==node);};
    return {element:node,querySelector:node.querySelector};
  }
  ids.receiptEntryTemplate={content:{cloneNode:row}};
  const context={document:{getElementById:id=>ids[id],createElement:element,createDocumentFragment:element,addEventListener:(name,fn)=>fn()},
    Option:function(text,value){this.text=text;this.value=value;},crypto:{randomUUID:()=> 'test-id'},
    window:{alert:message=>ids.alerts.push(message)},fetch:async(url, options)=>{ids.payload=JSON.parse(options.body);return response;}};
  const html=fs.readFileSync(path.join(__dirname,'../templates/failed_receipt_form.html'),'utf8');
  let script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
  script=script.replace('{{ receipt_choices.services|tojson }}',JSON.stringify(services)).replace(/\{\{[\s\S]*?\}\}/g,'null');
  vm.runInNewContext(script,context);
  return ids;
}
function selectService(row,index){
  row.querySelector('.receipt-service-menu').children[index].children[0].events.click();
}
test('service choices show images and load only the chosen service offers',()=>{
  const ids=browser(),row=ids.receiptEntries.children[0];
  assert.equal(row.querySelector('.receipt-offer').disabled,true);
  const options=row.querySelector('.receipt-service-menu').children;
  assert.equal(options.length,2);
  assert.equal(options[0].children[0].children[0].children[0].src,'/mtn.png');
  selectService(row,0);
  const offer=row.querySelector('.receipt-offer');
  assert.equal(offer.disabled,false);
  assert.deepEqual(offer.children.map(item=>item.text),['Select offer','5GB - GHS 30.00','10GB - GHS 60.00']);
  offer.value='1';
  selectService(row,1);
  assert.equal(offer.value,'');
  assert.deepEqual(offer.children.map(item=>item.text),['Select offer','2GB - GHS 12.00']);
});
test('multiple entries keep their selections independent',()=>{
  const ids=browser();
  selectService(ids.receiptEntries.children[0],0);
  ids.addReceiptEntry.events.click();
  selectService(ids.receiptEntries.children[1],1);
  assert.equal(ids.receiptEntries.children[0].querySelector('.receipt-service').value,'mtn');
  assert.equal(ids.receiptEntries.children[1].querySelector('.receipt-service').value,'telecel');
  assert.equal(ids.receiptEntries.children[0].querySelector('.receipt-offer').children.length,3);
  assert.equal(ids.receiptEntries.children[1].querySelector('.receipt-offer').children.length,2);
});

test('red and green buttons send their type using the same purchase fields',async()=>{
  const ids=browser({ok:false,json:async()=>({success:false,message:'Try again'})});
  const row=ids.receiptEntries.children[0];
  selectService(row,0);
  row.querySelector('.receipt-phone').value='0240000000';
  row.querySelector('.receipt-offer').value='0';
  for (const type of ['delivery_issue', 'no_receipt']) {
    ids.failedReceiptModal.events['show.bs.modal']({relatedTarget:{getAttribute:()=>type}});
    await ids.failedReceiptForm.events.submit({preventDefault(){}});
    assert.equal(ids.payload.report_type,type);
    assert.equal(ids.payload.entries[0].phone,'0240000000');
    assert.equal(ids.payload.entries[0].service_id,'mtn');
    assert.match(ids.submitReceiptEntries.className,type==='delivery_issue' ? /btn-success/ : /btn-danger/);
  }
});

test('duplicate phone and service in one batch are blocked before sending',async()=>{
  const ids=browser();
  ids.addReceiptEntry.events.click();
  ids.receiptEntries.children.forEach((row,index)=>{
    selectService(row,0);
    row.querySelector('.receipt-phone').value=index ? '+233240000000' : '0240000000';
    row.querySelector('.receipt-offer').value=String(index);
  });
  await ids.failedReceiptForm.events.submit({preventDefault(){}});
  assert.equal(ids.payload,undefined);
  assert.match(ids.receiptFeedback.textContent,/Remove the duplicate/);
  assert.equal(ids.receiptEntries.children.length,2);
});

test('double submit while request is running sends only one request',async()=>{
  let complete;
  const pending=new Promise(resolve=>{complete=resolve;});
  const ids=browser(pending);
  const row=ids.receiptEntries.children[0];
  selectService(row,0);
  row.querySelector('.receipt-phone').value='0240000000';
  row.querySelector('.receipt-offer').value='0';
  const first=ids.failedReceiptForm.events.submit({preventDefault(){}});
  const original=ids.payload;
  await ids.failedReceiptForm.events.submit({preventDefault(){}});
  assert.equal(ids.payload,original);
  assert.equal(ids.submitReceiptEntries.disabled,true);
  complete({ok:false,json:async()=>({success:false,message:'Try again'})});
  await first;
  assert.equal(ids.submitReceiptEntries.disabled,false);
});

test('duplicate response shows popup and keeps the entered purchase available',async()=>{
  const message='This issue has Already Being Submitted,Please wait , we are working on it';
  const ids=browser({ok:false,json:async()=>({success:false,code:'duplicate_receipt',message})});
  const row=ids.receiptEntries.children[0];
  selectService(row,0);
  row.querySelector('.receipt-phone').value='0599205294';
  row.querySelector('.receipt-offer').value='0';
  await ids.failedReceiptForm.events.submit({preventDefault(){}});
  assert.deepEqual(ids.alerts,[message]);
  assert.equal(ids.receiptFeedback.textContent,message);
  assert.equal(ids.receiptEntries.children[0],row);
  assert.equal(row.querySelector('.receipt-phone').value,'0599205294');
  assert.equal(ids.submitReceiptEntries.disabled,false);
});
