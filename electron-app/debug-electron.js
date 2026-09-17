
const e = require('electron');
console.log('Type:', typeof e);
console.log('Is string:', typeof e === 'string');
console.log('Has app property:', typeof e === 'object' && typeof e.app !== 'undefined');
if (typeof e === 'string') {
  console.log('Got path:', e);
}
