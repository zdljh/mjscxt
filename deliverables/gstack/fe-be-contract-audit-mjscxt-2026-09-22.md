# 漫剧生成系统 · 前后端契约一致性排查报告

**日期**：2026-09-22
**场景**：前端↔后端连通性 / 一致性 / 逻辑吻合排查
**参与成员**：排障手（gstack-investigator，因会话回收被杀，主理人降级直跑并逐条复核）
**代码基线**：HEAD = origin/main = `9751a9c`（本地/远端零分叉）

---

## 📌 TL;DR
- 整体结论：🟢 **前后端连通性与一致性良好，无阻塞缺陷**。
- 端点存在性：前端 72 个去重端点全部在后端 185 条路由中匹配到，**0 个 404**。
- 方法匹配：0 个真实 405（脚本初报 18 条经人工核对全为解析假阳）。
- 响应字段一致性：逐条核对的高危端点全部吻合；`9751a9c` 刚修过的 `/ai/chat/history` 读错层级已确认修复保持。
- 发现 **1 条非阻塞隐患**：`autopilotApi.progress` 声明读 `projects`，后端返回 `items`（字段错位），但该方法前端零消费方，属死方法。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go（连通性/一致性通过） |
| 阻塞缺陷 | 0 |
| 非阻塞隐患 | 1（autopilot/progress 字段错位，死方法） |
| 静态产物 | 与源码同步（`9751a9c`），无陈旧 |

---

## 1. 排查方法

| 维度 | 手段 | 工具 |
|------|------|------|
| 端点存在性 + 方法 | 机械化两侧集合差（解析 `@app.route` 与前端 `request/fetch` 调用） | `.workbuddy/test/_out/contract_scan.py` |
| 响应字段一致性 | 对高危端点人工读两侧「前端读 d.X」vs「后端 `return jsonify` 顶层键」 | Read/Grep |
| 静态产物新鲜度 | `git log` 比对 `frontend/src` 与 `app/static` 最近提交 | git |

> 说明：机械扫描的「B 类方法不匹配」经人工核对**全部为脚本 bug 假阳**（跨行/多路由解析不全 → 误判后端无 POST）。本报告结论以**逐条人工核对后端真实 `methods=`** 为准。

---

## 2. 逐项证据

### 2.1 端点存在性（0 个 404）
- 前端去重后 72 个端点，`contract_scan.py` 匹配结果「未匹配到路由」= **空**。
- 双 `/api` 前缀：`client.ts:816` 的 `/api/api/ai/config` 仅出现在**注释**（描述已修复的历史 404），现行代码 `request('/ai/config')` 正确。

### 2.2 方法匹配（0 个真实 405）
脚本初报 18 条「前端 POST/DELETE 但后端 GET」，逐条核对后端真实定义：
| 端点 | 后端真实 methods（`app/app.py`） | 判定 |
|------|------|------|
| `/api/characters` | `['GET']` + 另一条 `['POST']`（两条路由） | ✅ 支持 |
| `/api/relations` | `['GET']` + 另一条 `['POST']` | ✅ 支持 |
| `/api/ai/chat/apply` | `['POST']`（:5405） | ✅ |
| `/api/autonomous/stop`/`resume` | `['POST']`（:9813/:9823） | ✅ |
| `/api/autopilot/enable|disable|pause|resume` | 均 `['POST']`（:9535 等） | ✅ |
| `/api/memory/record` | `['POST']` | ✅ |
| `/api/qc/audio` | `['POST']`（:6469） | ✅ |
| `/api/qc/config/clear`、`reset-endpoint`、`sync-from-ai`、`prompt` | 均 `['POST']` | ✅ |

**结论：0 个真实方法不匹配。**

### 2.3 响应字段一致性（最高危，`9751a9c` 同类）
| 端点 | 后端顶层返回键 | 前端读法 | 判定 |
|------|------|------|------|
| `/api/projects` GET | `{success,total,projects}` | `projectsApi.list` | ✅ |
| `/api/projects` POST | `{success,project,created}` | `create` | ✅ |
| `/api/projects/<pid>` GET | `{success,project}`（:598） | `.then(d=>d.project)`（client.ts:74） | ✅ |
| `/api/projects/<pid>/assets` | `{success,project,project_id,…,storyboards,videos,counts,…}`（:729+） | `assets?.counts?.characters`（Workbench:124） | ✅（counts 为顶层键） |
| `/api/status` | `{…,comfyui,…}`（:978+） | `data?.comfyui`（ServiceMonitor:26） | ✅ |
| `/api/ai/chat/history` | `{success,state:{messages}}` | `9751a9c` 已归一化读 `d.state?.messages`（client.ts:215-233） | ✅ 已修保持 |
| `/api/characters` | `{success,characters:{字典}}` | `toAssetArray` 归一化（client.ts:154-173） | ✅ |

**唯一字段错位（非阻塞）**：
- 前端 `autopilotApi.progress()`（client.ts:722）声明 `request<{success,projects:AutopilotProgress[]}>`，读 **`projects`**；
- 后端 `/api/autopilot/progress`（无参）实际返回 `{success,count,items}`（:9520-9523），顶层键是 **`items`**；
- 但全前端 `grep .projects\b`、`autopilotApi.progress()`、`.progress()` **均无消费方** → 死方法，不产生实际故障。

### 2.4 静态产物新鲜度
- `git log -1 -- frontend/src` = `git log -1 -- app/static` = **`9751a9c`**（同一提交）。
- `app/static/index.html` 引 `index-BgNV0SXf.js`，与 `app/static/assets/` 现存文件一致，`git status app/static/ frontend/` 干净 → **产物与源码同步，无陈旧**。

---

## 2.5 散落裸 fetch（绕过 client.ts 归一层）
| 位置 | 端点 | 字段读取 | 判定 |
|------|------|------|------|
| `ServiceMonitor.tsx:26` | `/api/status` | `data?.comfyui` | ✅ |
| `ProjectWorkbenchPage.tsx:55` | `/api/projects/<pid>/assets` | `setAssets(await r.json())` 后读 `assets?.counts` | ✅ |

---

## 2.6 真契约错位（死方法，非阻塞）

`contract_scan.py` 修好后 B 类残留 2 条**真「方法 ∉ 后端并集」**（非假阳），逐条 grep 消费方后**均为零调用方的死方法**，当前不产生故障。**已处置**：`3db062e` 直接把这两个方法从 `client.ts` 删除（后端无对应 DELETE 端点，留下会误导后续开发者以为「DELETE 单资源即可删」）；`relationsApi.delete` 后端有 DELETE 路由（`:10338`）有效，保留不动。

| 前端方法 | 前端写法 | 后端路由（`app/app.py`） | 消费方 | 判定 |
|---------|---------|--------------------------|--------|------|
| `projectsApi.delete`（原 client.ts:80） | `DELETE /api/projects/<pid>` | `:572 /api/projects/<path:pid>` 仅 `GET`（删除实际走 `deleteV2` 的 `:679 /delete` POST） | 全前端 grep 零调用（`projectsApi.delete\b` exit=1） | ✅ 已在 `3db062e` 删除（死方法） |
| `charactersApi.delete`（原 client.ts:182） | `DELETE /api/characters/<id>` | `:9967 /api/characters/<char_id>` 仅 `PUT`，**无 DELETE 路由** | 全前端 grep 零调用（exit=1） | ✅ 已在 `3db062e` 删除（后端亦无删除端点，`relationsApi.delete` 因有 DELETE 路由保留） |

> 处置：两个死方法已在 `3db062e` 从 `client.ts` 直接删除（纯前端、零运行时行为变化），并在删除处留注释说明「删除走 `deleteV2` / 角色删除需后端新增端点」。`relationsApi.delete` 有对应后端 DELETE 路由（`:10338`），有效，保留。

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 状态 |
|---|------|--------|--------|------|
| 1 | 修 `autopilotApi.progress` 字段错位：类型 `projects`→`items`（并补 `count`） | 前端 | P2 | ✅ 已修 `3e9bad2`，纯类型层改动，tsc/vite 通过、产物 hash 不变 |
| 2 | 修正 `contract_scan.py` 的 methods/路径解析 bug（归一化/query/模板变量/跨行 method），消除假阳 | 测试工具 | P3 | ✅ 已修（重写为段级匹配 + 三态 method 判据 + `fe_full` 两段式模板清理），18 条假阳收敛为 0 假阳 + 2 条真契约错位（均死方法，见 §2.6） |
| 3 | 保持基线（现 `bd641e6` = 9751a9c + 远端 add8804 + P2 修复合并），无需回滚 | — | — | ✅ |

---

## ⚠️ 待完善 / 已知局限
- 响应字段一致性为**抽样人工核对**（聚焦高危 + 全部直接取字段调用），未对 72 个端点逐一穷举。
- `autopilot/progress` 字段错位因零消费方暂不阻塞；若未来启用该面板会踩坑。
- 机械扫描脚本的方法解析仍有 bug，需修后再作为常规守卫复用。

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
