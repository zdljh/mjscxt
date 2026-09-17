
// Force clear require cache and re-test
delete require.cache[require.resolve('electron')]
const electron = require('electron');
console.log('typeof:', typeof electron);
console.log('is string:', typeof electron === 'string');
if (typeof electron === 'string') {
  console.log('PATH:', electron.substring(0, 100));
} else if (electron) {
  console.log('keys:', Object.keys(electron));
}
// Check process
console.log('process.versions.electron:', process.versions.electron);
