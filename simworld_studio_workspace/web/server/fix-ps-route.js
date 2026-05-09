'use strict';
const fs = require('fs');
let src = fs.readFileSync('./index.js', 'utf-8');

const OLD = 'app.get("/api/pixel-streaming-url",(s,e)=>{const t=s.headers.host?.split(":")[0]||"127.0.0.1";e.json({url:`http://${t}:${CIRRUS_HTTP_PORT}`})})';

const NEW = [
  'app.get("/api/pixel-streaming-url",async(s,e)=>{',
  '  const host=s.headers.host?.split(":")[0]||"127.0.0.1";',
  '  const candidates=[CIRRUS_HTTP_PORT,8685,8585,8785,8885,8485].filter((v,i,a)=>a.indexOf(v)===i);',
  '  const net=require("net");',
  '  const probe=p=>new Promise(r=>{',
  '    const sock=new net.Socket();',
  '    sock.setTimeout(800);',
  '    sock.connect(p,"127.0.0.1",()=>{sock.destroy();r(p)});',
  '    sock.on("error",()=>r(null));',
  '    sock.on("timeout",()=>{sock.destroy();r(null)});',
  '  });',
  '  for(const port of candidates){',
  '    const found=await probe(port);',
  '    if(found)return e.json({url:"http://"+host+":"+found,detectedPort:found});',
  '  }',
  '  e.json({url:"http://"+host+":"+CIRRUS_HTTP_PORT,detectedPort:null});',
  '})',
].join('');

if (src.includes(OLD)) {
  src = src.replace(OLD, NEW);
  fs.writeFileSync('./index.js', src);
  console.log('OK: replaced pixel-streaming-url with auto-detect');
} else {
  console.error('Pattern not found');
  const idx = src.indexOf('pixel-streaming-url');
  if (idx >= 0) console.log('Context:', src.slice(Math.max(0,idx-20), idx+180));
}
