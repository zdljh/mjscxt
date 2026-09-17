
process.env.ELECTRON_ENABLE_STACK_DUMPING = '1';
const e = require('electron');
console.log('Type:', typeof e);
console.log('Is string:', typeof e === 'string');
console.log('Value:', typeof e === 'string' ? e.substring(0, 50) : Object.keys(e).slice(0, 5));
