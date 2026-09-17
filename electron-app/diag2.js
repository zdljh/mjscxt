
const electron = require('electron');
console.log('typeof:', typeof electron);
console.log('is string:', typeof electron === 'string');
if (typeof electron === 'string') {
  console.log('PATH:', electron);
} else {
  console.log('keys:', Object.keys(electron || {}));
}
