
const electron = require('electron');
console.log('typeof electron:', typeof electron);
console.log('electron type:', typeof electron);
if (typeof electron === 'string') {
  console.log('electron is PATH STRING:', electron);
} else if (electron && typeof electron === 'object') {
  console.log('electron is OBJECT with keys:', Object.keys(electron));
  console.log('app:', typeof electron.app);
} else {
  console.log('electron is:', electron);
}
console.log('process.versions.electron:', process.versions.electron);
