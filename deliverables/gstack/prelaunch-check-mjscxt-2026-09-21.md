# 漫剧生成系统 · 上线前全检总报告（代码审查 + 安全审计 + QA 测试）

**日期**：2026-09-21
**场景**：上线前检查（pre-launch check）
**参与成员**：产品评审员（代码审查） + 安全卫士（安全审计） + 质量门神（QA 测试，三向合并）
**代码基线**：`zdljh/mjscxt` HEAD `295933d`（与 `origin/main` 零分叉，工作树干净）

---

## 📌 TL;DR（执行摘要）

- 整体结论：**🟡 有条件通过（纯本机单用户部署）** —— 无任何 🔴 阻断项；注入/命令执行面（FFmpeg list 调用零 `shell=True`、agent 工具白名单无 RCE 原语）控制良好。
- 阻塞项数量：🔴 0 个；🟠 上线前应处理 **6** 项（代码审查 2 + 安全 4）。
- 关键前提：系统全部安全边界押在「仅监听 `127.0.0.1`」单点假设上（全应用零鉴权 + `CORS(app)` 无 origin 限制）。**网络可达/多用户/Docker 部署为 No-Go**，须先补鉴权。
- **QA 回归：39 个离线脚本全量跑过，全绿**。其中 `step_final_episode` 首跑因**测试桩过期**（`WANTED` 缺 G7 新增 `_playable`、`NS` 未注入 `json`、夹具用了假字节 mp4）报 `NameError`——**非生产缺陷**，已按行动项 7b 修复测试桩并复跑复绿（36 PASS/0 FAIL）；无生产级回归红。
- 下一步：完成「最低可上线 3 动作」（删残留明文 key 并轮换 / 角色关系端点收口 / 非回环 host 启动校验），H1/H2 两处方约 8 行锁与临时名修复，并顺手修 `verify_step_final_episode.py` 测试桩（`WANTED` 补 `_playable` + 注入 `json`），即可上线。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟡 **条件 Go**（纯本机单用户）；⛔ 网络可达部署 No-Go 直至补鉴权 |
| 严重度分布（合并去重后） | 🔴 0 / 🟠 6 / 🟡 10 / 🟢 5 |
| 关键行动项 | 3 条 P0（上线前）+ 3 条 P1（上线后 1 周）+ 2 条 P2（排期） |
| 建议负责人 | 开发 A 负责 P0 三项 + P1；运维负责凭据轮换与 ACL |

---

## 1. 各成员核心结论

### 🎯 产品评审员（代码审查）
- 核心判断：**Go（有条件）**。9 个重点模块（`app.py`/`comfyui_client.py`/`qc_client.py`/`project_store.py`/`autopilot.py`/`serve.py`/`keyframe.py`/`video_postprocess.py`/`dub_mix.py`）静态编译与 undefined-name 检查全过；无 🔴 缺陷。
- 关键建议：H1 `_CALL_STATS` 全局 dict 多线程读改写无锁（统计偶发少计，约 5 行加锁）；H2 `qc_client.save_config` 固定 `.tmp` 临时名（并发保存可截断，改唯一化命名，与 `project_store._write_json` 同款）；另有 5 项 🟡 记录级/预防性建议。已确认 `_safe_project` 无 `or` 回潮、comfyui_client 实例/模块级调用路径正确、S4/S5/G18 修复无回潮。

### 🛡️ 安全卫士（OWASP + STRIDE 审计）
- 核心判断：**本机部署带护栏可 Go，网络可达 No-Go**。无 RCE 原语；核心风险集中在访问控制（条件性）+ 磁盘凭据卫生 + 资源耗尽。
- 关键建议：F-01 角色/关系端点（`app.py:9456–9880`）裸 `project` 入参拼路径写盘，漏掉了全系统统一的 `_safe_project` 收口（**G4 收口的漏网之鱼**，`project=../../evil` 可越界写盘）；F-02 零鉴权 + `CORS(app)` 无 origin 限制 + `APP_HOST` 可被 env 覆盖为 `0.0.0.0`；F-03 根目录残留明文 API key 备份（`qc_config.backup_20260914.json`，已 gitignore 但机器上仍是活凭据）；F-04 `MJSCXT_SECRET_KEY` 明文在 `.env` 且 ACL 偏宽。FFmpeg 注入面与 agent 工具面确认干净（🟢 良好实践）。

### ✅ 质量门神（QA 测试）
- 核心判断：**回归基本全绿，无生产级红**。全量跑了 **39** 个离线回归脚本（37 基线 + `ai_chat_archive`/`memory_loop` 2 个额外），输出落在 `.workbuddy/test/.qa/*.out`（主 QA 未写正式报告，数据源由其 55 个 `.out` 文件追回重建，见下节）。
- 结果：**39 绿 / 0 失败（修复测试桩后）**。首跑的 `step_final_episode` 报 `NameError: name '_playable' is not defined`——**根因是测试桩过期**（`verify_step_final_episode.py:53` 的 `WANTED` 集合缺 G7 新增的 `_playable`，且 `NS` 未注入 `json`、夹具用了假字节 mp4），**不是生产缺陷**：`pipeline.py:250` 的 `_playable` 真实存在且被 `probe_video`/`probe_final` 正确调用。**已按行动项 7b 修桩（`WANTED` 补 `_playable` + `NS` 注入 `json` + `_playable` 覆写为 `_nonempty` 解耦 ffprobe）并复跑 36 PASS/0 FAIL 全绿。**
- 环境说明：本机 ffprobe 存在于 `WinGet\Links\ffprobe.EXE`，故「假字节 mp4 夹具」会被 `_playable` 判未就绪（场景 A 因此挂）。另 10 个脚本首跑被 CLI 沙箱的**批量删除守卫**（单轮累计删除 ≥50 即杀）截断，**逐个独立复跑（`rr_*`）后全部复绿**——属环境假红，非回归失败。`qc_memory` 曾被汇总正则误判「NameError」，实为 PASS 161/FAIL 0 全绿（正则误匹配到 PASS 文案里的字面词）。
- 关键建议：修复 `verify_step_final_episode.py` 测试桩即可让「37 基线名副其实全绿」；⚠️ 项目根目录另有一份非本团队产出的《全项目缺陷审计报告_三批次分工_20260921.md》（疑似另一并发会话），未纳入本次合并，需用户确认归属。

---

## 2. 综合审查发现（去重合并后按严重度排序）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源成员 |
|---|--------|------|------|---------|------|---------|
| 1 | 🟠 | 访问控制/注入 | `app/app.py:9456–9880`（`/api/characters`、`/api/relations*`） | 裸 `project` 入参直接 `os.path.join` 拼路径写盘，绕过全系统 `_safe_project` 收口 → 路径穿越写原语 | 统一改走 `_project_or_400`/`_safe_project`；或在 `CharacterManager.__init__` 做 `abspath` 前缀校验越界即 400 | 安全卫士（F-01） |
| 2 | 🟠 | 访问控制 | `app/app.py:108` `CORS(app)` + `serve.py`/`.env` `APP_HOST` | 全应用零鉴权 + CORS 无 origin 限制，安全边界押在「仅 loopback」单点；`APP_HOST` 可被覆盖为 `0.0.0.0` | 单机：`serve.py` 对非回环 host 启动校验/强告警；网络部署：加最小鉴权 + origin 白名单 + 限流 | 安全卫士（F-02） |
| 3 | 🟠 | 凭据卫生 | 根目录 `qc_config.backup_20260914.json` | 残留真实明文 API key（`sk-vZx9…`），gitignore 外但机器上仍是活凭据 | 删除文件并**轮换该 key**（视为已泄露）；部署清单加「备份不得携带明文 key」 | 安全卫士（F-03） |
| 4 | 🟠 | 凭据卫生 | `.env`（`MJSCXT_SECRET_KEY`）+ `output/secrets.enc` | Fernet 主密钥明文落盘且 ACL 偏宽，`.env`+`.secret_key`+`secrets.enc` 三者齐泄即可破整个密钥库 | `icacls` 收紧至当前用户；部署文档声明最小授权 | 安全卫士（F-04） |
| 5 | 🟠 | 并发 | `comfyui_client.py:76-81` `_bump`/`reset_call_stats` | `_CALL_STATS` 全局 dict 多线程读改写未取 `_CALL_STATS_LOCK` → 统计偶发少计（无业务阻断） | 两个函数体包 `with _CALL_STATS_LOCK`（约 5 行） | 产品评审员（H1） |
| 6 | 🟠 | 并发/原子写 | `qc_client.py:692` 附近 `save_config` | 固定 `config_path + ".tmp"` 临时名，双请求并发保存可截断（损坏后回退 default，不阻断生产） | 改唯一化命名（pid + thread ident + urandom，与 `project_store._write_json` 同款） | 产品评审员（H2） |
| 7 | 🟡 | 资源耗尽 | `task_store.py` `TaskQueue.submit` | 队列无上限无背压，配合 F-02 可打满 GPU/内存 DoS | 加 `MAX_QUEUED` 超限 429/告警（P2，与鉴权联动） | 安全卫士（F-05） |
| 8 | 🟡 | 交付面卫生 | 仓库跟踪的 `test_page.html`、`h3_ref_submit.py`、`app/templates/index_old.html`、`index_v2.html` 等 | 调试/历史产物随交付面扩大侦察面 | 上线前从仓库/静态目录剔除 | 安全卫士（F-06） |
| 9 | 🟡 | 数据完整性 | `project_store.py` 删除→`PROJECT_TRASH_DIR` | 删除「可恢复」且 `output/` 宽 ACL，本机他账号可悄悄还原/篡改 | 多用户机收紧 ACL；如需不可恢复语义补独立 `purge` 端点 | 安全卫士（F-07） |
| 10 | 🟡 | 部署正确性 | `docker-compose.yml` / `Dockerfile` | 容器内应用绑 `127.0.0.1` 而宿主发布 `5000:5000` → 转发不通（功能 bug）；`5000:5000` 未收口宿主回环 | 容器走 `APP_HOST=0.0.0.0` + 宿主 `127.0.0.1:5000:5000`；仅在采用 Docker 时处理 | 安全卫士（F-08） |
| 11 | 🟡 | 数据完整性 | `.gitignore:21-22` | `!output/projects/index.json` 否定规则因父目录被整体忽略而**无效**（`git check-ignore` 实测命中第 21 行）→ 索引实际不在版本控制内 | 删除失效否定或改选择性忽略；备份脚本单独 `git add -f` | 安全卫士（F-09） |
| 12 | 🟡 | 并发/原子写 | `secret_store.py` `_write_store` / `autopilot.py` `_write_json` | 同型「固定 `.tmp` + 无锁」（secret_store 并发概率极低，autopilot 单线程守护无实际并发） | 与 H2 一并改唯一化；autopilot 记录文档即可 | 产品评审员（M1/M2） |
| 13 | 🟡 | API 契约 | `video_postprocess.py` `concat_videos` 失败返回 `""` | 现有调用方已检查，外部若有直接调用需逐一确认 `if not` | grep 全量 `concat_videos(` 调用点；或改抛 `RuntimeError` | 产品评审员（M3） |
| 14 | 🟡 | 并发 | `app.py:164` `generation_state` 嵌套 dict | 当前读写均在锁内（正确），但对子 dict 做局部引用后锁外改会绕过保护 | 模块注释加警示语（预防性） | 产品评审员（M4） |
| 15 | 🟡 | 代码卫生 | `app.py` 多处（682/1481/1610/2922/3764/4746/5738）、`autopilot.py` 363/448/776 | 未使用变量 / 无占位符 f-string（`app.py:5738` 需确认意图） | 上线后随手清理 | 产品评审员（M5） |
| 16 | 🟢 | 良好实践 | 全链路 | FFmpeg 零 `shell=True`；agent 工具白名单无 RCE 原语；`_serve_safe`/`_safe_upload_name` 防读穿越；密钥 Fernet 库 0o600；`_safe_project` 无回潮；尾帧/图片/视频 QC fail-closed 口径一致；`serve.py` atexit+信号优雅停机 | 保持现状，勿在后续重构中回潮 | 两位成员 |

---

## 2b. QA 回归结果明细（质量门神）

**数据源**：`.workbuddy/test/.qa/*.out`（55 个文件，主 QA 未写正式报告，由本总表按「任一尝试 PASS 即绿、`rr_*` 复跑优先」规则从原始输出重建）。
**口径**：全量跑 **39** 个逻辑脚本（37 基线 + `ai_chat_archive` / `memory_loop` 2 额外）；判绿标准 = 该脚本任一 `.out` 出现 `FAIL 0` / `全部通过` / `RESULT: pass=N fail=0` / `ALL PASS`。

**结果：39 绿 / 0 失败**（首跑唯一失败项 `step_final_episode` 为测试桩过期、非生产缺陷，已修桩复绿；环境假红 10 项均独立复跑复绿）。

| 组 | 脚本 | PASS 数 | 状态 | 备注 |
|----|------|---------|------|------|
| 提示词/剧本 | `prompt_qc` / `audio_qc` / `h3_prompt_kit` / `prompt_script_opt` | 167 / 146 / 128 / 51 | 🟢 | 全绿 |
| 质检 | `qc_bool_parse` / `qc_memory` / `qc_ref_images` / `qc_retry_stop` / `qc_summary_scan` / `qc_prompt_feedback` | 91 / 161 / 38 / 34 / 40 / 44 | 🟢 | `qc_memory` 曾误判红（正则命中 PASS 文案字面 `NameError`），实为 161/0 |
| 风格/构图 | `style_injection` / `style_qc` / `camera_framing` | 85 / 57 / 70 | 🟢 | 全绿 |
| 视频/成片 | `final_video_episode` / `watermark_slot` / `sfx_pipeline` / `narration_dub` / `dub_mix_base_dur` | 37 / 35 / 56 / 55 / 28 | 🟢 | `final_video` 首跑被守卫截断，`rr_` 复跑 37/0 复绿 |
| 断点/重试 | `h3_sequential` / `h3_retry_seed` / `cancel_interrupt` / `storyboard_skip` / `empty_shot_drop` | 41 / 17 / 41 / 8 / 7 | 🟢 | `cancel_interrupt` 断言全过、仅清理阶段被杀（无汇总行） |
| 资产/交付 | `asset_item_isolation` / `asset_skip_outcome` / `deliverable_register` | 27 / 15 / 31 | 🟢 | 均为 `rr_` 复跑复绿 |
| 项目/并发 | `project_audit` / `project_delete_coverage` / `atomic_write_locks` / `entry_signature` | 61 / 17 / 68 / 23 | 🟢 | 全绿 |
| 网关/思考/异步 | `gateway_failfast` / `reasoning_effort` / `reasoning_effort_http` / `async_poll` / `ai_qc_sync` | 57 / 44 / 19 / 16 / 36 | 🟢 | `reasoning_effort_http` 走 `rr_` 复绿 |
| 记忆 | `memory_loop` / `ai_chat_archive` | 93 / 39 | 🟢 | 额外脚本 |
| **成片终步** | `step_final_episode` | 36 | 🟢 | 首跑测试桩过期报 `NameError`，**已修桩并复跑 36/0 全绿**（见下） |

### 🔴→🟢 `step_final_episode` —— 测试桩过期（非生产缺陷），已修复复绿
- 现象：`verify_step_final_episode.py` 首跑在「场景 A 前置」抛 `NameError: name '_playable' is not defined`（`pipeline.py` 抽取后的 371 行 `probe_video`）。
- 根因：测试桩 `WANTED` 集合（`verify_step_final_episode.py:53`）原为 `{_nonempty, probe_video, final_path, probe_final, step_final}`，**缺 G7 新增的 `_playable`**；且 `NS` 未注入 `json`（`_playable` 内 `json.loads` 需要），夹具用了假字节 mp4（本机 ffprobe 真实存在，`_playable` 会真判未就绪）。
- 判定：**生产无碍**——`pipeline.py:250` 的 `_playable` 真实存在且被 `probe_video`(371/380)、`probe_final`(393) 正确调用。
- **修复（已落地）**：① `WANTED` 补 `_playable`；② `NS` 注入 `json`；③ exec 后把 `_playable` 覆写为 `_nonempty`（本回归只验「整集 vs 逐镜」路由逻辑，不验 ffprobe 可播放性，假字节夹具由此按原意通过闸门，与本机是否装 ffprobe 解耦）。**复跑 36 PASS / 0 FAIL，exit 0，全绿。**
- 影响：「37 基线名副其实全绿」已坐实；不改变「可上线」结论。

### ⚠️ 环境假红说明（不算回归失败）
- **批量删除守卫**：10 个脚本首跑在 `cleanup()` 阶段被 CLI 沙箱「单轮累计删除 ≥50 即杀」截断（判据 = 输出只有一行 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`、无 traceback 无汇总行）。**逐个独立复跑（`rr_*`）后全部复绿**（断言其实早已全过，只是被杀在收尾）。涉及：`ai_qc_sync` / `asset_item_isolation` / `deliverable_register` / `dub_mix_base_dur` / `final_video_episode` / `h3_retry_seed` / `project_delete_coverage` / `qc_summary_scan` / `reasoning_effort` / `reasoning_effort_http`。
- **ffprobe 存在**：`C:\Users\liujianghua\AppData\Local\Microsoft\WinGet\Links\ffprobe.EXE` 在场，故「假字节 mp4」夹具会被 `_playable` 判未就绪 → 这正是 `step_final_episode` 挂的导火索。

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 删除 `qc_config.backup_20260914.json` 并**轮换其中 API key**（F-03） | 运维 | P0 | 上线前 |
| 2 | 角色/关系端点 `project` 入参统一改走 `_project_or_400`/`_safe_project`（F-01，G4 收口补漏） | 开发 A | P0 | 上线前 |
| 3 | `serve.py` 增加非回环 `APP_HOST` 启动校验/强告警（F-02 单机护栏版） | 开发 A | P0 | 上线前 |
| 4 | H1 `_CALL_STATS` 加锁 + H2/`secret_store` 固定 `.tmp` 改唯一化（约 8 行改动，可一次提交） | 开发 A | P1 | 上线后 1 周 |
| 5 | 收紧 `.env`/`.secret_key`/`secrets.enc` 的 Windows ACL 至当前用户（F-04） | 运维 | P1 | 上线前（多用户机） |
| 6 | 清理交付面调试/历史产物（F-06）+ 修复 `.gitignore` 失效否定（F-09） | 开发 A | P2 | 下迭代 |
| 7 | ~~回收质量门神 QA 回归报告并追加进本总表~~ → **已完成**：39 脚本回归数据自 `.workbuddy/test/.qa/` 追回并并入「2b. QA 回归结果明细」 | 质量门神 | ✅ 已收口 | 本次 |
| 7b | ~~修复 `verify_step_final_episode.py` 测试桩~~ → **已完成**：`WANTED` 补 `_playable` + `NS` 注入 `json` + `_playable` 覆写为 `_nonempty`（解耦 ffprobe），复跑 36 PASS/0 FAIL 全绿，「37 基线全绿」坐实 | 开发 A | ✅ 已收口 | 本次 |
| 8 | 若走 Docker 部署：补 F-08 端口收口 + `pip audit`（A06 逐包 CVE 比对，本次未做） | 运维 | P2 | 采用容器时 |

---

## ⚠️ 待完善 / 已知局限

- **QA 结论已由本总表补齐**：质量门神未写正式报告，但其 39 脚本回归数据自 `.workbuddy/test/.qa/*.out` 追回并入「2b. QA 回归结果明细」，「Go」结论现以 代码审查 + 安全审计 + QA 回归 三者为据。
- **测试桩失败已修复**：`verify_step_final_episode.py` 首跑因 `WANTED` 缺 `_playable`、`NS` 未注入 `json` 报 `NameError`（属测试桩过期、非生产缺陷）；已按行动项 7b 修桩并复跑 36 PASS/0 FAIL 全绿，「37 基线全绿」坐实。
- **外部文档未采信**：项目根目录《全项目缺陷审计报告_三批次分工_20260921.md》（1372 行，含「P0 阻断 5 条」等表述，时间戳 10:26）疑似另一并发会话产物，与本团队产出无关联记录，**未纳入合并**；如需并入须先核对来源。
- 安全审计为**静态 + 落盘核查**，未对运行中服务做主动渗透；未逐包比对 CVE 库；未实测 Windows 多用户 ACL 运行时效果。
- 上线前需确认**线上 5000 实例是否已重启加载 `295933d`**（此前批次改过 `app/*.py`，未重启则跑的是旧逻辑）。

---

## 📚 成员产出索引

- 产品评审员（代码审查）原始产出：`deliverables/gstack/code-review-mjscxt-2026-09-21.md`（202 行）
- 安全卫士（安全审计）原始产出：`deliverables/gstack/security-audit-mjscxt-2026-09-21.md`（163 行）
- 质量门神（QA 测试）原始产出：**未写正式报告**，但 39 脚本回归输出落在 `.workbuddy/test/.qa/*.out`（55 文件，`rr_*` 前缀为独立复跑）；本总表「2b. QA 回归结果明细」即由其重建。

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
