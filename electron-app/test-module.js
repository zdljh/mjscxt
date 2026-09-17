
const path = require('path');
process.chdir(__dirname);
const e = require('electron');
console.log('=== Electron Module Test ===');
console.log('Type:', typeof e);
console.log('Is string:', typeof e === 'string');
if (typeof e === 'string') {
  console.log('Got PATH:', e);
  console.log('This means require(electron) returns the binary path, NOT the API');
  console.log('Electron runtime is NOT injecting the API object');
} else {
  console.log('Has app:', typeof e.app !== 'undefined');
  console.log('Has BrowserWindow:', typeof e.BrowserWindow !== 'undefined');
}
