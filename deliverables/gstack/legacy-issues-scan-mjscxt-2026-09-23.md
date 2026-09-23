# 漫剧生成系统 · 遗留问题排查报告

- **时间**：2026-09-23
- **基线**：`origin/main` = `c69cc72`（本地与远端零分叉，工作区干净）
- **方法**：先读项目记忆/历次审计报告的「待办清单」，再逐条到 `app/*.py`、`frontend/src` 源码核实当前状态，并跑防回潮守卫与静态检查交叉验证。
- **结论一句话**：历史审计的 P0/P1 已全部落地且无回潮；**仍有 4 条有真实影响的后端遗留 + 3 条静默吞异常残留 + 6 条前端 P2 欠账 + 3 条安全/产品观察项**。

---

## 一、⚠️ 仍存在的遗留问题

### A 类 · 数据一致性 / 幂等（后端，有真实影响）

| 编号 | 位置 | 问题 | 实际影响 | 级别 |
|---|---|---|---|---|
| **A1** | `app/ai_credentials_db.py:297` | `migrate_from_legacy()` 首行 `if has_credentials() and not force: return` —— 判据是「DB 里有没有**任何**凭证」，而不是「**该模块**是否缺失」 | DB 一旦存在任意一行（如只有 `text`），迁移**永不补齐**其他模块 → 迁移后写入旧槽的密钥永久进不了 DB。09-23 的「密钥可写不可读」只被读侧回落兜住，**根因未除** | P1 |
| **A2** | `app/app.py:6566`（`/api/qc/config` POST） | 只调 `qc_client.save_config(...)`（写旧加密库），**不回写** `tasks.db` | 与 P0-5「DB 单点凭证」的目标不一致：qc 模块写/读两条路径不对称，靠读侧回落兜住，属同一根因的残留 | P1 |
| **A3** | `app/pipeline.py:463 _deliverable_review` | 裸 `open(...) + json.load`，异常出口 `except Exception: return {}`（fail-open） | `deliverables.json` 损坏时被判成「没有 review 记录」→ `probe_final` 里 `review == "rejected"` 判定失效 → **被打回的成片会被当成已完成、不再重做**（同函数族的 `_reset_deliverable_review` 已迁 `read_json_strict`，只有这个只读口漏了） | P1 |
| **A4** | `app/script_prompt_analyzer.py:315 save_script_inplace` | 裸 `open(...,"w") + json.dump`，非原子写 | 覆盖式写回 caller 传入的 `script_path`；崩溃/并发时可能写坏剧本文件。`app/fs_atomic.atomic_write_json` 已就位但此处未采用（D-03 同族残留） | P2 |

### B 类 · 静默吞异常（守卫已报红，需与守卫口径对齐）

| 编号 | 位置 | 问题 |
|---|---|---|
| **B1** | `app/ai_credentials_db.py:351` | `except Exception: pass`（读旧 `ai_config` 端点失败时静默）—— `verify_silent_except` §1.2 判为「彻底静默」 |
| **B2** | `app/app.py:5400` | `except Exception: pass`（`ai_selfcheck.reset_probe_cache()` 失败静默）—— §1.2 同上 |
| **B3** | `app/ai_credentials_db.py:71` | `except Exception:` + `logger.debug(...)` **未绑定异常对象**（`as e`）→ 日志带不出真实原因 —— §4.2 报红 |

> 影响评估：三条都属「可降级、不阻断」的正确方向，但**口径与既有守卫不一致**（守卫要求「有日志的 except 必须 `as e`」+「剩余静默点必须是日志自身兜底」）。属轻微，但会让守卫长期红，掩盖未来的真回归。

### C 类 · 前端（P2 优化欠账，均未做）

| 编号 | 位置 | 问题 |
|---|---|---|
| **C1** | `frontend/src/pages/OverviewPage.tsx:36-39` | 主加载 `Promise.all([...]).finally(...)` **无 `.catch`** → 请求失败静默落到空态，用户看不到错误、也没有重试入口 |
| **C2** | `frontend/src/pages/MemoryPage.tsx:92-98` | 同上（主加载无 `.catch`） |
| **C3** | `frontend/src/pages/MemoryPage.tsx:135-140` | 质检教训库 `.catch` 把失败吞成 `lessons: []` → 用户看到「暂无质检教训」而非「加载失败」 |
| **C4** | `frontend/src/components/AudioTab.tsx` | 4 个 loader 是 fire-and-forget，**无首屏 loading 态**（只有操作级 `qcLoading`） |
| **C5** | 全库 | **P2-15 语义化表格**未做（全库 0 个 `<table>`，列表仍 div 模拟）；**P2-16 i18n**：19 个 `.tsx` 仍含中文硬编码（11 个已接 i18n），`AudioTab` / `AIVaultPage` 最集中 |
| **C6** | `frontend/src/components/ui/index.tsx` | 组件层硬编码文案：`ErrorState` 默认标题「加载失败」、重试按钮「重试」、`StateBadge` 的 6 个状态词 —— 属 i18n 范围 |

### D 类 · 安全 / 产品观察项

| 编号 | 位置 | 问题 |
|---|---|---|
| **D1** | `app/app.py:1885 api_storyboard_retry_shot` | **无 `_autopilot_guard`、无鉴权**。托管暂停期间仍可触发单镜重跑；且该路由**不在 `gpu_task_gate` 内**，可与批量 worker 真正并发写 `storyboard_manifest.json`（并发缺陷前提成立，建议列入安全评审） |
| **D2** | 前端 + 后端 | **质检配置在前端没有入口**：`qcApi.updateConfig / clearConfig / resetEndpoint / syncFromAI` 全前端零调用，`QcTab` 是纯只读看板。而 `qc_client._empty_config()` 默认 `enabled: False` → **新环境部署后质检静默全关**（例外：音频阈值在 `AudioTab` 可改） |
| **D3** | `.workbuddy/test/verify_ai_qc_sync.py` | **过期测试**：断言的是 P0-5 已退役的 `ai.qc→qc` 桥接（`qc_synced`、`_save_ai_module` 内的 `set_endpoint(QC_CONFIG_PATH)`）。实证 `git show HEAD:app/app.py \| grep -c qc_synced` = **0** → 它变红**不是回归**，但每次跑都假红，建议改写为「DB 单点」口径或删除 |

### E 类 · 环境（非代码缺陷，但阻塞业务）

- 运行时 QC / LLM API 持续返回 **`HTTP 401 无效的令牌`**（09-22 日志记录）→ 托管「逆天系统」第 1 集资产生成连续失败。属**密钥配置**问题，需在 AI 设置页重新保存有效密钥，与代码无关。

---

## 二、✅ 已修复（项目记忆中的旧条目已失效，勿再按旧结论报）

| 旧结论（记忆里标「未修」） | 当前实际状态 | 证据 |
|---|---|---|
| `load_index()` 解析失败静默返回空索引 | **已修** | `project_store.py:302-309` 走 `_read_json(..., strict=True)` + `logger.error` + `raise`（fail-loud） |
| 资产链路无逐镜 try/except（一处异常整批判 failed） | **已修** | `app.py:2531-2533` 循环体已包 `try:`，逐资产隔离 |
| 图片 `shutil.copy2` 不清源 → ComfyUI output 膨胀 | **已修** | `app.py:2641` 已改 `shutil.move`（注释 G8②）；新增 `app/disk_reclaim.py` + 资产/分镜/成片三处收尾回收。现存 7 处 `copy2` 均在「质检暂存区 → 正式目录」路径，源在 `QC_DIR` 且会被清理，**非膨胀路径** |
| 整集视频 QC 双重降采样 | **已修** | 新增 `app/qc_coverage.py`（`DEFAULT_DESC_LIMIT=60`、`EPISODE_MAX_FRAMES=48`、按段中点 `frame_ratio`）；守卫 `verify_episode_qc_coverage` **19/19** |
| 提示词无长度闸门 | **已修** | `h3_prompt_kit` 结构感知 `clamp_h3_prompt`；守卫 `verify_prompt_clamp` **56/0** |
| 固定 `.tmp` + 零锁写点 | **0 处残留** | 全库 `grep '+ ".tmp"'` 仅命中 `fs_atomic.py:8` 的 docstring 反例 |
| `logger.w` / `logger.d` 短名错写 | **0 处** | 全库 logger 方法名仅 `warning/info/error/debug/exception` |
| `comfyui_client.generate_video()` 死代码 | **已删** | grep 零命中 |
| 前端 `Skeleton` / `ErrorState` 零引用 | **已接线** | 9 个文件引用（含 `MemoryPage`/`OverviewPage`/`GridPage`） |
| storyboard manifest 单镜重跑裸写 + fail-open | **已修** | `_update_storyboard_manifest_shot` 走 `read_json_strict` + `atomic_write_json`；`_storyboard_worker` 读旧 manifest（`app.py:3184-3192`）亦已统一口径 |
| `_storyboard_worker` 逐镜读配置（性能） | **已修** | `app.py:3209` worker 级读一次（G13） |

---

## 三、守卫 / 静态检查跑分（本轮实测）

| 检查项 | 结果 | 说明 |
|---|---|---|
| `verify_silent_except.py` | **18/20** | 2 红 = B1/B2（§1.2）+ B3（§4.2），见上 |
| `verify_atomic_write.py` | 58/0 | 无固定 .tmp、并发不写坏、损坏可恢复 |
| `verify_prompt_clamp.py` | 56/0 | 提示词 ≤6000、细节 ≤800，无绕过路径 |
| `verify_episode_qc_coverage.py` | 19/19 | D-05 整集抽帧覆盖 |
| `verify_index_failloud.py` | 23/0 | 索引三态：缺失→默认、损坏→.bak 或 fail-loud |
| `verify_residual_c4n_fixes.py` | 44/0 | C4/N 系列收口全部在位 |
| `compileall app/` | exit 0 | 全模块编译通过 |
| `pyflakes app/*.py` | 无 `undefined name` | — |
| `verify_ai_qc_sync.py` | 多条 FAIL | **过期测试**（见 D3），非回归 |

---

## 四、建议修复顺序

1. **A3**（`_deliverable_review` fail-open）—— 唯一一条会「让打回失效」的功能性缺陷，改动小（换 `read_json_strict` + 窄 try 响亮降级），建议优先。
2. **A1 + A2**（凭证 DB 单点化收尾）—— 同一根因，一起改：`migrate_from_legacy` 改按模块判空；`/api/qc/config` 同步写 `tasks.db`（与 `/api/ai/config` 对称）。
3. **B1/B2/B3**（静默吞异常口径对齐）—— 顺手修，恢复守卫全绿，避免掩盖未来真回归。
4. **A4**（`save_script_inplace` 原子写）—— 换 `atomic_write_json` 即可。
5. **C1/C2/C3**（前端错误态补齐）—— 需新增 state + `.catch`，属改数据获取逻辑，建议与 P2-16 i18n 一轮处理。
6. **D1**（retry-shot 鉴权/闸门）、**D2**（质检配置 UI 入口）—— 属产品/安全决策，需与需求方确认后再动。
7. **D3**（过期测试）—— 删除或改写为 DB 单点口径。

---

## 附：本轮未覆盖范围

- 未跑**全量 65 个**测试脚本（按「每脚本一次独立调用」纪律只为关键守卫单独跑；其余属历史已绿项）。
- 未做**在线服务级探针**（5000 正在监听、PID 32692，但本轮为静态/离线核实，未触发任何生成类请求）。
- 未审计 ComfyUI 侧（`D:\ComfyUI_portable_TE_v260619`，仓外目录）。
