// Electron preload脚本
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  getAppInfo: () => ipcRenderer.invoke('get-app-info'),
  openDialog: (options) => ipcRenderer.invoke('open-dialog', options),
  showMessage: (options) => ipcRenderer.invoke('show-message-box', options),
  onFlaskStatus: (callback) => {
    ipcRenderer.on('flask-status', (event, status) => callback(status));
  }
});
