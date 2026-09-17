// Electron 主进程 - 漫剧工坊桌面应用
const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let mainWindow;
let flaskProcess = null;
const PORT = 5000;
const HOST = '127.0.0.1';

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 768,
    frame: false,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 16 },
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js')
    }
  });

  // 加载Flask应用
  mainWindow.loadURL(`http://${HOST}:${PORT}`);

  // 开发模式打开DevTools
  if (process.env.ELECTRON_ENV === 'development') {
    mainWindow.webContents.openDevTools();
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function startFlask() {
  const flaskPath = path.join(__dirname, '..', 'app', 'serve.py');
  const pythonPath = process.env.PYTHON_PATH || 'python';

  flaskProcess = spawn(pythonPath, [flaskPath], {
    cwd: path.join(__dirname, '..'),
    stdio: 'ignore',
    detached: false
  });

  flaskProcess.on('error', (err) => {
    console.error('Failed to start Flask:', err);
    dialog.showErrorBox('启动失败', `无法启动Flask服务器: ${err.message}`);
    app.quit();
  });

  flaskProcess.on('exit', (code) => {
    if (code !== 0) {
      console.error('Flask exited with code', code);
    }
  });

  // 等待Flask启动
  setTimeout(() => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.reload();
    }
  }, 2000);
}

// 必须在 app.whenReady() 回调内使用 ipcMain
app.whenReady().then(() => {
  // IPC handlers
  ipcMain.handle('get-app-info', () => {
    return {
      version: app.getVersion(),
      platform: process.platform,
      pythonPath: process.env.PYTHON_PATH || 'python'
    };
  });

  ipcMain.handle('open-dialog', async (event, options) => {
    const result = await dialog.showOpenDialog(mainWindow, options);
    return result;
  });

  ipcMain.handle('show-message-box', async (event, options) => {
    return dialog.showMessageBox(mainWindow, options);
  });

  startFlask();
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (flaskProcess) {
    flaskProcess.kill();
  }
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('will-quit', () => {
  if (flaskProcess) {
    flaskProcess.kill();
  }
});
