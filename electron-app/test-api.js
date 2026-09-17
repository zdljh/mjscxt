
// Check different ways to get electron API
console.log('=== Testing electron API access ===');
console.log('1. require("electron"):', typeof require('electron'));
console.log('   Value:', require('electron').substring ? require('electron').substring(0, 50) : require('electron'));

// Check if there's a global electron object
console.log('2. global.electron:', typeof global.electron);

// Check process binding
console.log('3. process.electronBinding:', typeof process.electronBinding);

// Try require with different paths
const paths = [
  'electron',
  './node_modules/electron',
  '../../node_modules/electron'
];
for (const p of paths) {
  try {
    const m = require(p);
    console.log('4. require("' + p + '"):', typeof m, typeof m === 'string' ? m.substring(0, 30) : '');
  } catch(e) {
    console.log('4. require("' + p + '"):', e.message);
  }
}

// Check if we can access through module
console.log('5. module:', typeof module);
console.log('   module.id:', module.id);
