'use strict';
// Static PE inspection only: no DLL loading, device opening or CAN calls.
const fs=require('fs'),path=require('path');
function inspect(file){
const b=fs.readFileSync(file),u16=p=>b.readUInt16LE(p),u32=p=>b.readUInt32LE(p),z=p=>b.toString('ascii',p,b.indexOf(0,p));
const pe=u32(60),opt=pe+24,ss=opt+u16(pe+20),dir=opt+(u16(opt)===0x10b?96:112),sections=[];
for(let i=0;i<u16(pe+6);i++){let p=ss+i*40;sections.push({va:u32(p+12),size:Math.max(u32(p+8),u32(p+16)),raw:u32(p+20)});}
const off=r=>{let s=sections.find(s=>r>=s.va&&r<s.va+s.size);if(!s)throw Error('Bad RVA '+r);return s.raw+r-s.va;};
const exports=[],imports=[];
let er=u32(dir);if(er){let ep=off(er),nn=u32(ep+24),names=off(u32(ep+32));for(let i=0;i<nn;i++)exports.push(z(off(u32(names+i*4))));}
let ir=u32(dir+8);if(ir){let p=off(ir);while(u32(p)||u32(p+12)||u32(p+16)){imports.push(z(off(u32(p+12))));p+=20;}}
return {file,machine:'0x'+u16(pe+4).toString(16),bits:u16(opt)===0x10b?32:64,imports,exports};
}
const root='JKCAN_CHARGE',files=[path.join(root,'zlgcan.dll'),...fs.readdirSync(path.join(root,'kerneldlls')).filter(f=>/\.dll$/i.test(f)).map(f=>path.join(root,'kerneldlls',f))];
const result=files.map(inspect);fs.writeFileSync('research/can-binaries-static.json',JSON.stringify(result,null,2));
for(let r of result)console.log(r.file+' '+r.bits+'-bit imports '+r.imports.join(', ')+' exports '+r.exports.length);
console.log('\nZLGCAN exports: '+result[0].exports.join(', '));
