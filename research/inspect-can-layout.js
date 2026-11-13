'use strict';
// Reads the existing static CLI metadata extraction; never loads the assembly.
const fs=require('fs'),m=require('../reverse_engineering/metadata.json');
const b=fs.readFileSync('JKCAN_CHARGE/GBS_CAN上位机.exe');
const str=i=>{let p=m.streams['#Strings'].offset+i;return b.toString('utf8',p,b.indexOf(0,p));};
const blob=i=>{let p=m.streams['#Blob'].offset+i,v=b[p++],n;if(v<128)n=v;else if(v<192)n=((v&63)<<8)|b[p++];else n=((v&31)<<24)|(b[p++]<<16)|(b[p++]<<8)|b[p++];return b.subarray(p,p+n);};
const typeName=r=>(str(r[2])?str(r[2])+'.':'')+str(r[1]);
const types=m.rows['2'];
function sig(x){let i=1;function dec(){let c=x[i++];if(c<128)return c;if(c<192)return ((c&63)<<8)|x[i++];return ((c&31)<<24)|(x[i++]<<16)|(x[i++]<<8)|x[i++];}function t(){let e=x[i++];if(e===0x1d)return t()+'[]';if(e===0x11||e===0x12){let v=dec(),table=[2,1,27][v&3];return table===27?'TypeSpec':typeName(m.rows[table][(v>>>2)-1]);}return ({1:'void',2:'bool',3:'char',4:'sbyte',5:'byte',6:'short',7:'ushort',8:'int',9:'uint',10:'long',11:'ulong',12:'float',13:'double',14:'string',24:'IntPtr',25:'UIntPtr',28:'object'})[e]||('element'+e);}return t();}
let out=[];
for(let ti=0;ti<types.length;ti++){let r=types[ti],name=typeName(r);if(!/^ZLGCAN\.(ZCAN|CANFD|can_frame|canfd_frame|IProperty)/.test(name))continue;out.push('\n'+name+' TypeFlags=0x'+r[0].toString(16));let layout=(m.rows[15]||[]).find(x=>x[2]===ti+1);if(layout)out.push('ClassLayout packing='+layout[0]+' explicitSize='+layout[1]);for(let fi=r[4];fi<(types[ti+1]?.[4]||m.counts[4]+1);fi++){let f=m.rows[4][fi-1],fl=(m.rows[16]||[]).find(x=>x[1]===fi),ma=(m.rows[13]||[]).find(x=>x[0]===(fi<<1));out.push((fl?'offset '+fl[0]+' ':'')+sig(blob(f[2]))+' '+str(f[1])+(ma?' Marshal='+blob(ma[1]).toString('hex'):''));}}
fs.writeFileSync('research/can-structs-static.txt',out.join('\n'));console.log(out.join('\n'));
