
// Check if we're inside Electron by testing for process types
const isElectron = typeof process !== 'undefined' && process.versions && process.versions.electron;
console.log('Is Electron runtime:', isElectron);
console.log('Process versions:', process.versions);
if (isElectron) {
  console.log('Electron version:', process.versions.electron);
}
// Try to get electron API differently
let electronAPI = null;
try {
  // This is how Electron exposes its API internally
  electronAPI = process.electronBinding('electron');
} catch(e) {
  console.log('Cannot get binding:', e.message);
}
console.log('electronAPI:', typeof electronAPI, Object.keys(electronAPI || {}).slice(0, 5));
