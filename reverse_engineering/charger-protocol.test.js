'use strict';
const assert = require('node:assert/strict');
const p = require('./charger-protocol');
let checks=0;
const eq=(a,b)=>{assert.deepEqual(a,b);checks++;};
const bad=f=>{assert.throws(f);checks++;};
const bytes=f=>Array.from(f.data);
// Golden byte vectors taken from explicit IL constants and stores, not encoder output.
for(const [name,expected] of Object.entries({workStart:[0,20,0,0],shutdown:[0,20,0,128],calibrationStart:[0,20,0,1],saveVoltage:[0,23],saveFullCurrent:[0,24],saveHalfCurrent:[0,25]})) {
 const f=p.command(name); eq(f.id,0x1806E550);eq(f.extended,true);eq(f.remote,false);eq(f.dlc,expected.length);eq(bytes(f),expected);
}
eq(bytes(p.encodeDuty('voltage',80)),[0,21,3,32]);
eq(bytes(p.encodeDuty('current',80.1)),[0,22,3,33]);
eq(bytes(p.encodeConfig('ratedVoltage',48)),[0,2,1,224]);
eq(bytes(p.encodeConfig('ccCurrent',15)),[0,7,0,150]);
eq(bytes(p.encodeConfig('endVoltage',58.4)),[0,6,2,72]);
eq(bytes(p.encodeConfig('endCurrent',1)),[0,10,0,10]);
eq(bytes(p.encodeConfig('blindCharge',1)),[0,12,0,1]);
eq(bytes(p.poll(1)),[0,11,0,1]);eq(bytes(p.poll(6)),[0,11,0,6]);
const rx=(id,data)=>({id,extended:true,remote:false,dlc:8,data});
eq(p.decode(rx(0x18FF53E5,[0,1,1,224,1,144,0,75])).values,{batteryType:1,ratedVoltage:48,startVoltage:40,temperatureLimit:75});
eq(p.decode(rx(0x18FF54E5,[0,180,2,72,0,150,0,50])).values,{maxChargeMinutes:180,endVoltage:58.4,ccCurrent:15,derating110:50});
eq(p.decode(rx(0x18FF55E5,[0,100,0,10,1,1,2,38])).values,{derating220:100,endCurrent:1,blindCharge:1,rechargeVoltage:55});
eq(p.decode(rx(0x18FF56E5,[2,88,1,194,0,200,0,5])).values,{adjustCurrentMin:.5,adjustCurrentMax:20,adjustVoltageMin:45,adjustVoltageMax:60});
eq(p.decode(rx(0x18FF50E5,[2,72,0,150,4,0,0,0])).values,{outputVoltage:58.4,outputCurrent:15,statusByte:4});
eq(p.decode(rx(0x18FF52E5,[0,75,255,246,2,4,0,0])).values,{protectionTemperature:75,ambientTemperature:-10,faultWord:516});
eq(p.decode(rx(0x18FF51E5,[0,230,1,144,3,232,0,30])).values,{inputVoltage:230,pfcVoltage:400,maxPower:1000,workMinutes:30});
eq(p.decode(rx(0x1806E550,[0,23,0,0,0,0,0,0])).kind,'unknown'); // No invented save ACK.
for(const x of [NaN,Infinity,-1,1.23,'15',undefined])bad(()=>p.encodeConfig('ccCurrent',x));
bad(()=>p.encodeDuty('current',1000.1));bad(()=>p.encodeDuty('load',80));bad(()=>p.encodeConfig('bogus',1));bad(()=>p.encodeConfig('blindCharge',256));bad(()=>p.encodeConfig('ratedVoltage',3276.8));bad(()=>p.poll(0));bad(()=>p.poll(7));bad(()=>p.command('saveEverything'));
bad(()=>p.decode({...rx(0x18FF53E5,[0,0,0,0,0,0,0,0]),extended:false}));
bad(()=>p.decode({...rx(0x18FF53E5,[0,0,0,0,0,0,0,0]),remote:true}));
bad(()=>p.decode(rx(0x18FF53E5,[0,0,0,0,0,0,0])));
bad(()=>p.decode(rx(0x18FF53E5,[0,0,0,0,0,0,0,256])));
bad(()=>p.decode({...rx(0x18FF53E5,[0,0,0,0,0,0,0,0]),dlc:7}));
const complete=Object.fromEntries(Object.keys(p.CONFIG).map(k=>[k,1]));
eq(p.encodeAllConfig(complete).map(f=>f.data[1]),[16,17,14,15,13,12,1,2,3,4,5,6,7,8,9,10]);
bad(()=>p.encodeAllConfig({...complete,ccCurrent:undefined}));bad(()=>p.encodeAllConfig({...complete,unexpected:1}));
console.log('PASS: '+checks+' offline protocol checks. No hardware accessed.');
