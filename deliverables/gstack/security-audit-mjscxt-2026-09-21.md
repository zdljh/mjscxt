# 漫剧生成系统（mjscxt）上线前安全审计报告

- 审计模式：Comprehensive（全量，上线前）
- 审计日：2026-09-21
- 审计对象：`C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统`（GitHub zdljh/mjscxt，HEAD 295933d）
- 技术栈：Python 3.13/3.14 + Flask（`app/app.py`，监听 5000）+ waitress + FFmpeg 子进程 + ComfyUI（本机 8188）+ LLM 质检（外部 endpoint，密钥走加密库）
- 审计方式：纯本地静态审查 + 落盘产物核查，未修改任何生产代码，未对运行服务做主动渗透
- 执行方式：未调用任何外部 LLM API；shell 命令未使用通配符；仅报告可复现/可定位的发现，无臆测项

---

## TL;DR（3–5 行）

1. **系统默认是「本机优先」设计**：Flask 默认监听 `127.0.0.1`（`serve.py`/`main.py`/`run_app.bat` 均为 loopback），FFmpeg 全量 list 形式调用（零 `shell=True`），agent 工具白名单 + 进程内直连（零 `eval`/`exec`/`os.system`）。**本地单机场景下核心注入/命令执行面基本干净。**
2. **但整条安全边界完全建立在「只监听 loopback」这一单点假设上**：`APP_HOST` 可被环境变量覆盖为 `0.0.0.0`，且 `CORS(app)` 无 origin 限制、**全应用零鉴权层**。一旦监听面外扩（多用户机器 / Docker 发布 / 团队共网），删除项目、写文件、agent 工具、autopilot 启停等**破坏性 API 全裸暴露**，无访问控制。
3. **角色/关系端点（`/api/characters`、`/api/relations`）用裸 `project` 参数直接拼路径写盘**，绕过了系统其它端点统一使用的 `_safe_project` 收口 —— 这是**已存在的未净化入参写盘原语**，在监听面外扩时即路径穿越写。
4. **磁盘上残留一个明文 API key**：`qc_config.backup_20260914.json` 内含真实 `sk-vZx9...`（已被 gitignore，不在仓库，但机器上仍是活凭据，且文件为默认宽松 ACL）。`MJSCXT_SECRET_KEY`（加密库主密钥）也明文躺在 `.env`，一旦 `.env`+`.secret_key`+`secrets.enc` 三者齐遭外泄，整个密钥库可被解密。
5. **任务队列无界**：`TaskQueue.submit` 直接 append 普通 list，无上限/背压，每个任务都是数十分钟 GPU 渲染 → 可被用于内存/GPU 资源耗尽型 DoS。

**结论：默认本机部署可上线（带下述 🟠 项的运维护栏）；任何「网络可达 / 多人可达」部署为 No-Go，直到补齐鉴权 + 监听面收口。**

---

## 威胁建模（STRIDE 表）

| 威胁类别 | 攻击面 / 场景 | 现有缓解 | 残余风险 | 评级 |
|---|---|---|---|---|
| **S**poofing（伪造身份） | 全应用无鉴权，任何可达调用方都被当作「本机操作者」。若监听面外扩，任意图形界面可执行破坏性操作 | 默认 loopback 绑定 + 无共享凭据模型（单机单用户假设） | 监听面外扩即身份伪造，无会话/令牌 | 🟠 |
| **T**ampering（篡改） | ① 角色/关系端点裸 `project` 拼路径写盘，可写 `output/<attacker>`；② `index.json`/项目配置在 `output/` 下默认宽 ACL，本账号可读改 | `safe_key`/`_safe_project` 收口覆盖多数端点；原子写 + RLock 防交错；删除只移回收站不物理删 | 角色/关系端点未纳入收口；`output/` ACL 宽松，篡改产物/索引后无完整性校验 | 🟠/🟡 |
| **R**epudiation（抵赖） | 删除项目「可恢复」到 `output/projects/_trash/`，任何能读 `output/` 的本机账号可悄悄还原「已删除」数据；agent 审计日志（`_audit`）落盘但无防篡改 | agent 工具有审计痕迹（md5 去重 + 日志） | 产物/索引/回收站均无签名或审计链，抵赖可被本地账号达成 | 🟡 |
| **I**nformation Disclosure（信息泄露） | ① 明文 API key 残留（`qc_config.backup_*.json`）；② `.env` 含 `MJSCXT_SECRET_KEY` 明文；③ 仓库 git 跟踪 `test_page.html`、`h3_ref_submit.py`、历史审计报告等；④ API 响应回显 `disk_paths` 绝对路径 | 密钥加密库（Fernet）+ 对外脱敏 `api_key_masked`；密钥 json 已 gitignore；`_safe_upload_name`/`_serve_safe` 防读穿越 | 磁盘残留明文 key + 主密钥明文 + 交付面含调试 HTML/日志 | 🟠 |
| **D**enial of Service（拒绝服务） | ① 任务队列无上限无背压，可塞入大量数十分钟 GPU 任务；② 生成/质检重试退避（token ladder、抽帧）放大单任务时长；③ 无 per-source 限流 | 队列并发默认 1（串行），`_await_task` 有 25min 上限，FFmpeg 有 `PROBE_TIMEOUT`/`RENDER_TIMEOUT` | 队列深度无界 + 无鉴权属发源 = 可稳定打满 GPU/内存 | 🟡 |
| **E**levation of Privilege（提权） | agent 工具端点能否触发任意命令？—— **已确认不能**：工具白名单 + `Flask.test_client()` 进程内直连，无 `eval`/`exec`/`os.system`/`subprocess(shell=True)`；项目钉死防跨项目越权 | `execute_tool` 走 `TOOL_MAP` 白名单，`args` 钉回会话项目，急停开关 + 昂贵动作上限 | 无命令执行原语；若上述写盘穿越（角色端点）被利用，可写 `output/` 下任意子路径但**不能**写代码到执行路径 | 🟢（低） |

**关键判断**：本系统**不存在远程代码执行（RCE）原语**（FFmpeg list 调用 + agent 白名单 + 无 eval/exec 是强控制）。真正的风险是**未鉴权的破坏性 API 在监听面外扩时被误用**，以及**磁盘上残留明文密钥**。

---

## OWASP Top 10 检查表（逐条）

| 类别 | 检查项 | 结论 | 证据 / 备注 |
|---|---|---|---|
| **A01 访问控制失效** | 各 API 端点鉴权；删除/写盘/agent/autopilot 破坏性端点 | 🔴 **失效**（条件性） | 全应用无鉴权层；`/api/projects/<pid>/delete`（需 `confirm=true` 二次确认）/agent/autopilot/角色写盘 均无身份校验。风险等级取决于监听面：默认 `127.0.0.1` 时降级为本机单用户信任；外扩即完全裸奔。见 F-02。 |
| **A02 加密失败** | 密钥/传输 | 🟡 | LLM/QC 调用走 `Authorization: Bearer`；密钥本地用 Fernet 加密落 `output/secrets.enc`（0o600，正确）。**但** `.env` 里 `MJSCXT_SECRET_KEY` 与 `qc_config.backup_*.json` 里 API key 为明文。传输侧：本地回环 + 可选本地 LLM，TLS 非强制（可接受）。 |
| **A03 注入** | 命令/路径/SQL 注入 | 🟡 **局部** | ① FFmpeg 全部 `[exe, ...]` list 形式、数值强转 `:.4f`/`int`、**零 `shell=True`** → 命令注入面干净。② **路径穿越**：多数端点经 `_safe_project`/`safe_key`/`_serve_safe`（`abspath` 前缀校验）收口，正确；**但角色/关系端点（`9456–9880`）用裸 `project` 拼 `os.path.join(PROJECT_OUTPUT_DIR, project_name)` 写盘，未收口** → 见 F-01。③ 无 ORM/裸 SQL 拼接（SQLite 走 task_store，入参走 json）。 |
| **A04 不安全设计** | 业务逻辑/限流/队列 | 🟡 | 任务队列无上限无背压（F-05）；无 per-source 限流；删除「可恢复」设计无防篡改。删除走回收站是合理设计（非缺陷）。 |
| **A05 安全配置错误** | 监听面/默认凭据/错误泄露 | 🟡 | 默认 loopback（好）；`APP_HOST` 可被 env 覆盖为 `0.0.0.0`（坏点）；`CORS(app)` 无 origin 限制；Flask debug 默认关（`APP_DEBUG=0`）。`_call_api` 异常会把 `traceback.format_exc()[-400:]` 放进 agent 工具结果（可控度低）。 |
| **A06 易受攻击/过时组件** | 依赖 CVE | 🟡（需人工跟进） | 已核对 `requirements.txt`（flask / flask-cors / waitress / requests / cryptography / Pillow 等）。**本审计未做逐包 CVE 库比对**（无外部数据库访问约束）；上线前建议跑一次 `pip audit` / `safety`。`cryptography` 缺失会静默降级密钥库为「仅环境变量」，需在部署清单里固化安装。 |
| **A07 身份认证失效** | 凭据/会话 | 🟢（设计使然） | 单机单用户、无账号体系，符合本地工具定位。缺的不是「密码哈希」而是「访问控制」（归 A01）。 |
| **A08 软件/数据完整性失败** | 未签名代码/数据 | 🟡 | `output/projects/index.json` 为项目索引唯一事实来源，但 `.gitignore` 第 22 行 `!output/projects/index.json` 因父目录 `output/` 整体被忽略而**是无效否定**（已验证 `git check-ignore` 命中第 21 行）→ 索引实际**不在版本控制内**，可被静默篡改且无校验（F-09）。 |
| **A09 安全日志/监控失效** | 审计/告警 | 🟡 | agent 工具 `_audit` 有 md5 去重 + 落盘审计；FFmpeg/质检失败有 `app.logger` 打点。但**无鉴权失败/越权/异常入参的统一安全事件日志**（因为没有鉴权层），也**无防篡改日志存储**。 |
| **A10 服务端请求伪造（SSRF）** | 外部 endpoint 出站 | 🟢 **已缓解** | LLM/QC `base_url` 允许用户填，但 `llm_client._is_local` / `qc_client` / `sfx_isolate` 均实现 `_is_local` 门（仅放行 `127.0.0.1/localhost/0.0.0.0/::.1/.local` 时走本地快路径，否则走受限出站）。出站目标为 LLM 服务商 base_url，非云元数据服务，SSRF 风险低。 |

> 说明：本项是本地单机工具，A01/A07 的「失效」在 **loopback 假设成立**时降级为可接受的信任边界；核心上线决策应围绕「监听面是否会外扩」。

---

## 发现清单（按严重度分级）

> 严重度定义：🔴 需立即修复/阻断上线；🟠 上线前应处理或加运维护栏；🟡 应排期处理；🟢 观察/良好实践记录。

---

### 🟠 F-01 角色/关系端点：未净化 `project` 入参直接拼路径写盘（路径穿越写原语）
- **位置**：`app/app.py` `9456–9880`（`/api/characters`、`/api/relations*` 全部端点）+ `app/character_manager.py` / `app/relation_manager.py`
- **问题**：这些端点直接 `project_name = request.args.get('project')` / `data.get('project')`，随后
  `project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)`，再由 `CharacterManager(project_name, project_dir)` 执行 `self.character_dir.mkdir(parents=True)` 与 `json.dump`/`file.save(filepath)`。
  对比同一文件内其它端点（如 `9405/9418/9428/9449`）都用了 `_safe_project(...)` 收口，**唯独这组角色/关系端点漏掉了**。
- **攻击路径**：`project = ../../evil` → `os.path.join(PROJECT_OUTPUT_DIR, '../../evil')` → `mkdir(parents=True)` 在 `output/` 之外创建目录并写入 `characters.json`、参考图等。在默认 `127.0.0.1` 单机场景为「本机自己写自己」，**危害有限**；但一旦监听面外扩，即成为未鉴权的路径穿越写盘。
- **建议**：把这组端点的 `project_name` 统一改走 `_project_or_400(request.args.get('project') ...)` / `_safe_project(...)`；或在 `CharacterManager.__init__` 内对 `base_dir` 做 `abspath` 前缀校验（确认落在 `PROJECT_OUTPUT_DIR` 内，越界即 400）。
- **优先级**：P1（上线前，与 F-02 一起处理；若确认仅 loopback 单机，可 P2）

### 🟠 F-02 零鉴权 + 无 origin 限制的 CORS：整条安全边界押在「仅 loopback」单点假设上
- **位置**：`app/app.py:108` `CORS(app)`；`serve.py:90` / `main.py:40` 默认 `127.0.0.1`；`.env` / `.env.example` `APP_HOST`
- **问题**：全应用无身份认证/授权层，且 `CORS(app)` 未传 `origins` 白名单（默认允许所有来源 + 反射）。破坏性端点（删除项目、写角色/关系、agent 工具、autopilot 启停/急停、上传、质检开关）全部对「任意可达方」开放。
- **攻击路径**：`APP_HOST=0.0.0.0`（env 可覆盖）或 Docker 发布后，局域网内任何主机可访问 `http://<ip>:5000`，无凭据即可 `POST /api/projects/<id>/delete`、跑 agent、起 autopilot。
- **建议**（按部署形态择一）：
  1. **保持纯本机**：在 `serve.py` 里**硬校验**——若解析出的 host 不是回环（`127.0.0.1`/`::1`/`localhost`）则拒绝启动或打强告警，杜绝 `APP_HOST=0.0.0.0` 误设。
  2. **需被网络访问**：引入最小鉴权（至少一个共享 token / Basic Auth 中间件）+ `CORS(app, origins=<白名单>)` + 限流。
  3. 上线前**在部署文档里显式声明**「本服务必须仅监听回环」。
- **优先级**：P1（决定 Go/No-Go 的核心项）

### 🟠 F-03 磁盘残留明文 API key（`qc_config.backup_20260914.json`）
- **位置**：仓库根 `qc_config.backup_20260914.json`（已被 `.gitignore` 第 105 行 `qc_config.backup_*.json` 忽略，**不在 git 仓库内**，但存在于本机磁盘）
- **问题**：该备份含真实 `api_key`（`sk-vZx9...`，已在审计中读到，**本报告不全文复述**）。P0-3 密钥加固已把 `llm_config.json`/`qc_config.json`/`ai_config.json` 的 `api_key` 字段清空（已逐一核验为 EMPTY），但这份**备份文件未随迁移清理**，仍是活凭据。文件为默认宽 ACL，同机其它账号/进程可读。
- **攻击路径**：任意能读该目录的本机账号/进程 → 拿到该外部模型 key → 直接计费/滥用。
- **建议**：删除 `qc_config.backup_20260914.json`（或其 `api_key` 字段），并**轮换该 key**（视为已泄露）；在部署清单加一条「备份文件不得携带明文 key」。
- **优先级**：P1（凭据泄露面，建议上线前完成）

### 🟠 F-04 加密库主密钥（`MJSCXT_SECRET_KEY`）明文落在 `.env`，且 `.env` ACL 偏宽
- **位置**：`.env`（gitignore，本地）；`secret_store.get_or_create_secret_key` 优先级 env > `.secret_key` 文件
- **问题**：`MJSCXT_SECRET_KEY` 是 Fernet 主密钥，明文存于 `.env`。`.env` 的 ACL 显示 `羡进\CodexSandboxUsers:(I)(RX)`（某沙箱用户组可读写继承）。一旦 `.env` + `.secret_key` + `output/secrets.enc` 三者齐遭同机他账号/沙箱外泄，整个加密密钥库可被完整解密。
- **建议**：① 收紧 `.env`/`.secret_key`/`secrets.enc` 的 Windows ACL 到**当前用户仅**（`icacls` 去除沙箱组权限）；② 部署文档明确「主密钥与加密库同机同权限即可互破，务必最小授权」。
- **优先级**：P2（多用户机器上 P1）

### 🟡 F-05 任务队列无界（DoS 资源耗尽面）
- **位置**：`app/task_store.py` `TaskQueue.submit`（`self._q.append(...)`，`_q` 为普通 list）
- **问题**：`submit` 无长度上限、无背压、无 per-source 配额；每个队列项是数十分钟 GPU 渲染。监听面外扩（配合 F-02）后，可被批量塞满队列 → 内存增长 + GPU 长期被占用（拒绝服务）。
- **建议**：给 `self._q` 设 `MAX_QUEUED`（超出即 429/丢弃并告警）；或在无鉴权部署下对「提交任务」类端点做来源限流。
- **优先级**：P2（与 F-02 联动；纯 loopback 时低风险）

### 🟡 F-06 交付面残留调试/测试产物与宽 ACL
- **位置**：git 跟踪的 `test_page.html`、`h3_ref_submit.py`；`app/templates/index_old.html`、`index_v2.html`；未跟踪但落盘的 `serve_restart3.log`（80KB 运行日志）、`nul`（Git Bash 误建）、`electron-test.js`、历史审计报告
- **问题**：`test_page.html` 会随前端打包/静态目录被交付；`serve_restart3.log` 可能含运行期敏感路径/错误堆栈。非直接漏洞，但属「交付面卫生」问题，增大攻击者侦察面。
- **建议**：上线前从仓库/静态目录剔除 `test_page.html`、`index_old.html`、`index_v2.html`、`nul`、`*.log`；`.dockerignore` 已有，但本地交付物也要清。
- **优先级**：P3

### 🟡 F-07 删除仅「回收站」可恢复，无防篡改/无本地账号隔离
- **位置**：`app/project_store.py` `delete_project` → `PROJECT_TRASH_DIR`（`output/projects/_trash/`）
- **问题**：删除不物理删除，任何能读 `output/` 的本机账号可手工 `shutil.move` 还原「已删除」数据（抵赖/篡改面）。`output/` 默认宽 ACL 放大了这一点。对单机单用户可接受。
- **建议**：多用户机器上给 `output/` 设当前用户 ACL；如需「不可恢复删除」语义，补一个 `purge` 物理删除 + 二次确认端点（明确与 `delete` 区分）。
- **优先级**：P3（单机）/ P2（多用户）

### 🟡 F-08 Docker 部署端口/挂载脚坑（非漏洞，属配置正确性）
- **位置**：`docker-compose.yml`（`ports: "5000:5000"`）+ `Dockerfile`（`CMD python app/serve.py`，容器内 host 默认 `127.0.0.1`）+ `volumes: ./app/config:/app/app/config`
- **问题**：① 容器内 app 监听 `127.0.0.1`（容器回环），而 compose 把 `5000:5000` 发布到宿主机（默认 0.0.0.0）——宿主机转发到的容器 5000 因应用只绑容器回环而**连不上**（功能 bug，非漏洞）；② `5000:5000` 未写 `127.0.0.1:5000:5000`，一旦绑定成功即对外 0.0.0.0；③ 挂载的 `./app/config` 本地多不存在（config 是模块非目录），可能挂空。
- **建议**：若走容器化，`APP_HOST=0.0.0.0`（容器内绑全接口）+ 宿主机 `127.0.0.1:5000:5000` 收口；去掉无效挂载或改挂真实路径。
- **优先级**：P2（仅当采用 Docker 部署时）

### 🟡 F-09 项目索引 `index.json` 实际不在版本控制内（gitignore 否定失效）
- **位置**：`.gitignore:21 output/` 与 `:22 !output/projects/index.json`
- **问题**：已验证 `git check-ignore -v output/projects/index.json` 命中第 21 行 `output/`，否定规则因父目录被整体忽略而**无效**。而代码注释把 `index.json` 定位为「项目列表唯一事实来源」。结果：索引既不可回溯又无完整性校验（配合 F-07 宽 ACL），是数据完整性薄弱点。
- **建议**：若确实要版本化索引，改为 `!output/` + 选择性忽略，或在备份脚本里单独 `git add -f output/projects/index.json`；否则删除这条失效否定并改文档。
- **优先级**：P3

### 🟢 F-10 良好实践记录（勿误报）
- **FFmpeg 注入面干净**：`audio_qc.py` / `dub_mix.py` / `qc_client.py` / `nle_export.py` / `keyframe.py` / `video_postprocess.py` 全部 `[exe, arg, ...]` list 形式，**零 `shell=True`/`os.system`**，数值参数强转（`:.4f`、`int`），文件名/项目名进入命令亦无法注入 shell。
- **agent 工具无 RCE 原语**：`agent_core.execute_tool` 走 `TOOL_MAP` 白名单 + `Flask.test_client()` 进程内直连，`args` 钉回会话项目，无 `eval`/`exec`/`subprocess`；急停 + 昂贵动作上限 + md5 冷却。
- **路径穿越读防护到位**：`_serve_safe`/`_serve_attachment` 用 `abspath` + 前缀校验，杜绝 `../` 与绝对路径穿越；`_safe_upload_name` 去路径化 + 清洗非法字符；`safe_key` 对项目名做 `isalnum/_-` 白名单。
- **密钥加密库设计正确**：`secret_store` 用 Fernet（0o600、gitignore、env>文件优先级、迁移清空明文），对外仅回显 `api_key_masked`。
- **本地回环默认值正确**：`serve.py`/`main.py`/`run_app.bat`/`restart_flask.py` 均默认 `127.0.0.1`。
- **SSRF 出站受限**：LLM/QC/sfx 的 `_is_local` 门控，出站目标非云元数据服务。
- **已知机制确认（非缺陷）**：`qc_config.json`/`ai_config.json` 已 gitignore 不入库；ComfyUI 8188 为本地服务；「按 PID 不按镜像名」杀进程属运维约定。

---

## 上线安全 Go / No-Go 建议

| 部署形态 | 结论 | 前置条件 |
|---|---|---|
| **纯本机单用户**（默认 `127.0.0.1`，无共享网络） | ✅ **Go（带护栏）** | ① 修 F-01（角色端点收口）或至少确认不开放网络；② 删/轮换 F-03 残留明文 key；③ 收紧 F-04 `.env`/密钥 ACL；④ 清 F-06 交付面垃圾。F-05/F-07/F-08/F-09 可列 P3 排期。 |
| **网络可达 / 多用户机器 / Docker 发布** | ⛔ **No-Go** | 必须：① 加鉴权（F-02）+ CORS 白名单 + 限流；② F-01 收口；③ F-05 队列有界；④ F-03/F-04 凭据最小授权；⑤ 跑 `pip audit`（A06）。完成前不建议网络可达。 |

**最低可上线动作（3 件，1 小时内可落地）**
1. 删 `qc_config.backup_20260914.json` 并**轮换其中 API key**（F-03，凭据已泄露面）。
2. 角色/关系端点 `project` 入参统一改走 `_safe_project`/`_project_or_400`（F-01，消除已有写盘穿越原语）。
3. 在 `serve.py` 对非回环 `APP_HOST` 做启动校验/强告警（F-02 的单机护栏版本，防止误设 0.0.0.0）。

> 审计边界：本审计为**静态 + 落盘核查**，未对运行中服务做主动渗透，未逐包比对 CVE 库（受「不调外部 LLM/数据库」约束），未验证 Windows 多用户 ACL 的运行时实际效果。上线前建议补一次 `pip audit` 与（若走容器）Docker 端口/挂载实测。

---

## 严重度分布统计

| 级别 | 数量 | 编号 |
|---|---|---|
| 🔴 阻断 | 0 | —（无不可上线的硬阻断项） |
| 🟠 高（上线前应处理） | 4 | F-01, F-02, F-03, F-04 |
| 🟡 中（排期） | 5 | F-05, F-06, F-07, F-08, F-09 |
| 🟢 观察/良好 | 1 | F-10（良好实践记录） |
| **合计** | **10** | — |

**整体安全态势**：注入/命令执行面（A03 注入、FFmpeg、agent）在本地单机假设下**控制良好**；主要风险集中在**访问控制（A01，条件性）+ 磁盘凭据卫生（Information Disclosure）+ 资源耗尽（DoS）**。系统**无 RCE 原语**，上线风险可控 —— 纯本机部署「带护栏可 Go」，网络可达部署「No-Go 直到补鉴权」。
