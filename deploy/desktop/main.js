/**
 * 漫剧生成系统 · 桌面端主进程（P2-6）
 *
 * 职责：
 *   1) 按需拉起 ComfyUI（本机已有安装时）
 *   2) 拉起 Flask 后端并等待端口就绪
 *   3) 打开窗口加载后端页面；退出时回收子进程
 *
 * 环境变量（可在 .env 或系统环境配置）：
 *   COMFYUI_ROOT       ComfyUI 安装根目录（含 main.py）。留空则假定 ComfyUI 已由用户自行启动
 *   COMFYUI_URL        ComfyUI 地址，默认 http://127.0.0.1:8188
 *   FLASK_RUN_PORT     后端端口，默认 5000
 *   MJSCXT_PYTHON      指定 Python 解释器；留空自动探测（venv > python3 > python）
 *
 * 设计取舍：桌面壳不打包 Python 运行时与 GPU 权重（体积与可维护性），
 * 而是复用本机环境；打包发行时用 extraResources 带上 app/ 源码。
 */
const { app, BrowserWindow, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const IS_DEV = process.argv.includes('--dev');
const PORT = parseInt(process.env.FLASK_RUN_PORT || '5000', 10);
const COMFYUI_URL = process.env.COMFYUI_URL || 'http://127.0.0.1:8188';
const COMFYUI_ROOT = process.env.COMFYUI_ROOT || '';

let mainWindow = null;
let backendProc = null;
let comfyProc = null;

const log = (...args) => console.log('[mjscxt]', ...args);

/** 项目根目录：开发态 = 仓库根；打包态 = resources/ */
function projectRoot() {
  if (app.isPackaged) return process.resourcesPath;
  return path.resolve(__dirname, '..', '..');
}

function appDir() {
  return path.join(projectRoot(), 'app');
}

/** 探测可用 Python 解释器 */
function resolvePython() {
  if (process.env.MJSCXT_PYTHON && fs.existsSync(process.env.MJSCXT_PYTHON)) {
    return process.env.MJSCXT_PYTHON;
  }
  const candidates = [];
  const venvRel = ['.venv', 'venv'];
  for (const v of venvRel) {
    if (process.platform === 'win32') {
      candidates.push(path.join(projectRoot(), v, 'Scripts', 'python.exe'));
    } else {
      candidates.push(path.join(projectRoot(), v, 'bin', 'python3'));
    }
  }
  // 本机 WorkBuddy 隔离环境（开发机默认）
  if (process.platform === 'win32') {
    candidates.push('C:/Users/liujianghua/.workbuddy/binaries/python/envs/default/Scripts/python.exe');
  }
  candidates.push(process.platform === 'win32' ? 'python' : 'python3');
  for (const c of candidates) {
    try {
      if (c.includes('/') || c.includes('\\')) {
        if (fs.existsSync(c)) return c;
      } else {
        return c; // 走 PATH 探测
      }
    } catch (_) { /* ignore */ }
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

/** 端口探活：返回 true 表示后端已在监听 */
function probe(url, timeoutMs = 1200) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(true);
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(false); });
    req.on('error', () => resolve(false));
  });
}

async function waitFor(url, { tries = 60, intervalMs = 1000, label = 'service' } = {}) {
  for (let i = 0; i < tries; i++) {
    if (await probe(url)) { log(`${label} ready after ${i + 1}s`); return true; }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  log(`${label} NOT ready after ${tries}s`);
  return false;
}

/** 按需拉起 ComfyUI（已在线则不重复启动） */
async function ensureComfyUI() {
  if (await probe(`${COMFYUI_URL}/system_stats`)) {
    log('ComfyUI already online, skip spawn');
    return true;
  }
  if (!COMFYUI_ROOT) {
    log('COMFYUI_ROOT not set — assuming user starts ComfyUI manually');
    return false;
  }
  const main = path.join(COMFYUI_ROOT, 'main.py');
  if (!fs.existsSync(main)) {
    log(`ComfyUI main.py not found: ${main}`);
    return false;
  }
  const py = resolvePython();
  log(`spawning ComfyUI: ${py} ${main}`);
  comfyProc = spawn(py, ['main.py', '--listen', '127.0.0.1'], {
    cwd: COMFYUI_ROOT, stdio: 'inherit', env: { ...process.env },
  });
  comfyProc.on('exit', (code) => log(`ComfyUI exited: ${code}`));
  await waitFor(`${COMFYUI_URL}/system_stats`, { tries: 180, label: 'ComfyUI' });
  return true;
}

/** 拉起 Flask 后端 */
async function ensureBackend() {
  const probeUrl = `http://127.0.0.1:${PORT}/api/tasks`;
  if (await probe(probeUrl)) {
    log('backend already running, reuse it');
    return true;
  }
  const dir = appDir();
  if (!fs.existsSync(path.join(dir, 'app.py'))) {
    throw new Error(`找不到后端入口：${path.join(dir, 'app.py')}`);
  }
  const py = resolvePython();
  const bootstrap = [
    'import os',
    'from app import app',
    `app.run(host="127.0.0.1", port=${PORT}, threaded=True)`,
  ].join('; ');
  log(`spawning backend: ${py} -c "<bootstrap>" @ ${dir}`);
  backendProc = spawn(py, ['-u', '-c', bootstrap], {
    cwd: dir,
    stdio: 'inherit',
    env: {
      ...process.env,
      PYTHONPATH: dir,
      COMFYUI_URL,
      FLASK_RUN_PORT: String(PORT),
    },
  });
  backendProc.on('exit', (code) => log(`backend exited: ${code}`));
  const ok = await waitFor(probeUrl, { tries: 90, label: 'backend' });
  if (!ok) throw new Error('后端启动超时（90s）。请检查依赖是否已安装：pip install -r app/requirements.txt');
  return true;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 980,
    minWidth: 1100,
    minHeight: 720,
    title: '漫剧生成系统',
    backgroundColor: '#0f1216',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.loadURL(`http://127.0.0.1:${PORT}/`);
  if (IS_DEV) mainWindow.webContents.openDevTools({ mode: 'detach' });
  // 外部链接走系统浏览器
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function killChild(proc, name) {
  if (!proc || proc.killed) return;
  log(`killing ${name} (pid ${proc.pid})`);
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(proc.pid), '/f', '/t']);
    } else {
      process.kill(-proc.pid, 'SIGTERM');
    }
  } catch (e) {
    log(`kill ${name} failed: ${e.message}`);
  }
}

app.whenReady().then(async () => {
  try {
    await ensureComfyUI();
    await ensureBackend();
  } catch (e) {
    dialog.showErrorBox('启动失败', String(e.message || e));
    app.quit();
    return;
  }
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

process.on('exit', () => {
  killChild(backendProc, 'backend');
  killChild(comfyProc, 'ComfyUI');
});

app.on('window-all-closed', () => {
  killChild(backendProc, 'backend');
  killChild(comfyProc, 'ComfyUI');
  if (process.platform !== 'darwin') app.quit();
});
