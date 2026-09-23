# 漫剧生成系统 · 上线前代码审查报告

**审查日期**：2026-09-21
**审查人**：gstack-product-reviewer
**目标版本**：zdljh/mjscxt HEAD 295933d（工作树干净，已推送）
**静态验证**：`py_compile` 全部 9 模块通过；`pyflakes` 无 undefined name

---

## TL;DR

1. 9 个重点模块（`app.py` / `comfyui_client.py` / `qc_client.py` / `project_store.py` / `autopilot.py` / `serve.py` / `keyframe.py` / `video_postprocess.py` / `dub_mix.py`）均通过静态编译和 undefined-name 检查，无语法错误。
2. 无 🔴 级（阻塞上线）缺陷。
3. 共发现 2 处 🟠 级（有实际并发风险但影响可控）、5 处 🟡 级（低概率或文档已警示）、4 处 🟢 级（信息记录/无风险）。
4. 上线建议：**Go**，附带 2 项上线后立即修复的 🟠 项（见 P0/P1 行动清单）。

---

## 严重度分布

| 等级 | 数量 | 说明 |
|------|------|------|
| 🔴 严重（阻塞） | 0 | 无 |
| 🟠 高（上线后 1 周内修） | 2 | 并发竞态，影响可控 |
| 🟡 中（可排入下个迭代） | 5 | 低概率/文档已警示 |
| 🟢 低（记录 / 无需行动） | 4 | 信息项 |
| **合计** | **11** | |

---

## 发现清单

### 🟠 高

#### H1 · `_CALL_STATS` 无锁写操作（`comfyui_client.py:79`）

- **位置**：`comfyui_client.py` L76-81，`_bump()` 函数
- **问题描述**：
  `_bump(key, delta)` 执行 `_CALL_STATS[key] = _CALL_STATS.get(key, 0) + delta`，但整个函数体未获取 `_CALL_STATS_LOCK`。`_CALL_STATS` 是全局 dict，在多线程 Flask 环境中被 `generate_h3_sequence_sequential` 内多次调用（L681/684/686/742/743/747/758），每次调用都来自独立 worker 线程。两个线程同时读 `key` 的旧值后写入新值，会丢一次累加（读 5 → 写 6，另一线程也读 5 → 写 6，最终得 6 而非 7）。
  同理 `reset_call_stats()`（L71-72）也直接对 `_CALL_STATS` 做就地重置，未加锁。
- **影响范围**：仅 ComfyUI 调用统计计数，不涉及生产数据、不阻断任务，统计偏小但方向一致（少计、不会多计）。
- **建议**：
  ```python
  def _bump(key: str, delta=1) -> None:
      try:
          with _CALL_STATS_LOCK:
              _CALL_STATS[key] = _CALL_STATS.get(key, 0) + delta
      except Exception:
          pass

  def reset_call_stats() -> dict:
      with _CALL_STATS_LOCK:
          for k in list(_CALL_STATS.keys()):
              _CALL_STATS[k] = 0 if not isinstance(_CALL_STATS[k], float) else 0.0
          return dict(_CALL_STATS)
  ```
- **是否阻塞上线**：**否**。统计值偶有偏差，无业务影响，上线后 1 周内修。

---

#### H2 · `qc_client.save_config` 固定 `.tmp` 后缀并发截断（`qc_client.py` 约 L692-695）

- **位置**：`qc_client.py` `save_config()` 内部
- **问题描述**：
  原子写临时文件使用固定名 `config_path + ".tmp"`。Flask 是 threaded 模式，两个请求同时触发 QC 配置保存（例如管理员同时在两个标签页操作），A 线程写 tmp、B 线程覆写同一 tmp 文件，然后 A `os.replace(tmp, config_path)` 成功，B `os.replace` 也可能成功（覆盖为 B 的内容）——结果可预测（后写者胜），但文件可能被截断为 B 的半成品（B 的 `flush` 尚未完成）。
  对比 `project_store._write_json` 已实现唯一化临时名（pid + thread id + urandom），此处未同步。
- **影响范围**：QC 配置文件（`qc_config.json`），损坏后下次 `load_config` 回退到 default（不会崩溃，但用户配置丢失）。
- **建议**：将 `tmp = config_path + ".tmp"` 改为与 `project_store._write_json` 同款的唯一化命名（pid + thread ident + urandom）。
- **是否阻塞上线**：**否**。极低概率（并发保存 QC 配置的场景罕见），损坏后自动回退 default，不阻断生产。上线后 1 周内修。

---

### 🟡 中

#### M1 · `secret_store._write_store` 固定 `.tmp` 后缀（`secret_store.py` L274-346 区域）

- **位置**：`secret_store.py` `_write_store()` 内部
- **问题描述**：
  与 H2 同型。固定 `path + ".tmp"` 后缀，多线程并发写有截断风险。但 secret_store 在正常流程中只在首次启动迁移和手动保存 QC 密钥时调用，并发概率极低。
- **建议**：同 H2，加唯一化临时名。可一并处理。
- **是否阻塞上线**：**否**。

---

#### M2 · `autopilot._write_json` 固定 `.tmp` 后缀（`autopilot.py` 内部）

- **位置**：`autopilot.py` 内部 `_write_json` 函数
- **问题描述**：
  与 M1 同型。但 autopilot 是单线程守护线程（`_LOCK` 保护），`_write_json` 仅在 `_persist_runtime` 中被调用，实际无并发场景。
- **建议**：记录文档即可；若未来 autopilot 改为多线程或引入外部调用，需改唯一化。
- **是否阻塞上线**：**否**。纯记录项，无实际风险。

---

#### M3 · `video_postprocess.concat_videos` 失败返回空字符串（`video_postprocess.py` S4 已文档化）

- **位置**：`video_postprocess.py` `concat_videos()` 及 `generate_final_video()`
- **问题描述**：
  S4 修复后，`concat_videos` 失败时返回 `""`（非 `None`，非抛异常）。调用方 `generate_final_video` 若忘记检查返回值，可能将空字符串当路径继续处理。
  已确认 `generate_final_video` 内做了 `if not result: ...` 检查（S4 修复文档），但外部若有其他调用方直接调 `concat_videos` 需要逐一确认。
- **建议**：grep 全量 `concat_videos(` 调用点，确保每处均有 `if not` 检查；或在 `concat_videos` 内直接抛 `RuntimeError`，让调用方 try/except。
- **是否阻塞上线**：**否**。现有调用路径已检查，外部无已知误用。

---

#### M4 · `app.py` 中 `generation_state` 全局 dict 锁边界（`app.py` L164）

- **位置**：`app.py` L164 `lock = threading.Lock()` 及所有 `with lock:` 块
- **问题描述**：
  `generation_state` 是所有 worker 线程与 Flask 路由共享的全局 dict。当前代码在修改 `generation_state` 时均使用 `with lock:`，读时也加了锁。但 `generation_state` 是嵌套 dict（每个项目有自己的子 dict），外层锁保护的是整个 dict，无内部锁。如果未来有人做 `gen_state = generation_state[project]`（浅拷贝引用）后在锁外修改 `gen_state`，会绕过锁保护。
  当前代码无此模式（都是 `with lock: gen_state[project][field] = value`），所以现有行为正确。
- **建议**：在 `generation_state` 的模块注释中加一句警告：「`generation_state` 是嵌套 dict，任何读改写操作必须在 `with lock:` 内完成；不要对 `generation_state[project]` 做局部引用后在锁外修改。」
- **是否阻塞上线**：**否**。当前实现正确，此条为预防性建议。

---

#### M5 · `pyflakes` 未使用变量 / f-string 缺失占位符（`app.py` 多处）

- **位置**：`app.py:682 scripts`、`app.py:1481 ok`、`app.py:1610 idx`、`app.py:2922 _episode`、`app.py:3764 shot_refs`、`app.py:4746 cfg`、`app.py:5738 f-string missing placeholders`；`autopilot.py:363/448/776 A`
- **问题描述**：
  均为 pyflakes 报告的 unused variable / 无占位符 f-string。不影响运行，但 `app.py:5738` 的 f-string 若无占位符意味着那行是多余的引号或字符串，需确认是否有意图（如 `f"literal"` 本应写成 `"literal"`）。
- **建议**：上线后随手清理，优先级低。
- **是否阻塞上线**：**否**。

---

### 🟢 低（记录 / 无需行动）

#### L1 · `safe_key('')` 返回 `"project"` 陷阱——已确认无回潮（`project_store.py` `safe_key` / `app.py` `_safe_project`）

- **确认结果**：
  - `project_store.safe_key("")` 返回字面量 `"project"`（真值，非 falsy）
  - grep 全量 `_safe_project(x) or <fallback>` 模式：未发现（文档中 L244/L4918/L6252 已明确禁止此写法）
  - app.py L35-36 `from comfyui_client import camera_spec as _camera_spec, camera_key as _camera_key, camera_angle as _camera_angle`：模块级函数通过 `as _x` 导入，未误用实例调用
- **结论**：已合规，无回潮。

---

#### L2 · `comfyui_client` 实例与模块级函数调用路径（`app.py` L35-36, L186）

- **确认结果**：
  - `app.py` L186：`comfyui_client = ComfyUIClient()`（实例，用于类方法调用）
  - `app.py` L35-36：`from comfyui_client import camera_spec as _camera_spec, camera_key as _camera_key, camera_angle as _camera_angle`（模块级函数走 `import as _x` 路径）
  - `analytics.py` L201：`import comfyui_client` + `comfyui_client.get_call_stats()`（这里的 `comfyui_client` 是**模块**，`get_call_stats` 是模块级函数，调用正确）
  - 无 `ComfyUIClient.get_call_stats` 的实例调用（`get_call_stats` 是模块函数，不是类方法）
- **结论**：调用路径正确，无混淆。

---

#### L3 · `dub_mix.anullsrc d=` 定长静音底轨（`dub_mix.py`）

- **确认结果**：
  `anullsrc=r={sr}:cl=mono:d={base_dur:.3f}` 中 `d=` 参数写进 lavfi 滤镜字符串，确保 FFmpeg 不会无限生成音频流（之前有无限写盘事故）。`adelay={ms}:all=1` + `amix(normalize=0)` 逐句配音对齐逻辑正确。`cursor` 顺排不倒挂（`start = max(slot_start, cursor)`）防音序倒置。
- **结论**：S5 修复到位，无回潮。

---

#### L4 · `serve.py` 优雅停机与崩溃重启（`serve.py` 全文）

- **确认结果**：
  - `atexit` + `SIGINT`/`SIGBREAK`/`SIGTERM` handler → `autopilot.stop(timeout=15)` → `signal.signal(signum, signal.SIG_DFL); signal.raise_signal(signum)`（正确恢复默认 handler 后重发信号）
  - 崩溃自动重启：`MAX_RESTARTS=10`，冷却期 `min(30*restart_count, 300)s`，防无限重启风暴
  - waitress 优先，未安装回退 Flask dev server（有 warning）
- **结论**：实现正确，无缺陷。

---

## 上线 Go/No-Go 建议

### 结论：**Go（有条件）**

无 🔴 级阻塞缺陷。2 处 🟠 级（H1、H2）影响可控：
- H1：ComfyUI 统计计数偏差，无业务影响，上线后 1 周内修
- H2：QC 配置文件偶发截断（极低概率），损坏后自动回退 default，不阻断生产

**上线前需确认（checklist）**：
- [x] `py_compile` 全部通过
- [x] `pyflakes` 无 undefined name
- [x] `_safe_project` 无 `or fallback` 回潮
- [x] `comfyui_client` 模块级函数导入路径正确
- [x] 尾帧 QC fail-closed 口径一致（`keyframe.py` G18）
- [x] 图片/视频 QC 异常处理 fail-closed（`app.py` L3148-3150, L3893-3899）
- [x] `dub_mix` 定长静音（无无限写盘）
- [x] `serve.py` 优雅停机 + 崩溃重启

**上线后 1 周内修复（P0/P1）**：
| 优先级 | 项 | 修复方式 |
|--------|-----|---------|
| P0 | H1：`_CALL_STATS` 加锁 | `_bump`/`reset_call_stats` 包 `_CALL_STATS_LOCK`（约 5 行改动） |
| P0 | H2：`qc_client.save_config` 唯一化 tmp | 与 `project_store._write_json` 同款命名（约 3 行改动） |
| P1 | M1：`secret_store._write_store` 唯一化 tmp | 同上，可与 H2 一并提交 |
| P1 | M5：`app.py:5738` f-string 占位符 | 确认是否意图，删多余引号 |

---

## 审查范围外提示

以下模块不在本次 9 模块范围内，但代码中存在相同模式，建议纳入后续审计：

1. `secret_store.py`（已列为 M1）—— 固定 `.tmp` 后缀
2. `qc_client.py` 的 `append_history` —— 需确认落盘是否原子写
3. `video_postprocess.py` `concat_videos` 外部调用方 —— 需 grep 确认无漏检 `""` 返回值的调用
