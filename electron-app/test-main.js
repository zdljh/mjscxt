
// Test script matching main.js structure
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
console.log('typeof app:', typeof app);
console.log('app keys:', Object.keys(app || {}));

if (!app) {
  console.error('FAILED: app is undefined!');
  process.exit(1);
}

console.log('SUCCESS: app object available');
app.whenReady().then(() => {
  console.log('App ready!');
  const win = new BrowserWindow({ width: 800, height: 600, show: false });
  win.loadURL('http://127.0.0.1:5000');
  setTimeout(() => {
    console.log('Window loaded, quitting...');
    app.quit();
  }, 3000);
});
