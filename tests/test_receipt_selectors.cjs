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
<<<<<<< HEAD
function browser(response){
  const ids = Object.fromEntries(['failedReceiptForm','receiptEntries','addReceiptEntry','submitReceiptEntries','receiptFeedback'].map(id => [id,element()]));
  ids.alerts=[];
  ids.failedReceiptForm.reportValidity=()=>true;
=======
function browser(){
  const ids = Object.fromEntries(['failedReceiptForm','receiptEntries','addReceiptEntry','submitReceiptEntries','receiptFeedback'].map(id => [id,element()]));
>>>>>>> origin/main
  function row(){
    const fields={};
    for(const key of ['entry-title','remove','phone','offer','phone-label','offer-label','service-label','service-trigger','service-menu','service-caption','service']) fields[`.receipt-${key}`]=element();
    const node={querySelector(selector){return fields[selector];}};
    node.remove=()=>{ids.receiptEntries.children=ids.receiptEntries.children.filter(item=>item!==node);};
    return {element:node,querySelector:node.querySelector};
  }
  ids.receiptEntryTemplate={content:{cloneNode:row}};
  const context={document:{getElementById:id=>ids[id],createElement:element,createDocumentFragment:element,addEventListener:(name,fn)=>fn()},
<<<<<<< HEAD
    Option:function(text,value){this.text=text;this.value=value;},crypto:{randomUUID:()=> 'test-id'},
    window:{alert:message=>ids.alerts.push(message)},fetch:async()=>response};
=======
    Option:function(text,value){this.text=text;this.value=value;},crypto:{randomUUID:()=> 'test-id'}};
>>>>>>> origin/main
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
<<<<<<< HEAD

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
=======
>>>>>>> origin/main
