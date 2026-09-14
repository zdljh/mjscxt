/**
 * 桌面端 preload：只暴露「后端基地址 + 运行环境信息」，
 * 不向渲染进程开放任何 Node/文件系统能力（contextIsolation 已开启）。
 */
const { contextBridge } = require('electron');

const PORT = parseInt(process.env.FLASK_RUN_PORT || '5000', 10);

contextBridge.exposeInMainWorld('MJSCXT_DESKTOP', {
  isDesktop: true,
  baseUrl: `http://127.0.0.1:${PORT}`,
  comfyuiUrl: process.env.COMFYUI_URL || 'http://127.0.0.1:8188',
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
});
