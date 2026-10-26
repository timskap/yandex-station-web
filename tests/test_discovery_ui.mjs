import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const node = () => ({children:[],hidden:false,disabled:false,value:'',checked:false,textContent:'',
  append(...items){for(const item of items){item.parentElement=this;this.children.push(item);}},
  replaceChildren(...items){this.children=[];this.append(...items);}});
const nodes = new Map();
const $ = id => {if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
let data={scanning:false,checked:254,total:254,networks:['192.168.1.0/24'],configured_networks:[],ports:[5555],auto:true,error:'',
  devices:[{id:'s-test',address:'192.168.1.20:5555',name:'Станция <img>',available:true}]};
let error='',callback,assigned='';const calls=[];
const scope={$,online:false,connInfo:{state:'waiting'},document:{hidden:false,createElement:node},
  PanelContext:{deviceId:'',fetch:async(path,options)=>{
    calls.push({path,options});
    if(options)return {ok:!error,json:async()=>error?{error}:path.endsWith('/open')?{url:'/speaker/s-test/'}:data};
    return {ok:true,json:async()=>data};
  }},location:{assign(url){assigned=url;}},setInterval(fn){callback=fn;}};
vm.runInNewContext(fs.readFileSync(new URL('../static/discovery.js',import.meta.url),'utf8'),scope);
const settle=()=>new Promise(r=>setImmediate(r));
await settle();
assert.equal($('#discovery-card').parentElement,$('#discovery-offline-slot'));
assert.match($('#discovery-state').textContent,/Найдено колонок: 1/);
const row=$('#discovery-list').children[0];
assert.equal(row.children[0].children[0].textContent,'Станция <img>');
await row.children[1].onclick();
assert.equal(assigned,'/speaker/s-test/#overview');
assert.deepEqual(JSON.parse(calls.at(-1).options.body),{id:'s-test'});

scope.online=true;callback();await settle();
assert.equal($('#discovery-card').parentElement,$('#discovery-settings-slot'));
$('#discovery-ports').value='2222';
callback();await settle();
assert.equal($('#discovery-ports').value,'2222','Polling must preserve unsaved parameters');
error='Неверная сеть';
$('#discovery-form').onsubmit({preventDefault(){}});await settle();
assert.equal($('#discovery-error').textContent,'Неверная сеть');
callback();await settle();
assert.equal($('#discovery-error').textContent,'Неверная сеть','Polling must preserve a validation error');

scope.connInfo={state:'locked'};callback();await settle();
assert.equal($('#discovery-card').hidden,true);
console.log('Discovery UI: offline/online placement, scoped navigation, safe labels, preserved edits and errors passed');
