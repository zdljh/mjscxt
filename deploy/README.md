# 部署指南（P2-6 / P2-7）

三种部署形态共用同一份后端代码，差别只在「引擎地址」与「进程编排方式」。

---

## 一、本机开发态（默认，也最省事）

```bash
cd app
pip install -r requirements.txt
python app.py            # 或双击仓库根的 run_app.bat
```

- 引擎地址：`.env` 中的 `COMFYUI_URL`（默认 `http://127.0.0.1:8188`）
- 产物：`output/`、`novels/`（已被 `.gitignore` 排除，不会进版本库）

---

## 二、Docker 容器化后端（P2-7）

ComfyUI 依赖本机 GPU 与数十 GB 权重，**不放进镜像**；镜像只容器化 Flask 后端。

```bash
# 1) 准备密钥（容器通过 .env 注入，不要写进 compose）
cp .env.example .env
#    填 MJSCXT_SECRET_KEY；NVIDIA_API_KEY 等按需

# 2) 启动（自动连接宿主机的 ComfyUI）
docker compose up -d
docker compose logs -f backend

# 3) 访问
#    http://127.0.0.1:5000/
```

要点：

| 事项 | 说明 |
|------|------|
| 引擎连通 | Windows / macOS 用 `host.docker.internal`；Linux 已在 `extra_hosts` 配 `host-gateway` |
| 产物持久化 | `./output`、`./novels` 直接挂载，容器重建不丢数据 |
| 任务库 | `output/tasks.db`（SQLite）随 output 一起持久化，重启后自动 `recycle_interrupted()` |
| 健康检查 | `GET /api/tasks`，30s 间隔 |
| 镜像体积控制 | `.dockerignore` 排除产物 / 密钥 / 模型权重 / node_modules |

**扩容到多 GPU**：`docker-compose.yml` 里已预留 Redis 服务（注释状态）。
当前 SQLite + 线程串行队列在单机单 GPU 下足够；多机场景再启用 Redis 做分布式队列。

---

## 三、桌面端（P2-6）

Electron 外壳，自动编排 ComfyUI + 后端两个进程，退出时回收。

```bash
cd deploy/desktop
npm install
npm start          # 开发运行
npm run dist:win   # 打 Windows 安装包
npm run dist:mac   # 打 macOS 安装包
```

关键环境变量：

| 变量 | 作用 |
|------|------|
| `COMFYUI_ROOT` | ComfyUI 安装根目录（含 `main.py`）。留空则假定你已手动启动 ComfyUI |
| `COMFYUI_URL` | 引擎地址，默认 `http://127.0.0.1:8188` |
| `FLASK_RUN_PORT` | 后端端口，默认 `5000` |
| `MJSCXT_PYTHON` | 指定 Python 解释器；留空自动探测（`.venv` → 本机隔离环境 → PATH） |

行为说明：

- 启动顺序：探活 ComfyUI → （未在线且有 `COMFYUI_ROOT` 则拉起）→ 拉起后端 → 等端口就绪 → 开窗
- 后端已在线时**直接复用**，不会重复启动（方便与命令行并行调试）
- 关闭窗口：非 macOS 直接退出并回收子进程；macOS 保留应用（符合平台习惯）
- 渲染进程 `contextIsolation: true`，只通过 preload 暴露 `baseUrl` 等只读信息

**为什么不打包 Python 与模型权重**：体积（Python 环境 + 数十 GB 权重）与可维护性都不可接受，
且用户本机通常已为 ComfyUI 配好环境。安装包只带 `app/` 源码与 `locales/`。

---

## 四、部署档案（`DEPLOY_PROFILE`）

`.env` 中 `DEPLOY_PROFILE` 影响超分与视频生成的默认档位：

| 档案 | 适用 | 特点 |
|------|------|------|
| `8G` | 8G 显存笔记本 | 超分走 `tiny` 模式 + 强制 offload，速度优先 |
| `16G` | 16G 显存台式（默认） | 超分 `tiny-long`，视频段数适度 |
| `server` | 24G+ 服务器 | 超分 `full`，可并发更高档位 |

---

## 五、换机迁移检查表

1. `cp .env.example .env` 并填 `COMFYUI_ROOT` / `COMFYUI_URL` / `DEPLOY_PROFILE`
2. 装依赖：`pip install -r app/requirements.txt`（含 `cryptography`、`python-dotenv`）
3. 首次启动会**自动重建** `output/secrets.enc` 目录结构；原有明文密钥若随 json 一起拷过来，
   首次读取时会自动迁移进加密库
4. **主密钥不要跟着代码走**：`MJSCXT_SECRET_KEY` 变了，旧加密库存的密钥就解不开，需重新配置
5. ComfyUI 侧确认已装：Qwen 2512 / Qwen Edit 2511 / MiniMax H3 / FlashVSR / Qwen-TTS 相关节点与权重
