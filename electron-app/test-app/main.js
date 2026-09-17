
const { app, BrowserWindow } = require('electron');
console.log('Type of app:', typeof app);
console.log('Has whenReady:', typeof app?.whenReady);
if (typeof app === 'string') {
  console.log('Got PATH:', app);
}
app.whenReady().then(() => {
  console.log('App ready!');
  const win = new BrowserWindow({ width: 800, height: 600 });
  win.loadURL('data:text/html,<h1>Hello</h1>');
});
