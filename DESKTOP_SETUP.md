# 漫剧工坊 - 桌面应用部署指南

## 快速开始

### 方法一：Web模式（推荐）
```bash
cd "C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统"
python app/serve.py
# 访问 http://127.0.0.1:5000
```

### 方法二：双击启动
```bash
# Windows用户直接运行
双击运行 启动漫剧工坊.bat
```

### 方法三：Electron桌面应用
```bash
# 安装依赖
cd electron-app
npm install

# 开发模式运行
npm start

# 打包为EXE
npm run build:win
```

## 打包EXE

### PyInstaller方式（推荐）
```bash
pip install pyinstaller
pyinstaller 漫剧工坊.spec
# 输出: dist/漫剧工坊.exe
```

### Electron方式
```bash
cd electron-app
npm run build:win
# 输出: dist-electron/漫剧工坊 Setup 1.0.0.exe
```

## 功能特性

### 新增功能（对比分析报告改进后）
| 功能 | 状态 | 入口 |
|------|------|------|
| 角色四视图管理 | ✅ | 侧边栏「人物」图标 |
| 九宫格分镜 | ✅ | 侧边栏「网格」图标 |
| FCPXML/EDL导出 | ✅ | 侧边栏「导出」图标 |
| Docker部署 | ✅ | docker-compose.yml |
| EXE打包 | ✅ | 启动漫剧工坊.bat |

### 核心能力
- 🎬 全自动生产流水线
- 🤖 AI对话总控
- ⏰ 24小时无人值守
- 🔍 AI质检自动重试
- 📊 多项目并行管理

## 系统要求

- Windows 10/11
- Python 3.11+
- Node.js 18+ (Electron模式)
- 至少 8GB 内存
- NVIDIA GPU (推荐，用于ComfyUI)

## 依赖安装

```bash
pip install -r requirements.txt
pip install pyinstaller --upgrade
```

## 目录结构

```
漫剧生成系统/
├── app/                      # Flask后端
│   ├── app.py               # 主应用
│   ├── character_manager.py # 角色管理
│   ├── nine_grid_storyboard.py # 九宫格分镜
│   ├── export_manager.py    # 导出管理
│   ├── autonomous.py        # 全自动生产
│   └── templates/
│       └── index.html       # 前端界面
├── electron-app/            # Electron桌面应用
│   ├── main.js
│   ├── preload.js
│   └── package.json
├── novels/                  # 小说库
├── output/                  # 输出目录
├── main.py                  # 启动入口
├── build.bat                # 打包脚本
└── 漫剧工坊.spec            # PyInstaller配置
```

## 上线 / 交付运维清单（凭据与配置安全）

> 以下为**上线前必须逐条确认**的运维事项，属「F-03」上线闸门的一部分。

1. **备份文件不得携带明文 key。**
   禁止把 `qc_config.json` / `ai_config.json` / `llm_config.json` 等含 API key 的配置文件
   以 `*.backup_*`、`*.bak`、`*.old`、`*.orig` 等任何形式复制到仓库或交付包内
   （即便已被 `.gitignore` 忽略，**机器上的副本仍是活凭据**）。
2. **`qc_config.backup_20260914.json` 内的 API key 视为已泄露，需轮换。**
   该文件（位于仓库根目录）曾残留真实明文 key（形如 `sk-vZx9…`）。文件已删除，
   但 key 需在服务商控制台**立即作废并轮换**，并更新当前生效的 `qc_config.json`。
3. **密钥只以密文存在。** 生产配置请通过 `/api/qc/config` 写入（内部经 `secrets.enc`
   加密），不要把明文 key 直接落进仓库文件。
4. **交付/打包前自查：**

   ```bash
   # 列出所有可能含 key 的备份/副本文件（应为空）
   git ls-files | grep -Ei "backup|\.bak|\.old|\.orig" ; \
   ls -a | grep -Ei "backup|\.bak|\.old|\.orig"
   # 全仓库扫描明文 key 指纹（应无命中）
   grep -rEn "sk-[A-Za-z0-9]{8,}" --include=*.json --include=*.env . 2>/dev/null
   ```
5. **监听面保持回环。** 本应用默认 `APP_HOST=127.0.0.1` 且**无鉴权**；`serve.py` 已对
   非回环 host 做启动拦截（见 F-02 护栏）。网络部署前必须先补鉴权。
6. **凭据文件 ACL 收紧至当前用户最小授权（F-04）。**
   主密钥 `.secret_key`、加密密钥库 `output/secrets.enc`、`.env` 三者齐泄即可破整个
   密钥库（Fernet 主密钥泄露后 `secrets.enc` 的全部密文可被离线解出）。默认 Windows
   新建文件常继承宽泛组权限（如 `CodexSandboxUsers`、`SYSTEM`/`Administrators` 全权），
   须收紧到「仅当前交互用户读写、其它主体无权限」。在**管理员 PowerShell** 执行（路径
   换成实际仓库根目录）：

   ```powershell
   # 1) 清空三个凭据文件上的继承权限，只保留当前用户
   $here = "<仓库根目录>"
   foreach ($f in @(".secret_key", ".env", "output\secrets.enc")) {
       $p = Join-Path $here $f
       if (Test-Path $p) {
           # 去掉继承项（仅当前用户保留）
           & icacls $p /inheritance:r | Out-Null
           & icacls $p /grant:r "${env:USERNAME}:(R,W)" | Out-Null
           & icacls $p /remove:g "NT AUTHORITY\SYSTEM" "BUILTIN\Administrators" "CodexSandboxUsers" 2>$null | Out-Null
       }
   }
   # 2) 复检：除当前用户外不应再有 (R)/(F)/(W) 主体
   icacls "$here\.secret_key"; icacls "$here\.env"; icacls "$here\output\secrets.enc"
   ```

   > 注意：`.env`/`.secret_key`/`secrets.enc` 已 `.gitignore`，**本机副本仍是活凭据**，
   > 收紧 ACL 属必要但**非充分**——若曾以宽 ACL 暴露过，应视为已泄露，走第 2 条的
   > 密钥轮换流程。`icacls /inheritance:r` 会移除继承来源，故须在**确认当前用户仍被
   > 显式授权**后执行，避免把自己也锁在外面（执行前先 `icacls <file>` 记录原状）。
