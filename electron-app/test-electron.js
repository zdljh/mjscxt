
const { app, BrowserWindow } = require('electron');
console.log('Electron version:', process.versions.electron);
console.log('Node version:', process.versions.node);
console.log('Chrome version:', process.versions.chrome);
app.whenReady().then(() => {
  console.log('App ready! Creating window...');
  const win = new BrowserWindow({ width: 800, height: 600, show: false });
  win.loadURL('http://127.0.0.1:5000');
  setTimeout(() => {
    console.log('Window loaded, quitting...');
    app.quit();
  }, 5000);
});
