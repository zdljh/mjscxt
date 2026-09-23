# 漫剧生成系统 · 全项目整体再扫描收口报告

**日期**：2026-09-22
**场景**：QA 整体扫描（"问题已全部修复，再整体扫一遍"）
**参与成员**：质量门神（QA 与发布）
**代码基线**：HEAD = origin/main = `9751a9c`（本地/远端零分叉）

---

## 📌 TL;DR
- 整体结论：🟢 全部已知缺陷修复**保持无回潮**，无新增回归。
- 离线全量回归 `.workbuddy/test/`（52 脚本）+ 仓库根守卫（8 脚本）**全部 PASS，0 FAIL**。
- 静态：`compileall` OK、`pyflakes` 无 `undefined name`。
- 3 个在线探针假红（5000 端口未监听）+ 1 个探针假红（依赖已删除的用户数据），均**非代码回归**。
- 非阻塞观察项：`/api/storyboard/retry-shot` 无 `_autopilot_guard`、无鉴权——建议纳入安全评审。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 阻塞项 | 0 |
| 非阻塞观察项 | 1（retry-shot 鉴权/autopilot 守卫） |

---

## 1. 防回潮快扫（关键修复点逐项核实）

| 检查点 | 结果 |
|---|---|
| `logger.<单字母>(` 短名（B1） | 0 命中 ✅ |
| 固定 `path+".tmp"` 拼接（D-03） | 仅 `fs_atomic.py:8` docstring 反例；活代码全部唯一化（pid+tid+urandom）✅ |
| `def generate_video`（D-08 死代码） | 已删除 ✅ |
| 三新模块 `fs_atomic`/`qc_coverage`/`disk_reclaim` | 均在位 ✅ |
| 单镜重跑 manifest 读改 `read_json_strict`+`atomic_write_json`+响亮降级（B2/C3） | `app.py:2020` 在位 ✅ |
| `_storyboard_worker` 旧 manifest 读口径统一 `read_json_strict`（C4-1） | `app.py:3133` 在位 ✅ |
| `.bak` 快照失败级别对齐 `warning`（C4-2） | `fs_atomic.py:91` 在位 ✅ |
| prompt_qc 6000 服务端硬上限钳制（N1） | `prompt_qc.py:848` 在位 ✅ |
| LLM 密钥迁移失败 → `logger.error`（N3） | `llm_client.py:224` 在位 ✅ |
| 去水印临时文件唯一化（N4） | `watermark_cleanup.py:118` 在位 ✅ |
| TaskQueue「刻意未接线」声明（N2） | `task_store.py:342` 在位 ✅ |
| P-1 `_crit_kind` 按类型键 / P-2 `clamp_h3_prompt` 结构感知 / P-4 `clamp_prompt` 自定义标签 | 均在位 ✅ |

> 只读视图（`app.py:1130`、`1483` 两处 `open(manifest)`）保留 fail-open + `logger.warning` 降级，符合「只读视图可降级」约定，非缺陷。

---

## 2. 全量离线回归（逐项 exit=0）

`MJSCXT_AUTOPILOT=0 C:/Python314/python.exe`，逐脚本独立调用：

- `.workbuddy/test/`：verify_residual_c4n(44) / atomic_write(58) / index_failloud(23) / atomic_write_locks(77) / asset_* / audio_qc(146) / camera_framing(70) / cancel_interrupt(41) / episode_qc_exception(25) / final_video(37) / h3_*(128/17/41) / memory_loop(93) / negation(40) / p1/p2 / project_*(64/17/58) / prompt_*(56/167/51) / qc_*(91/28/162/44/34/40/38) / reasoning_*(44/19) / script_audit(51) / sfx(57) / shot_* / step_final(37) / storyboard_skip / style_*(85/57) / watermark_slot(35) / ai_chat_archive(39) / ai_qc_sync(36) / async_poll(16) → **全 PASS**
- 仓库根守卫：silent_except(20) / comfyui_reclaim(44) / concat_failure_logging(27) / episode_qc_coverage(19) / legacy_generate_video(9) / narration_deprecated(21) / project_audit(40) / task_queue_backpressure(18) → **全 PASS**

**在线探针（假红，非回归）**：
- `agent_feedback_probe` / `agent_probe` / `mix_probe` → `WinError 10061`（5000 未监听，socket 实测 NOT LISTENING）
- `probe_longjson_storyboard` → 依赖用户数据 `output/scripts/剑影孤城-测试/第1集.json` 已缺失（测试规则禁读写用户数据）
- `probe_gateway_health` / `thinking_probe` → PASS

---

## ⚠️ 待完善 / 非阻塞观察项
- `/api/storyboard/retry-shot`（`app.py:1856`）未挂 `_autopilot_guard`（全库 63 处 guard 中不在此列）、无鉴权 —— 既有设计，建议纳入安全评审。
- `probe_longjson_storyboard` 依赖已删除的用户数据，后续可将样本改为本地合成（`_out/`）以符合「测试不依赖用户数据」纪律。

---

## ✅ 行动清单
| # | 行动 | 紧急度 |
|---|------|--------|
| 1 | 维持当前 `9751a9c` 基线，无需回滚 | — |
| 2 | 为 `/api/storyboard/retry-shot` 补 `_autopilot_guard` + 鉴权（列入安全评审） | P2 |
| 3 | `probe_longjson_storyboard` 样本本地化 | P3 |

---
> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
