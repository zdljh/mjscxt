// Electron wrapper - fixed
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const appDir = 'C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统/electron-app';
const electronExe = 'C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统/electron-app/node_modules/electron/dist/electron.exe';

console.log('App dir:', appDir);
console.log('Electron:', electronExe);
console.log('Exe exists:', fs.existsSync(electronExe));

// Use absolute paths to avoid Git Bash conversion issues
const proc = spawn(electronExe, [appDir], {
  cwd: appDir,
  stdio: 'inherit',
  windowsHide: false,
  shell: false  // Don't use shell to avoid path mangling
});

proc.on('close', (code) => {
  console.log('Electron exited with code', code);
  process.exit(code || 0);
});

proc.on('error', (err) => {
  console.error('Failed to start Electron:', err.message);
  process.exit(1);
});
