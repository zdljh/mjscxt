// Electron wrapper test
const path = require('path');
const { spawn } = require('child_process');

const cwd = 'C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统/electron-app';
const electronExe = path.join(cwd, 'node_modules', 'electron', 'dist', 'electron.exe');

console.log('CWD:', cwd);
console.log('Exe:', electronExe);
console.log('Exists:', fs.existsSync(electronExe));
