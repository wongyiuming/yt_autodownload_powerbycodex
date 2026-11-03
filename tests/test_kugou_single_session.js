"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const dir = path.join(__dirname, "../extension");
const manifest = JSON.parse(fs.readFileSync(path.join(dir, "manifest.json"), "utf8"));
const hash = "A".repeat(32);
const song = `https://www.kugou.com/song/#hash=${hash}&album_id=456&album_audio_id=789`;
const publicList = "https://www.kugou.com/yy/special/single/123.html";
const flush = () => new Promise(setImmediate);

async function harness(options = {}) {
  const requests = [], broadcasts = [];
  let listener, reads = 0;
  let tab = {id:17, windowId:1, url:song};
  const tasks = [{id:"single",url:song,use_cookies:true,kugou_user_id:"123"},
                 {id:"public",url:publicList,use_cookies:false}];
  const chrome = {
    tabs: {query: async () => [tab], get: async () => tab},
    scripting: {executeScript: async ({target,args}) => {
      reads++;
      assert.equal(target.tabId,17);
      if (options.navigate) tab={...tab,url:"https://www.kugou.com/"};
      if (options.switchTab) tab={...tab,id:18};
      return [{frameId:0,result:{origin:args[0],session: options.noSession ? {} : {
        userid:"123", token:"fixture-secret", appid:1014, mid:"fixture-mid",dfid:"fixture-dfid"}}}];
    }},
    cookies: {getAllCookieStores:async()=>[{id:"same-profile",tabIds:[17]}],getAll:async()=>[]},
    runtime: {
      getManifest:()=>manifest,
      sendMessage:async value=>{broadcasts.push(value);},
      onInstalled:{addListener(){}},onStartup:{addListener(){}},
      onMessage:{addListener(cb){listener=cb;}},
      connectNative(){
        let receive;
        return {onMessage:{addListener(cb){receive=cb;}},onDisconnect:{addListener(){}},
          postMessage(message){requests.push(message);queueMicrotask(()=>receive({ok:true,
            reply_to:message.id,host:{version:manifest.version},queue:tasks}));}};
      },
    },
  };
  const context=vm.createContext({chrome,URL,URLSearchParams,console,setTimeout,clearTimeout,queueMicrotask,
    importScripts(name){vm.runInContext(fs.readFileSync(path.join(dir,name),"utf8"),context);}});
  vm.runInContext(fs.readFileSync(path.join(dir,"background.js"),"utf8"),context);
  await flush();
  return {requests,broadcasts,get reads(){return reads;},setTab(value){tab=value;},
    send:msg=>new Promise(resolve=>listener(msg,{},resolve))};
}

async function backgroundTests(){
  const h=await harness();
  let result=await h.send({type:"enqueue",url:song,mode:"audio",useCookies:true,tabId:17});
  assert.equal(result.ok,true);
  let request=h.requests.at(-1);
  assert.equal(request.action,"enqueue");assert.equal(request.use_cookies,true);
  assert.equal(request.cookies.find(c=>c.name==="token").value,"fixture-secret");
  assert.equal(h.reads,1);
  assert(!JSON.stringify(h.broadcasts).includes("fixture-secret"));
  result=await h.send({type:"retry",taskId:"single",tabId:17});
  assert.equal(result.ok,true);assert.equal(h.reads,2);
  assert.equal(h.requests.at(-1).use_cookies,true);
  result=await h.send({type:"retry",taskId:"single",tabId:17,useCookies:false});
  assert.equal(result.ok,true);assert.equal(h.reads,2);assert.equal(h.requests.at(-1).cookies.length,0);
  result=await h.send({type:"enqueue",url:song,mode:"audio",useCookies:false});
  assert.equal(result.ok,true);assert.equal(h.reads,2);assert.equal(h.requests.at(-1).use_cookies,false);
  result=await h.send({type:"enqueue",url:publicList,mode:"audio",useCookies:true});
  assert.equal(result.ok,true);assert.equal(h.reads,2);assert.equal(h.requests.at(-1).use_cookies,false);
  console.log("SINGLE_SESSION_ENQUEUE_RETRY_OPTOUT_OK");

  for(const options of [{navigate:true},{switchTab:true},{noSession:true}]){
    const x=await harness(options);
    const r=await x.send({type:"enqueue",url:song,mode:"audio",useCookies:true,tabId:17});
    assert.equal(r.ok,false,JSON.stringify(options));
    assert.equal(x.requests.filter(p=>p.action==="enqueue").length,0);
    assert(!JSON.stringify(r).includes("fixture-secret"));
  }
  const x=await harness();
  x.setTab({id:18,windowId:1,url:song});
  result=await x.send({type:"enqueue",url:song,mode:"audio",useCookies:true,tabId:17});
  assert.equal(result.ok,false);assert.equal(x.reads,0);
  x.setTab({id:17,windowId:1,url:"https://www.kugou.com/"});
  result=await x.send({type:"enqueue",url:song,mode:"audio",useCookies:true,tabId:17});
  assert.equal(result.ok,false);assert.equal(x.reads,0);
  console.log("SINGLE_SESSION_TAB_AND_FAILURE_BOUNDARIES_OK");
}

function element(value=""){
  return {value,checked:false,disabled:false,hidden:false,textContent:"",innerHTML:"",events:{},children:[],
    classList:{add(){},remove(){},toggle(){}},setAttribute(){},
    addEventListener(name,cb){this.events[name]=cb;},appendChild(child){this.children.push(child);},
    querySelector(selector){return selector===".task-actions"?(this.actions??=element()):this.input;}};
}
async function popupTests(){
  const elements=new Map(), messages=[];
  const modes=["video","audio"].map(v=>{const e=element();e.input=element(v);return e;});
  const document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
    querySelectorAll:()=>modes,querySelector:()=>modes.find(m=>m.input.checked)?.input,createElement:()=>element()};
  const chrome={tabs:{query:async()=>[{id:17,url:song,title:"Song"}]},runtime:{getManifest:()=>manifest,
    onMessage:{addListener(){}},sendMessage:async m=>{messages.push(m);return {ok:true,connected:true,
      hostInfo:{version:manifest.version},queue:[]};}}};
  const c=vm.createContext({document,chrome,URL,URLSearchParams,console});
  for(const file of ["platform_urls.js","popup.js"])vm.runInContext(fs.readFileSync(path.join(dir,file),"utf8"),c);
  await flush();
  const cookie=elements.get("useCookies");
  assert.equal(cookie.checked,true);assert.equal(cookie.disabled,false);
  assert.match(elements.get("cookieLabel").textContent,/当前歌曲页面.*登录状态/);
  await elements.get("enqueue").events.click();
  const request=messages.findLast(m=>m.type==="enqueue");
  assert.equal(request.useCookies,true);assert.equal(request.tabId,17);
  cookie.checked=false;cookie.events.change();
  elements.get("sourceUrl").value="https://www.kugou.com/";vm.runInContext("updateDetected()",c);
  assert.equal(cookie.checked,true);assert.equal(cookie.disabled,true);
  elements.get("sourceUrl").value=song;vm.runInContext("updateDetected()",c);
  assert.equal(cookie.checked,false);assert.equal(cookie.disabled,false);
  elements.get("sourceUrl").value=publicList;vm.runInContext("updateDetected()",c);
  assert.equal(cookie.checked,false);assert.equal(cookie.disabled,true);
  console.log("SINGLE_SESSION_POPUP_REAL_HANDLERS_OK");
}
(async()=>{await backgroundTests();await popupTests();})().catch(e=>{console.error(e);process.exitCode=1;});
