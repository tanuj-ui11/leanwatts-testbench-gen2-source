'use strict';
// Offline codec recovered from GBS_CAN上位机.exe. No CAN/USB/serial I/O exists here.
// Mappings describe this executable, not an independently verified charger specification.
const COMMAND_ID = 0x1806E550;
const CONFIG = Object.freeze({
  batteryType:       {selector: 0x01, scale: 1,  input: 'textBox1',  response: 0x18FF53E5, offset: 0},
  ratedVoltage:      {selector: 0x02, scale: 10, input: 'textBox2',  response: 0x18FF53E5, offset: 2},
  startVoltage:      {selector: 0x03, scale: 10, input: 'textBox3',  response: 0x18FF53E5, offset: 4},
  temperatureLimit:  {selector: 0x04, scale: 1,  input: 'textBox4',  response: 0x18FF53E5, offset: 6},
  maxChargeMinutes:  {selector: 0x05, scale: 1,  input: 'textBox7',  response: 0x18FF54E5, offset: 0},
  endVoltage:        {selector: 0x06, scale: 10, input: 'textBox8',  response: 0x18FF54E5, offset: 2},
  ccCurrent:         {selector: 0x07, scale: 10, input: 'textBox6',  response: 0x18FF54E5, offset: 4},
  derating110:       {selector: 0x08, scale: 1,  input: 'textBox9',  response: 0x18FF54E5, offset: 6},
  derating220:       {selector: 0x09, scale: 1,  input: 'textBox10', response: 0x18FF55E5, offset: 0},
  endCurrent:        {selector: 0x0A, scale: 10, input: 'textBox5',  response: 0x18FF55E5, offset: 2},
  blindCharge:       {selector: 0x0C, scale: 1,  input: 'textBox71', response: 0x18FF55E5, offset: 5, width: 1},
  rechargeVoltage:   {selector: 0x0D, scale: 10, input: 'textBox73', response: 0x18FF55E5, offset: 6},
  adjustCurrentMin:  {selector: 0x0E, scale: 10, input: 'textBox82', response: 0x18FF56E5, offset: 6},
  adjustCurrentMax:  {selector: 0x0F, scale: 10, input: 'textBox78', response: 0x18FF56E5, offset: 4},
  adjustVoltageMin:  {selector: 0x10, scale: 10, input: 'textBox76', response: 0x18FF56E5, offset: 2},
  adjustVoltageMax:  {selector: 0x11, scale: 10, input: 'textBox80', response: 0x18FF56E5, offset: 0},
});
// timer1_Tick uses a descending counter, with nonsequential selectors.
const WRITE_ORDER = Object.freeze(['adjustVoltageMin','adjustVoltageMax','adjustCurrentMin','adjustCurrentMax','rechargeVoltage','blindCharge','batteryType','ratedVoltage','startVoltage','temperatureLimit','maxChargeMinutes','endVoltage','ccCurrent','derating110','derating220','endCurrent']);
function frame(data) { return {id: COMMAND_ID, extended: true, remote: false, dlc: data.length, data: Uint8Array.from(data)}; }
function scaled(value, scale, max = 32767) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new RangeError('A finite nonnegative number is required.');
  const n = value * scale, raw = Math.round(n);
  // Reject silent truncation; legacy application truncates floating point at conv.i4.
  if (Math.abs(n - raw) > 1e-7 || raw > max) throw new RangeError('Value is outside the codec range or precision.');
  return raw;
}
function word(selector, raw) { return frame([0, selector, (raw >>> 8) & 255, raw & 255]); }
function command(name) {
  switch(name) {
    case 'workStart': return word(0x14, 0);
    case 'shutdown': return word(0x14, 0x80);
    case 'calibrationStart': return word(0x14, 1);
    case 'saveVoltage': return frame([0, 0x17]);
    case 'saveFullCurrent': return frame([0, 0x18]);
    case 'saveHalfCurrent': return frame([0, 0x19]);
    default: throw new Error('Unknown command; no bytes generated.');
  }
}
function encodeConfig(name, value) {
  if (!Object.prototype.hasOwnProperty.call(CONFIG, name)) throw new Error('Unknown configuration field.');
  const d = CONFIG[name];
  // 32767 upper bound preserves signed legacy readback; this is NOT a hardware-safe operating range.
  return word(d.selector, scaled(value, d.scale, d.width === 1 ? 255 : 32767));
}
function encodeAllConfig(values) {
  if (!values || typeof values !== 'object') throw new TypeError('A complete variant is required.');
  for (const key of Object.keys(values)) if (!Object.prototype.hasOwnProperty.call(CONFIG,key)) throw new Error('Unknown variant field: '+key);
  // Validate all before returning any frame; no partial configuration on failure.
  return WRITE_ORDER.map(name => encodeConfig(name, values[name]));
}
function encodeDuty(axis, value) {
  if (axis !== 'voltage' && axis !== 'current') throw new Error('Unknown calibration axis.');
  // Legacy UI permits 0..1000.0; the actual safe range needs bench confirmation.
  return word(axis === 'voltage' ? 0x15 : 0x16, scaled(value, 10, 10000));
}
function poll(group) {
  if (!Number.isInteger(group) || group < 1 || group > 6) throw new RangeError('Poll group must be 1..6.');
  return word(0x0B, group);
}
function decode(frameIn) {
  if (!frameIn || frameIn.extended !== true || frameIn.remote === true) throw new Error('Expected an extended CAN data frame.');
  const {id,dlc,data} = frameIn;
  if (!Number.isInteger(id) || id<0 || id>0x1FFFFFFF || dlc!==8 || !data || data.length!==8 || Array.from(data).some(x=>!Number.isInteger(x)||x<0||x>255)) throw new Error('Expected a valid 8-byte classic CAN receive frame.');
  const u16 = o => (data[o]<<8)|data[o+1];
  const i16 = o => {const x=u16(o); return x>=32768?x-65536:x;};
  const params = {};
  for (const [name,d] of Object.entries(CONFIG)) if(d.response===id) params[name]=(d.width===1?data[d.offset]:i16(d.offset))/d.scale;
  if(Object.keys(params).length) return {kind:'configurationReadback', id, values:params};
  if(id===0x18FF50E5) return {kind:'telemetry', id, values:{outputVoltage:u16(0)/10,outputCurrent:u16(2)/10,statusByte:data[4]}};
  if(id===0x18FF51E5) return {kind:'telemetry', id, values:{inputVoltage:i16(0),pfcVoltage:i16(2),maxPower:i16(4),workMinutes:i16(6)}};
  if(id===0x18FF52E5) return {kind:'telemetry', id, values:{protectionTemperature:i16(0),ambientTemperature:i16(2),faultWord:u16(4)}};
  return {kind:'unknown',id};
}
module.exports = {COMMAND_ID,CONFIG,WRITE_ORDER,command,encodeConfig,encodeAllConfig,encodeDuty,poll,decode};
