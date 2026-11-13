// Static PE/CLI metadata and IL parser. Does not load or execute the input assembly.
const fs = require('fs');
const b = fs.readFileSync(process.argv[2]);
const ops = new Map(JSON.parse(fs.readFileSync(__dirname+'/opcodes.json','utf8').replace(/^\uFEFF/, '')).map(o=>[o.Code,o]));
const u16=p=>b.readUInt16LE(p),u32=p=>b.readUInt32LE(p),i32=p=>b.readInt32LE(p);
const pe=u32(0x3c), nsections=u16(pe+6),opt=pe+24,sect=opt+u16(pe+20),dir=opt+(u16(opt)===0x10b?96:112);
const sections=[];for(let i=0;i<nsections;i++){let p=sect+i*40;sections.push({va:u32(p+12),size:Math.max(u32(p+8),u32(p+16)),raw:u32(p+20)});}
function off(rva){let s=sections.find(s=>rva>=s.va&&rva<s.va+s.size);if(!s)throw Error('RVA '+rva.toString(16));return s.raw+rva-s.va;}
const cli=off(u32(dir+14*8)),meta=off(u32(cli+8));let p=meta+16+u32(meta+12);p=(p+3)&~3;p+=2;let ns=u16(p);p+=2;
const streams={};for(let i=0;i<ns;i++){let start=p,ofs=u32(p),size=u32(p+4);p+=8;let end=b.indexOf(0,p),name=b.toString('ascii',p,end);p=(end+4)&~3;streams[name]={offset:meta+ofs,size};}
const str=i=>{if(!i)return '';let p=streams['#Strings'].offset+i;return b.toString('utf8',p,b.indexOf(0,p));};
const compressed=p=>{let v=b[p];if(v<128)return [v,1];if(v<192)return [((v&63)<<8)|b[p+1],2];return [((v&31)<<24)|(b[p+1]<<16)|(b[p+2]<<8)|b[p+3],4];};
const userstr=i=>{let p=streams['#US'].offset+i,[len,n]=compressed(p);return JSON.stringify(b.toString('utf16le',p+n,p+n+len-1));};
let tp=streams['#~']?.offset||streams['#-'].offset;let heap=b[tp+6],valid=b.readBigUInt64LE(tp+8);p=tp+24;
let counts=Array(64).fill(0);for(let t=0;t<64;t++)if(valid&(1n<<BigInt(t))){counts[t]=u32(p);p+=4;}
const coded={
 TypeDefOrRef:[2,[2,1,27]],HasConstant:[2,[4,8,23]],HasCustomAttribute:[5,[6,4,1,2,8,9,10,0,14,23,20,17,26,27,32,35,38,39,40,42,44,43]],HasFieldMarshal:[1,[4,8]],HasDeclSecurity:[2,[2,6,32]],MemberRefParent:[3,[2,1,26,6,27]],HasSemantics:[1,[20,23]],MethodDefOrRef:[1,[6,10]],MemberForwarded:[1,[4,6]],Implementation:[2,[38,35,39]],CustomAttributeType:[3,[-1,-1,6,10,-1]],ResolutionScope:[2,[0,26,35,1]],TypeOrMethodDef:[1,[2,6]]};
const schemas={
0:['u2','s','g','g','g'],1:['cResolutionScope','s','s'],2:['u4','s','s','cTypeDefOrRef','t4','t6'],3:['t4'],4:['u2','s','b'],5:['t6'],6:['u4','u2','u2','s','b','t8'],7:['t8'],8:['u2','u2','s'],9:['t2','cTypeDefOrRef'],10:['cMemberRefParent','s','b'],11:['u2','cHasConstant','b'],12:['cHasCustomAttribute','cCustomAttributeType','b'],13:['cHasFieldMarshal','b'],14:['u2','cHasDeclSecurity','b'],15:['u2','u4','t2'],16:['u4','t4'],17:['b'],18:['t2','t20'],19:['t20'],20:['u2','s','cTypeDefOrRef'],21:['t2','t23'],22:['t23'],23:['u2','s','b'],24:['u2','t6','cHasSemantics'],25:['t2','cMethodDefOrRef','cMethodDefOrRef'],26:['s'],27:['b'],28:['u2','cMemberForwarded','s','t26'],29:['u4','t4'],30:['u4','u4'],31:['u4'],32:['u4','u2','u2','u2','u2','u4','b','s','s'],33:['u4'],34:['u4','u4','u4'],35:['u2','u2','u2','u2','u4','b','s','s','b'],36:['u4','t35'],37:['u4','u4','u4','t35'],38:['u4','s','b'],39:['u4','u4','s','s','cImplementation'],40:['u4','u4','s','cImplementation'],41:['t2','t2'],42:['u2','u2','cTypeOrMethodDef','s'],43:['cMethodDefOrRef','b'],44:['t42','cTypeDefOrRef']};
function sz(s){if(s==='u2')return 2;if(s==='u4')return 4;if(s==='s')return heap&1?4:2;if(s==='g')return heap&2?4:2;if(s==='b')return heap&4?4:2;if(s[0]==='t')return counts[+s.slice(1)]>=65536?4:2;let [bits,tables]=coded[s.slice(1)];return Math.max(...tables.map(t=>counts[t]||0))>=(1<<(16-bits))?4:2;}
const rows={};for(let t=0;t<64;t++){if(!counts[t])continue;if(!schemas[t])throw Error('unknown table '+t);let schema=schemas[t],rs=[];for(let r=0;r<counts[t];r++){let row=[];for(let s of schema){let n=sz(s);row.push(n===2?u16(p):u32(p));p+=n;}rs.push(row);}rows[t]=rs;}
const typeName=row=>(str(row[2])?str(row[2])+'.':'')+str(row[1]);
const types=(rows[2]||[]).map((r,i)=>({name:typeName(r),field:r[4],method:r[5],endField:rows[2][i+1]?.[4]||(counts[4]+1),endMethod:rows[2][i+1]?.[5]||(counts[6]+1)}));
const owner=(table,row)=>types.find(t=>row>=(table===6?t.method:t.field)&&row<(table===6?t.endMethod:t.endField))?.name||'?';
function codedRef(name,v){let [bits,tables]=coded[name];return ref(tables[v&((1<<bits)-1)],v>>>bits);}
function ref(table,id){if(!id)return '(null)';let r=rows[table]?.[id-1];if(!r)return table+':'+id;
 switch(table){case 1:case 2:return typeName(r);case 4:return owner(table,id)+'::'+str(r[1]);case 6:return owner(table,id)+'::'+str(r[3]);case 10:return codedRef('MemberRefParent',r[0])+'::'+str(r[1]);case 26:return str(r[0]);case 35:return str(r[6]);case 27:return 'TypeSpec#'+id;case 43:return codedRef('MethodDefOrRef',r[0]);default:return 'table'+table+'#'+id;}}
const token=v=> (v>>>24)===0x70?userstr(v&0xffffff):ref(v>>>24,v&0xffffff);
let lines=[];lines.push('INPUT '+process.argv[2]);lines.push('Static parser only. Metadata and IL offsets refer to supplied binary.');
for(let r of rows[28]||[])lines.push('PINVOKE '+codedRef('MemberForwarded',r[1])+' => '+ref(26,r[3])+'!'+str(r[2]));
for(let type of types){lines.push('\nTYPE '+type.name);for(let id=type.field;id<type.endField;id++)lines.push('FIELD '+ref(4,id));
 for(let id=type.method;id<type.endMethod;id++){let r=rows[6][id-1];lines.push('\nMETHOD '+ref(6,id)+' TOKEN '+(0x06000000+id).toString(16)+' RVA '+r[0].toString(16));if(!r[0])continue;
  let h=off(r[0]),start,codeSize,locals;
  if((b[h]&3)===2){start=h+1;codeSize=b[h]>>>2;}else{start=h+((u16(h)>>>12)*4);codeSize=u32(h+4);locals=u32(h+8);}
  let q=start;while(q<start+codeSize){let il=q-start,val=b[q++];if(val===254)val=0xfe00+b[q++];let op=ops.get(val);if(!op){lines.push('UNKNOWN OPCODE '+val);break;}let v='';switch(op.Operand){case 'InlineNone':break;case 'ShortInlineI':v=b.readInt8(q++);break;case 'ShortInlineVar':v=b[q++];break;case 'InlineVar':v=u16(q);q+=2;break;case 'InlineI':v=i32(q);q+=4;break;case 'InlineI8':v=b.readBigInt64LE(q);q+=8;break;case 'ShortInlineR':v=b.readFloatLE(q);q+=4;break;case 'InlineR':v=b.readDoubleLE(q);q+=8;break;case 'ShortInlineBrTarget':v='IL_'+(q-start+1+b.readInt8(q)).toString(16).padStart(4,'0');q++;break;case 'InlineBrTarget':v='IL_'+(q-start+4+i32(q)).toString(16).padStart(4,'0');q+=4;break;case 'InlineSwitch':let n=u32(q);q+=4;let end=q-start+4*n;let ts=[];for(let i=0;i<n;i++){ts.push('IL_'+(end+i32(q)).toString(16).padStart(4,'0'));q+=4;}v=ts.join(', ');break;default:v=token(u32(q));q+=4;break;}
   lines.push('IL_'+il.toString(16).padStart(4,'0')+': '+op.Name+' '+v);
  }
 }
}
fs.writeFileSync(__dirname+'/vendor-il.txt',lines.join('\n'));
fs.writeFileSync(__dirname+'/metadata.json',JSON.stringify({streams,counts,types,rows},null,2));
console.log('Extracted '+types.length+' types and '+counts[6]+' methods into '+lines.length+' lines.');
