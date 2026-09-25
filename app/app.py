"""
漫剧生成系统 - Flask Web 应用
图片资产三类：角色（多视图）/ 物品（3D多视角）/ 场景（3D多视角）
"""
import os
import re
import json
import math
import time
import random
import shutil
import threading
import copy
import uuid
import contextvars
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import BadRequest, HTTPException

from config import (
    COMFYUI_URL, PROJECT_ROOT_DIR, PROJECT_OUTPUT_DIR, SCRIPT_DIR,
    CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR, KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR,
    PROJECT_TRASH_DIR,
    NOVELS_DIR, LLM_CONFIG_PATH, NOVEL_CHUNK_CHARS, NOVEL_MAX_CHUNKS,
    NOVEL_DEFAULT_SHOTS, NOVEL_PREVIEW_CHARS, NOVEL_BRIEF_CHARS, LLM_REQUEST_TIMEOUT,
    QC_CONFIG_PATH, QC_DIR,
    CLEAR_COMFYUI_HISTORY, CLEAR_COMFYUI_HISTORY_INTERVAL_SEC,
    WATERMARK_CONFIG_PATH, WATERMARK_DIR,
    AI_CONFIG_PATH, AI_MODULES, AI_CHAT_HISTORY_PATH, AI_SETTINGS_PATH,
    UPSCALE_DIR, UPSCALE_DEFAULT_PARAMS, COMFYUI_OUTPUT_DIR,
    TE_UPSCALE_DEFAULT_PARAMS, TE_UPSCALE_LOWVRAM_PARAMS, UPSCALE_ENGINE,
    DUB_DIR, TTS_DEFAULT_PARAMS, H3_STRIP_AUDIO, H3_EMIT_AUDIO, H3_SFX_ISOLATE,
    DUB_MIX_DIR, MIX_DEFAULT_PARAMS, CONTINUITY_DIR,
    TASKS_DB_PATH, TASK_QUEUE_CONCURRENCY, TASK_UNIT_MIN_BYTES,
    KEYFRAME_CHAIN_MODE, WORKFLOW_TEMPLATE,
    CHARACTER_SHEET_VIEWS, ASSET_VIEW_STEMS,
    PROJECT_DEFAULT_CONFIG,
)
from script_generator import ScriptGenerator
from comfyui_client import (ComfyUIClient, camera_spec as _camera_spec,
                            camera_key as _camera_key, camera_angle as _camera_angle)
# ⚠️ 注意：本文件里 `comfyui_client` 这个名字是**实例**（见下方 `comfyui_client = ComfyUIClient()`），
# 不是模块。因此**模块级函数**（camera_spec / camera_key / get_call_stats 等）必须像上面这样
# 直接 import 后用别名调用 —— 写成 `comfyui_client.camera_spec(...)` 会在运行时抛
# AttributeError（实例上没有该属性）。类方法（_build_h3_prompt / generate_storyboard 等）
# 通过实例调用没问题，已有的那种写法不用改。
from video_postprocess import VideoPostProcessor, ensure_no_audio, ensure_audio_track
from novel_parser import (
    SUPPORTED_EXTS, NovelParseError, ingest_novel, list_novels,
    get_novel, preview_novel, read_novel_text, split_chapters
)
from llm_client import (
    LLMClient, LLMError, LLMGatewayUnavailable, load_config as load_llm_config,
    save_config as save_llm_config, clear_config as clear_llm_config,
    public_view as llm_public_view
)
import ai_config
import ai_chat
import agent_core
import novel_to_script
import analytics
import autopilot
import consistency
import continuity
import coverage
import script_consistency
import keyframe
import export_manager
from export_manager import ExportManager
import nle_export
import pipeline
import audio_qc
import plugin_registry
import project_store
import shot_key
from fs_atomic import atomic_write_json, read_json_strict
import providers
import prompt_qc
import qc_client
import qc_coverage
import style_kit
import sheet_split
import task_store
import gpu_task_gate
import video_watermark
import upscale_client
import tts_client
import dub_mix
import dialogue_utils
import h3_prompt_kit
import autonomous
import cancellation
import ai_memory
import prompt_memory
from ai_memory import get_memory_system
from dub_mix import (
    DubMixError, ffmpeg_available as mix_ffmpeg_check, shot_timeline,
    build_entries, mix_video_with_entries, write_mix_report, mix_out_dir,
    probe_audio_info as mix_probe_audio,
)
from tts_client import (
    QwenTTSClient, TTSError, check_environment as tts_env_check,
    build_dub_plan, default_voice_map, normalize_voice, save_voice_map,
    load_voice_map, list_voices as tts_list_voices, probe_audio as probe_audio_info,
    concat_audio, clean_line_text
)
from upscale_client import (
    VideoUpscaler, UpscaleError, check_environment as upscale_env_check,
    probe_video as probe_video_info
)
from script_prompt_analyzer import analyze_script as analyze_script_prompts, save_script_inplace
from character_manager import CharacterManager, CharacterConsistencyEngine
from relation_manager import RelationManager, RelationConflictDetector
from nine_grid_storyboard import NineGridStoryboard

# ===== 镜号归一化收敛（缺陷 P1-19 / 任务 A-10）=====
# 全项目**唯一**的镜号归一化实现见 app/shot_key.py。此处把三处旧的本地实现收敛为
# 「一行代理」，供本模块与 pipeline 等外部引用无缝沿用 —— 写侧（keyframe 落盘）与
# 读侧（本模块查找）从此共用同一个函数，杜绝「写 shot_102、读 shot_01」的静默错位。
_shot_seq = shot_key.shot_seq
_shot_num_key = shot_key.norm_shot_key
_norm_shot_key = shot_key.norm_shot_key

app = Flask(__name__)
# 总控 AI 自主执行内核：注入 Flask 实例，工具调用走进程内直连（不走网络/不绑端口）
agent_core.bind_app(app)
CORS(app)

# 服务配置（可通过环境变量覆盖：APP_HOST / APP_PORT / APP_DEBUG）
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
APP_DEBUG = os.getenv("APP_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# D-05（P1）整集视频质检抽帧上限：常量与纯逻辑见 app/qc_coverage.py
# （独立零依赖模块，便于 verify_episode_qc_coverage.py 离线单测）。


def _h3_audio_policy(path: str) -> dict:
    """按 config 的 H3 音轨策略处理刚生成的视频，返回可展示的音频处理记录。

    - `H3_STRIP_AUDIO=True`  → 剥离音轨（2026-09-17 之前的旧行为）
    - 否则 `H3_EMIT_AUDIO=True` → **保留** H3 原生音轨（环境音/打斗音效），
      并顺带保证「每镜都有音轨」：缺的补一条静音轨。
      必须补齐的理由：成片拼接用 concat demuxer + `-c copy`，
      音轨时有时无会让拼接错位甚至失败。
    """
    if H3_STRIP_AUDIO:
        strip = ensure_no_audio(path, backup=True)
        return {
            "policy": "strip",
            "has_audio_before": strip.get("has_audio_before"),
            "has_audio_after": strip.get("has_audio_after"),
            "changed": strip.get("changed"),
            "method": strip.get("method"),
            "backup": strip.get("backup"),
            "message": strip.get("message") or ("已剥离音轨" if strip.get("changed") else ""),
            "error": strip.get("error"),
        }
    pad = ensure_audio_track(path) if H3_EMIT_AUDIO else None
    pad = pad or {}
    if pad.get("error"):
        msg = f"音轨处理异常（已保留原状）：{pad.get('error')}"
    elif pad.get("changed"):
        msg = "原无音轨，已补静音轨（保证拼接一致）"
    elif H3_EMIT_AUDIO:
        msg = "音轨保留（H3 原生音效）"
    else:
        msg = "未做音轨处理"
    return {
        "policy": "keep" if H3_EMIT_AUDIO else "keep_as_is",
        "has_audio_before": pad.get("has_audio_before"),
        "has_audio_after": pad.get("has_audio_after"),
        "changed": pad.get("changed"),
        "method": pad.get("method"),
        "message": msg,
        "error": pad.get("error"),
    }

# 全局状态
generation_state = {}
lock = threading.Lock()

# 视频 worker「是否托管（pipeline）任务」的执行期标记（contextvar，随线程上下文传递）。
# 用于让「托管暂停」只掐断托管任务，不误杀用户手动触发的生成。见 _video_should_stop。
_VIDEO_TASK_IS_PIPELINE = contextvars.ContextVar("video_task_is_pipeline", default=False)

# P0-4 持久化任务队列：任务全生命周期落盘（SQLite），支持重启后查询与断点续跑
# P2-3：任务完成/失败时通过 on_change 钩子写入耗时统计（成本看板数据源）
def _task_analytics_hook(task: dict, event: str) -> None:
    try:
        analytics.record_from_task(task, analytics_kind=task.get("kind"))
    except Exception as e:  # noqa: BLE001  统计失败不得影响任务
        app.logger.warning(f"任务统计写入失败（忽略）：{e}")


task_db = task_store.get_store(TASKS_DB_PATH, on_change=_task_analytics_hook)
task_queue = task_store.get_queue(TASKS_DB_PATH)
try:
    # 启动时把上次残留的 running 任务标记为 interrupted（可供前端提示「可继续」）
    _interrupted = task_db.recycle_interrupted()
except Exception as _e:  # noqa: BLE001  不得因任务库异常导致启动失败
    _interrupted = 0
    app.logger.warning(f"任务库中断恢复失败（不影响启动）：{_e}")

# ⭐ AI 凭证单一事实源（tasks.db · ai_credentials 表）：启动时一次性把旧
# secrets.enc 双槽（ai.text/ai.qc/ai.chat + qc）+ ai_config.json 非密钥字段迁入 DB。
# 幂等（DB 已有值则跳过），失败不影响启动（读侧对每个模块都有旧口径回落）。
try:
    import ai_credentials_db
    _mig = ai_credentials_db.migrate_from_legacy()
    if _mig.get("migrated"):
        app.logger.info(f"AI 凭证已从旧源迁入 {TASKS_DB_PATH}：{_mig['migrated']}"
                        + (f"（跳过 {_mig.get('skipped')}）" if _mig.get("skipped") else ""))
except Exception as _e:  # noqa: BLE001  迁移失败不得阻断启动
    app.logger.warning(f"AI 凭证库迁移失败（不影响启动，任务读侧仍回落旧口径）：{_e}")

# ⭐ AI 前置自检（P0-5）：启动即体检三个 AI 模块的配置完整性，缺 key / 缺 base_url 时
# 醒目告警（不阻断启动 —— 用户很可能正是启动后才去「AI 设置」页补配置）。
# 这里只做静态检查、不打外网，避免拖慢启动或让启动依赖外部网络；端点可达性在
# 开跑门禁（_ai_gate_or_400）与 /api/ai/selfcheck?probe=1 时才探测。
try:
    import ai_selfcheck
    AI_SELFCHECK_BOOT = ai_selfcheck.startup_report()
except Exception as _e:  # noqa: BLE001  自检失败不得阻断启动
    AI_SELFCHECK_BOOT = {}
    app.logger.warning(f"AI 前置自检执行失败（不影响启动）：{_e}")

# 初始化组件
script_gen = ScriptGenerator()
comfyui_client = ComfyUIClient()
video_processor = VideoPostProcessor()

# 无人值守托管：若存在已启用的托管计划，服务启动后自动接着生产（断点续跑）
# 用一个短延时线程延后启动，避免拖慢 Flask 首次响应；失败不影响服务可用性。
# 注意：若上次是用户「主动暂停」的，启动时尊重该状态，不擅自恢复生产。
def autopilot_boot_enabled() -> bool:
    """是否允许「随进程启动自动恢复生产」

    默认开启 —— 24/7 无人值守是本系统的主场景。
    但必须留一个逃生口：任何 `import app` 的短命脚本（单元验证、数据迁移、
    一次性批处理）都会触发 boot，从而与正在挂机的服务**抢同一块 GPU**。
    实测踩过这个坑：一条用于取路由列表的 `python -c "import app"` 直接
    启动了守护进程并开始跑图。需要这类脚本时设置 MJSCXT_AUTOPILOT=0 即可。
    """
    val = (os.getenv("MJSCXT_AUTOPILOT") or "").strip().lower()
    return val not in ("0", "false", "no", "off")


def _autopilot_boot():
    try:
        autopilot._restore_runtime()
        if autopilot.is_paused():
            app.logger.info("托管：上次为「已暂停」状态，启动后保持暂停（可在控制台恢复）")
            return
        enabled = autopilot.enabled_projects()
        if not enabled:
            app.logger.info("托管：没有已启用的项目，守护进程待命（可在控制台一键开启）")
            return
        app.logger.info("托管：检测到 %d 个启用项目，自动恢复生产", len(enabled))
        autopilot.resume()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"托管自动恢复失败（不影响服务）：{e}")


def _schedule_autopilot_boot(delay: float = 3.0):
    """按开关决定是否调度自动恢复（关闭时给出显式提示，避免误以为已托管）"""
    if not autopilot_boot_enabled():
        app.logger.info("托管：MJSCXT_AUTOPILOT=0，本次启动不自动恢复生产"
                        "（如需 24/7 托管请移除该环境变量）")
        return
    threading.Timer(delay, _autopilot_boot).start()


try:
    _schedule_autopilot_boot()
except Exception as _e:  # noqa: BLE001
    app.logger.warning(f"托管启动调度失败：{_e}")


def _safe_project(name: str) -> str:
    """项目名安全化（与项目注册表的项目键规则保持一致）"""
    return project_store.safe_key(name)


# B-16 P2-11：失败路径清理中间产物。把「生成失败 / 质检阻断 / 异常」时的 scratch
# 目录、.tmp 文件等中间产物统一删掉，避免只增不减。
def _cleanup_scratch_dir(dir_path: str, logger=None) -> None:
    """清空目录内容（保留目录本身），失败时只记日志不抛异常。"""
    import logging
    _log = logger or logging.getLogger(__name__)
    if not dir_path or not os.path.isdir(dir_path):
        return
    try:
        for fn in os.listdir(dir_path):
            fp = os.path.join(dir_path, fn)
            try:
                if os.path.isdir(fp):
                    import shutil
                    shutil.rmtree(fp, ignore_errors=True)
                else:
                    os.remove(fp)
            except OSError:
                _log.warning("清理中间产物失败：%s", fp)
        _log.info("已清理 scratch 目录：%s", dir_path)
    except OSError as e:
        _log.warning("清理 scratch 目录失败：%s", e)


# ===================== 质检不合格产物：统一移入回收站（可恢复） =====================
# 需求（用户原话）：「质检不合格的图片、提示词或视频要删除，不要留存在本地（包括
# ComfyUI 目录下的）」。这里统一收敛为**移入回收站**而非硬删 —— 误删好图好片是不可逆
# 事故，移入 `output/projects/_trash/qc_reject/<时间戳>_<项目>/` 可人工恢复/复核。
#
# ⚠️ 红线（缺一不可）：
#   1) 调用方必须用 `verdict.get("ok") is True and not gate["accept"]` 作为删除条件。
#      `ok=False`（接口故障/超时/鉴权失败）、`skipped=True`（质检未开启）、
#      `qc_declared 但 qc_on=False`（接口未就绪）**都不是产物不合格** —— 那些删下去会
#      把好图好片删光。本工具只负责「安全地移」，判定由调用方提供，工具内不再猜。
#   2) 路径必须落在 PROJECT_OUTPUT_DIR / COMFYUI_OUTPUT_DIR 之内；
#   3) 显式排除 PROJECT_TRASH_DIR（避免把自己的回收站再搬一层）；
#   4) 硬链接（st_nlink > 1）不释放空间、且可能被他处引用 → 拒绝移动。
_PURGE_REJECTED_ENV = "MJSCXT_PURGE_REJECTED"


def _purge_rejected_enabled() -> bool:
    """不合格产物清理开关。默认开启；设 `MJSCXT_PURGE_REJECTED=0` 时只记日志不移走。"""
    v = str(os.environ.get(_PURGE_REJECTED_ENV, "1")).strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _reject_artifact(paths, project: str = "", reason: str = "", kind: str = "") -> dict:
    """质检不合格产物 → 移入回收站（可恢复），**绝不硬删**。

    返回 ``{"moved": [...], "skipped": [...], "failed": [...]}``，三态都带
    `{"src":..., "why":...}`（moved 项另带 `dst`）。

    安全闸（任一不满足即跳过并记日志，绝不抛异常）：
      · 只接受绝对路径（相对路径无法可靠判边界，直接拒绝）；
      · `os.path.normpath(os.path.abspath(p))` 归一 —— 本项目已知坑：混合分隔符
        （`/` 与 `\\`）会让外部 API 静默匹配失败，必须先归一；
      · 必须落在 PROJECT_OUTPUT_DIR 或 COMFYUI_OUTPUT_DIR 之内（越界拒绝）；
      · 显式排除 PROJECT_TRASH_DIR（含 `_backup_*` / `_watermark_backup` 同理越界/排除）；
      · `os.path.lexists` 判存在 —— episode 成片 `move` 后 src 已消失是常态，
        不存在即跳过，**不能当异常**；
      · `os.stat().st_nlink == 1` 校验（硬链接不释放空间，见 disk_reclaim.py 的教训）；
      · 文件与目录都支持（目录走 shutil.move）。

    全程 try/except：清理是优化而非功能，**任何失败都不阻断主流程**，只 logger.warning。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    paths = [p for p in (paths or []) if p]
    if not paths:
        return res

    # 归一化的边界根（含尾分隔符，防止 /output 误匹配 /output2）
    def _root_ok(p: str) -> bool:
        cands = []
        for root in (PROJECT_OUTPUT_DIR, COMFYUI_OUTPUT_DIR):
            if root:
                try:
                    cands.append(os.path.normpath(os.path.abspath(root)) + os.sep)
                except Exception:  # noqa: BLE001
                    pass
        return any(p == c.rstrip(os.sep) or p.startswith(c) for c in cands)

    trash_abs = ""
    try:
        if PROJECT_TRASH_DIR:
            trash_abs = os.path.normpath(os.path.abspath(PROJECT_TRASH_DIR))
    except Exception:  # noqa: BLE001
        trash_abs = ""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "qc_reject",
                              f"{stamp}_{_safe_project(project or 'project')}")
    enabled = _purge_rejected_enabled()

    for raw in paths:
        try:
            if not os.path.isabs(raw):
                res["skipped"].append({"src": str(raw), "why": "非绝对路径"})
                app.logger.warning(
                    "[质检清理] 跳过：非绝对路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, raw)
                continue
            p = os.path.normpath(os.path.abspath(raw))
            if trash_abs and (p == trash_abs or p.startswith(trash_abs + os.sep)):
                res["skipped"].append({"src": p, "why": "回收站内，跳过"})
                continue
            if not _root_ok(p):
                res["skipped"].append({"src": p, "why": "越界（不在 output/ComfyUI 目录内）"})
                app.logger.warning(
                    "[质检清理] 拒绝越界路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            if not os.path.lexists(p):
                res["skipped"].append({"src": p, "why": "不存在"})
                continue
            try:
                if os.stat(p).st_nlink > 1:
                    res["skipped"].append({"src": p, "why": "硬链接（不释放空间）"})
                    app.logger.warning(
                        "[质检清理] 跳过硬链接（project=%s kind=%s reason=%s path=%s）",
                        project, kind, reason, p)
                    continue
            except OSError as se:
                res["failed"].append({"src": p, "why": f"stat 失败：{se}"})
                continue
            if not enabled:
                res["skipped"].append({"src": p, "why": f"{_PURGE_REJECTED_ENV}=0（仅记日志）"})
                app.logger.warning(
                    "[质检清理] 开关关闭，仅记日志不移走（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            os.makedirs(trash_root, exist_ok=True)
            base = os.path.basename(p.rstrip(os.sep)) or "artifact"
            dst = os.path.join(trash_root, base)
            # 同名冲突（同项目多次重试同名）→ 加序号，绝不覆盖已在回收站里的证据
            _n = 1
            while os.path.lexists(dst):
                stem, ext = os.path.splitext(base)
                dst = os.path.join(trash_root, f"{stem}__{_n}{ext}")
                _n += 1
            shutil.move(p, dst)
            res["moved"].append({"src": p, "dst": dst})
            app.logger.warning(
                "[质检清理] 不合格产物已移入回收站（project=%s kind=%s reason=%s）：%s → %s",
                project, kind, reason, p, dst)
        except Exception as e:  # noqa: BLE001  清理绝不能中断生产
            res["failed"].append({"src": str(raw), "why": f"{type(e).__name__}: {e}"})
            app.logger.warning("[质检清理] 移入回收站失败（已忽略，不影响主流程）：%s: %s",
                               type(e).__name__, e)
    return res


def _purge_rejected_artifacts(paths, project: str = "", reason: str = "", kind: str = "",
                              history_file: str = "") -> dict:
    """``_reject_artifact`` 的语义化包装：移走后顺手把「质检历史 file 断链」补掉。

    ⚠️ 质检历史 json 是排查依据，**只把断链的 `file` 字段置 null + 打 `purged` 标记**，
    绝不删除历史记录本身（否则事后无法查「当时为什么不合格」）。
    """
    out = _reject_artifact(paths, project=project, reason=reason, kind=kind)
    moved_srcs = {os.path.normpath(m["src"]) for m in out.get("moved") or []}
    if moved_srcs and history_file:
        try:
            _mark_history_file_purged(history_file, moved_srcs)
        except Exception as e:  # noqa: BLE001
            app.logger.warning("[质检清理] 质检历史 file 断链标记失败（忽略）：%s", e)
    return out


def _mark_history_file_purged(history_file: str, moved_srcs: set) -> None:
    """把质检历史里指向**已移走路径**的 `file` / `frames_f` 记为 null 并打 `purged=true`。

    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据，只是不再指向
    已不存在的本地文件（否则前端/排障脚本按路径取会 404）。
    """
    if not history_file or not os.path.isfile(history_file):
        return
    with open(history_file, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    hit = False
    for rec in (data.get("records") or []):
        if not isinstance(rec, dict):
            continue
        fp = rec.get("file")
        if fp and os.path.normpath(os.path.abspath(str(fp))) in moved_srcs:
            rec["file"] = None
            rec["purged"] = True
            hit = True
        # 抽帧图（整片质检）：整目录被移走 → 逐条把已消失的帧路径剔除
        frames = rec.get("frames_f")
        if isinstance(frames, list) and frames:
            kept = [x for x in frames
                    if os.path.normpath(os.path.abspath(str(x))) not in moved_srcs]
            if len(kept) != len(frames):
                rec["frames_f"] = kept
                rec["frames_purged"] = True
                hit = True
    if hit:
        atomic_write_json(history_file, data)
        app.logger.warning("[质检清理] 已标记质检历史断链：%s", history_file)


def _purge_prompt_records(project: str, shot_key, reason: str = "") -> dict:
    """提示词预检不通过 → 移走该镜**唯一落盘物** `prompt_<shot>.json`（P12）。

    用户决策 1：``prompt_qc.preflight`` 是生成前的文本合规检查，不通过直接阻断不生成，
    因此没有图片/视频可删 —— 只有这份提示词历史 json 留在了本地。
    """
    try:
        key = qc_client.safe_token(shot_key, "0")
        path = os.path.join(QC_DIR, _safe_project(project or "project"), f"prompt_{key}.json")
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检清理] 提示词落盘物路径解析失败（忽略）：%s", e)
        return {"moved": [], "skipped": [], "failed": []}
    return _reject_artifact([path], project=project, reason=reason or "提示词预检未通过",
                            kind="prompt")


def _purge_sb_refs(project: str) -> dict:
    """分镜参考图上传残留（ComfyUI output/sb_ref_*.png）按项目清理（P13）。

    ⚠️ 与「不合格产物」是**两码事**：`sb_ref_*` 是分镜**参考图输入**（上传给
    LoadImageOutput 的中间件），不是质检产物，**绝不能混进普通「不合格即删」逻辑**
    （那会在单镜重试中途删掉当前镜头正在用的参考图）。按用户决策 4：只在**本轮分镜
    批量生成循环全部结束后**调用一次，按项目前缀收口。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    try:
        root = COMFYUI_OUTPUT_DIR
        if not root or not os.path.isdir(root):
            return res
        # generate_storyboard 的命名：sb_ref_<filename_prefix 的 basename>_<idx>.png，
        # 分镜链路 filename_prefix 形如 `comic_drama_sb/<项目>_shot_NN[...]`，
        # 故前缀里含 `<项目>_shot_`。
        pref = f"sb_ref_{_safe_project(project or '')}_shot_"
        targets = []
        for fn in os.listdir(root):
            if fn.startswith(pref) and fn.lower().endswith(".png"):
                targets.append(os.path.join(root, fn))
        if not targets:
            return res
        res = _reject_artifact(targets, project=project,
                               reason="分镜参考图上传残留（本轮分镜生成结束）",
                               kind="sb_ref")
    except Exception as e:  # noqa: BLE001
        app.logger.warning("[质检清理] sb_ref 清理失败（忽略，不影响生产）：%s: %s",
                           type(e).__name__, e)
    return res


# B-14 P2-4：资产「取图判据」统一入口。就绪判据（_collect_asset_refs 的
# _first_nonempty_image）与取图判据（_build_asset_index 的 _first_existing）
# 此前各自维护一套「判有图」逻辑，口径漂移（一个只认 4 个扩展名、另一个只
# 认 front/base 固定名）。统一为：扩展名白名单 + 取第一张非空图片。
_ASSET_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

#: 资产目录内「主视角图」的取图优先级（2026-09-24 修复）。
#: ⚠️ 实测 bug：原来只按**文件名字典序**取第一张非空图，而角色资产目录里
#:    back.png < base.png < front.png < left.png < right.png —— 于是 `back.png`
#:    （**背面图**）被当成「主角锚点」喂给分镜/视频链路（_build_asset_index 在
#:    剧本 characters 没有 front/base 字段时就走这条兜底，autopilot 正是这种形态）。
#:    当时看不出来，是因为 4 张视角图与 base 内容完全一致（都是同一张三视图整图）；
#:    一旦视角图变成真单机位（2026-09-24 sheet_split 改造后就是如此），
#:    这个字典序兜底就会静默地把每个镜头的角色锚点换成「只有背面」。
#: 故改为显式优先级：正面 > 整图 > 左侧 > 右侧 > 背面 > 其它图片（字典序）。
_ASSET_IMG_PRIORITY = ("front.png", "base.png", "front.jpg", "base.jpg",
                       "left.png", "right.png", "back.png",
                       "left.jpg", "right.jpg", "back.jpg")


def _first_existing_asset_image(directory: str) -> str:
    """在目录内取「主视角」图片（扩展名白名单），无则返回 ''。

    统一判据：
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")；
      2) 按 :data:`_ASSET_IMG_PRIORITY` 显式优先级取（**不是**文件名字典序，
         否则 back.png 会排在 base/front 之前被取走，详见该常量注释）；
      3) 优先级名都不存在时，再按字典序取第一张非空图片；
      4) 找不到任何图片 → 返回 ''，调用方自行决策（报错/跳过）。
    """
    if not directory or not os.path.isdir(directory):
        return ""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return ""

    def _ok(fn: str) -> bool:
        return (fn.lower().endswith(_ASSET_IMG_EXTS)
                and os.path.isfile(os.path.join(directory, fn))
                and os.path.getsize(os.path.join(directory, fn)) > 0)

    for cand in _ASSET_IMG_PRIORITY:
        if cand in entries and _ok(cand):
            return os.path.join(directory, cand)
    for fn in entries:
        if _ok(fn):
            return os.path.join(directory, fn)
    return ""


# B-20 P2-10：磁盘余量检查。大体积写入（视频/图片/混音成片）前确认剩余空间，
# 不足时 fail-loud（logger.warning + 返回 False），不静默写入导致半截文件。
def _ensure_disk_headroom(directory: str, min_bytes: int, logger=None) -> bool:
    """检查 directory 所在分区剩余空间是否 ≥ min_bytes。
    返回 True（充足）/ False（不足，已记 warning）。失败时不影响调用方继续。
    """
    _log = logger
    try:
        # 用 shutil.disk_usage 取实际分区剩余空间
        usage = shutil.disk_usage(os.path.abspath(directory))
        free = usage.free
        if free < min_bytes:
            if _log:
                _log.warning("磁盘余量不足：%s 剩余 %.1fMB < 需要 %.1fMB",
                             os.path.abspath(directory), free / 1048576, min_bytes / 1048576)
            return False
        return True
    except Exception as e:
        if _log:
            _log.warning("磁盘余量检查失败（已放行）：%s", e)
        return True


def _project_or_400(raw, field_name="project_name"):
    """G4 收口：路由层「项目入参 → 安全键 / 400」的统一入口。

    ⚠️ 不能写 `_safe_project(x) or 兜底`、也不能判 `_safe_project(x)` 的真值——
    `safe_key('')` 返回**字面量 'project'**（真值），守卫恒不成立（死守卫），
    漏传项目名会静默写进共享 `project` 命名空间。判空必须看**原始入参**
    （与 api_qc_project_summary 的 G3 修复同一口径）。

    A-01（F-01）加固：额外**拒绝路径穿越**入参（含 `..` / 绝对路径 / 路径分隔符）。
    仅靠 `safe_key` 收敛会把 `../../evil` 静默变成合法键 `evil`——虽不越界写盘，
    但把越界尝试当成正常项目混淆视听；此处直接 400，作到「越界即拒 + 不落盘」。
    收敛后仍做一次 abspath 前缀校验作为双保险（防御未来 safe_key 规则变更）。

    返回 (project, error)：error 为 None 表示合法（project 已 safe_key）；
    否则 error 是 (jsonify, 400) 响应，直接 return 它。
    用法::

        project, err = _project_or_400((data.get('project_name') or '').strip())
        if err is not None:
            return err
    """
    if not (isinstance(raw, str) and raw.strip()):
        return "", (jsonify({"success": False, "error": f"缺少 {field_name}"}), 400)
    raw_s = raw.strip()
    # task#7 口径补齐：含控制字符（如 NUL `\x00`）/ **无任何有效字符**（如 `.` `。` `…`）的
    # 入参 → 与空串**同口径 400**。否则 `safe_key` 会把它们坍缩成共享默认键 `project`
    # （非越界、无写盘，但会静默写进共享命名空间，且与空串口径不一致、掩盖调用方 bug）。
    # ⚠️ 判「有效字符」只看 isalnum/_/-（与 safe_key 的存活字符一致）：中文名（isalnum 为真，
    #    如「剑影孤城」「蛊真人精校版」）照常通过，绝不被误杀。
    if any(ord(_c) < 32 or ord(_c) == 0x7f for _c in raw_s):
        app.logger.warning("[task#7] 拒绝含控制字符的项目名：%r", raw_s)
        return "", (jsonify({"success": False,
                             "error": f"非法的 {field_name}（含控制字符）"}), 400)
    _cleaned = re.sub(r"[《》〈〉【】「」『』]", "", raw_s)
    if not any((_c.isalnum() or _c in "_-") for _c in _cleaned):
        app.logger.warning("[task#7] 拒绝无有效字符的项目名（与空串同口径）：%r", raw_s)
        return "", (jsonify({"success": False,
                             "error": f"非法的 {field_name}（无有效字符）"}), 400)
    if (raw_s.startswith(("/", "\\")) or ".." in raw_s
            or "/" in raw_s or "\\" in raw_s
            or os.path.isabs(raw_s) or os.path.splitdrive(raw_s)[0]):
        app.logger.warning("[A-01] 拒绝越界项目名（疑似路径穿越）：%r", raw_s)
        return "", (jsonify({
            "success": False,
            "error": f"非法的 {field_name}（禁止路径分隔符 / 绝对路径 / 「..」）"}), 400)
    project = _safe_project(raw_s)
    _root = os.path.abspath(PROJECT_OUTPUT_DIR)
    _pdir = os.path.abspath(os.path.join(PROJECT_OUTPUT_DIR, project))
    if not _pdir.startswith(_root + os.sep):
        app.logger.warning("[A-01] 项目名收敛后仍越界，拒绝：%r → %r", raw_s, project)
        return "", (jsonify({"success": False, "error": f"非法的 {field_name}"}), 400)
    return project, None


def _serve_safe(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的 send_file 统一入口。

    返回 send_file(...) 或 403/404 的 Flask 响应；永不抛异常。
    用法：``return _serve_safe(KEYFRAMES_DIR, filename)``。

    防护原理（S-06）：
    - 旧写法用 ``os.path.normpath(filename).startswith('..')`` 拦截穿越，
      但 ``os.path.join(base, safe_path)`` 遇**绝对路径**（如 ``C:\\Windows``、``/etc/passwd``）
      会丢弃 base 前缀直接返回绝对路径 → 穿越成功。
    - 这里用 ``os.path.abspath`` 归一后校验目标路径必须落在 base_dir 之内
      （前缀匹配 base_dir + 分隔符），从根上杜绝穿越。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    # 必须严格落在 base 之内：base 本身（目录）或 base + 分隔符 开头
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    return send_file(target, **send_kw)


def _serve_attachment(base_dir: str, filename: str, **send_kw):
    """目录穿越防护的附件下载统一入口（as_attachment + download_name）。

    与 _serve_safe 防护逻辑相同，但额外指定 as_attachment=True 与 download_name。
    """
    base = os.path.abspath(base_dir)
    target = os.path.abspath(os.path.join(base, filename or ""))
    if not (target == base or target.startswith(base + os.sep)):
        return abort(403)
    if not os.path.isfile(target):
        return abort(404)
    send_kw.setdefault("as_attachment", True)
    send_kw.setdefault("download_name", os.path.basename(target))
    return send_file(target, **send_kw)


def _body() -> dict:
    """统一取请求 body，**永不抛异常**（返回 ``{}`` 兜底）

    为什么不用裸 ``request.json``：Flask 在 Content-Type 不是 application/json 时抛
    ``UnsupportedMediaType``（415），body 非法 JSON 时抛 ``BadRequest``（400）。
    这类错误会让接口以 4xx 结束，而不是「按缺参数处理并给出可读错误」——
    用 ``curl -d '{}'``（默认表单 Content-Type）调就会直接 415，排查成本很高。
    项目约定：所有取 body 的地方统一走这里。
    """
    return request.get_json(silent=True) or {}


# ==========================================================================
# 集级产物目录（多集自动生产的必要条件）
# --------------------------------------------------------------------------
# 背景：配音文件名早已含集号（ep{NN}_shot{NN}_角色.wav），但分镜图 / 尾帧 / 视频
# 历史上按 shot_NN 平铺存放，**多集生产会互相覆盖**——第 2 集的分镜图会覆盖第 1 集的。
# 手动流程一次只做一集，问题不暴露；24 小时托管必须解决。
#
# 策略：第 1 集沿用平铺目录（现有项目、现有前端、现有数据完全不受影响），
#       第 2 集起写入 epNN/ 子目录。读取时优先集目录、回落平铺目录，兼容旧数据。
# ==========================================================================

def _ep_dir(base_dir: str, episode_no=None) -> str:
    """写入用的集级产物目录（第 1 集 = 平铺目录）"""
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1
    if ep <= 1:
        return base_dir
    return os.path.join(base_dir, f"ep{ep:02d}")


def _ep_read_dir(base_dir: str, project: str, episode_no=None) -> str:
    """读取用的集级产物目录：优先集目录，不存在则回落平铺目录（兼容旧数据）"""
    flat = os.path.join(base_dir, project)
    d = _ep_dir(flat, episode_no)
    if os.path.isdir(d) and d != flat:
        return d
    return flat if os.path.isdir(flat) else d


def _ep_of_script(script: dict, fallback=None):
    """从剧本里取集号（metadata.episode_no > 顶层 episode_no > 兜底）"""
    if not isinstance(script, dict):
        return fallback
    meta = script.get("metadata") or {}
    for v in (meta.get("episode_no"), script.get("episode_no"), fallback):
        try:
            if v is not None and str(v).strip() != "":
                return int(v)
        except (TypeError, ValueError):
            continue
    return fallback


def _episode_schema_defaults(project_name: str, shots: list) -> dict:
    """⑥ 下游链路自动引用剧本自动判定的「镜头数 / 每集时长」字段。

    - 镜头缺 duration 时按项目配置的 duration_per_shot 兜底；
    - 返回 episode_stats（shot_count / duration_sec / episode_plan）供接口回显与后续步骤使用。
    """
    cfg = {}
    try:
        cfg = project_store.read_config(project_name)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取项目配置失败（{project_name}）：{e}")
    per_shot = cfg.get("duration_per_shot") or 5
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        if not s.get("duration"):
            s["duration"] = per_shot
    return novel_to_script.build_episode_stats(shots)


# ===== 项目管理（A：每部小说 = 一个独立项目） =====

@app.route('/api/projects', methods=['GET', 'POST'])
def api_projects_list():
    if request.method == 'POST':
        return api_projects_create()
    with_stats = (request.args.get('stats', '1') not in ('0', 'false', 'no'))
    projects = project_store.list_projects(with_stats=with_stats)
    return jsonify({"success": True, "total": len(projects), "projects": projects,
                    "index_path": project_store.PROJECT_INDEX_PATH})


@app.route('/api/projects', methods=['POST'])
def api_projects_create():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "项目名称不能为空"}), 400
    pid = (data.get('id') or '').strip() or None
    existing = project_store.get_project(pid) if pid else None
    if existing:
        return jsonify({"error": f"项目 ID 已存在：{pid}", "project": existing}), 409
    novel_id = (data.get('novel_id') or '').strip()
    novel_name = (data.get('novel_name') or '').strip()
    # 前端只传 novel_id，不传 novel_name。此前直接 `or name` 兜底，等于把
    # 「小说名」写成了「项目名」（两者通常不同，项目名可以是任意自定义名称）。
    # 这里回查小说库取真实标题，取不到才退回项目名。
    if novel_id and not novel_name:
        try:
            meta = get_novel(NOVELS_DIR, novel_id) or {}
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"读取小说元信息失败 {novel_id}: {e}")
            meta = {}
        novel_name = str(meta.get('title') or meta.get('name') or '').strip()
    rec = project_store.create_project(
        name,
        novel_id=novel_id,
        novel_name=novel_name or name,
        config=data.get('config') if isinstance(data.get('config'), dict) else None,
        pid=pid,
    )
    return jsonify({"success": True, "project": project_store.summarize(rec["id"]),
                    "created": True})


@app.route('/api/projects/ensure-for-novel', methods=['POST'])
def api_projects_ensure_for_novel():
    """确保某部小说有对应项目（有则复用，无则创建），并绑定小说

    自动生产控制台的第 1 步：用户上传小说后，前端立刻调用本接口把项目建好并选中，
    避免「小说传上来了但托管没项目可生产」的断档。名称取小说标题（清洗后）。
    """
    data = request.json or {}
    novel_id = str(data.get('novel_id') or '').strip()
    if not novel_id:
        return jsonify({"success": False, "error": "缺少 novel_id"}), 400
    meta = {}
    try:
        meta = get_novel(NOVELS_DIR, novel_id) or {}
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"小说读取失败：{e}"}), 400
    if not meta:
        return jsonify({"success": False, "error": f"小说不存在：{novel_id}"}), 404
    raw_name = (data.get('name') or meta.get('title') or meta.get('name')
                or novel_id)
    name = re.sub(r"[《》〈〉【】「」『』\s]+", "", str(raw_name)).strip() or novel_id
    rec = project_store.ensure_project_for_novel(novel_id, name)
    if not rec:
        return jsonify({"success": False, "error": "项目创建失败"}), 500
    return jsonify({"success": True, "project": project_store.summarize(rec["id"]),
                    "novel_id": novel_id,
                    "project_key": rec.get("dir_key") or rec.get("name")})


@app.route('/api/projects/<path:pid>', methods=['GET'])
def api_project_detail(pid):
    detail = project_store.summarize(pid)
    if not detail:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    scripts = []
    for sp in project_store.project_scripts(detail["dir_key"]):
        st = project_store.script_stats(sp)
        st.update({"path": sp})
        scripts.append(st)
    detail["scripts"] = scripts
    detail["gallery_summary"] = project_store.gallery_summary(detail["dir_key"])
    detail["product_dirs"] = {
        k: [os.path.basename(d) for d in project_store.project_dirs(v, detail["dir_key"])]
        for k, v in {"characters": CHARACTERS_DIR, "items": ITEMS_DIR, "scenes": SCENES_DIR,
                     "storyboards": STORYBOARDS_DIR, "videos": VIDEOS_DIR,
                     "final": FINAL_DIR, "upscale": UPSCALE_DIR, "dub": DUB_DIR}.items()
    }
    return jsonify({"success": True, "project": detail})


@app.route('/api/projects/<path:pid>/detail', methods=['GET'])
def api_project_detail_alias(pid):
    return api_project_detail(pid)


@app.route('/api/projects/<path:pid>/novel-brief', methods=['GET'])
def api_project_novel_brief(pid):
    """本项目「原著简报」：书名 + 章节数 + 开篇正文 + 已定风格。

    ⚠️ 2026-09-19 用户旅程实测教训：AI 总控此前**没有任何读取本项目小说的能力**，
    而它在「启动生产前先与用户沟通风格」这一步必须知道原著讲什么。缺了这个接口，
    模型只能靠上下文里别的东西瞎猜 —— 实测拿《蛊真人》的方案回答了《铜铃巷》的项目。
    """
    rec = project_store.summarize(pid)
    if not rec:
        return jsonify({"success": False, "error": f"项目不存在：{pid}"}), 404
    dir_key = rec.get("dir_key") or rec.get("key") or pid
    try:
        cfg = project_store.read_config(dir_key) or {}
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取项目配置失败（%s）：%s", dir_key, e)
        cfg = {}
    novel_id = str(rec.get("novel_id") or "")
    out = {
        "project": rec.get("name") or dir_key,
        "project_key": dir_key,
        "style": cfg.get("style") or "",
        "art_style": cfg.get("art_style") or "",
        "episodes": cfg.get("episodes"),
        "target_shots": cfg.get("target_shots") or cfg.get("shots_per_episode"),
        "novel_id": novel_id,
        "novel_name": rec.get("novel_name") or "",
        "novel_title": "",
        "chapter_count": 0,
        "preview": "",
        "preview_chars": 0,
        "total_chars": 0,
        "note": "",
    }
    if not novel_id:
        out["note"] = "该项目未关联小说，无法给出原著依据"
        return jsonify({"success": True, **out})
    try:
        meta = get_novel(NOVELS_DIR, novel_id) or {}
        out["novel_title"] = meta.get("title") or ""
        out["chapter_count"] = len(meta.get("chapters") or [])
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取小说元信息失败（%s）：%s", novel_id, e)
    try:
        pv = preview_novel(NOVELS_DIR, novel_id, offset=0, limit=NOVEL_BRIEF_CHARS)
        out["preview"] = pv.get("text") or ""
        out["preview_chars"] = len(out["preview"])
        out["total_chars"] = pv.get("total_chars") or 0
    except Exception as e:  # noqa: BLE001
        out["note"] = f"原著正文读取失败：{e}"
        app.logger.warning("读取小说正文失败（%s）：%s", novel_id, e)
    return jsonify({"success": True, **out})


@app.route('/api/projects/<path:pid>/rename', methods=['POST'])
def api_project_rename(pid):
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "新项目名不能为空"}), 400
    rec = project_store.rename_project(pid, name)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    return jsonify({"success": True, "project": project_store.summarize(rec["id"]),
                    "note": "项目键与目录未变，已有产物保持原样，零丢失"})


@app.route('/api/projects/<path:pid>/config', methods=['GET', 'POST'])
def api_project_config(pid):
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    if request.method == 'GET':
        return jsonify({"success": True, "project": rec["dir_key"],
                        "config": project_store.read_config(rec["dir_key"]),
                        "config_path": project_store.paths(rec["dir_key"])["config"]})
    cfg = project_store.update_config(rec["dir_key"], request.json or {})
    return jsonify({"success": True, "project": rec["dir_key"], "config": cfg,
                    "project_record": project_store.get_project(rec["id"])})


# ===== 项目封面（项目中心卡片对外展示的第一张图） =====

def _project_cover_path(dir_key: str) -> str:
    return os.path.join(project_store.paths(dir_key)["root"], "cover.png")


def _generate_project_cover(rec: dict, seed=None) -> str:
    """生成项目封面并落到项目根目录 cover.png，返回落盘绝对路径。

    走场景生图链路（16:9 横版，与项目卡片 aspect-video 一致）：场景模板自带
    去人 + 去水印 + 负向冲突清理，比裸 t2i 稳；封面要的是氛围主视觉而非人像
    （项目刚建时角色资产多半还没生成，也避开人物一致性问题）。
    """
    dir_key = rec["dir_key"]
    cfg = project_store.read_config(dir_key)
    style = str(cfg.get("style") or "").strip()
    name = str(rec.get("name") or "").strip()
    prompt = (f"漫剧主视觉封面插画，《{name}》主题氛围场景，戏剧性光影，电影感构图，"
              f"景深层次丰富，高细节，画面中不出现任何文字")
    size = style_kit.aspect_size((16, 9)) or (960, 544)
    outs = comfyui_client.generate_scene_base(
        prompt, seed=seed, style=style, size=size,
        filename_prefix=f"comic_drama/{dir_key}_cover")
    if not outs:
        raise RuntimeError("ComfyUI 未返回任何图片")
    cover = _project_cover_path(dir_key)
    os.makedirs(os.path.dirname(cover), exist_ok=True)
    shutil.move(outs[0], cover)   # G8② 同款：move 而非 copy，ComfyUI output 不留副本
    return cover


@app.route('/api/projects/<path:pid>/cover')
def api_project_cover(pid):
    """项目封面图（无封面 404，前端回落占位图标）"""
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    cover = _project_cover_path(rec["dir_key"])
    if not os.path.isfile(cover):
        abort(404)
    return send_file(cover, conditional=True)


@app.route('/api/projects/<path:pid>/cover/generate', methods=['POST'])
def api_project_cover_generate(pid):
    """生成项目封面（同步阻塞，单图 t2i 约 10~60s）。

    故意不设 AI/总控确认门禁：封面属装饰性产物（与分镜图同理不挂 LLM 门禁），
    且项目刚建时总控设定往往还没敲定，门禁会把「建完项目就想给个封面」拦死。
    body 可选 {"seed": int}（换一张）。
    """
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    data = request.json or {}
    try:
        cover = _generate_project_cover(rec, seed=data.get('seed'))
    except Exception as e:  # noqa: BLE001
        app.logger.error(f"封面生成失败 {rec['dir_key']}: {e}")
        return jsonify({"success": False, "error": f"封面生成失败：{e}"}), 500
    return jsonify({"success": True, "cover_path": cover,
                    "cover_url": f"/api/projects/{rec['dir_key']}/cover?t={int(time.time())}"})


@app.route('/api/projects/<path:pid>/delete', methods=['POST'])
def api_project_delete(pid):
    data = request.json or {}
    confirm = bool(data.get('confirm'))
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    if not confirm:
        return jsonify({
            "error": "删除项目需要二次确认",
            "requires_confirm": True,
            "warning": (f"将把项目《{rec['name']}》的工作区与全部产物目录"
                        f"（剧本/角色/物品/场景/分镜/视频/成片/超分/配音/质检）"
                        f"整体移入回收站 {project_store.PROJECT_TRASH_DIR}，可从磁盘还原，非物理删除。"),
            "project": rec,
            "stats": project_store.project_stats(rec["dir_key"]),
        }), 409
    result = project_store.delete_project(rec["id"], confirm=True)
    return jsonify({"success": True, **result})


@app.route('/api/projects/migrate', methods=['POST'])
def api_projects_migrate():
    report = project_store.migrate_legacy()
    return jsonify({"success": True, "report": report,
                    "report_path": project_store.PROJECT_MIGRATE_REPORT})


@app.route('/api/projects/<path:pid>/scripts', methods=['GET'])
def api_project_scripts(pid):
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    rows = []
    for sp in project_store.project_scripts(rec["dir_key"]):
        st = project_store.script_stats(sp)
        st["path"] = sp
        rows.append(st)
    return jsonify({"success": True, "project": rec["dir_key"], "total": len(rows),
                    "scripts": rows})


# ===== 资产点击查看（B：角色 / 场景 / 物品 / 分镜详情） =====

_PROJECT_KIND_DIRS = {"characters": CHARACTERS_DIR, "items": ITEMS_DIR,
                      "scenes": SCENES_DIR, "storyboards": STORYBOARDS_DIR,
                      "videos": VIDEOS_DIR, "final": FINAL_DIR,
                      "upscale": UPSCALE_DIR, "dub": DUB_DIR, "qc": QC_DIR}


@app.route('/api/projects/<path:pid>/assets', methods=['GET'])
def api_project_assets(pid):
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    key = rec["dir_key"]
    gallery = project_store.asset_gallery(key)
    # 分镜图（点击可进入详情）
    storyboards = []
    for d in project_store.project_dirs(project_store.paths(key)["storyboards"], key):
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".png"):
                continue
            full = os.path.join(d, fn)
            storyboards.append({
                "name": os.path.splitext(fn)[0],
                "file": full,
                "size": os.path.getsize(full),
                "url": f"/api/storyboards/file/{os.path.basename(d)}/{fn}",
                "project_dir": os.path.basename(d),
            })
    # 视频 / 成片 / 超分 / 配音 产物（按项目隔离后的实际目录）
    def _files(root, exts):
        out = []
        for d in project_store.project_dirs(root, key):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(exts):
                    full = os.path.join(d, fn)
                    out.append({"name": fn, "file": full, "size": os.path.getsize(full),
                                "project_dir": os.path.basename(d)})
        return out
    p = project_store.paths(key)
    return jsonify({"success": True, "project": key, "project_id": rec["id"],
                    "gallery": gallery,
                    "storyboards": storyboards,
                    "videos": _files(p["videos"], (".mp4", ".mov", ".mkv")),
                    "final": _files(p["final"], (".mp4", ".mov", ".mkv")),
                    "upscale": _files(p["upscale"], (".mp4", ".mov", ".mkv")),
                    "dub": _files(p["dub"], (".wav", ".flac", ".mp3")),
                    "counts": {k: len(v) for k, v in gallery.items()},
                    "product_dirs": {k: [os.path.basename(d) for d in project_store.project_dirs(v, key)]
                                     for k, v in {"characters": CHARACTERS_DIR, "items": ITEMS_DIR,
                                                  "scenes": SCENES_DIR, "storyboards": STORYBOARDS_DIR,
                                                  "videos": VIDEOS_DIR, "final": FINAL_DIR,
                                                  "upscale": UPSCALE_DIR, "dub": DUB_DIR}.items()}})


@app.route('/api/projects/<path:pid>/asset-detail', methods=['GET'])
def api_project_asset_detail(pid):
    """资产条目点击后的详情：大图列表 + 名称 / 提示词 / 所属镜头 + 下载 + 重新生成入口"""
    rec = project_store.get_project(pid)
    if not rec:
        return jsonify({"error": "项目不存在", "ref": pid}), 404
    kind = (request.args.get('kind') or 'character').strip().lower()
    name = (request.args.get('name') or '').strip()
    key = rec["dir_key"]
    p = project_store.paths(key)

    kind_map = {
        "character": ("characters", p["characters"], "character"),
        "characters": ("characters", p["characters"], "character"),
        "item": ("items", p["items"], "item"),
        "items": ("items", p["items"], "item"),
        "scene": ("scenes", p["scenes"], "scene"),
        "scenes": ("scenes", p["scenes"], "scene"),
        "storyboard": ("storyboards", p["storyboards"], "storyboard"),
        "storyboards": ("storyboards", p["storyboards"], "storyboard"),
    }
    if kind not in kind_map:
        return jsonify({"error": f"不支持的资产类型：{kind}",
                        "supported": sorted(set(kind_map.keys()))}), 400
    folder_kind, folder, asset_type = kind_map[kind]

    detail_scripts = []
    for sp in project_store.project_scripts(key):
        try:
            with open(sp, "r", encoding="utf-8") as f:
                detail_scripts.append({"path": sp, "data": json.load(f)})
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"剧本读取失败（{sp}）：{e}")

    if asset_type == "storyboard":
        sid = re.sub(r"[^0-9]", "", name) or "1"
        png = project_store.find_asset_file(key, "storyboards", f"shot_{int(sid):02d}.png") \
            if sid.isdigit() else ""
        if not png:
            hits = []
            for d in project_store.project_dirs(p["storyboards"], key):
                hits += [os.path.join(d, f) for f in sorted(os.listdir(d))
                         if f.lower().endswith('.png') and sid in f]
            png = hits[0] if hits else ""
        views = [{
            "view": "storyboard", "file": png, "size": os.path.getsize(png),
            "url": f"/api/storyboards/file/{os.path.basename(os.path.dirname(png))}/{os.path.basename(png)}",
            "download_url": f"/api/storyboards/file/{os.path.basename(os.path.dirname(png))}/{os.path.basename(png)}",
        }] if png and os.path.exists(png) else []
        shot_meta = {}
        for sp in detail_scripts:
            for sh in (sp["data"].get("shots") or []):
                if str(sh.get("shot_id")) == str(int(sid) if sid.isdigit() else sid):
                    shot_meta = {k: sh.get(k) for k in
                                 ("shot_id", "duration", "camera", "location", "description",
                                  "dialogue", "emotion", "prompt_h3", "episode")}
                    shot_meta["script_path"] = sp["path"]
                    break
            if shot_meta:
                break
        manifest = {}
        for d in project_store.project_dirs(p["storyboards"], key):
            mpath = os.path.join(d, "storyboard_manifest.json")
            if os.path.exists(mpath):
                manifest = (project_store._read_json(mpath, {}) or {})
                manifest = next((s for s in (manifest.get("shots") or [])
                                 if str(s.get("shot_id")) == str(int(sid) if sid.isdigit() else sid)), {})
                if manifest:
                    break
        return jsonify({
            "success": True, "kind": "storyboard", "name": f"shot_{sid}",
            "project": key, "project_id": rec["id"],
            "views": views, "exists": bool(views),
            "meta": {"shot": shot_meta, "manifest": manifest,
                     "description": shot_meta.get("description") or manifest.get("prompt") or "",
                     # 分镜图提示词：优先给**实际用于分镜图生成**的那条
                     # （build_storyboard_prompt 会优先采用 storyboard_prompt_zh / description，
                     #   manifest.prompt 就是当时真正提交给 ComfyUI 的提示词）
                     "prompt": (manifest.get("prompt")
                                or shot_meta.get("storyboard_prompt_zh")
                                or shot_meta.get("description") or ""),
                     # 视频提示词单列，避免与分镜图提示词混在一个字段里
                     "video_prompt": shot_meta.get("prompt_h3") or "",
                     "shot_id": shot_meta.get("shot_id") or (int(sid) if sid.isdigit() else sid),
                     "duration_sec": shot_meta.get("duration"),
                     "location": shot_meta.get("location"),
                     "dialogue": shot_meta.get("dialogue")},
            "regenerate": {"endpoint": "/api/storyboards/generate", "method": "POST",
                           "payload": {"project_name": (os.path.basename(os.path.dirname(png))
                                                        if png else key),
                                       "shots": [shot_meta] if shot_meta else [{"shot_id": sid}],
                                       "limit": 1}},
            "downloads": [v["url"] for v in views],
        })

    # 角色 / 物品 / 场景
    if not name or os.sep in name or "/" in name or ".." in name:
        return jsonify({"error": "资产名称非法", "name": name}), 400
    hit_dir = project_store.find_kind_dir(key, folder_kind, name)
    target = hit_dir["asset_dir"]
    if not target:
        return jsonify({"error": f"未找到资产：{name}", "kind": folder_kind,
                        "searched_root": hit_dir["kind_root"]}), 404
    views = project_store._views_of(target)
    base = next((v for v in views if v["view"] == "base"), None)
    meta = {}
    for sp in detail_scripts:
        bucket = sp["data"].get(folder_kind) or []
        hit = next((x for x in bucket if isinstance(x, dict) and x.get("name") == name), None)
        if hit:
            meta = dict(hit)
            meta["script_path"] = sp["path"]
            break
    shot_refs = []
    for sp in detail_scripts:
        for sh in (sp["data"].get("shots") or []):
            if name in (sh.get("characters_in_shot") or []) or name in (sh.get("items_in_shot") or []) \
                    or sh.get("location") == name:
                shot_refs.append({"shot_id": sh.get("shot_id"),
                                  "duration_sec": sh.get("duration"),
                                  "episode": sh.get("episode"),
                                  "script_path": sp["path"]})
    return jsonify({
        "success": True, "kind": asset_type, "name": name,
        "project": key, "project_id": rec["id"],
        "views": views, "exists": bool(views),
        "thumb_url": (base or (views[0] if views else {})).get("url") if views else "",
        "meta": {
            "name": name,
            "prompt_zh": meta.get("reference_prompt_zh") or "",
            "prompt_en": meta.get("reference_prompt_en") or "",
            "appearance": meta.get("appearance") or "",
            "personality": meta.get("personality") or "",
            "age": meta.get("age") or "",
            "voice_style": meta.get("voice_style") or "",
            "category": meta.get("category") or "",
            "owner": meta.get("owner") or "",
            "location": meta.get("location") or "",
            "script_path": meta.get("script_path") or "",
            "shots": sorted(shot_refs, key=lambda r: (r.get("episode") or 1, r.get("shot_id") or 0))[:60],
        },
        "regenerate": {"endpoint": "/api/assets/generate", "method": "POST",
                       "payload": {"asset_type": asset_type,
                                   "project_name": hit_dir["project_dir"] or key,
                                   "overwrite": True,
                                   "assets": [{"name": name,
                                               "reference_prompt_zh": meta.get("reference_prompt_zh") or "",
                                               "reference_prompt_en": meta.get("reference_prompt_en") or "",
                                               "appearance": meta.get("appearance") or ""}]}},
        "downloads": [v["url"] for v in views],
    })


# ===== 页面（Vite SPA）=====
_STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')

@app.route('/')
def index():
    return send_from_directory(_STATIC_DIR, 'index.html')


@app.route('/assets/<path:filename>')
def static_assets(filename):
    """服务 Vite 构建的静态资源"""
    return send_from_directory(os.path.join(_STATIC_DIR, 'assets'), filename)


@app.route('/vite.svg')
def vite_icon():
    """Vite favicon 回退（旧版本 index.html 仍可能引用，保留向后兼容）"""
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">⚡</text></svg>'


@app.route('/favicon.svg')
def favicon_svg():
    """站点图标：由 Vite 从 frontend/public/favicon.svg 复制到 static 根目录"""
    return send_from_directory(_STATIC_DIR, 'favicon.svg')


# ===== 状态 =====

def _task_queue_status() -> dict:
    """TaskQueue 状态 + 「未接线」显式标注（N2，2026-09-22 复验）。

    ``TaskQueue.submit`` 在本项目**没有生产调用方**：单 GPU 并发由 ``gpu_task_gate``
    的进程级 Semaphore 承担（见 ``gpu_task_gate.py`` 模块头「不做什么」）。D-07 给
    submit 加的背压/去重是该模块**自身契约**的加固，供嵌入使用与测试。这里显式标注
    ``wired=False``，避免 ``/api/status`` 的 ``task_queue`` 字段让调用方误以为它是
    生产并发闸门（即「已修但不可达」的假象）。

    既有字段（running/concurrency/queued/max_queue/pending/current/current_elapsed_sec）
    原样保留，``wired`` / ``note`` 均为**新增**字段。
    """
    try:
        st = dict(task_queue.status())
    except Exception as e:  # noqa: BLE001  可观测性接口自身绝不能把 /api/status 打挂
        return {"wired": False, "error": f"{type(e).__name__}: {e}"}
    st["wired"] = False
    st["note"] = ("本进程 GPU 并发由 gpu_gate 承担；TaskQueue.submit 未接线"
                  "（D-07 加固属模块自身契约，非生产路径）")
    return st


@app.route('/api/status')
def api_status():
    comfyui_status = comfyui_client.get_status()
    return jsonify({
        "comfyui": comfyui_status,
        "assets": {
            "characters": sum(
                len(files) for _, _, files in os.walk(CHARACTERS_DIR)
            ) if os.path.exists(CHARACTERS_DIR) else 0,
            "items": sum(
                len(files) for _, _, files in os.walk(ITEMS_DIR)
            ) if os.path.exists(ITEMS_DIR) else 0,
            "scenes": sum(
                len(files) for _, _, files in os.walk(SCENES_DIR)
            ) if os.path.exists(SCENES_DIR) else 0,
        },
        "task_queue": _task_queue_status(),
        "gpu_gate": gpu_task_gate.status(),
        "interrupted_tasks": _interrupted,
    })


# ===== P0-4 持久化任务队列查询 =====

@app.route('/api/tasks', methods=['GET'])
def api_tasks_list():
    """查询任务列表（可按项目 / 状态 / 类型过滤），用于重启后查看进度与续跑提示"""
    project = (request.args.get('project') or '').strip()
    status = (request.args.get('status') or '').strip()
    kind = (request.args.get('kind') or '').strip()
    try:
        limit = max(1, min(500, int(request.args.get('limit') or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        items = task_db.list(project=project or None, status=status or None,
                             kind=kind or None, limit=limit)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"任务查询失败：{e}"}), 500
    return jsonify({"success": True, "count": len(items), "items": items,
                    "queue": _task_queue_status()})


@app.route('/api/tasks/<task_id>', methods=['GET'])
def api_task_detail(task_id):
    """查询单个任务详情（含单元级进度，用于展示断点续跑可跳过的部分）"""
    try:
        t = task_db.get(task_id)
        if not t:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        units = task_db.list_units(task_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"任务查询失败：{e}"}), 500
    done = [u for u in units if u.get("status") == task_store.ST_DONE]
    t["units"] = units
    t["unit_summary"] = {"total": len(units), "done": len(done),
                         "pending": len(units) - len(done)}
    return jsonify({"success": True, "task": t})


@app.route('/api/tasks/<task_id>/resume-preview', methods=['GET'])
def api_task_resume_preview(task_id):
    """断点续跑预检：给出该任务「已完成 / 待重跑」的单元清单

    判据以磁盘产物为准（产物存在且非空即视为已完成），
    因此即使任务状态表丢失，也能正确识别可跳过的部分。
    """
    try:
        t = task_db.get(task_id)
        if not t:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        units = task_db.list_units(task_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"查询失败：{e}"}), 500

    done_units, pending_units = [], []
    for u in units:
        path = u.get("result_path") or ""
        if path and task_store.is_unit_done(path, min_bytes=TASK_UNIT_MIN_BYTES):
            done_units.append(u.get("unit_key"))
        else:
            pending_units.append(u.get("unit_key"))
    return jsonify({"success": True, "task_id": task_id,
                    "status": t.get("status"),
                    "done_units": done_units, "pending_units": pending_units,
                    "resumable": t.get("status") in (task_store.ST_INTERRUPTED,
                                                     task_store.ST_FAILED)})


# ===== P1-1 一致性校验（跨镜头角色一致性） =====

# 注：`_shot_num_key` 已收敛为 app/shot_key.norm_shot_key 的一行代理（见文件顶部），
# 定义不再落在此处 —— 全项目唯一的镜号归一化实现见 app/shot_key.py。


def _consistency_collect(project_name: str, episode_no: int = None) -> dict:
    """从磁盘采集一致性校验所需素材

    - character_refs：output/assets/characters/<项目>/<角色>/front.png（缺则 base.png）
    - shot_images：优先读 storyboards manifest（含每镜产出图与 QC 记录），
      其次按 shot_NN.png 命名约定扫描，再从剧本补齐 characters_in_shot
    """
    chars_dir = os.path.join(CHARACTERS_DIR, project_name)
    character_refs = {}
    asset_dirs = {}
    if os.path.isdir(chars_dir):
        for name in sorted(os.listdir(chars_dir)):
            d = os.path.join(chars_dir, name)
            if not os.path.isdir(d):
                continue
            ref = _first_existing(os.path.join(d, "front.png"), os.path.join(d, "base.png"))
            if ref:
                character_refs[name] = ref
            asset_dirs[name] = d

    # 分镜图 + 剧本角色归属（统一用数字键，保证文件名/剧本/manifest 三方对齐）
    shot_images = {}
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project_name, episode_no)

    # 剧本 → 每镜角色
    shot_chars = {}
    try:
        key = project_store.safe_key(project_name)
        script = None
        if episode_no:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        if not script:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
            if eps:
                first = eps[0]
                no = first if isinstance(first, int) else (
                    first.get("episode_no") if isinstance(first, dict) else 1)
                script = novel_to_script.load_episode_script(SCRIPT_DIR, key, no)
        for s in ((script or {}).get("shots") or []):
            if isinstance(s, dict):
                shot_chars[_shot_num_key(s.get("shot_id"))] = s.get("characters_in_shot") or []
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"剧本读取失败（一致性校验将缺少角色归属）：{e}")

    # 先按目录命名约定扫描
    if os.path.isdir(sb_dir):
        for fn in sorted(os.listdir(sb_dir)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            k = _shot_num_key(os.path.splitext(fn)[0])
            shot_images[k] = {"image": os.path.join(sb_dir, fn),
                              "characters": shot_chars.get(k, [])}

    # manifest 覆盖（可能指向非默认目录，并附带 QC 分数）
    manifest_path = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            for s in (mf.get("shots") or []):
                if not isinstance(s, dict):
                    continue
                fp = comfyui_client.resolve_local_path(s.get("file") or "")
                if not (fp and os.path.isfile(fp)):
                    continue
                k = _shot_num_key(s.get("shot_id"))
                shot_images.setdefault(k, {})
                shot_images[k]["image"] = fp
                shot_images[k]["characters"] = shot_chars.get(k, [])
                shot_images[k]["qc"] = (s.get("qc") or {})
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")

    return {"character_refs": character_refs, "shot_images": shot_images,
            "asset_dirs": asset_dirs}


@app.route('/api/consistency/run', methods=['POST'])
def api_consistency_run():
    """执行一致性校验（可指定 project_name / episode_no / 是否含资产多视图）"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or '')
    if not project_name:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    episode_no = data.get('episode_no')
    try:
        collect = _consistency_collect(project_name, episode_no)
        cfg = _qc_load_cfg()
        report = consistency.run(
            project_name,
            character_refs=collect["character_refs"],
            shot_images=collect["shot_images"],
            asset_dirs=collect["asset_dirs"],
            cfg=cfg,
            include_assets=bool(data.get('include_assets', True)),
            include_shots=bool(data.get('include_shots', True)),
        )
    except Exception as e:  # noqa: BLE001
        app.logger.exception("一致性校验失败")
        return jsonify({"success": False, "error": f"一致性校验失败：{e}"}), 500
    return jsonify({"success": True, "project": project_name,
                    "summary": report.get("summary"),
                    "report": report, "report_path": report.get("report_path")})


@app.route('/api/consistency/report/<path:project_name>', methods=['GET'])
def api_consistency_report(project_name):
    """读取已有的一致性报告（不重新校验）"""
    project_name = _safe_project(project_name)
    rep = consistency.load_report(project_name)
    if not rep:
        return jsonify({"success": False, "error": "暂无一致性报告，请先执行校验",
                        "project": project_name}), 404
    return jsonify({"success": True, "project": project_name, "report": rep,
                    "summary": rep.get("summary")})


# ===== P2-3 成本与耗时看板 =====

@app.route('/api/analytics/summary', methods=['GET'])
def api_analytics_summary():
    """全局或按项目的成本/耗时汇总"""
    project = (request.args.get('project') or '').strip()
    try:
        limit = max(1, min(500, int(request.args.get('recent') or 100)))
    except (TypeError, ValueError):
        limit = 100
    try:
        data = analytics.summarize(project=project or None, recent_limit=limit)
        data["projects"] = analytics.list_projects()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"统计读取失败：{e}"}), 500
    return jsonify({"success": True, **data})


@app.route('/api/analytics/project/<path:project_name>', methods=['GET'])
def api_analytics_project(project_name):
    """单项目成本/耗时"""
    project_name = _safe_project(project_name)
    try:
        data = analytics.summarize(project=project_name)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"统计读取失败：{e}"}), 500
    return jsonify({"success": True, **data})


@app.route('/api/analytics/event', methods=['POST'])
def api_analytics_record():
    """手工登记一条耗时事件（供前端/外部脚本补充统计）"""
    data = request.json or {}
    ok = analytics.record_event(
        kind=str(data.get('kind') or 'other'),
        project=_safe_project(data.get('project') or ''),
        label=str(data.get('label') or ''),
        duration_sec=float(data.get('duration_sec') or 0),
        units=int(data.get('units') or 0),
        success=bool(data.get('success', True)),
        meta=data.get('meta') if isinstance(data.get('meta'), dict) else None,
    )
    return jsonify({"success": bool(ok)})


@app.route('/api/analytics/reset', methods=['POST'])
def api_analytics_reset():
    """清空统计（project 为空则整体清空）"""
    data = request.json or {}
    project = _safe_project(data.get('project') or '') if data.get('project') else None
    return jsonify({"success": True, **analytics.reset(project=project)})


# ==========================================================================
# 通用辅助：剧本读取 / 章节原文 / 关键帧目录
# ==========================================================================

def _load_script_for(project_name: str, episode_no=None) -> dict:
    """按项目名（+可选集号）读取剧本；缺集号时取该项目第一集

    兼容两种历史布局（否则「迁移项目」会永远读不到剧本）：
      A. 现行：SCRIPT_DIR/<project_key>/第N集.json
      B. 迁移遗留：SCRIPT_DIR/<name>_<时间戳>.json（扁平，无子目录）
    遗留项目在项目索引里登记着 episode_count（例如 10），但按 A 找不到任何一集，
    于是分镜画布 / 导出 / 质检等全部读到空数据，界面显示「10 集 · 0 分镜」。
    这里在 A 落空时回退到 B，并优先取时间戳最新的一份。
    """
    key = project_store.safe_key(project_name)
    script = None
    if episode_no:
        try:
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, int(episode_no))
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"剧本读取失败（第{episode_no}集）：{e}")
    if not script:
        try:
            eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
        except Exception:  # noqa: BLE001
            eps = []
        if eps:
            first = eps[0]
            epno = first if isinstance(first, int) else (first.get("episode_no") or 1)
            script = novel_to_script.load_episode_script(SCRIPT_DIR, key, epno)
    if not script:
        script = _load_legacy_flat_script(project_name)
    return script or {}


def _load_legacy_flat_script(project_name: str) -> dict:
    """回退：读取旧版扁平命名的剧本（SCRIPT_DIR/<name>_<时间戳>.json）。

    仅在现行目录布局读不到剧本时调用，因此不会遮蔽正常的第N集.json。
    按修改时间倒序取第一份「含 shots」的文件，避免命中空壳/中间态产物。
    """
    try:
        names = os.listdir(SCRIPT_DIR)
    except OSError:
        return {}
    cands = []
    for fn in names:
        if not fn.lower().endswith(".json"):
            continue
        stem = fn[:-5]
        # 允许 <name>_<时间戳> 与 <name> 本身（例如「剑心初醒_兼容版」）
        if stem != project_name and not stem.startswith(project_name + "_"):
            continue
        path = os.path.join(SCRIPT_DIR, fn)
        try:
            cands.append((os.path.getmtime(path), path))
        except OSError:
            continue
    for _, path in sorted(cands, reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"遗留剧本读取失败 {path}：{e}")
            continue
        if isinstance(data, dict) and (data.get("shots") or data.get("episode_no")):
            app.logger.info("剧本回退：%s 使用遗留扁平剧本 %s", project_name, os.path.basename(path))
            return data
    return {}


def _chapter_text_for_script(script: dict) -> str:
    """由剧本 metadata（novel_id + chapter_index）反查该集对应的原文章节文本"""
    meta = (script or {}).get("metadata") or {}
    novel_id = meta.get("novel_id")
    if not novel_id:
        return ""
    ch_index = meta.get("chapter_index") or (script or {}).get("episode_no") or 1
    try:
        text = read_novel_text(NOVELS_DIR, str(novel_id))
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"小说正文不可读（覆盖率归属将缺失）：{e}")
        return ""
    for c in split_chapters(text):
        if int(c.get("index") or 0) == int(ch_index or 0):
            return text[c.get("start") or 0:c.get("end") or 0]
    return ""


def _shot_coverage_map(script: dict) -> dict:
    """把原文章节正文单元归属到镜头（用于分镜画布展示「该镜承载了原文哪几句」）

    规则：逐单元与各镜「描述+台词+prompt_h3」做 4-gram 字面比对，
    取命中率最高的镜头归属；命中率低于 0.3 视为未承载。
    这是**离线规则判定**，与 coverage.py 的 LLM 判定同源（同一 gram 口径），
    仅供画布展示定位用，不替代覆盖率报告结论。
    """
    text = _chapter_text_for_script(script)
    if not text:
        return {}
    try:
        units, _total = coverage.split_source_units(text)
    except Exception:  # noqa: BLE001
        return {}
    if not units:
        return {}
    shots = [s for s in ((script or {}).get("shots") or []) if isinstance(s, dict)]
    if not shots:
        return {}
    shot_grams = []
    for s in shots:
        corpus = " ".join(str(x) for x in (
            s.get("description"), s.get("dialogue_text"), s.get("prompt_h3"),
            s.get("location"), s.get("camera")) if x)
        try:
            shot_grams.append(coverage._grams(coverage._norm(corpus)))
        except Exception:  # noqa: BLE001
            shot_grams.append(set())

    out: dict = {}
    for uid, unit in enumerate(units, start=1):
        if coverage.is_title_unit(unit):
            continue
        best_i, best_r = -1, 0.0
        for i, grams in enumerate(shot_grams):
            if not grams:
                continue
            try:
                r = coverage.literal_ratio(unit, grams)
            except Exception:  # noqa: BLE001
                continue
            if r > best_r:
                best_i, best_r = i, r
        if best_i >= 0 and best_r >= 0.3:
            sid = shots[best_i].get("shot_id", best_i + 1)
            out.setdefault(str(sid), []).append(
                {"unit_id": uid, "text": unit[:200], "ratio": best_r})
    return out


def _keyframes_dir(project_name: str, episode_no=None) -> str:
    d = _ep_dir(os.path.join(KEYFRAMES_DIR, _safe_project(project_name)), episode_no)
    os.makedirs(d, exist_ok=True)
    return d


def _collect_asset_refs(project: str) -> tuple:
    """从磁盘自动收集项目的角色 / 场景参考图（无需前端传入）

    返回 (character_refs, scene_refs)，元素形如 {"name":..., "front": 本地路径}，
    可直接喂给 _collect_reference_images。

    为什么需要它：单镜重跑等「带内调用的接口」如果只依赖前端传参，
    前端一旦传了结构不完整的对象（例如直接传剧本里的 characters，只有
    reference_prompt_zh 而没有 front/base 键），参考图会静默丢失、
    视频退化成无角色锚点——这类静默降级比报错更难发现。

    S6 修复：判据与 pipeline.probe_assets 对齐 ——
      1) 扩展名白名单 (".png", ".jpg", ".jpeg", ".webp")，不再只认 4 个固定文件名；
      2) 同一目录下取第一张非空图片（兼容 ComfyUI 直接输出 base_123.png 等非标名）；
      3) 找不到任何图片 → 返回空 dict，**绝不静默 take-first**（由调用方决策报错/跳过）。
    B-14 P2-4：判据统一抽到模块级 _first_existing_asset_image，取图判据
    _build_asset_index 复用同一函数，消除两处「判有图」口径漂移。
    """
    def _first_nonempty_image(d: str) -> str:
        return _first_existing_asset_image(d)

    def _scan(root: str) -> list:
        out = []
        base = os.path.join(root, _safe_project(project))
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            # S6 候选顺序：front/base 固定名优先，否则取目录内第一张非空图片
            ref = ""
            for cand in ("front.png", "base.png", "front.jpg", "base.jpg"):
                p = os.path.join(d, cand)
                if os.path.isfile(p):
                    ref = p
                    break
            if not ref:
                ref = _first_nonempty_image(d)
            if ref:
                out.append({"name": name, "front": ref, "base": ref})
        return out

    return _scan(CHARACTERS_DIR), _scan(SCENES_DIR)


# ==========================================================================
# P1-2 关键帧驱动视频模式
# ==========================================================================

def _keyframe_sb_map(project_name: str, script: dict = None, storyboards=None,
                     episode_no=None) -> dict:
    """取该项目的分镜图映射 {shot键: 本地路径}

    键同时注册「数字键」与「shot_NN 键」（如 "1" 与 "shot_01"），
    避免调用方因键风格不同而漏配。

    注意（真实缺陷修复）：**必须合并** manifest 与目录扫描，不能命中 manifest 就提前返回。
    manifest 可能只记录了部分镜头（例如某镜曾在画布上单独重跑，manifest 被写成了单条），
    此时提前返回会让其余镜头在后续 index 兜底里**错配到别的镜头的分镜图**，
    进而用错误的画面当关键帧首帧。
    """
    project = _safe_project(project_name)
    sb_map: dict = {}

    def _put(key, path):
        if not path or not os.path.isfile(path):
            return
        k = _shot_num_key(key)
        sb_map[k] = path
        # 同时注册 shot_NN 风格键（若 k 为纯数字）
        if k.isdigit():
            sb_map[f"shot_{int(k):02d}"] = path

    # ① 前端显式传入优先
    if isinstance(storyboards, dict):
        for k, v in storyboards.items():
            local = comfyui_client.resolve_local_path(v) if isinstance(v, str) else None
            if local:
                _put(k, local)
    # ② 目录扫描作为基底（覆盖所有实际存在的分镜图）
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project,
                          episode_no if episode_no is not None else _ep_of_script(script))
    if not sb_map and os.path.isdir(sb_dir):
        for fn in sorted(os.listdir(sb_dir)):
            if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                name = os.path.splitext(fn)[0]
                seq = "".join(ch for ch in name if ch.isdigit())
                if seq:
                    _put(seq, os.path.join(sb_dir, fn))
    # ③ manifest 覆盖（含质检状态与可能位于非默认目录的产物路径）
    manifest_path = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                mf = json.load(f) or {}
            for s in (mf.get("shots") or []):
                if not isinstance(s, dict) or not s.get("success"):
                    continue
                fp = comfyui_client.resolve_local_path(s.get("file") or "")
                _put(s.get("shot_id"), fp)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")
    return sb_map


@app.route('/api/keyframes/plan', methods=['GET'])
def api_keyframes_plan():
    """关键帧尾帧生成预检（不调用模型）"""
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(request.args.get('project_name') or '')
    if err is not None:
        return err
    episode_no = request.args.get('episode_no')
    script = _load_script_for(project, episode_no)
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    kf_dir = _keyframes_dir(project, episode_no)
    sb_map = _keyframe_sb_map(project, script, episode_no=episode_no)
    chain_mode = keyframe.norm_chain_mode(
        request.args.get('chain_mode') or KEYFRAME_CHAIN_MODE)
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir,
                                   only_missing=(request.args.get('only_missing', '1') != '0'),
                                   chain_mode=chain_mode)
    return jsonify({"success": True, "project": project,
                    "shot_count": len(shots),
                    "keyframes_dir": kf_dir,
                    "chain_mode": chain_mode,
                    "start_frames_ready": sum(1 for p in plan if p["has_start"]),
                    "end_frames_ready": sum(1 for p in plan if p["has_end"]),
                    "chained_count": sum(1 for p in plan if p.get("chained")),
                    "to_generate": sum(1 for p in plan if p["need_gen"]),
                    "plan": plan})


@app.route('/api/keyframes/generate', methods=['POST'])
def api_keyframes_generate():
    """批量生成尾帧（Qwen Edit，以分镜图为首帧参考）——后台任务 + 断点续跑"""
    # ⚠️ 故意不设 AI 门禁：尾帧提示词在 shot/剧本数据里（上游产出），本步只做 Qwen Edit
    # 图生图 + 质检，不读 AI 凭证。门禁挂这里会误伤「有存量分镜、但 AI key 未配」的续跑。
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no')) if not data.get('shots') \
        else {"shots": data.get('shots') or []}
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "没有镜头数据"}), 400
    _g = _style_aspect_guard(project)
    if _g is not None:
        return _g
    _kf_ep = _ep_of_script(script, data.get('episode_no'))
    kf_dir = _keyframes_dir(project, _kf_ep)
    sb_map = _keyframe_sb_map(project, script, data.get('storyboards'), episode_no=_kf_ep)
    only_missing = bool(data.get('only_missing', True))
    seed = data.get('seed')
    timeout = int(data.get('timeout') or 900)
    chain_mode = keyframe.norm_chain_mode(
        data.get('chain_mode') or KEYFRAME_CHAIN_MODE)

    task_id = f"keyframe_{project}_{int(time.time())}"
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir, only_missing=only_missing,
                                   chain_mode=chain_mode)
    # 尾帧质检（可选）：默认跟随图片质检开关，不达标换 seed 重画，仍不通过则本镜判失败
    _kf_verify, _kf_vretries = _keyframe_qc_verifier(project, script=script)
    # 尾帧提示词预检（生成前质检）：能自愈的先自愈再出图；成批生成不阻断
    # （与资产 / 整集视频同一取舍 —— 为一条提示词打断整批代价过大）
    _kf_pre, _kf_pre_on = _keyframe_prompt_preflight(project)
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "关键帧尾帧生成",
            "total": len([p for p in plan if p["need_gen"]]), "current": 0,
            "results": [], "keyframes_dir": kf_dir,
        }
    try:
        task_store.get_store(TASKS_DB_PATH).create(
            kind="keyframe", project=project, label=f"{project} 尾帧生成",
            total=len([p for p in plan if p["need_gen"]]), task_id=task_id)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"任务库登记失败（不影响生成）：{e}")

    def _kf_worker():
        store = None
        try:
            store = task_store.get_store(TASKS_DB_PATH)
        except Exception:  # noqa: BLE001
            store = None
        if store:
            try:
                store.start(task_id)     # 登记开始时间（否则任务列表「开始」为空）
            except Exception as e:  # noqa: BLE001
                app.logger.debug("任务库 start 登记失败（不影响执行）：%s", e)

        def _progress(done, total, item):
            with lock:
                st = generation_state.get(task_id) or {}
                st.update({"current": done, "total": total,
                           "progress": int(done / max(total, 1) * 100)})
                st.setdefault("results", []).append(item)
            if store:
                # 单元级进度：尾帧产物落盘即视为该镜完成（断点续跑判据同源）
                try:
                    store.set_progress(task_id, progress=int(done / max(total, 1) * 100))
                    store.mark_unit(task_id, f"shot_{item.get('seq') or item.get('shot_id')}",
                                    task_store.ST_DONE if item.get("ok") else task_store.ST_FAILED,
                                    result_path=item.get("path") or "",
                                    error=item.get("error") or "")
                except Exception as e:  # noqa: BLE001
                    app.logger.debug("任务库单元进度写入失败（忽略）：%s", e)

        try:
            report = keyframe.generate_keyframes(
                shots, sb_map, kf_dir, seed=seed, timeout=timeout,
                only_missing=only_missing, progress_cb=_progress,
                chain_mode=chain_mode, verify_cb=_kf_verify,
                max_verify_retries=_kf_vretries, preflight_cb=_kf_pre,
                client=comfyui_client,  # S-04：注入全局 ComfyUIClient 实例（复用连接/共享状态）
                qc_stop_cb=_qc_retry_hopeless,  # G1：尾帧连续两次缺陷相同 → 止损
                recall_cb=_keyframe_recall_cb(project),  # T03a：尾帧质检重试召回历史教训
                project_name=project,  # A-16：尾帧达标落盘时写旁路 .meta.json 用
            )
            with lock:
                generation_state[task_id].update({
                    "status": "completed" if report.get("ok") else "failed",
                    "progress": 100, "report": report,
                    "error": "" if report.get("ok") else "全部尾帧生成失败",
                })
            if store:
                if report.get("ok"):
                    store.finish(task_id, result_path=os.path.join(kf_dir, "keyframes_manifest.json"))
                else:
                    store.fail(task_id, "全部尾帧生成失败")
        except Exception as e:  # noqa: BLE001
            app.logger.exception("关键帧生成任务失败")
            with lock:
                generation_state[task_id].update({"status": "failed", "error": str(e)})
            if store:
                store.fail(task_id, str(e))

    # B-01 P1-12：GPU 并发闸门（不接管 task_db 生命周期，worker 内部已写好）
    def _kf_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, "关键帧生成"):
            _kf_worker()
    th = threading.Thread(target=_kf_worker_gated, daemon=True)
    th.start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len([p for p in plan if p["need_gen"]]),
                    "keyframes_dir": kf_dir, "chain_mode": chain_mode,
                    "qc_enabled": bool(_kf_verify),
                    "prompt_qc_enabled": bool(_kf_pre_on)})


@app.route('/api/keyframes/file/<path:filename>')
def api_keyframes_file(filename):
    """关键帧图片访问：/api/keyframes/file/<项目>/shot_01_end.png"""
    return _serve_safe(KEYFRAMES_DIR, filename)


@app.route('/api/keyframes/list/<path:project_name>')
def api_keyframes_list(project_name):
    """列出项目已有首尾帧"""
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    _kl_ep = request.args.get('episode_no')
    _kl_sub = f"ep{int(_kl_ep):02d}/" if str(_kl_ep or '').strip() and int(_kl_ep) > 1 else ""
    kf_dir = _ep_read_dir(KEYFRAMES_DIR, project, _kl_ep)
    items = []
    if os.path.isdir(kf_dir):
        for fn in sorted(os.listdir(kf_dir)):
            if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                continue
            seq = "".join(ch for ch in fn.split("_")[1] if ch.isdigit()) if "_" in fn else ""
            kind = "end" if "_end." in fn else ("start" if "_start." in fn else "other")
            items.append({"file": fn, "shot": int(seq) if seq else None, "kind": kind,
                          "url": f"/api/keyframes/file/{project}/{_kl_sub}{fn}",
                          "size": os.path.getsize(os.path.join(kf_dir, fn))})
    return jsonify({"success": True, "project": project, "dir": kf_dir,
                    "count": len(items), "items": items})


# ==========================================================================
# P1-3 可视化分镜画布 + 单镜重跑
# ==========================================================================

@app.route('/api/storyboard/canvas/<path:project_name>', methods=['GET'])
def api_storyboard_canvas(project_name):
    """分镜画布数据：每镜一张卡片（分镜图/视频 + 质检分 + 一致性分 + 承载原文 + 台词）

    卡片按剧本 shots 顺序排列；手动排序（order）保存在剧本 metadata.shot_order，
    因此画布顺序与后续视频生成顺序始终一致。
    """
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    script = _load_script_for(project, request.args.get('episode_no'))
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    meta = script.get("metadata") or {}

    # 分镜图 + 质检（集级目录：第 1 集平铺，第 2 集起含 epNN）
    _cv_ep = _ep_of_script(script, request.args.get('episode_no'))
    _cv_sub = f"ep{int(_cv_ep):02d}/" if _cv_ep and int(_cv_ep) > 1 else ""
    sb_map = _keyframe_sb_map(project, script, episode_no=_cv_ep)
    sb_dir = _ep_read_dir(STORYBOARDS_DIR, project, _cv_ep)
    sb_manifest = {}
    mpath = os.path.join(sb_dir, "storyboard_manifest.json")
    if os.path.isfile(mpath):
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                for s in ((json.load(f) or {}).get("shots") or []):
                    if isinstance(s, dict):
                        sb_manifest[_shot_num_key(s.get("shot_id"))] = s
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"分镜 manifest 读取失败：{e}")

    # 视频
    vid_dir = _ep_dir(os.path.join(VIDEOS_DIR, project), _cv_ep)
    vid_map = {}
    if os.path.isdir(vid_dir):
        for fn in sorted(os.listdir(vid_dir)):
            if fn.lower().endswith((".mp4", ".mov", ".webm")):
                vid_map[_shot_num_key(os.path.splitext(fn)[0])] = os.path.join(vid_dir, fn)

    # 一致性报告（按镜头取最低分）
    consistency_by_shot = {}
    try:
        rep = consistency.load_report(project) or {}
        for r in ((rep.get("shot_check") or {}).get("results") or []):
            k = _shot_num_key(r.get("shot"))
            cur = consistency_by_shot.get(k)
            if cur is None or (r.get("score") or 0) < (cur.get("score") or 0):
                consistency_by_shot[k] = {"score": r.get("score"),
                                          "verdict": r.get("verdict"),
                                          "character": r.get("character"),
                                          "mode": r.get("mode")}
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"一致性报告读取失败（画布将不含一致性分）：{e}")

    # 原文承载归属
    try:
        cover_map = _shot_coverage_map(script)
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"覆盖率归属计算失败：{e}")
        cover_map = {}
    cov_report = {}
    try:
        cov_report = coverage.load_coverage_report(CONTINUITY_DIR, project,
                                                   meta.get("episode_no") or script.get("episode_no") or 1)
    except Exception:  # noqa: BLE001
        cov_report = {}

    order = meta.get("shot_order") or []
    ordered = list(shots)
    if isinstance(order, list) and order:
        ordered = sorted(shots, key=lambda s: (order.index(str(s.get("shot_id")))
                                                if str(s.get("shot_id")) in order else 10 ** 6))
    kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project), _cv_ep)
    cards = []
    for i, s in enumerate(ordered):
        sid = s.get("shot_id", i + 1)
        k = _shot_num_key(sid)
        seq = _shot_seq(sid, i + 1)
        sb_file = sb_map.get(k) or sb_map.get(f"shot_{seq:02d}")
        sb_item = sb_manifest.get(k) or {}
        vid = vid_map.get(k) or vid_map.get(f"shot_{seq:02d}")
        kf_end = os.path.join(kf_dir, f"shot_{seq:02d}_end.png")
        cards.append({
            "order": i,
            "shot_id": sid,
            "seq": seq,
            "camera": s.get("camera"),
            "duration": s.get("duration"),
            "location": s.get("location"),
            "emotion": s.get("emotion"),
            "description": s.get("description"),
            "dialogue": s.get("dialogue") or [],
            "dialogue_text": s.get("dialogue_text"),
            "characters_in_shot": s.get("characters_in_shot") or [],
            "items_in_shot": s.get("items_in_shot") or [],
            "storyboard": {
                "exists": bool(sb_file),
                "url": (f"/api/storyboards/file/{project}/{_cv_sub}shot_{seq:02d}.png"
                        if sb_file else ""),
                "path": sb_file or "",
                "qc": sb_item.get("qc") or {},
                "success": bool(sb_item.get("success")),
                "blocked": bool(sb_item.get("qc_blocked")),
                "error": sb_item.get("error") or "",
            },
            "video": {
                "exists": bool(vid),
                "url": (f"/api/videos/{project}/{_cv_sub}{os.path.basename(vid)}"
                        if vid else ""),
                "path": vid or "",
            },
            "keyframe": {
                "start": bool(sb_file),
                "end_exists": os.path.isfile(kf_end),
                "end_url": (f"/api/keyframes/file/{project}/{_cv_sub}shot_{seq:02d}_end.png"
                            if os.path.isfile(kf_end) else ""),
            },
            "consistency": consistency_by_shot.get(k) or {},
            "coverage": {"units": cover_map.get(str(sid)) or [],
                         "unit_count": len(cover_map.get(str(sid)) or [])},
        })

    summary = {
        "shot_count": len(cards),
        "storyboard_ready": sum(1 for c in cards if c["storyboard"]["exists"]),
        "video_ready": sum(1 for c in cards if c["video"]["exists"]),
        "keyframe_end_ready": sum(1 for c in cards if c["keyframe"]["end_exists"]),
        "qc_blocked": sum(1 for c in cards if c["storyboard"]["blocked"]),
        "coverage": {
            "plot_coverage_percent": cov_report.get("plot_coverage_percent"),
            "detail_coverage_percent": cov_report.get("detail_coverage_percent"),
            "missing_count": cov_report.get("missing_count"),
            "passed": cov_report.get("passed"),
            "checked_at": cov_report.get("checked_at"),
        } if cov_report else {},
    }
    return jsonify({"success": True, "project": project,
                    "episode_no": meta.get("episode_no") or script.get("episode_no"),
                    "episode_title": meta.get("episode_title") or script.get("episode_title"),
                    "title": script.get("title"),
                    "summary": summary, "cards": cards,
                    "shot_order": order or [str(s.get("shot_id")) for s in shots]})


@app.route('/api/storyboard/shot/reorder', methods=['POST'])
def api_storyboard_shot_reorder():
    """分镜拖拽排序：写回剧本 shots 顺序 + metadata.shot_order

    body: {project_name, episode_no, order: [shot_id, ...]}
    副作用：shot_id 保持原值不变（避免打断既有产物文件名映射），
    仅调整 shots 数组顺序与 shot_order 记录。
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    order = data.get('order') or []
    if err is not None:
        return err
    if not order:
        return jsonify({"success": False, "error": "缺少 project_name / order"}), 400
    key = project_store.safe_key(project)
    episode_no = data.get('episode_no')
    script = _load_script_for(project, episode_no)
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    shots = script.get("shots") or []
    idx = {str(s.get("shot_id")): s for s in shots}
    new_shots = [idx[str(sid)] for sid in order if str(sid) in idx]
    if len(new_shots) != len(shots):
        missing = [str(s.get("shot_id")) for s in shots if str(s.get("shot_id")) not in
                   {str(x) for x in order}]
        return jsonify({"success": False,
                        "error": f"排序清单与镜头不匹配（缺少：{missing[:5]}）"}), 400
    script["shots"] = new_shots
    ep_no = script.get("episode_no") or episode_no or 1
    script.setdefault("metadata", {})["shot_order"] = [str(x) for x in order]
    script["metadata"]["shot_order_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep_no)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本落盘失败：{e}"}), 500
    return jsonify({"success": True, "project": project, "episode_no": ep_no,
                    "shot_order": [str(x) for x in order]})


# ==========================================================================
# 托管接口统一异常兜底
# --------------------------------------------------------------------------
# D1（2026-09-23）：本函数原先定义在文件后段，只能装饰**其后**注册的路由，
# 导致更早注册的 /api/storyboard/retry-shot、/api/video/retry-shot 拿不到兜底
# —— 它们抛错时前端收到的是 werkzeug 的 HTML 500，`readError()` 解析不出 error
# 字段，用户只看到「服务内部错误」而无从判断。故前移到首个使用点之前。
# 实现未改（仍复用下方的 _friendly_error，运行期解析）。
# ==========================================================================
def _autopilot_guard(fn):
    """统一异常兜底：托管接口不应把 500 抛给前端，而是返回可读错误

    注意不要把客户端错误（HTTPException，例如请求体不是合法 JSON 时
    werkzeug 抛出的 400 BadRequest）误判成服务端 500——否则前端会看到
    「500 服务内部错误」，而真实原因是自己发了个畸形请求，排查方向会被带偏。
    """
    def _wrap(*a, **k):
        try:
            return fn(*a, **k)
        except KeyError as e:
            return jsonify({"success": False, "error": f"对象不存在：{e}"}), 404
        except HTTPException as e:
            # 保留 werkzeug 原本的语义状态码（400/404/405…），不要降级成 500
            return jsonify({
                "success": False,
                "error": e.description or e.name,
            }), (e.code or 400)
        except Exception as e:  # noqa: BLE001
            app.logger.exception("托管接口异常")
            return jsonify({"success": False, "error": _friendly_error(e)}), 500
    _wrap.__name__ = fn.__name__
    return _wrap


@app.route('/api/storyboard/retry-shot', methods=['POST'])
@_autopilot_guard
def api_storyboard_retry_shot():
    """单镜分镜图重跑（同步返回；只影响该镜，不触碰其它镜头产物）

    body: {project_name, shot: {...}, seed?, episode_no?}
    未传 shot 时按 shot_id 从剧本取。

    D1（2026-09-23）：见 `_storyboard_retry_shot_impl` 上方说明。
    """
    with gpu_task_gate.run_gpu_task(
            f"sb_retry_{uuid.uuid4().hex[:8]}", "分镜图单镜重跑"):
        return _storyboard_retry_shot_impl()


def _storyboard_retry_shot_impl():
    """单镜分镜图重跑的实际实现（整段在 GPU 闸门内执行）

    D1（2026-09-23）：
    - 加 @_autopilot_guard → 异常不再泄漏成裸 HTML 500（与其它托管接口一致）。
    - 整段关键区（出图 → 质检 → 入库 → manifest 回写）进入 gpu_task_gate：
        · 避免与批量分镜 worker 抢同一张 GPU（TASK_QUEUE_CONCURRENCY 默认 1）；
        · 消除两边并发 read-modify-write storyboard_manifest.json 的**丢更新**
          —— 批量 worker 的 manifest 写入在它的 gate 内（`_storyboard_worker`
          由 `run_gpu_task` 包裹），本函数的写入也在本 gate 内，两者互斥。
      代价：批量任务在跑时手动重跑会排队等待（与「单 GPU 并发度 1」的设计一致；
      排队超过 30s 由 gpu_task_gate 打 warning，不静默）。
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    shots = script.get("shots") or []
    shot = data.get('shot') or {}
    if not shot and shots:
        want = str(data.get('shot_id'))
        shot = next((s for s in shots if str(s.get("shot_id")) == want), {})
    if not shot:
        return jsonify({"success": False, "error": "未找到目标镜头"}), 400

    shot_id = shot.get("shot_id", 1)
    seq = _shot_seq(shot_id, 1)
    char_idx = _build_asset_index(script.get("characters") or [], project, "character")
    item_idx = _build_asset_index(script.get("items") or [], project, "item")
    scene_idx = _build_asset_index(script.get("scenes") or [], project, "scene")
    refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project)
    # S6 修复：参考图匹配失败时，不再静默取首角色（旧行为会把"不存在的角色"当主角色），
    # 而是明确 400 + 具体错误。
    if not refs and shot.get("_no_reference"):
        return jsonify({"success": False, "no_reference": True,
                        "error": shot.get("_ref_error") or "该镜头角色在资产索引中无匹配",
                        "hint": "请检查剧本 characters_in_shot 与资产目录名是否一致"}), 400
    if not refs:
        return jsonify({"success": False,
                       "error": "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"}), 400
    labels = [r[1] for r in refs]
    # 风格：剧本自带 style（用户与总控敲定）优先，缺失时退回项目 plan 的 style
    _rs_style = style_kit.normalize_style(script.get("style")) or style_kit.normalize_style(
        (autopilot.get_plan(project) or {}).get("style"))
    if _rs_style:
        shot = dict(shot, style=(shot.get("style") or _rs_style))
    # G19 同款兜底：风格串无画幅关键词时以默认 9:16 为底。本端点此前漏了这层兜底
    # → size=None → 完全不覆写，画幅完全沿用模板/参考图，与批量 worker 口径不一致。
    _rs_size = style_kit.aspect_size(style_kit.aspect_ratio(_rs_style) or style_kit.DEFAULT_RATIO)
    prompt = comfyui_client.build_storyboard_prompt(shot, labels)
    refs = _unify_ref_canvas(refs, _rs_size, project)
    # ---- 提示词预检（生成前质检）：先判 → 确定性自愈 → 再出图 ----
    # 目的：把 GPU 花在有问题的提示词上是纯浪费，且出图后质检才发现就已经晚了。
    qc_cfg = _qc_load_cfg()
    prompt, _pf, _pgate = _prompt_preflight(
        "storyboard", prompt, ctx=shot, style=(shot.get("style") or _rs_style),
        ref_count=len(refs), project_name=project)
    if not _pgate.get("accept"):
        # ★ 用户需求：质检不合格的提示词不留本地。用户决策 1：提示词预检是**生成前**的文本
        # 合规检查，不通过直接阻断不生成 → 没有图片/视频可删，只有这份提示词历史 json 落盘
        # （P12 `output/qc/<项目>/prompt_<shot>.json`），把它移回收站。
        try:
            _purge_prompt_records(project, shot_id,
                                  reason=f"提示词预检未通过（{_pgate.get('label')}）")
        except Exception as _pe:  # noqa: BLE001
            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
        return jsonify({"success": False, "prompt_qc_blocked": True,
                        "error": f"提示词预检未通过（{_pgate.get('label')}）：{_pgate.get('reason')}"
                                 + (f"；建议：{_pf.get('rebuild_hint')}" if _pf.get("rebuild_hint") else ""),
                        "prompt_qc": _pf.get("verdict")}), 200
    seed = data.get('seed')
    try:
        result = comfyui_client.generate_storyboard(
            prompt_zh=prompt, ref_images=[r[2] for r in refs],
            filename_prefix=f"comic_drama_sb/{project}_shot_{seq:02d}_retry",
            seed=seed, size=_rs_size)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"分镜图重跑失败：{e}"}), 500
    files = (result or {}).get("files") or []
    if not files:
        return jsonify({"success": False, "error": "ComfyUI 未返回分镜图"}), 500

    # 质检（若已开启）：不达标同样阻断入库（与批量链路一致）
    qc_on = qc_client.image_qc_ready(qc_cfg)
    _ep = _ep_of_script(script, data.get('episode_no'))
    dst_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project), _ep)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, f"shot_{seq:02d}.png")
    verdict = None
    gate = None
    scratch_dir = os.path.join(QC_DIR, project, "storyboard_scratch")
    os.makedirs(scratch_dir, exist_ok=True)
    scratch = os.path.join(scratch_dir, f"shot_{seq:02d}_retry.png")
    # G8②：消费 ComfyUI output 源（与主 worker 的 move 语义对齐），不留 output 残留
    shutil.move(files[0], scratch)
    if qc_on:
        verdict = qc_client.check_image(scratch, _qc_shot_desc(shot), qc_cfg,
                                        style=(shot.get("style") or _rs_style),
                                        ref_images=_qc_ref_images(
                                            shot, char_idx, item_idx, scene_idx, refs))
        gate = _qc_gate(verdict)
        _qc_record_verdict(project, "image", shot_id, "单镜重跑质检",
                           1, seed, scratch, verdict, style=(shot.get("style") or _rs_style))
    if qc_on and not (gate or {}).get("accept"):
        # ★ 用户需求：质检「判定不通过」的暂存图不留本地（含 ComfyUI 侧）。
        # ⚠️ 只删「质检成功返回（ok=True）且判定不合格」的产物；ok=False（接口故障/超时/
        # 鉴权失败）不是产物不合格，绝不能删（那会把好图删光）。
        if (verdict or {}).get("ok") is True:
            _purge_rejected_artifacts([scratch], project=project, kind="storyboard_image_retry",
                                      reason=f"单镜重跑质检不合格（{(gate or {}).get('label')}）",
                                      history_file=(_qc_history_file_for(project, "image", shot_id)))
        return jsonify({"success": False, "qc_blocked": True,
                        "error": f"分镜图质检阻断（{(gate or {}).get('label')}）："
                                 f"{(gate or {}).get('reason')}；未写入正式目录",
                        "verdict": verdict}), 200
    shutil.copy2(scratch, dst)
    # 同步更新 manifest 中该镜条目
    # B-2 收口（2026-09-22 复验）：manifest 损坏时 read_json_strict 会 fail-loud 抛错，
    # 但此刻图片**已经**重跑成功并写进正式目录（上一行的 copy2）。若让异常直接冒泡，
    # 会把「部分成功」整镜报成失败，前端还只能拿到裸 HTML 500（全库仅注册了
    # BadRequest 处理器，无 JSON 500 处理器）。
    # 这里用窄 try 做**响亮降级**（不是 fail-open）：
    #   · 记 error 级日志（数据层异常不静默）
    #   · 在响应里显式带 manifest_updated=False + 原因，调用方可感知
    #   · **绝不**把 manifest 重建为 {} —— 那才会清空其他镜头的记录
    _manifest_updated = True
    _manifest_err = ""
    try:
        _update_storyboard_manifest_shot(project, shot_id, seq, dst, prompt, refs, verdict, gate,
                                         episode_no=_ep)
    except Exception as _m_err:  # noqa: BLE001
        _manifest_updated = False
        _manifest_err = f"{type(_m_err).__name__}: {_m_err}"
        app.logger.error(
            "单镜重跑：图片已写入正式目录，但 manifest 同步失败（不影响本次出图；"
            "project=%s shot=%s dst=%s）：%s", project, shot_id, dst, _manifest_err)
    # 提示词预检结论也落质检历史（kind=prompt），便于回溯「这一镜出图前提示词是什么状态」
    if not _pf.get("skipped"):
        try:
            _qc_record_verdict(project, "prompt", shot_id, "分镜图提示词预检",
                               0, seed, None, _pf.get("verdict") or {}, extra={
                                   "prompt_kind": "storyboard",
                                   "repairs": _pf.get("repairs") or [],
                                   "mode": (_pf.get("verdict") or {}).get("mode"),
                                   "accept": bool(_pf.get("accept")),
                               }, style=(shot.get("style") or _rs_style))
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"提示词预检记录落盘失败：{e}")
    _sub = f"ep{int(_ep):02d}/" if _ep and int(_ep) > 1 else ""
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "path": dst,
                    "url": f"/api/storyboards/file/{project}/{_sub}shot_{seq:02d}.png",
                    "prompt": prompt, "ref_count": len(refs),
                    # B-2：本次出图是否已同步进 manifest。False 表示图已出好、但清单未更新
                    # （manifest 损坏等），调用方可据此提示用户「重跑成功、清单待修」。
                    "manifest_updated": _manifest_updated,
                    "manifest_error": _manifest_err,
                    "prompt_qc": _pf.get("verdict"), "prompt_qc_repairs": _pf.get("repairs") or [],
                    "qc": verdict})


def _update_storyboard_manifest_shot(project: str, shot_id, seq: int, dst: str,
                                     prompt: str, refs: list, verdict=None, gate=None,
                                     episode_no=None):
    """把单镜重跑结果写回分镜 manifest（保持既有 schema 不变）

    集级目录：第 1 集沿用平铺，第 2 集起写 epNN/ 下的 manifest 与 URL。
    """
    _flat = os.path.join(STORYBOARDS_DIR, project)
    mpath = os.path.join(_ep_dir(_flat, episode_no), "storyboard_manifest.json")
    _sub = os.path.basename(_ep_dir(_flat, episode_no)) if _ep_dir(_flat, episode_no) != _flat else ""
    _url_prefix = f"{project}/{_sub}/" if _sub else f"{project}/"
    manifest = {}
    if os.path.isfile(mpath):
        # B-2（2026-09-22 复核补漏）：本路径与 _storyboard_worker（app.py 的
        # atomic_write_json 落盘）写的是**同一个** storyboard_manifest.json。
        # 旧实现用 `except: manifest = {}` 的 fail-open 读 + 裸 open(w) 非原子写，
        # 与批量 worker 并发时会出现「读到半截 → 用残缺 manifest 覆盖回去 → 其他
        # 镜头记录整批丢失」。这里改为与 D-03/D-04 同口径：严格读（损坏→.bak 恢复或
        # fail-loud）+ 原子写。单镜重跑是用户显式操作，manifest 损坏时报错远好过静默清空。
        manifest = read_json_strict(mpath, {})
    items = [s for s in (manifest.get("shots") or []) if isinstance(s, dict)]
    target = next((s for s in items if _shot_num_key(s.get("shot_id")) == _shot_num_key(shot_id)), None)
    entry = {
        "shot_id": shot_id, "success": True,
        "file": dst, "url": f"/api/storyboards/file/{_url_prefix}shot_{seq:02d}.png",
        "prompt": prompt, "ref_count": len(refs),
        "refs": {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs},
        "regenerated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regenerated": "single_shot_retry",
    }
    if verdict:
        entry["qc"] = {"enabled": True, "status": "pass" if (gate or {}).get("accept") else "blocked",
                       "label": (gate or {}).get("label"), "attempts": 1,
                       "score": verdict.get("score"), "verdict": verdict.get("verdict"),
                       "reason": verdict.get("reason")}
    if target is not None:
        target.update(entry)
    else:
        items.append(entry)
    manifest.setdefault("project_name", project)
    manifest["shots"] = items
    manifest["total"] = len(items)
    manifest["success_count"] = sum(1 for s in items if s.get("success"))
    manifest["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        # B-2：与 worker 侧统一走 fs_atomic（唯一临时名 + fsync + .bak 快照 + replace 重试），
        # 避免单镜重跑与批量分镜 worker 并发写同一 manifest 互相截断。
        atomic_write_json(mpath, manifest)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"分镜 manifest 更新失败：{e}")


@app.route('/api/video/retry-shot', methods=['POST'])
@_autopilot_guard
def api_video_retry_shot():
    """单镜视频重跑（同步；只重生成该镜的 mp4）

    支持 mode：reference（默认，分镜图+主角锚点）/ keyframe（首尾帧插值）

    D1（2026-09-23）：与分镜重跑同口径——加 @_autopilot_guard（异常不再泄漏成
    裸 HTML 500）+ 整段关键区进入 gpu_task_gate（与批量视频 worker 互斥，
    避免两个 ComfyUI 任务抢同一张 GPU）。
    """
    with gpu_task_gate.run_gpu_task(
            f"video_retry_{uuid.uuid4().hex[:8]}", "单镜视频重跑"):
        return _video_retry_shot_impl()


def _video_retry_shot_impl():
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    shots = script.get("shots") or []
    shot = data.get('shot') or {}
    if not shot and shots:
        want = str(data.get('shot_id'))
        shot = next((s for s in shots if str(s.get("shot_id")) == want), {})
    if not shot:
        return jsonify({"success": False, "error": "未找到目标镜头"}), 400

    shot_id = shot.get("shot_id", 1)
    seq = _shot_seq(shot_id, 1)
    mode = str(data.get('mode') or 'reference').strip().lower()
    char_refs = data.get('character_refs') or []
    scene_refs = data.get('scene_refs') or []
    ref_imgs = _collect_reference_images(char_refs, scene_refs)
    main_char_img = _collect_reference_images(char_refs[:1], [])
    # 参考图兜底：前端未传、或传了结构不完整的对象（例如直接传剧本 characters，
    # 只有 reference_prompt_zh 而无 front/base 键）时，从磁盘资产目录自动收集，
    # 避免「无角色锚点」的静默降级。
    if not main_char_img or not ref_imgs:
        auto_chars, auto_scenes = _collect_asset_refs(project)
        if not main_char_img:
            char_refs = char_refs or auto_chars
            main_char_img = _collect_reference_images(char_refs[:1], [])
        if not ref_imgs:
            scene_refs = scene_refs or auto_scenes
            ref_imgs = _collect_reference_images(char_refs, scene_refs)
        if main_char_img or ref_imgs:
            app.logger.info(f"[retry-shot] 参考图已由磁盘资产补齐："
                            f"角色 {len(main_char_img)} / 合计 {len(ref_imgs)}")
    _rs_ep = _ep_of_script(script, data.get('episode_no'))
    _rs_sub = f"ep{int(_rs_ep):02d}/" if _rs_ep and int(_rs_ep) > 1 else ""
    sb_map = _keyframe_sb_map(project, script, episode_no=_rs_ep)
    sb_local = sb_map.get(_shot_num_key(shot_id))

    if mode == 'keyframe':
        kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project), _rs_ep)
        end_p = os.path.join(kf_dir, f"shot_{seq:02d}_end.png")
        if not (sb_local and os.path.isfile(sb_local)):
            return jsonify({"success": False, "error": "缺少分镜图，无法关键帧驱动"}), 400
        if not os.path.isfile(end_p):
            return jsonify({"success": False,
                            "error": "缺少尾帧，请先执行关键帧生成（/api/keyframes/generate）"}), 400
        prompt = comfyui_client._build_h3_prompt(
            shot, char_refs, scene_refs, storyboard_ref={"name": f"shot_{seq}"})
        try:
            dur = float(shot.get('duration') or 5)
        except (TypeError, ValueError):
            dur = 5.0
        seg = {"prompt": prompt, "duration": dur,
               "reference_images": [sb_local, end_p], "name": f"shot_{seq:02d}"}
    else:
        if sb_local:
            refs = [sb_local] + main_char_img
            prompt = comfyui_client._build_h3_prompt(
                shot, char_refs, scene_refs, storyboard_ref={"name": f"shot_{seq}"})
        else:
            refs = ref_imgs
            # 择优：既有 prompt_h3 结构合规才采用，否则用规范构建器重建
            # （历史缺陷：`shot.get('prompt_h3') or _build_h3_prompt(...)` 让
            #  剧本里那句无参考图标签的裸英文把结构化提示词整个顶掉）
            prompt = comfyui_client.resolve_h3_prompt(shot, char_refs, scene_refs)
        try:
            dur = float(shot.get('duration') or 5)
        except (TypeError, ValueError):
            dur = 5.0
        seg = {"prompt": prompt, "duration": dur, "reference_images": refs,
               "name": f"shot_{seq:02d}"}

    # ---- 提示词预检（生成前质检）----
    # H3 的结构缺段只有生成端能重建（必须有每张参考图的用途），所以这里做「验证 + 安全追加」，
    # 命中致命缺陷就直接拦：缺段的 H3 提示词等于出片跑偏，而一次视频生成的代价远大于一次判断。
    prompt, _pf_v, _pgate_v = _prompt_preflight(
        "h3", prompt, ctx=shot,
        style=(shot.get("style") or style_kit.normalize_style(
            (autopilot.get_plan(project) or {}).get("style"))),
        # ⚠️ 用 seg 里的参考图数量，不要用 `refs`：关键帧分支只设 ref_images，没有 `refs`，
        #    直接引用会 NameError（该分支走不到 else，`refs` 从未绑定）。
        expect_refs=bool(seg.get("reference_images")),
        project_name=project)
    seg["prompt"] = prompt
    if not _pgate_v.get("accept"):
        # ★ 用户需求：视频提示词预检不通过 → 提示词唯一落盘物（P12）移回收站（同决策 1）。
        try:
            _purge_prompt_records(project, shot_id,
                                  reason=f"视频提示词预检未通过（{_pgate_v.get('label')}）")
        except Exception as _pe:  # noqa: BLE001
            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
        return jsonify({"success": False, "prompt_qc_blocked": True,
                        "error": f"视频提示词预检未通过（{_pgate_v.get('label')}）：{_pgate_v.get('reason')}"
                                 + (f"；建议：{_pf_v.get('rebuild_hint')}" if _pf_v.get("rebuild_hint") else ""),
                        "prompt_qc": _pf_v.get("verdict")}), 200

    try:
        result = comfyui_client.generate_h3_sequence(
            segments=[seg], filename_prefix=f"comic_drama_retry/{project}_shot_{seq:02d}",
            seed=data.get('seed'), timeout_per_segment=int(data.get('timeout') or 900))
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"单镜视频重跑失败：{e}"}), 500
    files = (result or {}).get('files') or []
    if not files or not os.path.isfile(files[0]):
        return jsonify({"success": False, "error": "ComfyUI 未返回视频文件"}), 500
    vid_dir = _ep_dir(os.path.join(VIDEOS_DIR, project), _rs_ep)
    os.makedirs(vid_dir, exist_ok=True)
    dst = os.path.join(vid_dir, f"shot_{seq:02d}.mp4")
    shutil.move(files[0], dst)
    # 该镜已更新 → 同集的旧成片失效，打上「已过期」标记，避免用户对着旧成片点验收
    stale = {}
    try:
        stale = pipeline.mark_deliverable_stale(
            project, _rs_ep or 1, "镜头重做后成片需重新合成",
            {"shot_id": shot_id, "seq": seq, "mode": mode,
             "video": os.path.basename(dst)})
    except Exception as e:  # noqa: BLE001 - 打标失败不影响重做本身
        app.logger.warning(f"标记成片过期失败：{e}")
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "mode": mode, "path": dst,
                    "url": f"/api/videos/{project}/{_rs_sub}{os.path.basename(dst)}",
                    "ref_count": len(seg["reference_images"]), "duration": seg["duration"],
                    "deliverable_marked_stale": bool(stale)})


# ==========================================================================
# P2-1 / P2-2  NLE 导出（剪映草稿 / FCPXML / SRT / 帧序列）
# ==========================================================================

@app.route('/api/export/run', methods=['POST'])
def api_export_run():
    """一键导出：剪映草稿 + FCPXML + SRT + 帧序列清单

    body: {project_name, episode_no?, formats?: ["jianying","fcpxml","srt","frames"]}
    """
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project_name') or '')
    if err is not None:
        return err
    script = _load_script_for(project, data.get('episode_no'))
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    formats = data.get('formats')
    # B-17 P2-13：传集号给 nle_export，按集号过滤视频目录，避免跨集混用素材
    ep_no = data.get('episode_no')
    try:
        results = nle_export.export_all(project, script,
                                        formats=formats if isinstance(formats, list) else None,
                                        episode=ep_no)
    except Exception as e:  # noqa: BLE001
        app.logger.exception("NLE 导出失败")
        return jsonify({"success": False, "error": f"导出失败：{e}"}), 500
    return jsonify({"success": bool(results.get("ok")), "project": project, **results})


@app.route('/api/export/list', methods=['GET'])
def api_export_list():
    """导出记录列表（可按项目过滤）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else None
    try:
        items = nle_export.list_exports(project)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 500
    # 前端（ExportPage / 工作台 ExportTab）统一消费 `files` 字段：
    # 这里在保留 `items` 原始结构的同时，补一份前端可直接渲染的规范化列表。
    files = []
    seen_names = set()
    for it in items:
        p = it.get("path") or ""
        name = os.path.basename(p) if p else ""
        if name:
            seen_names.add(name)
        files.append({
            "format": it.get("format"),
            "filename": name,
            "exists": bool(p and os.path.isfile(p)),
            "path": p,
            "dir": it.get("dir"),
            "project": it.get("project"),
            "exported_at": it.get("exported_at"),
            "shot_count": it.get("shot_count"),
            "total_sec": it.get("total_sec"),
            "size_mb": it.get("size_mb"),
        })

    # 合并 ExportManager 产物（output/exports/<project>/），
    # 该目录与 nle_export.EXPORT_DIR（output/export/）不同，需单独扫描，
    # 否则「生成导出文件」后列表仍显示为空。
    if project:
        em_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project)
        known = [
            ("fcpml", f"{project}_fcpml.xml"),
            ("edl", f"{project}_edl.edl"),
            ("json", f"{project}_timeline.json"),
        ]
        for fmt, fn in known:
            fp = os.path.join(em_dir, fn)
            if os.path.isfile(fp) and fn not in seen_names:
                files.append({
                    "format": fmt,
                    "filename": fn,
                    "exists": True,
                    "path": fp,
                    "dir": em_dir,
                    "project": project,
                    "exported_at": datetime.fromtimestamp(
                        os.path.getmtime(fp)).isoformat(timespec="seconds"),
                    "size_mb": round(os.path.getsize(fp) / 1048576, 3),
                })

    return jsonify({"success": True, "count": len(files), "items": items, "files": files})


@app.route('/api/export/download/<path:filename>')
def api_export_download(filename):
    """导出产物下载（限导出根目录内）"""
    return _serve_attachment(nle_export.EXPORT_DIR, filename)


# ==========================================================================
# P1-4 / P2-4  引擎 Provider 与插件注册表（透明化，只读为主）
# ==========================================================================

@app.route('/api/providers', methods=['GET'])
def api_providers():
    """列出各环节可用引擎与当前生效实现

    refresh=1 时绕过可用性探测缓存重新探测（可用性探测会真连 ComfyUI / TTS，
    因此默认走 TTL 缓存，避免前端刷新把列表接口拖到数秒）。
    """
    force = str(request.args.get('refresh') or '').strip() in ('1', 'true', 'yes')
    try:
        data = providers.catalog(force=force)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"引擎目录读取失败：{e}"}), 500
    env_keys = {kind: info.get("env_key") for kind, info in (data or {}).items()}
    return jsonify({"success": True, "kinds": data, "env_keys": env_keys})


@app.route('/api/providers/select', methods=['POST'])
def api_providers_select():
    """切换某环节引擎（写入运行时环境变量；持久化请改 .env 的 MJSCXT_PROVIDER_*）"""
    data = request.json or {}
    kind = str(data.get('kind') or '').strip().lower()
    name = str(data.get('name') or '').strip()
    if kind not in ('image', 'video', 'tts') or not name:
        return jsonify({"success": False, "error": "参数非法（kind ∈ image/video/tts）"}), 400
    try:
        info = providers.set_active(kind, name)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "kind": kind, **info})


@app.route('/api/plugins', methods=['GET'])
def api_plugins():
    """插件目录（内置环节 + plugins/ 目录下用户自定义 Agent）"""
    try:
        data = plugin_registry.catalog()
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"插件目录读取失败：{e}"}), 500
    return jsonify({"success": True, **data,
                    "dependency_order": plugin_registry.get_registry().dependency_order()})


@app.route('/api/plugins/run', methods=['POST'])
def api_plugins_run():
    """执行指定插件（把剧本等上下文传入插件，返回插件产出）"""
    data = request.json or {}
    pid = str(data.get('plugin_id') or '').strip()
    if not pid:
        return jsonify({"success": False, "error": "缺少 plugin_id"}), 400
    ctx = data.get('context') if isinstance(data.get('context'), dict) else {}
    project = _safe_project(data.get('project_name') or '')
    if project and 'script' not in ctx:
        ctx['script'] = _load_script_for(project, data.get('episode_no'))
        ctx['project_name'] = project
    try:
        result = plugin_registry.run(pid, ctx)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"插件执行失败：{e}"}), 500
    return jsonify({"success": bool(result.get("ok")), "plugin_id": pid, "result": result})


# ==========================================================================
# P2-5 国际化
# ==========================================================================

@app.route('/api/i18n/<lang>', methods=['GET'])
def api_i18n(lang):
    """读取前端语言包（zh-CN / en-US）"""
    lang = (lang or 'zh-CN').strip()
    safe = "".join(c for c in lang if c.isalnum() or c in "-_") or "zh-CN"
    locale_dir = os.path.join(PROJECT_ROOT_DIR, "locales")
    path = os.path.join(locale_dir, f"{safe}.json")
    if not os.path.isfile(path):
        fallback = os.path.join(locale_dir, "zh-CN.json")
        if not os.path.isfile(fallback):
            return jsonify({"success": False, "error": f"语言包不存在：{safe}",
                            "available": []}), 404
        path = fallback
        safe = "zh-CN"
    try:
        with open(path, "r", encoding="utf-8") as f:
            pack = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"语言包解析失败：{e}"}), 500
    available = []
    if os.path.isdir(locale_dir):
        available = sorted(os.path.splitext(f)[0] for f in os.listdir(locale_dir)
                           if f.lower().endswith(".json"))
    return jsonify({"success": True, "lang": safe, "messages": pack,
                    "available": available})


# ===== 步骤1：剧本生成 =====

@app.route('/api/script/generate', methods=['POST'])
def api_generate_script():
    # P0-5 门禁：文本分析模型未配置 / 端点不可达 → 直接阻断，不给「静默跑进执行中」的机会
    _gate = _ai_gate_or_400("script")
    if _gate is not None:
        return _gate
    data = _body()
    theme = data.get('theme', '')
    episodes = data.get('episodes', 1)
    duration = data.get('duration', 60)
    style = data.get('style', '古风仙侠')
    style = _apply_project_settings(style, _safe_project(data.get('project_name') or (theme or '')[:20] or 'project'))

    if not theme:
        return jsonify({"error": "请提供主题"}), 400

    try:
        # 统一走前端「AI 设置 → 文本分析模型」的密钥（OpenAI 兼容，任意厂商），
        # 不再读环境变量 ANTHROPIC_API_KEY / 硬编码 CLAUDE_MODEL。
        client = _current_llm_client()
        script = script_gen.generate_script_with_client(
            client, theme=theme, episodes=episodes,
            duration_per_episode=duration, style=style
        )
        project_name = _safe_project(theme[:20])
        script_path = script_gen.save_script(script, project_name,
                                             provider="openai-compatible")
        script["metadata"]["script_path"] = script_path

        # S1：剧本质检接线（原为死代码——生产路径只调无质检的 generate_script，
        # 结构缺陷直接流入分镜/视频后才暴露）。这里在剧本落盘后跑一次 check_script，
        # 按 script_qc_ready 门控（与 image/video qc 同模式，开关关时 no-op、不报错），
        # 结果透出给前端如实回显「剧本是否已质检」。单次检测（非 generate_script_with_qc
        # 的 3 次重试+AI 判断重链路），止血优先、行为保守。
        qc_cfg = _qc_load_cfg()
        qc_result = {"skipped": True, "reason": "剧本质检未启用"}
        if qc_client.script_qc_ready(qc_cfg):
            qc_result = qc_client.check_script(
                script_data=script, style=style, target_duration=duration, cfg=qc_cfg)
            app.logger.info("剧本质检：passed=%s score=%s reason=%s",
                            qc_result.get("passed"), qc_result.get("score"),
                            qc_result.get("reason"))

        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "script_qc_active": qc_client.script_qc_ready(qc_cfg),
            "qc_result": qc_result,
        })
    except LLMError as e:
        # 未配置「文本分析模型」或密钥错误：给引导（400）而非裸 500，
        # 引导用户去 AI 设置配置 base_url / api_key / model。
        # _ai_guide_response 已返回 (jsonify, code) 元组，直接透传。
        app.logger.error(f"剧本生成 LLM 调用失败: {e}")
        return _ai_guide_response(str(e))
    except Exception as e:
        app.logger.error(f"生成剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


# ===== 步骤2/3/4：资产生成（角色/物品/场景 + 多视角） =====

def _generate_asset_task(task_id: str, assets: list, asset_type: str, project_name: str,
                         style: str = "", overwrite: bool = False):
    """后台资产生成任务：基础图 + （角色）由整图本地切分派生的视角单图

    P0 修复（④⑤）：全链路接入 AI 质检——基础图必须送检；不达标自动重生成（换 seed），
    重试仍不达标 / 质检调用异常 → 阻断入库并标记 qc_blocked。

    A-2 P0 断点续跑：新增 overwrite 参数（默认 False）。已达标入库的资产
    （目录内已有非空图，判据同 pipeline.probe_assets）直接跳过，不再重复
    「生成→质检→重画」；overwrite=True 时强制全量重生成。

    风格落地（2026-09-18 修复）：新增 style 参数。此前该任务**完全没有风格入参**，
    资产提示词只有 bible 的 reference_prompt_zh（实测其中零风格词），于是物品/角色/场景
    出的参考图完全不体现用户与总控敲定的风格。现在：
      - 正向提示词在生成前统一追加风格后缀（幂等，二次追加不重复）；
      - 解析画幅并覆写尺寸节点，「竖屏 9:16」真正落到画布；
      - 派生视角图继承基础图的画幅（切分件贴回同尺寸画布，见 sheet_split）。

    ⚠️ 2026-09-24 视角图改造（勿回退为 GPU 多视角重渲染）：详见 app/sheet_split.py 模块头。
      角色视角图由**基础图整图本地列投影切分**得到（零 GPU、零质检），物品/场景不再产出
      视角图；`success` 也不再受视角质量影响。
    """
    try:
        base_dir = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}[asset_type]
        gen_base = {
            "character": comfyui_client.generate_character_base,
            "item": comfyui_client.generate_item_base,
            "scene": comfyui_client.generate_scene_base,
        }[asset_type]

        # 风格（文字部分，如画风/色调）仍从总控敲定的 style 串解析；
        # 画幅**按资产类型内置写死**（2026-09-22 需求，不跟随视频比例）：
        #   角色参考图(三视图设定图) 1:1 / 道具 item 1:1 / 场景 scene 16:9
        # 与成片画幅解耦——即使用户拍 9:16 成片，角色参考图仍是 1:1。
        # ⚠️ 角色基础图内容是「正/侧/背三张全身视图横排的三视图设定图」，不是单人立绘，
        #    2026-09-23 已从 3:4 竖幅改回 1:1（3:4 会把三人挤到贴边，实测留白 0~2px）；
        #    详见 style_kit.ASSET_BASE_RATIO 上方注释。
        # ⚠️ 2026-09-24：视角图已改为从基础图**本地切分**派生（app/sheet_split.py），
        #    不再有「多视图独立画幅」这条路径，故这里只需解析基础图画幅。
        style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO)
        gen_style = style_res["style"]
        _base_ratio = style_kit.asset_aspect_ratio(asset_type) or style_kit.DEFAULT_RATIO
        gen_size = style_kit.aspect_size(_base_ratio)
        app.logger.info("[资产风格] %s 资产生成风格=%s；内置画幅 %s×%s",
                        asset_type, gen_style or style, _base_ratio[0], _base_ratio[1])

        # 质检配置：任务级读取一次，本任务内所有资产共用
        qc_cfg = _qc_load_cfg()
        qc_on = qc_client.image_qc_ready(qc_cfg)          # 质检接口是否可用
        qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
        if qc_declared and not qc_on:
            # 已声明开启图片质检但接口不可用：后续逐个资产明确阻断，绝不静默放行
            app.logger.error("[资产质检] 图片质检已开启但接口未就绪（base_url/api_key/model 不完整），"
                             "本次资产生成将阻断入库；请检查 qc_config.json")
        max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
        scratch_root = os.path.join(QC_DIR, project_name, "assets_scratch")

        results = []
        total = len(assets)

        def _set_phase(phase: str, qc_phase: str = None):
            with lock:
                generation_state[task_id]["phase"] = phase
                if qc_phase:
                    generation_state[task_id]["qc_phase"] = qc_phase

        for i, asset in enumerate(assets):
            name = asset.get('name', f'{asset_type}_{i+1}')
            try:
            
                # A-2 P0 断点续跑：已达标入库的资产不重复「生成→质检→重画」。
                # 就绪判据与 pipeline.probe_assets / _first_existing_asset_image 一致
                # （目录内任意一张非空白名单图）。仅 overwrite=True 时强制重生成。
                # ⚠️ 只在「已就绪」时提前 continue；未就绪项继续走下面的重要性过滤
                #    与完整生成链路，临时道具的 skip 路径不受影响。
                if not overwrite:
                    _ready_img = _first_existing_asset_image(
                        os.path.join(base_dir, project_name, name))
                    if _ready_img:
                        app.logger.info("资产已达标入库，断点续跑跳过：%s（%s）",
                                        name, _ready_img)
                        results.append({"name": name, "status": "skipped",
                                        "reason": "已达标入库，断点续跑跳过"})
                        continue

                # 物品过滤：只生成重要道具的参考图
                if asset_type == 'item':
                    importance = asset.get('importance', '')
                    if importance and importance != '重要':
                        app.logger.info(f"跳过临时道具 '{name}'（importance={importance}），不生成参考图")
                        results.append({"name": name, "status": "skipped", "reason": f"临时道具，importance={importance}"})
                        continue
            
                prompt_zh = asset.get('reference_prompt_zh', asset.get('prompt_zh', asset.get('appearance', '')))

                with lock:
                    generation_state[task_id].update({
                        "current": i + 1, "progress": int((i + 1) / total * 100),
                        "current_asset": name, "phase": "基础图"
                    })

                asset_dir = os.path.join(base_dir, project_name, name)
                os.makedirs(asset_dir, exist_ok=True)
                scratch_dir = os.path.join(scratch_root, f"{asset_type}_{name}")
                os.makedirs(scratch_dir, exist_ok=True)
                _qc_prune_attempts(scratch_dir)   # G8③：清理上一轮遗留的过期 try（只留最近 4）
                qc_desc = f"资产类型：{asset_type}；资产名称：{name}；资产设定：{str(prompt_zh)[:400]}"

                # ---------- 阶段1：基础图（生成 → 质检 → 重生成 → 阻断判定） ----------
                base_dst = os.path.join(asset_dir, "base.png")
                base_attempts = []
                base_gate = None
                base_ok = False
                base_files = []
                # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None 走模板默认常量），
                # 使 qc.history[0].seed 不再为 null，产物可复现、可追溯。
                seed = random.randint(1, 2 ** 31 - 1)
                orig_asset_prompt = prompt_zh     # 教训库稳定键（改写后的提示词不参与指纹）
                # ---- 提示词预检（生成前质检）----
                # 资产是「一对多」批量生成（一个项目几十个角色/物品/场景），与整集同理不做硬阻断：
                # 单条提示词有问题就自愈 + 记录，不让整批资产生成中断。缺失/过短这类致命缺陷
                # 由后面的生成+质检链路兜底（空提示词本就出不来可用资产）。
                prompt_zh, _pf_asset, _pgate_asset = _prompt_preflight(
                    "asset", prompt_zh, ctx=asset, style=(asset.get("style") or gen_style or ""),
                    project_name=project_name, cfg=qc_cfg)   # G13：复用 worker 级配置
                if not _pgate_asset.get("accept"):
                    app.logger.warning("资产「%s」参考图提示词预检未通过（%s）：%s",
                                       name, _pgate_asset.get("label"), _pgate_asset.get("reason"))
                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        seed = random.randint(1, 2 ** 31 - 1)
                        # ① 优先：针对**上一轮这张图**的质检缺陷，用 LLM 即时改写提示词（精准）
                        # ② 回落：召回历史教训库改写；再回落：仅换种子。
                        # ⚠️ 基准用 orig_asset_prompt，避免建议块一轮轮累积
                        prompt_zh = orig_asset_prompt
                        optimized = None
                        if base_attempts:
                            optimized = _optimize_prompt_from_qc(
                                "asset", orig_asset_prompt, base_attempts[-1],
                                style=gen_style or _qc_style_of(project_name))
                        if optimized:
                            prompt_zh = optimized
                            app.logger.info("资产 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
                                            name, attempt + 1)
                        else:
                            try:
                                suggestions = prompt_memory.suggest(
                                    kind="asset",
                                    prompt=orig_asset_prompt,
                                    project=project_name,
                                    root_dir=PROJECT_OUTPUT_DIR
                                )
                                learned = prompt_memory.learned_prompt(
                                    kind="asset",
                                    prompt=orig_asset_prompt,
                                    project=project_name,
                                    root_dir=PROJECT_OUTPUT_DIR,
                                    style=_qc_style_of(project_name),
                                )
                                if learned and learned != orig_asset_prompt:
                                    prompt_zh = learned
                                    app.logger.info("资产 %s 第 %d 次重试，按历史质检教训改写提示词：%s",
                                                    name, attempt + 1, suggestions[:2])
                                else:
                                    app.logger.info("资产 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                    name, attempt + 1)
                            except Exception as mem_err:
                                app.logger.warning(f"读取记忆模块失败: {mem_err}")
                    
                        _set_phase(f"{name} 基础图质检不达标，修改提示词后重新生成（第 {attempt}/{max_retries} 次）",
                                   "regenerating")
                    # B-13 P1-14：output 基础图改按「项目/类型/资产名」分桶，不再按
                    # 「项目×类型」混放——同名资产跨项目、同项目不同集共享 asset_type 时
                    # 互串基础图。资产目录仍按 name 分桶（asset_dir 不变），仅 output 桶细化。
                    base_files = gen_base(prompt_zh, seed=seed, style=gen_style, size=gen_size,
                                          filename_prefix=f"comic_drama/{project_name}/{asset_type}/{name}")
                    if not base_files:
                        base_attempts.append({"attempt": attempt + 1, "seed": seed, "stage": "基础图生成",
                                              "ok": False, "error": "基础图生成失败"})
                        # B-16 P2-11：基础图生成失败 → 清理本资产产生的 scratch 中间产物
                        _cleanup_scratch_dir(scratch_dir, app.logger)
                        base_gate = {"accept": False, "blocked": True, "skipped": False,
                                     "label": "生成失败", "reason": "基础图生成失败", "critical_issues": []}
                        break
                    scratch_base = os.path.join(scratch_dir, f"base_try{attempt + 1}.png")
                    # G8②：消费 ComfyUI output 源（视频链路一直用 move，图片链路此前 copy2
                    # 导致 output/comic_drama/ 只增不减）。move 后 output 目录不留残留。
                    shutil.move(base_files[0], scratch_base)
                    if not qc_on:
                        if qc_declared:
                            # 已声明开启质检但接口不可用：明确阻断（图仅留在暂存区），不静默放行
                            base_gate = {"accept": False, "blocked": True, "skipped": False,
                                         "label": "质检接口未就绪",
                                         "reason": "已开启图片质检但质检接口不可用"
                                                   "（qc_config.json 缺 base_url / api_key / model）",
                                         "critical_issues": []}
                            break
                        base_gate = {"accept": True, "blocked": False, "skipped": True,
                                     "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                     "critical_issues": []}
                        base_ok = True
                        break
                    _set_phase(f"{name} 基础图质检中（第 {attempt + 1} 次）", "checking")
                    verdict = qc_client.check_image(scratch_base, qc_desc, qc_cfg, style=gen_style)
                    app.logger.info(f"[资产质检] base {asset_type}/{name} 第{attempt + 1}次 → "
                                    f"{verdict.get('call_url')} model={verdict.get('model')} "
                                    f"ok={verdict.get('ok')} passed={verdict.get('passed')} "
                                    f"score={verdict.get('score')} style_mismatch={verdict.get('style_mismatch')} "
                                    f"latency={verdict.get('latency_ms')}ms")
                    base_attempts.append(_qc_record_verdict(project_name, "asset_image", f"{name}_base",
                                                            "资产基础图质检", attempt + 1, seed,
                                                            scratch_base, verdict, style=gen_style))
                    base_gate = _qc_gate(verdict)
                    if base_gate["accept"]:
                        base_ok = True
                        break
                    if not verdict.get("ok"):
                        break     # 质检接口异常，重生成无意义
                    # ★ 立刻沉淀：让同一次循环的下一次重试就能召回这条缺陷
                    _record_qc_lesson(project_name, "asset", orig_asset_prompt, base_attempts[-1])
                    # ★ G1 止损：连续两次基础图缺陷完全相同 → 继续重试只是重复烧 GPU，提前停
                    _hopeless, _hopeless_detail = _qc_retry_hopeless(base_attempts)
                    if _hopeless:
                        base_attempts[-1]["retry_stopped"] = True
                        base_attempts[-1]["retry_stopped_features"] = _hopeless_detail
                        app.logger.warning(
                            f"资产基础图重试止损（{name}）：连续 {len(base_attempts)} 次缺陷完全相同，"
                            f"提前停止重试。缺陷：{_hopeless_detail}；建议改写该资产提示词后重跑")
                        break
                if not base_ok:
                    # 把「基础图哪里不对」沉淀进教训库（供下次重生成时改写提示词）
                    if base_attempts and isinstance(base_attempts[-1], dict):
                        _record_qc_lesson(project_name, "asset", prompt_zh, base_attempts[-1])
                    # ★ 用户需求：质检「判定不通过」的暂存基础图不留本地（含 ComfyUI 侧）。
                    # ⚠️ 仅当最后一次尝试是「质检成功返回且不合格」（ok=True）时才删；
                    # ok=False（接口故障）/ skipped（未开启）/ 质检接口未就绪 都不删。
                    try:
                        _last_b = base_attempts[-1] if (base_attempts and isinstance(base_attempts[-1], dict)) else {}
                        if qc_on and _last_b.get("ok") is True:
                            _purge_rejected_artifacts(
                                [_last_b.get("file") or scratch_base],
                                project=project_name,
                                reason=f"资产基础图质检不合格（{(base_gate or {}).get('label')}）",
                                kind="asset_base_image",
                                history_file=_last_b.get("history_file") or "")
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"资产不合格基础图清理失败（忽略）：{_pe}")

                    results.append({
                        "name": name, "success": False, "dir": asset_dir, "stage": "基础图",
                        "qc_blocked": bool(base_gate and base_gate.get("blocked")),
                        "error": (f"基础图未通过质检（{base_gate['label']}）：{base_gate['reason']}"
                                  if base_gate else "基础图生成失败"),
                        "qc": _qc_summary(base_attempts, qc_declared, qc_on,
                                          int(qc_cfg.get("max_retries", 0))),
                    })
                    continue
                # 质检达标 → 正式入库
                if base_attempts:
                    shutil.copy2(base_attempts[-1]["file"], base_dst)
                else:
                    # G8②：2380 处已把 ComfyUI output move 到 scratch_base（不再 copy2），
                    # 故入库源是 scratch_base（base_files[0] 此时已 move 走、不可再取）
                    shutil.copy2(scratch_base, base_dst)
                # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论），可复现可追溯
                _write_artifact_meta(
                    base_dst, kind="asset_base", project_name=project_name,
                    seed=seed, prompt=orig_asset_prompt,
                    workflow_key={"character": "character_gen", "item": "item_gen",
                                  "scene": "scene_gen"}.get(asset_type),
                    qc=base_gate, asset_name=name,
                    extra={"asset_type": asset_type, "style": gen_style or None})

                # ---------- 阶段2：视角单图（本地切分，不再走 GPU 多视角编辑） ----------
                # 2026-09-24 改造，机制与实测见 app/sheet_split.py 模块头 + config 同名注释：
                #   旧实现在这里调 `comfyui_client.generate_multiview` 逐视角**重渲染** —— 实测
                #   4 张产物与 base.png **内容一致**（参考图编辑 cfg=1.0 只复刻已见机位），
                #   净成本 = 每资产 4 次 GPU 渲染 + 4 次质检，收益 = 0（下游只取 front.png），
                #   且多视角不达标会把**整个资产判 failed**（旧 success = not blocked_views）。
                #   现在：
                #     · 角色 → 从三视图整图**本地列投影切分**出 front/left/back 单视角图
                #       （零 GPU、零质检），并清掉旧实现遗留的 right.png；
                #     · 物品 / 场景 → 基础图本身就是单主体图，不再产出任何视角图。
                #   切分是**已通过质检**的基础图的确定性派生 —— 没有可重试的自由度
                #   （重跑只会得到同一张切图），故不再需要「逐视角质检 → 整组重生成」循环。
                view_paths = {"base": base_dst}
                view_attempts = {}
                view_gate = {}
                saved_views = []
                blocked_views = []      # 保留字段：派生无「阻断」语义，恒为空
                derive_error = None
                if asset_type == "character":
                    try:
                        _derived = sheet_split.split_sheet_to_files(
                            base_dst, asset_dir, CHARACTER_SHEET_VIEWS,
                            logger=app.logger, prune=False)
                    except Exception as _dv_err:  # noqa: BLE001 派生失败绝不拖垮已达标的基础图
                        _derived = {}
                        derive_error = f"{type(_dv_err).__name__}: {_dv_err}"
                        app.logger.warning(
                            "角色「%s」三视图整图切分失败，本次仅保留整图 %s"
                            "（下游参考图将回退它）：%s",
                            name, os.path.basename(base_dst), _dv_err)
                    for vk, vpath in _derived.items():
                        view_paths[vk] = vpath
                        view_gate[vk] = {
                            "accept": True, "blocked": False, "skipped": True,
                            "label": "本地派生（继承基础图质检）",
                            "reason": "由已达标的基础图整图切分得到，不单独质检",
                            "critical_issues": [],
                        }
                        # O2：视角图旁路元数据（显式标注「由整图切分派生」，与基础图 seed 同源）
                        _write_artifact_meta(
                            vpath, kind="asset_view", project_name=project_name,
                            seed=seed, prompt=orig_asset_prompt, workflow_key=None,
                            qc=view_gate[vk], asset_name=name,
                            extra={"asset_type": asset_type, "view": vk,
                                   "derived_from": os.path.basename(base_dst),
                                   "derive_mode": "sheet_crop"})
                        saved_views.append(vk)
                    # ⚠️ 切分**失败时也要清理**陈旧视角（2026-09-24 修正）。
                    # 旧注释写的是「失败时保留旧图，避免删了又没有新的」，但那个顾虑不成立：
                    # base.png 是本轮刚重画并通过质检的整图，下游 _first_existing_asset_image
                    # 会按 _ASSET_IMG_PRIORITY（front > base）回落它 —— 一定「有新的」。
                    # 而保留旧视角图的代价很大：旧图来自**上一轮**（可能是旧设定），却因优先级
                    # 高于 base.png 被下游优先取走，于是「文字是新设定、参考图是旧设定」的互斥
                    # 照旧存在（实测 逆天系统/赵天霸：重画后切分失败 → 旧发型图继续被用作参考，
                    # 上游的外形收敛等于白做）。清掉后下游自动回落新整图。
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=CHARACTER_SHEET_VIEWS if _derived else (),
                            known=ASSET_VIEW_STEMS, logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)
                else:
                    # 物品 / 场景：基础图即单主体图；清掉旧实现遗留的视角图，避免 UI 把陈旧的
                    # 「重渲染整图」继续当成一个视角展示。
                    try:
                        sheet_split.prune_stale_views(
                            asset_dir, keep=(), known=ASSET_VIEW_STEMS, logger=app.logger)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning("清理陈旧视角文件失败（不影响入库）：%s", _pe)

                results.append({
                    "name": name,
                    "success": True,
                    "dir": asset_dir,
                    "views": list(view_paths.keys()),
                    "qc_blocked": False,
                    "qc_blocked_views": blocked_views,
                    "derive_error": derive_error,
                    "error": None,
                    # 视角图为本地派生（不单独质检），故 qc 汇总只反映基础图
                    "qc": _qc_summary(base_attempts, qc_declared, qc_on,
                                      int(qc_cfg.get("max_retries", 0))),
                    "qc_base": _qc_summary(base_attempts, qc_declared, qc_on,
                                           int(qc_cfg.get("max_retries", 0))),
                    "qc_views": {},
                })

            except Exception as _asset_err:  # noqa: BLE001
                # ⚠️ 审计 S11：旧代码这里没有 try —— `generate_multiview` 上传基础图失败会
                #    raise RuntimeError，`queue_prompt` 遇 5xx/超时也会抛。任一处抛出 →
                #    整个批次被标 failed，`results`（已成功资产的结果）全部丢弃：
                #    20 个资产在第 7 个时来一次连接抖动，前 6 个已入库的成果用户也看不见。
                app.logger.error("资产「%s」生成失败（已隔离，继续后续资产）：%s: %s",
                                 name, type(_asset_err).__name__, _asset_err)
                results.append({
                    "name": name, "success": False,
                    "dir": os.path.join(base_dir, project_name, name),
                    "stage": "异常中断", "qc_blocked": False,
                    "error": f"{type(_asset_err).__name__}: {_asset_err}",
                })
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / total * 100),
                        "phase": f"{name} 生成异常（已跳过）",
                    })
                continue
        blocked_count = sum(1 for r in results if r.get("qc_blocked"))
        with lock:
            generation_state[task_id].update({
                "status": "completed", "results": results,
                "success_count": sum(1 for r in results if r.get("success")),
                "qc_blocked_count": blocked_count,
            })
    except Exception as e:
        app.logger.error(f"资产生成失败: {e}")
        _partial = locals().get("results") or []   # 审计 S11：已成功的部分结果不能丢
        with lock:
            generation_state[task_id].update({
                "status": "failed", "error": str(e), "results": _partial,
                "success_count": sum(1 for r in _partial if r.get("success")),
            })
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    _maybe_clear_comfyui_history("资产批量生成收尾")


@app.route('/api/assets/generate', methods=['POST'])
def api_generate_assets():
    """生成资产（角色/物品/场景，含多视角）"""
    # ⚠️ 这里**故意不设** AI 前置门禁：资产生成是「消费已产出的提示词 + ComfyUI 出图 +
    # 质检」的链路，全程不读 text/qc/chat 凭证（提示词由上游剧本步骤产出、随 assets 传入）。
    # 早前一版把门禁挂在这里，后果是「AI key 没配 → 连本来能出的图也一起被拦」，
    # 属于护栏误伤业务。凡是光跑 ComfyUI 就能完成的入口都不挂门禁。
    data = _body()
    asset_type = data.get('asset_type', '')  # character / item / scene
    # P2-T2：写盘路由统一走 _project_or_400（契约必填 project_name）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    assets = data.get('assets', [])
    # A-2 P0：断点续跑开关。默认 False → 已达标入库的资产跳过；显式传 true 强制重生成
    overwrite = bool(data.get('overwrite'))

    if asset_type not in ("character", "item", "scene"):
        return jsonify({"error": "asset_type 必须是 character/item/scene"}), 400
    if not assets:
        return jsonify({"error": "没有资产数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # B-12 P1-15：资产任务 ID 改用 uuid（G5 只改了分镜/视频/配音/混音，资产漏改），
    # 同秒并发请求不再互撞。
    task_id = f"{asset_type}_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": asset_type,
            "progress": 0, "total": len(assets), "current": 0,
            "phase": "基础图", "results": [],
            "overwrite": overwrite,
        }

    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, assets, asset_type, project_name,
              data.get('style') or _project_style(project_name), overwrite)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "started"})


@app.route('/api/generation/status/<task_id>', methods=['GET'])
def api_generation_status(task_id):
    with lock:
        # P2-14（A-21）：锁内深拷贝快照再出锁。旧实现在锁外 jsonify 会读到
        # 「progress 已 100 但 results 只有 3 条」这类半更新快照（与写端点
        # 在锁内 .append/.update 竞态）。深拷贝把一致性窗口收敛到持锁段内。
        state = copy.deepcopy(generation_state.get(task_id, {}))
    return jsonify(state)


# ===== 步骤5：分镜图片生成 =====

_ASSET_DIRS = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}
_ASSET_VIEW_FILES = {"character": ("front.png", "base.png"),
                     "item": ("front.png", "base.png"),
                     "scene": ("front.png", "base.png")}


def _first_existing(*candidates):
    for c in candidates:
        if isinstance(c, str) and c and os.path.exists(c):
            return c
    return None


def _build_asset_index(assets: list, project_name: str, kind: str) -> dict:
    """构建「资产名 → 本地图片绝对路径」索引

    优先使用前端传来的 HTTP 资源路径（解析回本地），其次按资产目录约定推导
    output/assets/<kind>/<项目>/<名称>/(front|base).png，最后补齐磁盘上已有但未上报的资产。
    """
    base_dir = _ASSET_DIRS[kind]
    names = []
    for a in (assets or []):
        if isinstance(a, dict) and a.get("name"):
            names.append((a.get("name"), a))
        elif isinstance(a, str) and a:
            names.append((a, {}))
    project_dir = os.path.join(base_dir, project_name)
    if os.path.isdir(project_dir):
        known = {n for n, _ in names}
        for d in sorted(os.listdir(project_dir)):
            if os.path.isdir(os.path.join(project_dir, d)) and d not in known:
                names.append((d, {}))

    index = {}
    for name, payload in names:
        if name in index:
            continue
        # B-14 P2-4：取图判据统一走 _first_existing_asset_image——
        # 不再只认 front/base 固定名，改为「扩展名白名单 + 第一张非空」。
        # front/base 的 http URL 仍由前端 resolve_local_path 传回本地路径；
        # 本地推导时按 (project_dir, name) 目录取第一张可用图。
        asset_dir_for_name = os.path.join(project_dir, name)
        local = _first_existing(
            comfyui_client.resolve_local_path(payload.get("front") or ""),
            comfyui_client.resolve_local_path(payload.get("base") or ""),
            _first_existing_asset_image(asset_dir_for_name),
        )
        index[name] = {"name": name, "image": local,
                       "url": f"/api/assets/{kind}s/{project_name}/{name}/front.png"}
    return index


def _normalize_char_alias(name) -> str:
    """归一化角色名别名（S6）：剥离 _主角/_角色/_主/_人 后缀、去空白与《》。

    与 pipeline.probe_assets 的资产目录扫描口径对齐：资产目录名可能是
    "青玉_主角" 而剧本里写 "青玉"，或反之。这里只做「后缀剥离 + 去符号」，
    **不做模糊匹配**（避免把"阿青"误归到"青玉"）。
    """
    s = str(name or "").strip()
    s = s.replace("《", "").replace("》", "").replace(" ", "")
    for suf in ("_主角", "_角色", "_主", "_人"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


def _match_shot_chars(shot: dict, char_idx: dict) -> list:
    """S6 修复：按镜头 characters_in_shot 匹配 char_idx，**禁止静默 take-first**。

    返回匹配到的角色名列表（保持 shot 原顺序）；镜头一个角色都匹配不到 → 返回 []，
    由调用方设 shot['_no_reference']=True / shot['_ref_error']=... 决定 400 / 跳过。
    """
    chars_in = [n for n in (shot.get("characters_in_shot") or []) if n]
    if not chars_in:
        return []
    alias_map = {k: _normalize_char_alias(k) for k in char_idx.keys()}
    alias_rev = {}
    for k, v in alias_map.items():
        if v and v not in alias_rev:
            alias_rev[v] = k
    matched: list = []
    for cname in chars_in:
        if cname in char_idx:
            matched.append(cname)
            continue
        norm = _normalize_char_alias(cname)
        hit = alias_rev.get(norm)
        if hit:
            matched.append(hit)
    # 去重保序
    seen = set(); out = []
    for m in matched:
        if m not in seen:
            seen.add(m); out.append(m)
    return out


def _allocate_storyboard_refs(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                               project_name: str = None) -> list:
    """为单个镜头分配最多 3 张参考图（对应分镜工作流的 3 个参考图槽位）

    槽位键名随编辑节点换代而变（QwenImage2.1 的 TextEncodeQwenImage21 是
    ``images.image_1..3``，老的 TextEncodeQwenImageEditPlus 是 ``image1..3``），
    由 comfyui_client._find_image_slots 统一识别；本函数只负责**按序**给出这 3 张图。

    槽位语义（按重要性排序）：
      1) 主角色正视图 —— 人物外观锚点（S6：匹配不到任何角色时不再 take-first，
         而是让调用方走 no_reference 分支 → 400 / 跳过 + 警告日志）
      2) 次要角色正视图，缺则用镜头内物品正视图（物品/道具锚点）
      3) 镜头场景正视图（环境氛围锚点）
    """
    chars_in = _match_shot_chars(shot, char_idx)
    items_in = [n for n in (shot.get("items_in_shot") or []) if n in item_idx]

    refs = []
    if not chars_in:
        # S6：禁止静默 take-first —— 镜头一个角色都匹配不到时，标记 no_reference，
        # 由调用方决定 400（单镜）/ 跳过 + 警告日志（批量）。
        shot["_no_reference"] = True
        shot["_ref_error"] = (
            f"镜头 {shot.get('shot_id')} 的角色 {shot.get('characters_in_shot')} "
            f"在资产索引中均无匹配（别名归一化后仍无）")
    main_name = chars_in[0] if chars_in else None
    main_img = char_idx.get(main_name, {}).get("image") if main_name else None
    if main_img:
        refs.append(("主角色", f"参考图1是角色「{main_name}」的外貌、服装与发型", main_img))

    second_img, second_label = None, None
    for cand_name in chars_in[1:]:
        img = char_idx.get(cand_name, {}).get("image")
        if img and img != main_img:
            second_img, second_label = img, f"参考图2是角色「{cand_name}」的外貌与服装"
            break
    if not second_img:
        for cand_name in items_in:
            img = item_idx.get(cand_name, {}).get("image")
            if img and img != main_img:
                second_img, second_label = img, f"参考图2是物品「{cand_name}」的形状、材质与配色"
                break
    if second_img:
        refs.append(("次要参考", second_label, second_img))

    loc = shot.get("location")
    scene_name = loc if loc in scene_idx else None
    scene_img = scene_idx.get(scene_name, {}).get("image") if scene_name else None
    if scene_img:
        used = {r[2] for r in refs}
        if scene_img in used:
            scene_img = None
    if scene_img:
        refs.append(("场景", f"参考图3是场景「{scene_name}」的环境与氛围", scene_img))

    return _apply_closeup_ref_strategy(refs, shot, project_name)


# 特写镜头参考图策略 ---------------------------------------------------------
# 根因：分镜生成用的 Qwen Edit 以参考图构图为强先验。全身角色图 + 道具图作参考时，
# 模型倾向输出中全景，并把道具明确画在人物手中，导致「特写 + 道具已收起」类镜头反复不达标。
# 对策（仅对 camera 含「特写」的镜头生效）：
#   1) 角色参考图换为该角色全身视图的「头部特写裁剪图」（现裁现用，不改动原始资产）；
#   2) 剔除道具参考图（特写中道具应已入袖、不应出镜）；
#   3) 场景参考图保留但降级为「仅色调与氛围参考，不作为构图范围依据」。
# 角色资产 5 个视角均为「右手握笛」形象（道具已被画进人物），直接作参考会让模型把笛子
# 画进画面，与「道具已收起」类动作冲突；此处只取角色图顶部「头部条带」作锚点，
# 从参考层面切断手部/道具先验。
#
# ⚠️ 2026-09-24 口径变化：角色参考图（front.png）已从「三视图整图」改为
#    「从整图切分出的**单人格**并居中贴回同尺寸方底」（见 app/sheet_split.py）。
#    切分后人物恰好落在画面**水平中部约 31%** 宽（x≈0.34~0.66），
#    故下面的 x 区间 (0.32, 0.68) 现在正好框住这张单人格的头部 —— 语义与常量取值一致，
#    **不需要再改**。反过来，若哪天把 front.png 换回三视图整图，这个区间会落到
#    **中格（左侧面）** 的头上，取到侧脸锚点，需同步调整为最左格。
CLOSEUP_CHAR_CROP_TOP = (0.32, 0.02, 0.68, 0.28)   # 头部条带（x0, y0, x1, y1）


def _closeup_char_crop(img_path: str, project_name: str, shot_id) -> str:
    """把角色正视/半身图裁剪为头部特写图，作为特写镜头的构图锚点（失败则回退原图）。"""
    try:
        from PIL import Image
        out_dir = os.path.join(QC_DIR, str(project_name or "default"), "closeup_refs")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, f"char_closeup_shot{_shot_seq(shot_id, 1):02d}.png")
        with Image.open(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            x0, y0, x1, y1 = CLOSEUP_CHAR_CROP_TOP
            band = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
            side = max(1, min(band.width, band.height))
            left = max(0, (band.width - side) // 2)
            crop = band.crop((left, 0, left + side, side))
            target_w = max(768, crop.width)
            crop = crop.resize((target_w, max(1, int(target_w * crop.height / crop.width))))
            crop.save(dst, format="PNG")
        return dst
    except Exception as e:  # 裁剪失败不影响主流程，回退原图
        app.logger.warning(f"特写参考图裁剪失败，回退原图: {e}")
        return img_path


def _apply_closeup_ref_strategy(refs: list, shot: dict, project_name: str = None) -> list:
    """特写镜头：只保留「角色头部特写」单一锚点。

    实测（4 轮 12 次生成）：混入场景/道具参考时，模型会按参考图的取景范围把画面
    铺开成中全景，并把道具画回手中；道具参考剔除后模型仍会自行"想象"出手持物。
    因此特写镜头只给头部特写锚点（多出的槽位自动复用同一张），构图约束最强。
    """
    if "特写" not in str(shot.get("camera") or ""):
        return refs
    out = []
    for kind, label, path in refs:
        if kind == "主角色":
            out.append((kind, label + "（已替换为该角色头部特写，画面取景范围以此为准：仅肩部以上）",
                        _closeup_char_crop(path, project_name, shot.get("shot_id", 1))))
    return out or refs


# --------------------------------------------------------------------------- #
# 分镜参考图「统一画幅」（2026-09-24）
#   ⚠️ 为什么必须做：分镜模板 `分镜生成_Qwen21.json` 是「参考图编辑」型，**没有尺寸
#      节点** → style_kit.apply_latent_size 返回空 → 输出画幅**继承第一张参考图**。
#      而参考图随镜头而变：建立镜（characters_in_shot 为空）只有场景图（资产内置
#      16:9 → 960×544 横屏）；有角色的镜头第一张是角色图（1:1 → 736×736 方形）；
#      特写镜头第一张是头部裁剪条带（更小）→ **同一集分镜画幅在两三种尺寸间跳变**，
#      与项目画幅（9:16 竖屏 544×960）不符，成片拼接会出现黑边 / 拉伸。
#      这里在送进工作流前把每张参考图 cover 到目标画幅，使输出画幅恒定。
# --------------------------------------------------------------------------- #

#: 统一画幅结果的进程内缓存：键 = (绝对路径, mtime_ns, 目标宽, 目标高) → 处理后路径。
#: 同一批分镜里同一张参考图会出现多次（84 镜共用寥寥数张），缓存避免逐镜重复裁剪。
_REF_CANVAS_CACHE: dict = {}


def _ref_canvas_target(size):
    """把目标画幅规整成 (W, H) 正整数元组；None / 非法 → None（调用方按「不处理」走）。"""
    try:
        w, h = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def _fit_ref_to_canvas(im, size):
    """按 **cover** 把图缩放到恰好覆盖 size 画布并居中裁剪（内容充满、无条带）。

    ⚠️ 为什么用 cover 而不是 contain(letterbox)：2026-09-24 真图 A/B 实测
    （逆天系统 shot_02，同镜同 prompt）——
      · contain（内容缩放居中 + 自身模糊放大作底）→ 图像编辑型工作流**会模仿这个
        布局**：输出内容只占中间约 44%，上下是模型自绘的虚化带，画面利用率腰斩；
      · cover（放大到覆盖画布 + 居中裁剪）→ 内容充满整幅，构图正常（中景主体 +
        背景群像，与 camera 描述一致）。
    代价：宽幅参考图（场景资产内置 16:9）会被裁掉两侧。参考图的语义是「内容锚点」，
    中心区域通常已含代表性主体，环境细节由模型按 prompt 补全 —— 比留虚化带更划算。
    """
    from PIL import Image
    W, H = int(size[0]), int(size[1])
    if im.width == W and im.height == H:
        return im
    s = max(W / im.width, H / im.height)
    scaled = im.resize((max(W, int(round(im.width * s))),
                        max(H, int(round(im.height * s)))), Image.LANCZOS)
    left, top = (scaled.width - W) // 2, (scaled.height - H) // 2
    return scaled.crop((left, top, left + W, top + H))


def _unify_ref_canvas(refs: list, size, project_name: str = "") -> list:
    """把分镜参考图统一到目标画幅（cover 填充），返回新的 refs（结构不变）。

    只替换第 3 项（本地路径），kind / label 原样保留。失败降级：单张处理失败 → 该张
    沿用原图；缓存目录不可建 → 整批沿用原图；size 非法 → 原样返回。任何情况都不抛
    异常、不阻断分镜生成。
    """
    tgt = _ref_canvas_target(size)
    if not tgt or not refs:
        return refs
    out_dir = os.path.join(QC_DIR, str(project_name or "default"), "ref_canvas")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        app.logger.warning("参考图统一画幅：缓存目录不可建，本次沿用原图（%s）", e)
        return refs
    unified, changed = [], 0
    for item in refs:
        try:
            kind, label, path = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            unified.append(item)
            continue
        newp = path
        try:
            local = comfyui_client.resolve_local_path(path) or path
            if not local or not os.path.isfile(local):
                raise FileNotFoundError(f"参考图本地路径不可用: {path}")
            key = (os.path.normcase(os.path.abspath(local)),
                   int(os.stat(local).st_mtime_ns), tgt[0], tgt[1])
            cached = _REF_CANVAS_CACHE.get(key)
            if cached and os.path.isfile(cached):
                newp = cached
            else:
                import hashlib
                import tempfile
                from PIL import Image
                with Image.open(local) as _im:
                    _rgb = _im.convert("RGB")
                    if (_rgb.width, _rgb.height) == tgt:
                        unified.append(item)
                        continue
                    fixed = _fit_ref_to_canvas(_rgb, tgt)
                _stem = os.path.splitext(os.path.basename(local))[0]
                _h = hashlib.sha1(os.path.abspath(local).encode("utf-8")).hexdigest()[:8]
                _dst = os.path.join(out_dir, f"{_stem}_{_h}_{tgt[0]}x{tgt[1]}.png")
                # 仓库纪律：禁止「路径拼接固定 .tmp 后缀」这类**固定临时名**（并发会互相写坏，
                # 守卫 verify_asset_skip_existing A3.3 会红）。用 mkstemp 拿唯一名。
                _fd, _tmp = tempfile.mkstemp(dir=out_dir, prefix=".refcanvas_", suffix=".png")
                os.close(_fd)
                try:
                    fixed.save(_tmp, format="PNG")
                    os.replace(_tmp, _dst)
                except BaseException:
                    try:
                        os.unlink(_tmp)
                    except OSError:
                        pass
                    raise
                _REF_CANVAS_CACHE[key] = _dst
                newp = _dst
            if newp != path:
                changed += 1
        except Exception as e:  # noqa: BLE001 —— 单张失败不影响整镜
            app.logger.warning("参考图统一画幅失败，该张沿用原图（%s: %s）",
                               type(e).__name__, e)
            newp = path
        unified.append((kind, label, newp))
    app.logger.info("[分镜参考图画幅] 目标 %d×%d，%d/%d 张已统一（其余原尺寸或降级）",
                    tgt[0], tgt[1], changed, len(refs))
    return unified


# 注：本处原为 `_shot_seq(shot_id, fallback)`。已收敛为 app/shot_key.shot_seq 的
# 一行代理（见文件顶部），全项目唯一的镜号归一化实现见 app/shot_key.py。


def _storyboard_worker(task_id: str, project_name: str, shots: list,
                       char_idx: dict, item_idx: dict, scene_idx: dict,
                       episode_no=None, style: str = "", overwrite: bool = False):
    """后台分镜图生成任务：逐镜头生成并落盘 output/storyboards/<项目>[/epNN]/shot_XX.png

    style：用户与总控敲定的风格。用于 ① 补齐镜头 style 字段（老剧本无该字段时兜底）
    ② 解析画幅并覆写分镜图尺寸，保证分镜与成片同为竖屏 9:16。

    overwrite：是否「全量重做」。默认 False —— 已达标入库的 shot_XX.png 直接复用、
    只重跑缺失/被质检阻断的镜头（断点续跑语义）。这是本次修复的核心：
    修复前无论如何都从第 1 镜重跑到最后一镜，导致「补跑 21 个不达标镜」要重烧全部 83 镜。
    需要强制全部重画时（如换风格）显式传 overwrite=True。
    注意：质检不达标的镜头只写质检暂存区、不写正式目录，所以「正式目录里已有该图」
    ⟺ 「该镜上一轮已通过质检」——跳过它不会漏掉任何不达标镜。
    """
    out_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project_name), episode_no)
    os.makedirs(out_dir, exist_ok=True)
    # 风格/画幅：整批分镜共用
    # G19：风格串未含画幅关键词时以默认 9:16 为底，不再静默回落模板 16:9
    _sb_style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO)
    _sb_style = _sb_style_res["style"]
    _sb_size = _sb_style_res["size"]
    if _sb_style:
        app.logger.info("[分镜风格] 风格=%s；画幅=%s", _sb_style,
                        _sb_style_res["label"] or "未指定（沿用模板）")
        shots = [dict(s, style=(s.get("style") or _sb_style)) for s in (shots or [])
                 if isinstance(s, dict)]
    manifest_shots = []
    # 旧 manifest：断点续跑时给「被跳过的镜头」回填上一轮的质检/提示词信息，避免信息丢失
    _prev_by_key = {}
    _prev_manifest = os.path.join(out_dir, "storyboard_manifest.json")
    # C4-1（2026-09-22 复验收口）：读取口径与 D-03/D-04 统一 —— 交给 read_json_strict
    # 自己负责三态（缺失→{}；活文件缺失但有 .bak→自动恢复；损坏→.bak 或 fail-loud），
    # 故不再用 os.path.isfile 预判。口径与 _update_storyboard_manifest_shot（本文件
    # L1990-1998 的 B-2 收口）一致；差异在于**本处是只读视图**：只给被跳过的镜头回填
    # 上一轮的质检/提示词信息、从不写回，所以损坏时**响亮降级**（error 日志 + 不回填），
    # 而不是 fail-loud 把整批分镜打挂。
    try:
        for _it in (read_json_strict(_prev_manifest, {}).get("shots") or []):
            if not isinstance(_it, dict):
                continue
            _sid = _it.get("shot_id")
            if _sid is not None:
                _prev_by_key[str(_sid)] = _it
            _sq = _shot_seq(_sid, 0)
            if _sq:
                _prev_by_key[f"shot_{_sq:02d}"] = _it
    except Exception as _e:  # noqa: BLE001
        app.logger.error("旧分镜清单 %s 不可读（%s: %s）：本次不回填被跳过镜头的信息，不影响生成",
                         _prev_manifest, type(_e).__name__, _e)
    app.logger.info("[分镜断点续跑] overwrite=%s；待处理 %d 镜（已存在者将跳过）",
                    overwrite, len(shots))
    # G13（P1）：质检配置 worker 级读一次，本批所有镜头共用（对齐资产 worker 2302）。
    # 旧代码逐镜 _qc_load_cfg()（每次 load JSON + Fernet 解密 secrets.enc），单集 56 镜 ≈
    # 上百次读盘；改为进循环前读一次，既省开销又避免「同批任务新旧配置混用」（审计 G13）。
    qc_cfg = _qc_load_cfg()
    qc_on = qc_client.image_qc_ready(qc_cfg)
    qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
    max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
    try:
        for i, shot in enumerate(shots):
            shot_id = shot.get("shot_id", i + 1)
            seq = _shot_seq(shot_id, i + 1)
            dst = os.path.join(out_dir, f"shot_{seq:02d}.png")
            # ---------- 断点续跑：已达标入库的镜头直接复用，只重跑缺失/不达标的 ----------
            # 正式目录里已有非空 shot_XX.png ⟺ 上一轮该镜已通过质检（不达标的只落暂存区）。
            # 因此这里跳过是安全的，且能把「补跑 N 个不达标镜」的代价从「全量 M 镜」降回 N 镜。
            if not overwrite and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                prev = _prev_by_key.get(str(shot_id)) or _prev_by_key.get(f"shot_{seq:02d}") or {}
                item = dict(prev) if prev else {}
                item.update({"shot_id": shot_id, "success": True, "skipped": True,
                             "file": dst, "error": "",
                             "url": f"/api/storyboards/file/{project_name}/shot_{seq:02d}.png"})
                item.setdefault("qc", {"enabled": False, "status": "skipped",
                                       "label": "沿用已达标图", "attempts": 0, "regenerated": 0})
                item.pop("qc_blocked", None)
                manifest_shots.append(item)
                with lock:
                    generation_state[task_id].update({
                        "current": i + 1,
                        "progress": int((i + 1) / max(len(shots), 1) * 100),
                        "current_shot": shot_id,
                    })
                    generation_state[task_id]["results"].append(item)
                continue
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int(i / max(len(shots), 1) * 100),
                    "current_shot": shot_id,
                    "phase": "分镜图生成",
                })
            item = {"shot_id": shot_id, "success": False, "refs": {}, "prompt": ""}
            try:
                refs = _allocate_storyboard_refs(shot, char_idx, item_idx, scene_idx, project_name)
                # 统一参考图画幅：分镜工作流无尺寸节点，输出画幅继承第一张参考图，
                # 不统一会让同集画幅在 16:9 / 1:1 间跳变（详见 _unify_ref_canvas）。
                refs = _unify_ref_canvas(refs, _sb_size, project_name)
                if not refs:
                    # S6：区分"无参考图"与"角色匹配失败"（_no_reference）
                    if shot.get("_no_reference"):
                        item["no_reference"] = True
                        item["ref_error"] = shot.get("_ref_error") or ""
                        item["error"] = f"角色匹配失败（禁止静默兜底）：{item['ref_error']}"
                        app.logger.warning(
                            f"[S6] 分镜 shot {shot_id} 角色匹配失败"
                            f"（characters_in_shot={shot.get('characters_in_shot')}），"
                            f"跳过该镜参考图分配：{item['ref_error']}")
                    else:
                        item["error"] = "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"
                else:
                    labels = [r[1] for r in refs]
                    prompt = comfyui_client.build_storyboard_prompt(shot, labels)
                    item["prompt"] = prompt
                    orig_prompt = prompt      # 教训库的稳定键：改写后的提示词不参与指纹
                    item["refs"] = {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs}

                    # ---------- 提示词预检（生成前质检）：先判 → 确定性自愈 → 再出图 ----------
                    # ⚠️ 放在 orig_prompt 之后：教训库指纹仍以构建器输出为准（自愈不参与指纹，
                    #    否则同一镜头在自愈前后会生成两条互不相认的教训）。
                    prompt, _pf_item, _pgate_item = _prompt_preflight(
                        "storyboard", prompt, ctx=shot,
                        style=(shot.get("style") or _sb_style), ref_count=len(refs),
                        project_name=project_name, cfg=qc_cfg)   # G13：复用 worker 级配置
                    item["prompt"] = prompt
                    item["prompt_qc"] = _pf_item.get("verdict")
                    item["prompt_qc_repairs"] = _pf_item.get("repairs") or []
                    if not _pgate_item.get("accept"):
                        item["prompt_qc_blocked"] = True
                        # ★ 用户需求：不合格提示词不留本地（P12）—— 该镜不生成，
                        # 把上一轮遗留的 `output/qc/<项目>/prompt_<shot>.json` 移回收站。
                        try:
                            _purge_prompt_records(
                                project_name, shot_id,
                                reason=f"提示词预检未通过（{_pgate_item.get('label')}）")
                        except Exception as _pe:  # noqa: BLE001
                            app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
                        raise _PromptQCBlocked(
                            f"提示词预检未通过（{_pgate_item.get('label')}）："
                            f"{_pgate_item.get('reason')}" +
                            (f"；建议：{_pf_item.get('rebuild_hint')}" if _pf_item.get("rebuild_hint") else ""))
                    # ★ G10：捕获自愈后提示词作为重试基准。
                    #   旧 bug：重试召回教训库以 orig_prompt（自愈前）为键，导致重试
                    #   回落到未自愈提示词，自愈修复被静默丢弃。
                    self_healed_prompt = prompt

                    # ---------- 图片 AI 质检（不达标自动重生成） ----------
                    # G13：qc_cfg/qc_on/qc_declared/max_retries 已在 worker 级读一次（见上方），
                    # 本批所有镜头共用，不再逐镜 _qc_load_cfg()。
                    attempts = []
                    # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None 走模板默认常量），
                    # 使 manifest.shots[*].qc.history[0].seed 不再为 null，产物可复现、可追溯。
                    seed = random.randint(1, 2 ** 31 - 1)
                    dst = os.path.join(out_dir, f"shot_{seq:02d}.png")

                    for attempt in range(max_retries + 1):
                        if attempt > 0:
                            seed = random.randint(1, 2 ** 31 - 1)
                            # G10：基准改为 self_healed_prompt（自愈后提示词）。
                            # 旧 bug：基准是 orig_prompt（自愈前），重试会回落到未自愈提示词，
                            # 导致首次自愈对后续重试不再生效。
                            # 教训库召回也改用 self_healed_prompt 作键：
                            # 自愈改变了提示词 → 指纹也变了，用旧指纹的教训与自愈后提示词不匹配。
                            _retry_base = self_healed_prompt
                            # ① 优先：针对上一轮这张图的缺陷，LLM 即时改写（精准）
                            # ② 回落：历史教训库召回；再回落：仅换种子
                            prompt = _retry_base
                            _opt_prompt = None
                            if attempts:
                                _opt_prompt = _optimize_prompt_from_qc(
                                    "storyboard", _retry_base, attempts[-1],
                                    style=_qc_style_of(project_name))
                            if _opt_prompt:
                                prompt = _opt_prompt
                                item["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次重试，针对本次缺陷即时优化提示词",
                                                shot_id, attempt + 1)
                            else:
                                try:
                                    hints = prompt_memory.suggest(
                                        kind="storyboard",
                                        prompt=_retry_base,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR
                                    )
                                    learned = prompt_memory.learned_prompt(
                                        kind="storyboard",
                                        prompt=_retry_base,
                                        project=project_name,
                                        root_dir=PROJECT_OUTPUT_DIR,
                                        style=_qc_style_of(project_name),
                                    )
                                    if learned and learned != _retry_base:
                                        prompt = learned
                                        item["prompt"] = prompt
                                        item["prompt_hints"] = hints[:3]
                                        app.logger.info("镜头 %s 第 %d 次重试，按质检教训改写提示词：%s",
                                                        shot_id, attempt + 1, hints[:2])
                                    else:
                                        prompt = _retry_base
                                        item["prompt"] = prompt
                                        app.logger.info("镜头 %s 第 %d 次重试，暂无可用教训，仅换种子",
                                                        shot_id, attempt + 1)
                                except Exception as mem_err:
                                    app.logger.warning(f"读取记忆模块失败: {mem_err}")

                            with lock:
                                generation_state[task_id]["phase"] = \
                                    f"质检不达标，修改提示词后重新生成（第 {attempt}/{max_retries} 次）"
                                generation_state[task_id]["qc_phase"] = "regenerating"
                        result = comfyui_client.generate_storyboard(
                            prompt_zh=prompt,
                            ref_images=[r[2] for r in refs],
                            filename_prefix=f"comic_drama_sb/{project_name}_shot_{seq:02d}",
                            seed=seed,
                            size=_sb_size,
                        )
                        if not result["files"]:
                            item["error"] = "ComfyUI 未返回分镜图（可能节点缺失或超时）"
                            break
                        # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该图）
                        sb_scratch_dir = os.path.join(QC_DIR, project_name, "storyboard_scratch")
                        os.makedirs(sb_scratch_dir, exist_ok=True)
                        _qc_prune_attempts(sb_scratch_dir)   # G8③：清本镜历史过期 try（共享目录按镜头前缀保留最近4）
                        scratch_png = os.path.join(sb_scratch_dir,
                                                   f"shot_{seq:02d}_try{attempt + 1}.png")
                        # G8②：消费 ComfyUI output 源（与视频链路的 move 语义对齐），
                        # 不再 copy2 导致 output/comic_drama_sb/ 只增不减。
                        # 每轮 attempt 都会重新 generate 出新文件，move 走旧源无副作用。
                        shutil.move(result["files"][0], scratch_png)
                        item.update({
                            "success": True,
                            "file": dst,
                            "url": f"/api/storyboards/file/{project_name}/shot_{seq:02d}.png",
                            "ref_count": len(refs),
                        })
                        item.pop("error", None)
                        if not qc_on:
                            # P0-1 fail-closed：qc_declared=True 但接口未就绪（qc_on=False）→ 阻断，
                            # **不写正式目录**。旧实现此处 `copy2(scratch_png, dst)` 属 fail-open，
                            # 会把未质检产物当成品交付并破坏「正式目录有产物 ⟺ 已过质检」不变量。
                            # 资产链路早已 fail-closed（见资产生成处的同型分支），此处对齐口径。
                            if qc_declared:
                                item["error"] = ("分镜图质检阻断（质检接口未就绪）：已开启图片质检，"
                                                 "但 base_url / api_key / model 不可用；"
                                                 "未质检产物不写入正式目录（暂存图见质检历史）")
                                app.logger.warning(
                                    "[分镜质检] qc_declared=True 但 qc_on=False → fail-closed 阻断入库"
                                    "（不写正式目录）：project=%s shot=%s", project_name, shot_id)
                                break
                            shutil.copy2(scratch_png, dst)   # 质检本就未开启：按原行为直接入库
                            break
                        with lock:
                            generation_state[task_id]["phase"] = f"图片质检中（镜头 {shot_id} · 第 {attempt + 1} 次）"
                            generation_state[task_id]["qc_phase"] = "checking"
                        verdict = qc_client.check_image(scratch_png, _qc_shot_desc(shot), qc_cfg,
                                                        style=(shot.get("style") or _sb_style),
                                                        ref_images=_qc_ref_images(
                                                            shot, char_idx, item_idx, scene_idx, refs))
                        rec = _qc_record_verdict(project_name, "image", shot_id, "图片质检",
                                                 attempt + 1, seed, scratch_png, verdict,
                                                 style=(shot.get("style") or _sb_style))
                        attempts.append(rec)
                        gate = _qc_gate(verdict)
                        item["qc_gate"] = gate
                        item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                        if gate["accept"]:
                            shutil.copy2(scratch_png, dst)   # 质检达标 → 写入正式交付目录
                            item["file"] = dst
                            # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论）
                            _write_artifact_meta(
                                dst, kind="storyboard", project_name=project_name,
                                seed=seed, prompt=item.get("prompt"), workflow_key="storyboard_gen",
                                qc=gate, shot_id=shot_id,
                                extra={"ref_count": item.get("ref_count")})
                            break
                        if not verdict.get("ok"):
                            # 质检接口异常：保留暂存图，不盲目重生成（闸门会阻断入库）
                            break
                        # ★ 立刻把这次的缺陷沉淀进教训库 ——
                        #   这样「同一次重试循环的下一次」就能召回它（旧实现只在循环结束后记一次，
                        #   导致前 N 次重试拿不到任何信息，纯粹换种子瞎撞）
                        _record_qc_lesson(project_name, "storyboard", orig_prompt, rec)
                        # ★ 重试止损：连续两次缺陷一字不差 → 「改提示词 + 换种子」根本没带来
                        #   任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 这样白烧 6 次，
                        #   全程 GPU 十几分钟，出的图全都一样）。放在闸门与教训沉淀之后：
                        #   本镜若达标早已 break，不受影响；止损只减少无效重试，不改结论。
                        _hopeless, _hopeless_detail = _qc_retry_hopeless(attempts)
                        if _hopeless:
                            # 标记最后一条质检记录：_qc_summary 据此把 label 写成「已停止重试」
                            rec["retry_stopped"] = True
                            rec["retry_stopped_features"] = _hopeless_detail
                            item["qc_retry_stopped"] = {
                                "reason": "连续两次缺陷完全相同，判定重试无收益，已提前停止",
                                "features": _hopeless_detail, "attempts": len(attempts)}
                            app.logger.warning(
                                f"分镜重试止损（镜头 {shot_id}）：连续 {len(attempts)} 次缺陷完全相同，"
                                f"提前停止重试。缺陷：{_hopeless_detail}；"
                                f"建议改写该镜剧本字段（camera / description）后单独重跑该镜")
                            break
                    if qc_declared or qc_on:
                        item["qc"] = _qc_summary(attempts, qc_declared, qc_on,
                                                 int(qc_cfg.get("max_retries", 0)))
                        gate = item.get("qc_gate")
                        if not gate or not gate.get("accept"):
                            # 教训已在循环内逐次沉淀；这里兜底记一次终态（同键会被去重）
                            if attempts and isinstance(attempts[-1], dict):
                                _record_qc_lesson(project_name, "storyboard",
                                                  orig_prompt, attempts[-1])
                            # P0：质检不达标 / 调用异常 → 阻断入库
                            item["success"] = False
                            item["qc_blocked"] = True
                            item.pop("file", None)
                            item.pop("url", None)
                            # ★ 用户需求：质检「判定不通过」的暂存图不留本地（含 ComfyUI 侧）。
                            # ⚠️ 只删「质检成功返回且判定不合格」的产物：ok=False（接口故障/
                            # 超时/鉴权失败）、skipped（未开启）、qc_on=False（接口未就绪）都
                            # 不是产物不合格，删下去会误删好图。见 _reject_artifact 红线说明。
                            try:
                                if qc_on and attempts and attempts[-1].get("ok") is True:
                                    _purge_rejected_artifacts(
                                        [scratch_png], project=project_name,
                                        reason=f"分镜图质检不合格（{gate['label']}）" if gate else "",
                                        kind="storyboard_image",
                                        history_file=attempts[-1].get("history_file") or "")
                            except Exception as _pe:  # noqa: BLE001
                                app.logger.warning(f"分镜图不合格产物清理失败（忽略）：{_pe}")
                            item["error"] = ((f"分镜图质检阻断（{gate['label']}）：{gate['reason']}"
                                              "；未通过质检，未写入正式目录（暂存图见质检历史）")
                                             if gate else (item.get("error")
                                                           or "分镜图生成失败，未写入正式目录"))
                    elif "qc" not in item:
                        item["qc"] = {"enabled": False, "status": "disabled",
                                      "label": "质检未开启", "attempts": 0, "regenerated": 0}
            except Exception as shot_err:
                app.logger.error(f"镜头 {shot_id} 分镜图生成失败: {shot_err}")
                item["error"] = str(shot_err)

            manifest_shots.append(item)
            with lock:
                generation_state[task_id]["results"].append(item)
                generation_state[task_id]["progress"] = int((i + 1) / max(len(shots), 1) * 100)

        ok = sum(1 for r in manifest_shots if r.get("success"))
        blocked = sum(1 for r in manifest_shots if r.get("qc_blocked"))
        manifest = {
            "project_name": project_name,
            # 从模板表取，避免模型换代后 manifest 里还写着旧工作流名（口径漂移）
            "workflow": WORKFLOW_TEMPLATE.get("storyboard_gen", ""),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(manifest_shots),
            "success_count": ok,
            "qc_blocked_count": blocked,
            "shots": manifest_shots,
        }
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」。落盘失败仍回滚 status。
        _sb_mp = os.path.join(out_dir, "storyboard_manifest.json")
        try:
            atomic_write_json(_sb_mp, manifest)
        except Exception as _mp_err:
            app.logger.warning(f"分镜 manifest 原子写失败（已回滚 status）：{_mp_err}")
            with lock:
                generation_state[task_id].update({
                    "status": "failed",
                    "progress": 100,
                    "success_count": ok,
                    "qc_blocked_count": blocked,
                    "output_dir": out_dir,
                    "error": f"分镜 manifest 落盘失败：{_mp_err}",
                })
            return

        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok else "failed",
                "progress": 100,
                "success_count": ok,
                "qc_blocked_count": blocked,
                "output_dir": out_dir,
                "error": "" if ok else "所有镜头分镜图均生成失败",
            })
    except Exception as e:
        app.logger.error(f"分镜图任务失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    # ★ 用户决策 4：ComfyUI 侧分镜参考图上传残留（sb_ref_*）只在**本轮分镜批量生成全部
    # 结束后**按项目清理一次 —— 不在单镜循环里调（那会在重试中途删掉当前镜头正在用的参考图）。
    try:
        _purge_sb_refs(project_name)
    except Exception as _sb_ref_err:  # noqa: BLE001
        app.logger.warning(f"sb_ref 残留清理失败（忽略）：{_sb_ref_err}")
    _maybe_reclaim_comfyui_output()   # D-11a：任务收尾滚动回收 ComfyUI 重试残留（节流+全容错）
    # 分镜批量是本项目重跑最密集的环节（每失败一次多一条任务历史）→ 收尾顺手清历史面板。
    _maybe_clear_comfyui_history("storyboard 批量生成收尾")


def _project_style(project_name: str = "") -> str:
    """取项目的生效风格：plan.json 的 style（总控 AI 敲定）> AI 设定面板 > config.json 的 style。

    前端手工触发的生成路由（分镜 / 视频 / 资产）此前**完全不传风格**，
    导致「界面按钮点出来的图」和「托管跑出来的图」风格行为不一致。
    统一从这里取，保证两条链路同源。

    2026-09-23（建项目选风格）：新增第三级兜底 config.json 的 style —— 用户「新建项目」时
    下拉/自定义的风格写进 config.style，但此前这里完全不读它，选了什么都不会生效
    （总控没敲定风格时 style 恒为空 → 生成被 409 拦截或回落默认）。现在总控没敲定时
    退回 config.style，让「建项目时选风格」这条路径真正闭环。
    """
    proj = _safe_project(project_name or "")
    try:
        brief = (autopilot.get_plan(proj) or {}).get("style") or ""
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取项目风格失败（忽略）：{e}")
        brief = ""
    if not brief:
        try:
            brief = _apply_project_settings("", proj)
        except Exception:  # noqa: BLE001
            brief = ""
    if not brief:
        # 建项目时选的风格 + 画面比例（config.json 的 style / aspect_ratio 字段）作为最后兜底。
        # 画面比例拼进风格串，让 style_kit.aspect_ratio 能解析，从而真正落到视频/分镜画布
        # （与总控 style_brief 里的「画面比例：9:16 竖屏」同一种表达，解析口径一致）。
        try:
            rec = project_store.get_project(proj)
            if rec:
                cfg = project_store.read_config(rec["dir_key"])
                brief = str(cfg.get("style") or "").strip()
                ar = str(cfg.get("aspect_ratio") or "").strip()
                if ar:
                    brief = f"{brief}，画面比例：{ar}" if brief else f"画面比例：{ar}"
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"读取项目 config.style/aspect_ratio 失败（忽略）：{e}")
            brief = ""
    return style_kit.normalize_style(brief)


def _project_subtitle_enabled(project_name: str = "") -> bool:
    """该项目的成片「硬字幕」开关（config.json 的 subtitle_enabled），默认 False。

    2026-09-24（用户明确要求「不要生成字幕」）：
    成片阶段有两处会往视频里烧硬字幕（pipeline.step_final / video_postprocess.finalize_episode），
    此前无条件执行。现在统一从这里取值：读不到 / 非 true → 视为关闭，直接不烧字幕。
    这样「H3 提示词不诱导字幕」+「成片不烧字幕」两层都封死，
    确需硬字幕的老项目可在其 config.json 里显式写 "subtitle_enabled": true 单独放开。
    """
    proj = _safe_project(project_name or "")
    default = bool(PROJECT_DEFAULT_CONFIG.get("subtitle_enabled", False))
    try:
        rec = project_store.get_project(proj)
        if rec:
            cfg = project_store.read_config(rec["dir_key"])
            if "subtitle_enabled" not in cfg:
                return default
            val = cfg.get("subtitle_enabled")
            # 宽容解析：字符串 "false"/"0"/"no"/"off" 不能被 bool() 误判为「开」
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "1", "yes", "on"):
                    return True
                if s in ("false", "0", "no", "off", "none", "null", ""):
                    return False
                return default
            return bool(val)
    except Exception as e:  # noqa: BLE001  开关读取失败按「关闭」处理（安全侧）
        app.logger.warning(f"读取项目 config.subtitle_enabled 失败（按关闭处理）：{e}")
    return default


def _style_aspect_confirmed(project_name: str) -> dict:
    """生成前置确认门判据：用户是否已与总控 AI 确认「风格」与「视频比例」。

    单一事实源 = 已应用的总控设定（ai_chat/project_settings.json，按项目）。
    - 风格确认：settings 的风格基调(style) 或 画风(art_style) 任一非空；
    - 比例确认：settings 的画面比例(aspect_ratio，即视频画幅，如 9:16) 非空。
    资产图的画幅已按类型内置写死（见 style_kit.asset_aspect_ratio），不依赖此比例；
    这里确认比例只为「视频 / 分镜」画幅服务。
    """
    try:
        view = ai_chat.settings_view(AI_SETTINGS_PATH, project_name or "") or {}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"确认门读取总控设定失败（按未确认处理）：{e}")
        view = {}
    s = view.get("settings") or {}
    style_confirmed = bool((s.get("style") or s.get("art_style") or "").strip())
    # 2026-09-23（建项目选风格）：用户「新建项目」时下拉/自定义的风格写进 config.json 的
    # style，也算「风格已确认」——否则用户明明选了风格，生成仍被 409 拦在「尚未确认风格」，
    # 与「建项目时就能选风格」的体验自相矛盾。总控 AI 敲定（project_settings）仍是第一优先级。
    if not style_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_style = str(project_store.read_config(rec["dir_key"]).get("style") or "").strip()
                style_confirmed = bool(cfg_style)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"确认门读取 config.style 失败（忽略）：{e}")
    aspect_confirmed = bool((s.get("aspect_ratio") or "").strip())
    # 2026-09-23（建项目选比例）：用户「新建项目」时选的画面比例写进 config.json 的
    # aspect_ratio，也算「比例已确认」，与 style 的同源兜底保持一致。总控 AI 敲定仍是第一优先级。
    if not aspect_confirmed:
        try:
            rec = project_store.get_project(_safe_project(project_name or ""))
            if rec:
                cfg_ar = str(project_store.read_config(rec["dir_key"]).get("aspect_ratio") or "").strip()
                aspect_confirmed = bool(cfg_ar)
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"确认门读取 config.aspect_ratio 失败（忽略）：{e}")
    missing = []
    if not style_confirmed:
        missing.append("风格")
    if not aspect_confirmed:
        missing.append("视频比例")
    return {"confirmed": style_confirmed and aspect_confirmed,
            "style_confirmed": style_confirmed, "aspect_confirmed": aspect_confirmed,
            "missing": missing, "settings": s}


def _style_aspect_guard(project_name: str, override_style: str = ""):
    """生成入口前置校验门（2026-09-22 需求）。

    用户未与总控 AI 确认「风格 / 视频比例」时拦截生成：返回 409 + 可读提醒响应；
    已确认则返回 None（放行）。各生成端点在解析出 project_name 后调用它。
    前端 client.ts readError 会自动弹出 error + guide 文案，提示去总控确认。

    ``override_style``：个别入口（如托管 /api/autonomous/start）允许调用方**显式传风格**
    （plan_overrides.style）——此时视「风格」为已确认，但「视频比例」仍须总控确认。
    """
    chk = _style_aspect_confirmed(project_name)
    if override_style and not chk["style_confirmed"]:
        chk["style_confirmed"] = True
        chk["missing"] = [m for m in chk["missing"] if m != "风格"]
        chk["confirmed"] = chk["style_confirmed"] and chk["aspect_confirmed"]
    if chk["confirmed"]:
        return None
    _names = "、".join(chk["missing"])
    return jsonify({
        "success": False,
        "requires_confirm": True,
        "error": f"尚未与总控 AI 确认{_names}，暂不开展生成。",
        "guide": ("请先在「AI 对话 · 创作总控」里与 AI 敲定" + _names
                  + "（风格：画风/基调；视频比例：画面画幅，如 9:16 / 16:9 / 1:1），"
                    "点击「应用设定」落盘后再开始生成。"),
        "missing": chk["missing"],
        "style_confirmed": chk["style_confirmed"],
        "aspect_confirmed": chk["aspect_confirmed"],
    }), 409


@app.route('/api/storyboards/generate', methods=['POST'])
def api_generate_storyboards():
    """为剧本的每个 shot 生成一张分镜图（参考角色/物品/场景资产图）"""
    # ⚠️ 故意不设 AI 门禁：分镜图 = 消费剧本里已产出的 shot.prompt + ComfyUI 出图 + 质检，
    # 不读任何 AI 凭证。挂在 LLM 门禁上会把「没配 key 但有存量剧本」的用户一起拦死。
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    shots = data.get('shots', [])
    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400

    limit = data.get('limit')
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g
    if isinstance(limit, int) and limit > 0:
        shots = shots[:limit]

    # ⑥ 自动引用剧本中已判定的「镜头数 / 每集时长」字段（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    char_idx = _build_asset_index(data.get('characters', []), project_name, "character")
    item_idx = _build_asset_index(data.get('items', []), project_name, "item")
    scene_idx = _build_asset_index(data.get('scenes', []), project_name, "scene")

    # G5：任务 ID 用 uuid（秒级时间戳同秒双 POST 会覆盖 generation_state 且双线程并发抢同一目标路径）；
    # 入口幂等：同项目+同集已有 running 的分镜任务 → 复用其 task_id（reused=True），不重复开线程。
    # 匹配用稳定的 step 字段（worker 运行中 phase 会变化，不能用 phase 判）。
    # B-11 P1-8：守卫键加 episode_no —— 第 2 集请求不再被第 1 集运行中任务吞掉。
    _ep_no = data.get('episode_no')
    with lock:
        _existing_sb = next((tid for tid, st in generation_state.items()
                             if st.get("status") == "running"
                             and st.get("project_name") == project_name
                             and st.get("step") == "storyboard"
                             and st.get("episode_no") == _ep_no), None)
        if _existing_sb:
            return jsonify({"task_id": _existing_sb, "status": "started", "reused": True,
                            "total": len(shots), "overwrite": bool(data.get('overwrite')),
                            "episode_stats": episode_stats})
        task_id = f"storyboard_{project_name}_{uuid.uuid4().hex[:12]}"
        generation_state[task_id] = {
            "status": "running", "progress": 0, "total": len(shots),
            "current": 0, "phase": "分镜图生成", "results": [],
            "qc": _qc_brief("image"),
            "project_name": project_name, "step": "storyboard",
            "episode_no": _ep_no,
            "refs_available": {
                "characters": {k: bool(v["image"]) for k, v in char_idx.items()},
                "items": {k: bool(v["image"]) for k, v in item_idx.items()},
                "scenes": {k: bool(v["image"]) for k, v in scene_idx.items()},
            },
        }

    # B-01 P1-12：GPU 并发闸门
    def _storyboard_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, "分镜图生成"):
            _storyboard_worker(task_id, project_name, shots, char_idx, item_idx,
                               scene_idx, data.get('episode_no'),
                               _project_style(project_name), bool(data.get('overwrite')))
    thread = threading.Thread(target=_storyboard_worker_gated, daemon=True)
    thread.daemon = True
    thread.start()
    return jsonify({"task_id": task_id, "status": "started", "total": len(shots),
                    "overwrite": bool(data.get('overwrite')),
                    "episode_stats": episode_stats})


@app.route('/api/storyboards/manifest/<path:project_name>')
def api_storyboard_manifest(project_name):
    """读取已生成的分镜图清单（用于页面回看）"""
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    out_dir = _ep_read_dir(STORYBOARDS_DIR, project, request.args.get('episode_no'))
    manifest_path = os.path.join(out_dir, "storyboard_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        return jsonify({"success": True, "exists": True, "project_name": project,
                        "manifest": manifest})

    # 无清单时按磁盘文件兜底（项目可能由其他会话生成）
    shots = []
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            if fn.lower().endswith(".png"):
                sid = fn.replace("shot_", "").replace(".png", "")
                shots.append({
                    "shot_id": int(sid) if sid.isdigit() else sid,
                    "success": True,
                    "file": os.path.join(out_dir, fn),
                    "url": f"/api/storyboards/file/{project}/{fn}",
                })
    return jsonify({"success": True, "exists": bool(shots), "project_name": project,
                    "manifest": {"project_name": project, "shots": shots,
                                 "total": len(shots),
                                 "success_count": sum(1 for s in shots if s.get("success"))}})


@app.route('/api/storyboards/file/<path:filename>')
def api_storyboard_file(filename):
    """提供分镜图文件访问"""
    return _serve_safe(STORYBOARDS_DIR, filename)


# ===== 步骤6：视频生成 =====

def _collect_reference_images(character_refs: list, scene_refs: list) -> list:
    """把前端传来的参考图（可能是 /api/assets/... 的 HTTP 资源路径）解析为本地绝对路径

    修复 P0-4：原实现直接 os.path.exists(HTTP 路径) 恒为 False，参考图永远传不到 H3。
    """
    ref_imgs = []
    for ref in list(character_refs)[:2]:
        for key in ("front", "base"):
            p = ref.get(key) if isinstance(ref, dict) else None
            local = comfyui_client.resolve_local_path(p) if p else None
            if local and os.path.exists(local):
                ref_imgs.append(local)
                break
        else:
            app.logger.warning(f"角色参考图不可用: {ref.get('name') if isinstance(ref, dict) else ref}")
    for ref in list(scene_refs)[:1]:
        for key in ("front", "base"):
            p = ref.get(key) if isinstance(ref, dict) else None
            local = comfyui_client.resolve_local_path(p) if p else None
            if local and os.path.exists(local):
                ref_imgs.append(local)
                break
    return ref_imgs



# 注：本处原为 `_norm_shot_key(key)`。已收敛为 app/shot_key.norm_shot_key 的一行代理
# （见文件顶部），全项目唯一的镜号归一化实现见 app/shot_key.py。


@app.route('/api/videos/generate', methods=['POST'])
def api_generate_videos():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（同 api_generate_storyboards 口径）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    shots = data.get('shots', [])
    character_refs = data.get('character_refs', [])
    scene_refs = data.get('scene_refs', [])
    storyboards = data.get('storyboards', {}) or {}   # {shot_id: /api/storyboards/file/... 或本地路径}
    use_storyboard = data.get('use_storyboard', True)
    # 视频生成模式：
    #   per_shot（默认）= 逐镜头提交，工作流段数=1（一个分镜一段）
    #   episode        = 整集一次提交，工作流段数=该集分镜数（如第 4 集 22 段）
    #   keyframe       = 逐镜头提交，参考图槽位改为 [首帧=分镜图, 尾帧]（P1-2 关键帧驱动）
    mode = str(data.get('mode') or 'per_shot').strip().lower()
    if mode not in ('per_shot', 'episode', 'keyframe'):
        return jsonify({"error": f"mode 参数非法: {mode}"
                                 f"（仅支持 per_shot / episode / keyframe）"}), 400
    timeout_per_segment = int(data.get('timeout_per_segment') or 900)
    episode_tag = str(data.get('episode_tag') or '').strip()
    # 跨镜链式：上一镜尾帧 = 下一镜首帧（auto / always / off，默认取 KEYFRAME_CHAIN_MODE）
    chain_mode = keyframe.norm_chain_mode(
        data.get('chain_mode') or KEYFRAME_CHAIN_MODE)

    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400
    _g = _style_aspect_guard(project_name)
    if _g is not None:
        return _g

    # ⑥ 视频链路自动引用剧本自动判定的镜头时长（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    task_id = f"video_{project_name}_{uuid.uuid4().hex[:12]}"
    with lock:
        # G5：同项目已有 running 的视频任务 → 复用（匹配 step="video"，与分镜任务互不误伤）
        _existing_vid = next((tid for tid, st in generation_state.items()
                              if st.get("status") == "running"
                              and st.get("project_name") == project_name
                              and st.get("step") == "video"), None)
        if _existing_vid:
            return jsonify({"success": True, "task_id": _existing_vid, "status": "started",
                            "reused": True, "total": len(shots),
                            "mode": mode, "project_name": project_name})
        generation_state[task_id] = {
            "status": "running", "progress": 0,
            "total": len(shots), "current": 0, "results": [],
            "phase": "视频生成", "qc": _qc_brief("video"),
            "project_name": project_name, "step": "video",
            "episode_stats": episode_stats,
        }

    # 抽取 worker 时这里被截断了：既没启动线程也没有 return，
    # 导致 POST /api/videos/generate 抛 "did not return a valid response" (500)。
    # 现在把「启动后台线程 + 返回 task_id」补回路由本身（worker 只负责干活）。
    # B-01 P1-12：GPU 并发闸门
    def _video_generate_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, f"视频生成({mode})"):
            _video_generate_worker(
                task_id, project_name, shots, character_refs, scene_refs,
                storyboards, use_storyboard, mode, timeout_per_segment,
                episode_tag, data.get('episode_no'),
                chain_mode=chain_mode,
                style=(data.get('style') or _project_style(project_name)),
                overwrite=bool(data.get('overwrite')))
    thread = threading.Thread(target=_video_generate_worker_gated, daemon=True)
    thread.daemon = True
    thread.start()

    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len(shots), "mode": mode})


def _video_generate_worker(task_id, project_name, shots, character_refs,
                          scene_refs, storyboards, use_storyboard, mode,
                          timeout_per_segment, episode_tag, episode_no=None,
                          chain_mode="auto", style="", overwrite=False):
    """逐镜/整集/关键帧三种模式的视频生成（后台任务体，可被路由与流水线复用）

    overwrite：是否全量重做。默认 False —— per_shot 模式下已达标入库的
    shot_XX.mp4 直接复用、只重跑缺失/被质检阻断的镜头（断点续跑）。
    episode 模式为「整集一次生成」，无逐镜跳过语义，本参数不影响该分支。

    从 /api/videos/generate 抽出的模块级实现：原闭包变量（项目名、镜头、参考图、
    模式等）改为显式参数，业务逻辑不变。抽出的目的是让自动生产流水线
    （pipeline.py / autopilot.py）能直接复用同一条视频生成链路，避免两套实现漂移。

    episode_no：集级目录隔离（第 1 集沿用平铺，第 2 集起写 epNN/），
    避免多集自动生产时 shot_NN.mp4 互相覆盖。
    style：用户与总控敲定的风格描述。用途有二 ——
      ① 补齐镜头 style 字段（剧本未注入时的兜底），使提示词带上风格；
      ② 解析画幅并覆写 H3 分辨率（竖屏 9:16 真正落地，而非模板死板的 16:9）。
    """
    # ⚠️ 关键：本 worker 是**裸线程**（见 /api/videos/generate 的 threading.Thread），
    # 运行在 Flask 请求线程之外 —— contextvars 不会从请求线程继承过来，因此
    # `comfyui_client.wait_for_completion` 轮询里的 `cancellation.should_stop()`
    # 在过去**恒为 False**：前端点「暂停」只停了托管的下一步，**正在跑的 ComfyUI
    # 渲染任务不会被打断**（用户反馈「暂停要同步停止 comfyui 的任务」的根因）。
    # 这里显式把中止判定器注册进本线程的执行上下文：
    #   - 托管暂停（autopilot.is_paused）→ 立即中断远端
    #   - 进程退出（autopilot._STOP / 解释器与用户交互无关的关停）→ 一并中断
    # 判定器自身异常在 cancellation.should_stop 里 fail-open 处理，不影响生产。
    _cancel_token = cancellation.push(_video_should_stop)
    # 该任务是否由托管流水线发起（pipeline._run_task_worker 会置 state["pipeline"]=True）。
    # 决定「托管暂停」是否应掐断本任务：手动生成不受托管开关影响。
    _is_pipeline = False
    with lock:
        _st0 = generation_state.get(task_id)
        if isinstance(_st0, dict):
            _is_pipeline = bool(_st0.get("pipeline"))
    _pipe_token = _VIDEO_TASK_IS_PIPELINE.set(_is_pipeline)
    try:
        _video_generate_worker_body(
            task_id, project_name, shots, character_refs, scene_refs, storyboards,
            use_storyboard, mode, timeout_per_segment, episode_tag, episode_no,
            chain_mode, style, overwrite)
    except cancellation.Cancelled as e:
        # 协作式中止：不是失败，落到「已取消」态，前端展示为已停止而非报错
        app.logger.info("[视频] 任务因中止信号停止（task=%s）：%s", task_id, e)
        with lock:
            _st = generation_state.get(task_id)
            if isinstance(_st, dict):
                _st.update({"status": "cancelled", "phase": "已停止",
                            "error": "已收到中止信号，ComfyUI 远端任务已中断（已完成镜头保留，可续跑）"})
    finally:
        _VIDEO_TASK_IS_PIPELINE.reset(_pipe_token)
        cancellation.reset(_cancel_token)


def _video_should_stop() -> bool:
    """视频 worker 的中止判定器：**仅对托管（pipeline）任务**生效。

    刻意与 `autopilot._halt_requested` 同口径，但**不 import autopilot**（app.py 与
    autopilot 相互 import 会成环）。用惰性 import 规避循环依赖，失败时 fail-open。

    ⚠️ 2026-09-24 修正：早期实现「只要 autopilot 处于 paused 就停」，会把**用户手工
    触发**的生成一起掐掉 —— 实测：托管暂停期间点「生成视频（手动）」，第一次轮询就
    命中中止信号，报「ComfyUI 远端等待期间收到中止信号」（manual 任务被 pause 误杀）。
    暂停的语义应只覆盖「托管自动生产」，不该阻断用户当前手动操作。
    因此这里判定的前提是 `_VIDEO_TASK_IS_PIPELINE`（由 worker 外壳按任务态设置）：
      - 托管任务（generation_state[task]["pipeline"] is True）：托管暂停 → 停；
      - 进程退出（autopilot._STOP）：任何任务都停（关服就该全停）。
    """
    try:
        import autopilot
        # 进程退出：无论手动还是托管，都应立刻停
        stop_ev = getattr(autopilot, "_STOP", None)
        if stop_ev is not None and stop_ev.is_set():
            return True
        # 托管暂停：只对 pipeline 任务生效（手动任务不受托管开关影响）
        if _VIDEO_TASK_IS_PIPELINE.get() and autopilot.is_paused():
            return True
    except Exception as e:  # noqa: BLE001  判定器故障不得影响生产
        app.logger.debug("视频中止判定器读取失败（按不中止处理）：%s", e)
    return False


def _video_generate_worker_body(task_id, project_name, shots, character_refs,
                                scene_refs, storyboards, use_storyboard, mode,
                                timeout_per_segment, episode_tag, episode_no=None,
                                chain_mode="auto", style="", overwrite=False):
    """视频生成的实际业务体（外壳见 :func:`_video_generate_worker`，负责注册中止信号）"""
    try:
        # 风格/画幅：整集共用一次解析
        # G19：风格串未含画幅关键词时以默认 9:16 为底，不再静默回落模板 16:9
        _style_res = style_kit.resolve(style, default_ratio=style_kit.DEFAULT_RATIO)
        _size = _style_res.get("size")
        if style:
            app.logger.info("[视频] 风格=%s；画幅=%s", _style_res.get("style") or style,
                            _style_res.get("label") or "未指定（沿用模板）")
        # 镜头 style 兜底：老剧本没有该字段时（历史产物），用本次传入的风格补齐
        if _style_res.get("style"):
            shots = [dict(s, style=(s.get("style") or _style_res["style"]))
                     for s in (shots or []) if isinstance(s, dict)]
        videos_dir = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
        os.makedirs(videos_dir, exist_ok=True)
        try:
            _epn = int(episode_no) if str(episode_no or "").strip() else 1
        except (TypeError, ValueError):
            _epn = 1
        # 视频 URL 前缀：第 2 集起带 epNN 段（与 _ep_dir 的落盘位置一致）
        _vurl = (f"/api/videos/{project_name}/ep{_epn:02d}" if _epn > 1
                 else f"/api/videos/{project_name}")

        ref_imgs = _collect_reference_images(character_refs, scene_refs)
        main_char_img = _collect_reference_images(character_refs[:1], [])
        app.logger.info(f"视频参考图解析结果: {ref_imgs}；主角锚点: {main_char_img}")

        # B-18 P1-7：构建角色索引，供 _shot_segment 逐镜匹配参考图（与分镜链路口径对齐）
        char_idx = _build_asset_index(character_refs, project_name, "character")

        # 分镜图映射（步骤5产物）→ 作为 H3 的 <Picture 1> 构图基准
        # 修复：改用合并式映射（目录扫描 + manifest + 前端传入）。
        # 原实现只认前端传入的 storyboards，前端漏传某镜时该镜会静默退化为
        # 「无分镜图参考」，与用户所见不符。
        sb_map = _keyframe_sb_map(project_name, None, storyboards, episode_no=episode_no)
        app.logger.info(f"分镜图参考映射: {sorted(sb_map.keys())}")

        # 关键帧驱动模式：取该项目尾帧目录，并按镜登记 [首帧, 尾帧]
        # 首帧支持跨镜链式（上一镜尾帧 = 下一镜首帧），见 keyframe.plan_keyframes
        kf_end_map = {}
        kf_start_map = {}
        if mode == 'keyframe':
            kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project_name), episode_no)
            for i, _s in enumerate(shots):
                _sid = _s.get('shot_id', i + 1)
                _seq = _shot_seq(_sid, i + 1)
                _end = os.path.join(kf_dir, f"shot_{_seq:02d}_end.png")
                # P1-19 一次性兼容：旧 keyframe 曾用「拼接全部数字」命名（"S01-C02"→102），
                # 新语义为「首段数字」（→1）。旧文件仍在时按旧名回退命中并告警；整数镜号下
                # 新旧同名，`_legacy != _seq` 恒为假，此分支不触发（零行为变更）。
                if not os.path.isfile(_end):
                    _legacy = shot_key.legacy_shot_seq(_sid)
                    if _legacy is not None and _legacy != _seq:
                        _legacy_path = os.path.join(kf_dir, f"shot_{_legacy:02d}_end.png")
                        if os.path.isfile(_legacy_path):
                            app.logger.warning(
                                "[P1-19 兼容] 命中旧镜号命名尾帧 %s（镜号 %r 现按首段数字"
                                "归一为 %d）；建议重跑关键帧以迁移到 shot_%02d_end.png",
                                os.path.basename(_legacy_path), _sid, _seq, _seq)
                            _end = _legacy_path
                if os.path.isfile(_end):
                    kf_end_map[str(_sid)] = _end
                    kf_end_map[f"shot_{_seq:02d}"] = _end
            _cm = keyframe.norm_chain_mode(chain_mode if chain_mode is not None
                                           else KEYFRAME_CHAIN_MODE)
            kf_start_map = keyframe.resolve_start_map(
                shots, sb_map, kf_dir, chain_mode=_cm)
            app.logger.info(f"[keyframe] 尾帧就绪 {len(set(kf_end_map.values()))}/{len(shots)} 镜"
                            f"；链式模式 {_cm}，串帧 {sum(1 for v in kf_start_map.values() if v) // 2} 镜")

        def _shot_segment(shot, seq, qc_cfg=None):
            """把一个分镜转成 H3 工作流的一个「段」（提示词 + 时长 + 参考图）

            keyframe 模式：参考图 = [首帧(分镜图), 尾帧]，让 H3 在两端之间插值运动；
            缺尾帧时自动退化为首帧单锚（并在返回值中标记，便于前端提示）。
            """
            sid = shot.get('shot_id')
            sb_local = sb_map.get(_norm_shot_key(sid)) if use_storyboard else None
            if mode == 'keyframe' and sb_local:
                sb_local = sb_map.get(_norm_shot_key(seq)) or sb_map.get(
                    f"shot_{seq:02d}") or sb_local
                # 链式首帧优先：上一镜尾帧（resolve_start_map 已按同场景判定回退）
                start_p = (kf_start_map.get(str(sid))
                           or kf_start_map.get(f"shot_{seq:02d}") or sb_local)
                end_p = kf_end_map.get(str(sid)) or kf_end_map.get(f"shot_{seq:02d}")
                refs = [start_p] + ([end_p] if end_p else [])
                prompt = comfyui_client._build_h3_prompt(
                    shot, character_refs, scene_refs,
                    storyboard_ref={"name": f"shot_{sid}"})
            elif sb_local:
                # B-18 P1-7：参考图按 characters_in_shot 逐镜匹配，不再全段共用 main_char_img
                # H3 参考图上限 2 张：[分镜图, 本镜主角锚点]
                _matched_chars = _match_shot_chars(shot, char_idx)
                _shot_char_img = None
                for _mc in (_matched_chars or []):
                    _p = (char_idx.get(_mc) or {}).get("image")
                    if _p:
                        _shot_char_img = _p
                        break
                if _shot_char_img:
                    refs = [sb_local, _shot_char_img]
                else:
                    # 兜底：逐镜匹配失败时回退到全局主角锚点（保持原行为）
                    refs = [sb_local] + main_char_img
                prompt = comfyui_client._build_h3_prompt(
                    shot, character_refs, scene_refs,
                    storyboard_ref={"name": f"shot_{sid}"})
            else:
                refs = ref_imgs
                # 择优：合规的既有 prompt_h3 直接用，否则规范重建（见 resolve_h3_prompt 说明）
                prompt = comfyui_client.resolve_h3_prompt(
                    shot, character_refs, scene_refs)
            try:
                dur = float(shot.get('duration') or 5)
            except (TypeError, ValueError):
                dur = 5.0
            # ---- 提示词预检（生成前质检）----
            # ⚠️ 这里**只自愈 + 记录，不阻断**：整集模式一次提交 N 段，为一条提示词的问题把
            #    整集生成打断，代价远大于收益；且 H3 提示词由构建器产出、结构必然齐全，
            #    出现 fatal 只可能是构建器自身有 bug —— 那更该留下证据继续跑，
            #    而不是让整集静默失败。单镜重跑接口（用户显式只跑一镜）才做硬阻断。
            prompt, _pf_seg, _pgate_seg = _prompt_preflight(
                "h3", prompt, ctx=shot,
                style=(shot.get("style") or _style_res.get("style") or ""),
                expect_refs=bool(refs), project_name=project_name,
                cfg=qc_cfg)   # G13：复用 worker 级质检配置，避免逐镜再读盘+解密
            seg = {"prompt": prompt, "duration": dur, "reference_images": refs,
                   "name": f"shot_{seq:02d}"}
            if _pf_seg.get("repairs") or (_pf_seg.get("verdict") or {}).get("issues"):
                seg["prompt_qc"] = _pf_seg.get("verdict")
                seg["prompt_qc_repairs"] = _pf_seg.get("repairs") or []
            if not _pgate_seg.get("accept"):
                seg["prompt_qc_blocked"] = True
                app.logger.warning("镜头 %s 视频提示词预检未通过（%s）：%s",
                                   shot.get("shot_id"), _pgate_seg.get("label"),
                                   _pgate_seg.get("reason"))
                # ★ 用户需求：不合格提示词不留本地（P12）。整集模式该段不生成，
                # 把可能存在的上一轮 `prompt_<shot>.json` 移回收站。
                try:
                    _purge_prompt_records(project_name, shot.get("shot_id") or seq,
                                          reason=f"视频提示词预检未通过（{_pgate_seg.get('label')}）")
                except Exception as _pe:  # noqa: BLE001
                    app.logger.warning(f"不合格提示词清理失败（忽略）：{_pe}")
            return seg, sb_local

        # ---------- 模式 episode：整集 N 段一次生成（H3 原生衔接）+ 整片 QC 门控 ----------
        if mode == 'episode':
            # G13：质检配置 worker 级读一次，本集所有段共用（上提到循环前，供 _shot_segment
            # 内的提示词预检复用，避免逐段再 _qc_load_cfg() 读盘+解密）。
            qc_cfg = _qc_load_cfg()
            qc_on = qc_client.video_qc_ready(qc_cfg)
            qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("video_enabled"))
            max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
            segs, shot_meta_map = [], []
            for i, shot in enumerate(shots):
                shot_id = shot.get('shot_id', i + 1)
                seq = _shot_seq(shot_id, i + 1)
                seg, sb_local = _shot_segment(shot, seq, qc_cfg)
                segs.append(seg)
                shot_meta_map.append({"shot_id": shot_id, "seq": seq,
                                      "duration": seg["duration"],
                                      "used_storyboard": bool(sb_local)})
            # 质检开关结论已在上方 worker 级算好（与 per_shot 保持一致）
            eff_style = (shot.get("style") if shot else None) or _style_res.get("style") or ""
            # [教训][video] 诊断（§2.3.5）：qc_off = 质检总开关/类型开关/接口任一未就绪
            # → 整片 QC 门控不会注入（qc_fn=None），本模式**根本不写教训库**，如实打点。
            if not qc_on:
                app.logger.info("[教训][video] project=%s mode=episode qc_off=true "
                                "enabled=%s video_enabled=%s → 无质检门控，本模式不沉淀教训",
                                project_name, qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))

            # 整片 QC 门控回调：对 N 段一次生成出的单个连续整集视频抽帧质检
            _ep_qc_attempt = {"n": 0}

            def _seg_qc_fn(video_path, shot_desc, cfg, style):
                """QC 门控：对整片抽帧 → 多模态判定 → 返回 {"passed": bool, "verdict": {...}, "gate": {...}}

                ⚠️ 两点与「单镜模式」必须对齐，否则整集模式（默认）会缺半边能力：
                1) **镜头信息**：comfyui_client 传进来的是所有段 H3 提示词全文拼接
                   （每段六段式，几十段叠一起）——又长又难判读。改用本集镜头摘要。
                2) **教训沉淀**：此前整集模式只做 QC+门控、**从不写教训库**，
                   于是「质检不达标 → 改提示词重生成」的闭环在最常用模式下完全断裂，
                   重试只会换随机种子瞎撞。这里补齐 `_record_qc_lesson`。
                """
                _ep_qc_attempt["n"] += 1
                fr_dir = os.path.join(QC_DIR, project_name, "frames",
                                      f"episode_full_{os.path.basename(video_path)}")
                # D-05（P1）：按「每段中点」抽帧，取代原先「全片均布 3 帧（配置上限 6）」。
                # 原实现下一次产出 20~44 段的整集只抽 3~6 帧，中段几十段零采样 →
                # 崩坏镜必然漏检、整集质检形同虚设。这里把各段时长折算成全片占比传下去，
                # 抽帧数随段数增长（>6），使每一段至少被采到一次。
                _qc_ratios = _episode_frame_ratios(segs)
                if _qc_ratios:
                    app.logger.info("[整集质检] 按段抽帧：%d 段 → 请求 %d 帧（每段中点占比）",
                                    len(segs), len(_qc_ratios))
                else:
                    app.logger.warning(
                        "[整集质检] 段时长不可用（segs=%d）→ 退回配置抽帧数（可能漏检中段）",
                        len(segs))
                verdict = qc_client.check_video(video_path, _episode_qc_desc(shots), cfg,
                                               frames_dir=fr_dir,
                                               style=style,
                                               frame_ratio=_qc_ratios or None,
                                               expected_duration=sum(
                                                   float(s.get("duration") or 0.0) for s in shots))
                gate = _qc_gate(verdict)
                passed = bool(gate.get("accept", False))
                if not verdict.get("ok"):
                    # P1-18：质检「不可判定」（ffmpeg 缺失 / 接口 5xx 等，与内容无关）——
                    # 不得当作「不达标」触发换种子整片重跑（会白烧 20~44 段 H3；见报告 P1-18）。
                    # 标记 qc_unavailable，交由 _ep_qc_stop_cb 停止重试；口径与「不达标」分开。
                    _ep_qc_attempt["unavailable"] = True
                    app.logger.warning(
                        "[整集质检] 质检不可判定 → qc_unavailable（不重试）："
                        "project=%s attempt=%d error=%s",
                        project_name, _ep_qc_attempt["n"],
                        (verdict.get("error") or gate.get("reason") or ""))
                if not passed and verdict.get("ok"):
                    try:
                        # 抽帧图路径写进历史 extra.frames_f，供产物被移走后做「断链修正」
                        # （见下方 _mark_history_file_purged）。
                        rec = _qc_record_verdict(
                            project_name, "video", episode_tag or "episode", "整片质检",
                            _ep_qc_attempt["n"], None, video_path, verdict, style=style,
                            extra={"frames_f": list(verdict.get("frames") or []),
                                   "frames_dir": fr_dir})
                        # 挂上本集的段提示词集合，供下次重试时按相似度召回
                        # A-5 P1：整片模式段数可达 20~44 段，全量拼接可达数十 KB —— 全量入
                        # 教训库会撑爆/稀释检索。截断到 2000 字符（保留段边界换行，人可读）。
                        _seg_blob = "\n".join((sg.get("prompt") or "") for sg in segs)
                        _record_qc_lesson(project_name, "video", _seg_blob[:2000], rec)
                        app.logger.info(
                            "[教训][video] project=%s mode=episode attempt=%d ok=True "
                            "passed=False → 已沉淀",
                            project_name, _ep_qc_attempt["n"])
                    except Exception as le:  # noqa: BLE001
                        app.logger.warning("整片质检教训沉淀失败：%s", le)
                elif not passed and not verdict.get("ok"):
                    # [教训][video] 诊断（§2.3.5）：质检调用异常/超时（ok=false）时静默跳过、
                    # 不记教训——视频质检需 ffmpeg 抽帧 + 多模态，失败率高，如实打点便于排障。
                    app.logger.info(
                        "[教训][video] project=%s mode=episode attempt=%d ok=False "
                        "passed=False verdict.ok=false → 质检异常/超时，不沉淀教训",
                        project_name, _ep_qc_attempt["n"])
                # ★ 用户需求：整片质检「判定不通过」的抽帧图不留本地。⚠️ 仅当质检成功返回
                # 且不合格（ok=True、passed=False）时删；ok=False（接口故障/ffmpeg 缺失）时
                # 抽帧图保留供排障。整片成片本身按用户决策 2 保留（在调用方处理）。
                if not passed and verdict.get("ok") and not verdict.get("unavailable"):
                    # 抽帧图整目录移入回收站，并把质检历史里指向它的帧路径一并标记为断链
                    _hist_f = _qc_history_file_for(project_name, "video", episode_tag or "episode")
                    try:
                        _purge_rejected_artifacts(
                            [fr_dir], project=project_name,
                            reason=f"整片质检不合格（{gate.get('label')}）抽帧图",
                            kind="episode_frames", history_file=_hist_f)
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"整片抽帧图清理失败（忽略）：{_pe}")
                return {"passed": passed, "verdict": verdict, "gate": gate}

            def _ep_qc_stop_cb(qc_results):
                """G1 止损 + P1-18：质检「不可判定」优先于缺陷重复判定 —— 不可判定不重试。

                与分镜/逐镜/资产的既有口径一致：`not verdict.get("ok")` 时 break（不重画）。
                这里通过止损回调把该语义传达给 comfyui_client 的整片重试循环，避免在质检
                接口/ffmpeg 不可用时换种子白烧整集。
                """
                if _ep_qc_attempt.get("unavailable"):
                    return True, "质检不可判定（接口 / ffmpeg 不可用，与内容无关），不重试"
                return _qc_retry_hopeless(qc_results)

            # P0-1 fail-closed：qc_declared=True 但质检接口未就绪（qc_on=False）→ 整集
            # **不生成、不写正式目录**，直接阻断并如实告警。旧实现在 qc_fn=None 下仍把
            # 未质检成片 move 进正式目录（fail-open），与资产链路口径不一致。
            if qc_declared and not qc_on:
                app.logger.warning(
                    "[整集质检] qc_declared=True 但 qc_on=False → fail-closed 阻断："
                    "整集视频不生成、不写正式目录（project=%s，enabled=%s video_enabled=%s）",
                    project_name, qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": 0,
                        "qc_blocked": True,
                        "qc_unavailable": True,
                        "error": ("整集视频质检阻断（质检接口未就绪）：已开启视频质检，"
                                  "但 base_url / api_key / model 不可用；"
                                  "未质检产物不写入正式目录"),
                    })
                    generation_state[task_id].update({
                        "status": "failed",
                        "success_count": 0,
                        "error": "整集视频质检阻断（质检接口未就绪）",
                    })
                return

            with lock:
                generation_state[task_id].update({
                    "current": 0, "progress": 5,
                    "phase": f"整集 {len(segs)} 段一次生成（H3 原生衔接，整片 QC 门控）",
                    "segment_count": len(segs),
                })
            app.logger.info(f"[episode] 整集生成开始：{len(segs)} 段，qc_on={qc_on}，max_retries={max_retries}")

            result = comfyui_client.generate_h3_sequence_sequential(
                segments=segs,
                filename_prefix=f"comic_drama/{project_name}_{episode_tag or 'episode'}",
                seed=None,
                timeout_per_segment=timeout_per_segment,
                size=_size,
                qc_fn=_seg_qc_fn if qc_on else None,
                qc_cfg=qc_cfg,
                qc_style=eff_style,
                max_retries=max_retries,
                qc_stop_cb=(_ep_qc_stop_cb if qc_on else None),
            )
            files = result.get("files") or []
            episode_failed = bool(result.get("failed"))
            attempts_used = result.get("attempts_used", 1)
            qc_results = result.get("qc_results") or []
            ep_name = f"{episode_tag or 'episode'}_full.mp4"

            if not files:
                _ep_err = result.get("error") or "整片生成失败（ComfyUI 未返回视频文件或整片 QC 全部不通过）"
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": len(segs),
                        "qc_results": qc_results,
                        "error": _ep_err,
                    })
                with lock:
                    generation_state[task_id].update({
                        "status": "failed",
                        "success_count": 0,
                        "error": "整片生成失败或整片 QC 未通过",
                    })
                return

            src = files[0]
            dst = os.path.join(videos_dir, ep_name)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.move(src, dst)
            qc_passed = not episode_failed
            # ★ 用户决策 2：整集成片**保留现行为** —— 不通过仍写入正式目录（dst）供人工复核，
            # 故 app.py 这里的 fail-open **不动**。但**必须清理 ComfyUI 侧历次重试的整集 mp4**
            # （每轮 attempt 都会在 COMFYUI_OUTPUT_DIR/comic_drama/ 生成一个 `<项目>_<集>_0000N_.mp4`，
            # 不清理就是每次重试堆一个几十分钟的成片）。qc_results[].file 是 comfyui_client
            # 回传的**原始**产物路径（dst 已 move 走，不在其中）。
            try:
                if qc_on and qc_results:
                    _comfy_retries = []
                    for _r in qc_results:
                        if not isinstance(_r, dict):
                            continue
                        _f = _r.get("file")
                        # 只清「质检成功返回且判定不合格」的轮次。comfyui_client 写入的
                        # qc_results 条目里：接口故障轮 unavailable=True 且 passed=None；
                        # 正常不合格轮 passed=False（`is False` 严格判等，None 不命中）。
                        _ok_true = _r.get("passed") is False and _r.get("unavailable") is not True
                        if _f and _ok_true:
                            _comfy_retries.append(_f)
                    if _comfy_retries:
                        _purge_rejected_artifacts(
                            _comfy_retries, project=project_name,
                            reason="整集视频历次质检不合格重试残留",
                            kind="episode_video_retry")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"整集重试残留清理失败（忽略）：{_pe}")
            # A-1 P1：整片 QC 调用异常（comfyui_client 已改「break + 追加 unavailable 条目」，
            # 不再触碰 app.py 的 _ep_qc_attempt 闭包）时，仅看闭包会漏判 → 结果/UI 会误报
            # 「QC 不通过」。这里同时看 qc_results 里是否存在 unavailable 条目，口径与闭包对齐。
            qc_unavailable = bool(_ep_qc_attempt.get("unavailable")) or \
                any(isinstance(r, dict) and r.get("unavailable") for r in qc_results)
            item = {"success": True, "mode": "episode",
                    "segment_count": len(segs),
                    "qc_passed": qc_passed,
                    "qc_unavailable": qc_unavailable,
                    "attempts_used": attempts_used,
                    "path": dst, "url": f"{_vurl}/{ep_name}",
                    "shots": shot_meta_map,
                    "qc": _qc_summary([], qc_declared, qc_on, max_retries),
                    "qc_results": qc_results,
                    "prompt_id": result.get("prompt_id")}
            # H3 音轨策略
            item["audio"] = _h3_audio_policy(dst)
            with lock:
                generation_state[task_id]["results"].append(item)
                generation_state[task_id]["progress"] = 100
                generation_state[task_id]["phase"] = (
                    f"整集 {len(segs)} 段视频生成完成（QC 通过）" if qc_passed
                    else (f"整集 {len(segs)} 段视频生成（QC 不可判定，已停止重试，保留成片供人工复核）"
                          if qc_unavailable else
                          f"整集 {len(segs)} 段视频生成（QC 未通过，保留最后生成成片供人工复核）"))
            app.logger.info(f"[episode] 产物落盘: {dst}（qc_passed={qc_passed}，"
                            f"qc_unavailable={qc_unavailable}，attempts={attempts_used}）")
            with lock:
                results = generation_state[task_id]["results"]
                ok = sum(1 for r in results if r.get("success"))
                generation_state[task_id].update({
                    "status": "completed" if ok and qc_passed else "failed",
                    "success_count": ok,
                    "error": ("" if qc_passed else
                              ("整片质检不可判定（qc_unavailable，已停止重试；已保留成片供人工复核）"
                               if qc_unavailable else "整片 QC 未通过（已保留最后生成成片）")),
                })
            return

        # G13（P1）：per_shot 分支的质检配置 worker 级读一次，本批所有镜头共用（对齐资产
        # worker）。旧代码逐镜 _qc_load_cfg() 反复读盘 + Fernet 解密，现上提省开销、避免
        # 同批新旧配置混用。注意 episode 分支在上已单独读一次并 return，二者互不影响。
        qc_cfg = _qc_load_cfg()
        qc_on = qc_client.video_qc_ready(qc_cfg)
        qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("video_enabled"))
        max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
        for i, shot in enumerate(shots):
            shot_id = shot.get('shot_id', i + 1)
            seq = _shot_seq(shot_id, i + 1)
            # ---------- 断点续跑：已达标入库的视频直接复用，只重跑缺失/不达标的 ----------
            # 正式目录里已有非空 shot_XX.mp4 ⟺ 上一轮该镜视频已通过质检（不达标的只落暂存区）。
            if not overwrite:
                _vdst = os.path.join(videos_dir, f"shot_{seq:02d}.mp4")
                if os.path.isfile(_vdst) and os.path.getsize(_vdst) > 0:
                    _vitem = {"shot_id": shot_id, "success": True, "skipped": True,
                              "mode": "per_shot", "path": _vdst,
                              "url": f"{_vurl}/shot_{seq:02d}.mp4",
                              "qc": {"enabled": False, "status": "skipped",
                                     "label": "沿用已达标视频", "attempts": 0, "regenerated": 0}}
                    with lock:
                        generation_state[task_id]["results"].append(_vitem)
                        generation_state[task_id].update({
                            "current": i + 1,
                            "progress": int((i + 1) / len(shots) * 100),
                            "current_shot": shot_id,
                        })
                    # [教训][video] 诊断（§2.3.5）：断点续跑复用已达标视频 → 该镜**根本不重新
                    # 质检**，自然没有新的 video 教训要沉淀（这是 0 落盘的正常原因之一）。
                    app.logger.info("[教训][video] project=%s shot=%s 复用跳过：沿用已达标视频，"
                                    "不重新质检、不沉淀教训", project_name, shot_id)
                    continue
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int((i + 1) / len(shots) * 100),
                    "current_shot": shot_id
                })

            try:
                seg, sb_local = _shot_segment(shot, seq, qc_cfg)   # G13：传 worker 级配置
                prompt = seg["prompt"]

                # ---------- 视频 AI 质检（抽帧送检，不达标自动重生成） ----------
                # G13：qc_cfg/qc_on/qc_declared/max_retries 已在 per_shot worker 级读一次（见上方），
                # 本批所有镜头共用，不再逐镜 _qc_load_cfg()。
                if not qc_on:
                    # [教训][video] 诊断（§2.3.5）：qc_off = 质检总开关/类型开关/接口任一未就绪
                    # → per_shot 模式直接按原行为入库，**根本不质检**，自然无 video 教训可沉淀。
                    app.logger.info("[教训][video] project=%s shot=%s mode=per_shot qc_off=true "
                                    "enabled=%s video_enabled=%s → 未开质检，不沉淀教训",
                                    project_name, shot_id,
                                    qc_cfg.get("enabled"), qc_cfg.get("video_enabled"))
                attempts = []
                # O3：第 1 轮也用真实随机 seed 并始终注入（不再 None），视频 qc.history[0].seed 不再为 null
                seed = random.randint(1, 2 ** 31 - 1)
                dst = os.path.join(videos_dir, f"shot_{seq:02d}.mp4")
                video_item = {"shot_id": shot_id, "success": False,
                              "mode": "per_shot", "segment_count": 1,
                              "duration": seg.get("duration"),
                              "used_storyboard": bool(sb_local)}
                orig_video_prompt = prompt     # 教训库稳定键（改写后的提示词不参与指纹）

                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        seed = random.randint(1, 2 ** 31 - 1)
                        # 从教训库召回「上一轮质检哪里不对」，据此改写视频提示词再生成
                        try:
                            learned = prompt_memory.learned_prompt(
                                kind="video",
                                prompt=orig_video_prompt,
                                project=project_name,
                                root_dir=PROJECT_OUTPUT_DIR,
                                style=_qc_style_of(project_name),
                            )
                            if learned and learned != orig_video_prompt:
                                prompt = learned
                                seg["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次视频重试，按质检教训改写提示词",
                                                shot_id, attempt + 1)
                            else:
                                prompt = orig_video_prompt
                                seg["prompt"] = prompt
                                app.logger.info("镜头 %s 第 %d 次视频重试，暂无可用教训，仅换种子",
                                                shot_id, attempt + 1)
                        except Exception as mem_err:
                            app.logger.warning(f"读取记忆模块失败: {mem_err}")
                        with lock:
                            generation_state[task_id]["phase"] = \
                                f"视频质检不达标，重新生成（第 {attempt}/{max_retries} 次）"
                            generation_state[task_id]["qc_phase"] = "regenerating"
                    with lock:
                        if attempt == 0:
                            generation_state[task_id]["phase"] = f"视频生成中（镜头 {shot_id}）"
                    # 一个分镜 = 一段：按分镜数动态构建工作流，此处段数固定为 1
                    result = comfyui_client.generate_h3_sequence(
                        segments=[seg],
                        filename_prefix=f"comic_drama/{project_name}_shot_{seq:02d}",
                        seed=seed,
                        timeout_per_segment=timeout_per_segment,
                        size=_size,
                    )
                    if not result['files']:
                        video_item["error"] = "ComfyUI 未返回视频文件（可能未安装 H3 节点或超时）"
                        break
                    src = result['files'][0]
                    # 防御：源文件不存在时明确判失败（避免重试时对已 move 过的路径二次读取报错）
                    if not os.path.isfile(src):
                        video_item["error"] = f"ComfyUI 返回的视频文件不存在: {src}"
                        break
                    # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该视频）
                    v_scratch_dir = os.path.join(QC_DIR, project_name, "video_scratch")
                    os.makedirs(v_scratch_dir, exist_ok=True)
                    _qc_prune_attempts(v_scratch_dir)   # G8③：清本镜历史过期 try（视频暂存按镜头前缀保留最近4）
                    v_scratch = os.path.join(v_scratch_dir,
                                             f"shot_{seq:02d}_try{attempt + 1}.mp4")
                    if os.path.abspath(src) != os.path.abspath(v_scratch):
                        shutil.move(src, v_scratch)
                    video_item.update({"success": True, "path": dst,
                                       "url": f"{_vurl}/shot_{seq:02d}.mp4"})
                    video_item.pop("error", None)
                    if not qc_on:
                        # P0-1 fail-closed：qc_declared=True 但接口未就绪 → 阻断，不写正式目录
                        # （旧实现 `move(v_scratch, dst)` 属 fail-open，未质检视频直接入库）。
                        if qc_declared:
                            video_item["error"] = ("视频质检阻断（质检接口未就绪）：已开启视频质检，"
                                                   "但 base_url / api_key / model 不可用；"
                                                   "未质检产物不写入正式目录（暂存视频见质检历史）")
                            app.logger.warning(
                                "[视频质检] qc_declared=True 但 qc_on=False → fail-closed 阻断入库"
                                "（不写正式目录）：project=%s shot=%s", project_name, shot_id)
                            break
                        if os.path.exists(dst):
                            os.remove(dst)
                        shutil.move(v_scratch, dst)   # 质检本就未开启：按原行为直接入库
                        break
                    with lock:
                        generation_state[task_id]["phase"] = \
                            f"视频质检中（镜头 {shot_id} · 抽帧 {qc_cfg.get('video_frame_count', 3)} 张 · 第 {attempt + 1} 次）"
                        generation_state[task_id]["qc_phase"] = "checking"
                    frames_dir = os.path.join(QC_DIR, project_name, "frames",
                                              f"shot_{seq:02d}_try{attempt + 1}")
                    verdict = qc_client.check_video(v_scratch, _qc_shot_desc(shot), qc_cfg,
                                                    frames_dir=frames_dir,
                                                    style=(shot.get("style") or _style_res.get("style")),
                                                    expected_duration=float(
                                                        shot.get("duration") or 0.0))
                    rec = _qc_record_verdict(
                        project_name, "video", shot_id, "视频质检",
                        attempt + 1, seed, v_scratch, verdict,
                        extra={
                            "duration": verdict.get("duration"),
                            "frames": [f"/api/qc/frames/{os.path.relpath(f, QC_DIR).replace(os.sep, '/')}"
                                       for f in (verdict.get("frames") or [])],
                            "frames_local": verdict.get("frames") or [],
                        },
                        style=(shot.get("style") or _style_res.get("style")))
                    attempts.append(rec)
                    gate = _qc_gate(verdict)
                    video_item["qc_gate"] = gate
                    video_item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                    if gate["accept"]:
                        if os.path.exists(dst):
                            os.remove(dst)
                        shutil.move(v_scratch, dst)   # 质检达标 → 写入正式交付目录
                        # O2：产物旁路元数据（seed/提示词/工作流 SHA256/质检结论）
                        _write_artifact_meta(
                            dst, kind="video", project_name=project_name,
                            seed=seed, prompt=prompt, workflow_key="h3_video",
                            qc=gate, shot_id=shot_id,
                            extra={"mode": "per_shot", "used_storyboard": video_item.get("used_storyboard")})
                        break
                    if not verdict.get("ok"):
                        # [教训][video] 诊断（§2.3.5）：per_shot 质检调用异常/超时（ok=false）
                        # → 静默跳过、不记教训（ffmpeg 抽帧 + 多模态失败率高），如实打点便于排障。
                        app.logger.info("[教训][video] project=%s shot=%s mode=per_shot attempt=%d "
                                        "verdict.ok=false → 质检异常/超时，不沉淀教训",
                                        project_name, shot_id, attempt + 1)
                        break
                    # ★ 立刻沉淀教训（含风格不达标强化），供下一次重试改写提示词
                    _record_qc_lesson(project_name, "video", orig_video_prompt, rec)
                    app.logger.info("[教训][video] project=%s shot=%s mode=per_shot attempt=%d "
                                    "ok=true passed=False → recorded",
                                    project_name, shot_id, attempt + 1)
                    # ★ 重试止损（同分镜）：连续两次缺陷完全相同 → 「改提示词 + 换种子」没带来
                    #   任何变化，提前停止重试，别再重复烧 GPU。达标早已 break，不改结论。
                    _hopeless, _hopeless_detail = _qc_retry_hopeless(attempts)
                    if _hopeless:
                        # 标记最后一条质检记录：_qc_summary 据此把 label 写成「已停止重试」
                        rec["retry_stopped"] = True
                        rec["retry_stopped_features"] = _hopeless_detail
                        video_item["qc_retry_stopped"] = {
                            "reason": "连续两次缺陷完全相同，判定重试无收益，已提前停止",
                            "features": _hopeless_detail, "attempts": len(attempts)}
                        app.logger.warning(
                            f"视频重试止损（镜头 {shot_id}）：连续 {len(attempts)} 次缺陷完全相同，"
                            f"提前停止重试。缺陷：{_hopeless_detail}；"
                            f"建议改写该镜剧本字段后单独重跑该镜")
                        break
                if qc_declared or qc_on:
                    video_item["qc"] = _qc_summary(attempts, qc_declared, qc_on,
                                                   int(qc_cfg.get("max_retries", 0)))
                    gate = video_item.get("qc_gate")
                    if not gate or not gate.get("accept"):
                        # P0：质检不达标 / 调用异常 → 阻断入库（不写正式目录，暂存区留证，不计入成功）
                        video_item["success"] = False
                        video_item["qc_blocked"] = True
                        video_item.pop("path", None)
                        video_item.pop("url", None)
                        # ★ 用户需求：质检「判定不通过」的暂存视频 + 抽帧图不留本地（含 ComfyUI 侧）。
                        # ⚠️ 仅当最后一次尝试是「质检成功返回且不合格」（ok=True）时才删；
                        # ok=False（接口故障/超时）/ skipped（未开启）不是产物不合格，绝不删。
                        try:
                            _last_v = attempts[-1] if (attempts and isinstance(attempts[-1], dict)) else {}
                            if qc_on and _last_v.get("ok") is True:
                                _purge_rejected_artifacts(
                                    [v_scratch, frames_dir], project=project_name,                                    reason=f"视频质检不合格（{gate['label']}）" if gate else "视频质检不合格",
                                    kind="shot_video",
                                    history_file=_last_v.get("history_file") or "")
                        except Exception as _pe:  # noqa: BLE001
                            app.logger.warning(f"视频不合格产物清理失败（忽略）：{_pe}")
                        # 摘掉响应体里的 frames URL（抽帧目录已移走，前端再取会 404），
                        # 但保留质检历史里的 frames_local 供排障。
                        video_item.pop("frames", None)
                        video_item["error"] = ((f"视频质检阻断（{gate['label']}）：{gate['reason']}"
                                                "；未通过质检，未写入正式目录（暂存视频见质检历史）")
                                               if gate else (video_item.get("error")
                                                             or "视频生成失败，未写入正式目录"))
                elif "qc" not in video_item:
                    video_item["qc"] = {"enabled": False, "status": "disabled",
                                        "label": "质检未开启", "attempts": 0, "regenerated": 0}

                # ---------- H3 音轨策略（保留原生音效 / 或按 config 剥离，见 _h3_audio_policy） ----------
                if video_item.get("success"):
                    aud = _h3_audio_policy(dst)
                    video_item["audio"] = aud
                    if aud.get("policy") == "strip" and aud.get("has_audio_after"):
                        app.logger.warning(f"镜头 {shot_id} 去音轨后仍检测到音频流: {dst}")
                    elif aud.get("error"):
                        app.logger.warning(f"镜头 {shot_id} 音轨处理异常（已保留原状）: {aud.get('error')}")
                    # ---------- 音效提取：分离出「纯音效」，供混音垫底 ----------
                    if H3_SFX_ISOLATE and H3_EMIT_AUDIO and not H3_STRIP_AUDIO \
                            and aud.get("has_audio_after"):
                        sfx = _isolate_shot_sfx(dst, project_name,
                                                shot.get("episode") or 1, shot_id)
                        video_item["sfx"] = sfx
                        if sfx.get("ok"):
                            app.logger.info(f"镜头 {shot_id} 音效已分离: {sfx.get('out_path')}")
                        else:
                            app.logger.warning(f"镜头 {shot_id} 音效分离未成功"
                                               f"（混音将退回原音轨）: {sfx.get('error')}")
                with lock:
                    generation_state[task_id]["results"].append(video_item)
                    generation_state[task_id]["phase"] = "视频生成"
            except Exception as shot_err:
                # 单镜头失败不影响其余镜头（原实现会让整批 failed）
                app.logger.error(f"镜头 {shot_id} 生成失败: {shot_err}")
                with lock:
                    generation_state[task_id]["results"].append({
                        "shot_id": shot_id, "success": False, "error": str(shot_err)
                    })

        with lock:
            results = generation_state[task_id]["results"]
            ok = sum(1 for r in results if r.get("success"))
            blocked = sum(1 for r in results if r.get("qc_blocked"))
            generation_state[task_id].update({
                "status": "completed" if ok else "failed",
                "success_count": ok,
                "qc_blocked_count": blocked,
                "error": "" if ok else "所有镜头均生成失败",
            })
    except Exception as e:
        app.logger.error(f"视频生成失败: {e}")
        # B-16 P2-11：视频任务异常 → 清理本任务产生的视频 scratch 中间产物
        _cleanup_scratch_dir(os.path.join(QC_DIR, project_name, "video_scratch"), app.logger)
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})


# ===== 步骤6：成片合成 =====

@app.route('/api/final/video', methods=['POST'])
def api_generate_final():
    data = _body()
    # P2-T2：写盘路由统一走 _project_or_400（成片合成需定位项目内剧本，缺省无合理语义）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    script_path = data.get('script_path', '')

    # P0-4：剧本路径必须先落在项目输出目录内（与 project_store.bind_script 同口径），
    # 越界（如 C:/Windows/... 或项目外路径）直接拒读，避免被 index.json 里被污染的
    # 绝对路径拖出目录读走任意文件。
    if not script_path or not project_store.is_path_inside_output(script_path):
        return jsonify({"error": "剧本路径必须在项目输出目录内（output/），越界路径已拒读"}), 400
    if not os.path.exists(script_path):
        return jsonify({"error": "剧本文件不存在"}), 400

    try:
        # 集号与剧本一起解析：合成必须知道是第几集（审计 S4 —— 旧代码合成完才读集号，
        # 而合成函数压根没有集号入参，于是第 2 集及以后合成的是第 1 集的片段）
        ep_no = 0
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                _sc = json.load(f) or {}
            ep_no = int(_sc.get('episode_no')
                        or (_sc.get('metadata') or {}).get('episode_no') or 0)
        except Exception:  # noqa: BLE001 - 剧本读不到就退回第 1 集
            ep_no = 0
        output = video_processor.generate_final_video(script_path, project_name, ep_no or 1)
        if not output:
            return jsonify({"error": f"没有可合并的视频片段（第 {ep_no or 1} 集），"
                                     "请先完成步骤5的视频生成"}), 400
        filename = os.path.basename(output)
        # URL 按「相对 FINAL_DIR 的路径」拼，避免成片落在项目子目录时 404
        try:
            rel_path = os.path.relpath(output, FINAL_DIR).replace(os.sep, "/")
        except ValueError:
            rel_path = f"{project_name}/{filename}"
        resp = {
            "success": True,
            "output_path": output,                       # 保留原字段（本地绝对路径）
            "episode_no": ep_no or 1,
            "filename": filename,
            "url": f"/api/final/{rel_path}"              # 前端可直接播放/下载的 URL
        }
        # 整集成片落盘 → 自动登记进「成品验收」队列（用户只需看这里）
        reg = register_final_deliverable(
            project_name, ep_no or 1, output,
            meta={"source": "final_video", "script": os.path.basename(script_path)})
        resp["deliverable"] = {"registered": bool(reg.get("registered")),
                               "reason": reg.get("reason") or "",
                               "episode_no": ep_no or 1}
        # 视频水印（默认关闭；开启后额外产出一份带水印成片，不影响上面的无水印成片）
        resp["watermark"] = _wm_apply_to_final(output, project_name)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== 静态资产访问 =====

@app.route('/api/assets/<path:filename>')
def api_asset_file(filename):
    """提供资产文件访问"""
    return _serve_safe(os.path.join(PROJECT_OUTPUT_DIR, "assets"), filename)


@app.route('/api/videos/<path:filename>')
def api_video_file(filename):
    """提供视频文件访问"""
    return _serve_safe(VIDEOS_DIR, filename, conditional=True)


@app.route('/api/final/<path:filename>')
def api_final_file(filename):
    """提供最终成片文件访问（新增：使成片可在页面内联播放/下载）"""
    # conditional=True 支持 Range 请求，视频可拖动进度条
    return _serve_safe(FINAL_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')


# ===== 视频水印（C 项：可配置、默认关闭、支持「全视频移动」） =====
# 说明：图片生成链路不添加任何水印（C 项⑧）；本区块只处理视频后处理，不改 ComfyUI 工作流。

def _wm_load_cfg() -> dict:
    return video_watermark.load_config(WATERMARK_CONFIG_PATH)


def _wm_view(cfg: dict) -> dict:
    view = video_watermark.public_view(cfg)
    view["config_path"] = os.path.abspath(WATERMARK_CONFIG_PATH)
    view["output_dir"] = os.path.abspath(WATERMARK_DIR)
    return view


def _wm_apply_to_final(final_path: str, project_name: str) -> dict:
    """成片后处理：水印开启时额外产出一份带水印成片；默认关闭则整体跳过"""
    try:
        cfg = _wm_load_cfg()
        if not cfg.get("enabled"):
            return {"enabled": False, "skipped": True,
                    "message": "视频水印未开启（默认关闭，成片保持无水印）"}
        out_dir = os.path.join(WATERMARK_DIR, project_name)
        res = video_watermark.apply_watermark(final_path, cfg=cfg, out_dir=out_dir)
        out_path = res.get("out_path") or ""
        res.update({
            "type": cfg.get("type"), "mode": cfg.get("mode"),
            "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode"), cfg.get("mode")),
            "url": (f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}"
                    if out_path else ""),
        })
        if not res.get("ok"):
            app.logger.warning(f"成片水印烧写失败（不影响无水印成片）：{res.get('error')}")
        return res
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"成片水印处理异常（忽略）：{e}")
        return {"enabled": True, "ok": False, "error": str(e)}


@app.route('/api/watermark/config', methods=['GET'])
def api_watermark_config_get():
    cfg = _wm_load_cfg()
    return jsonify({"success": True, "config": cfg, "view": _wm_view(cfg),
                    "config_path": os.path.abspath(WATERMARK_CONFIG_PATH),
                    "output_dir": os.path.abspath(WATERMARK_DIR),
                    "message": "视频水印默认关闭；开启并保存后，成片会自动追加一份带水印版本"})


@app.route('/api/watermark/config', methods=['POST'])
def api_watermark_config_save():
    data = request.json or {}
    cfg = video_watermark.save_config(WATERMARK_CONFIG_PATH, data)
    view = _wm_view(cfg)
    warning = ""
    if cfg.get("enabled") and not view.get("ready"):
        warning = {
            "disabled": "禁用",
            "no_ffmpeg": "未找到 ffmpeg，无法烧写水印",
            "no_font": "未找到可用字体，文字水印无法生效",
            "no_image": "图片水印文件不存在",
        }.get(view.get("ready_state"), "")
    return jsonify({"success": True, "config": cfg, "view": view,
                    "config_path": os.path.abspath(WATERMARK_CONFIG_PATH),
                    "warning": warning,
                    "message": ("视频水印已开启（模式：%s）" % view["mode_label"]) if cfg.get("enabled")
                               else "视频水印已关闭（成片不会带水印）"})


@app.route('/api/watermark/apply', methods=['POST'])
def api_watermark_apply():
    """对指定视频烧写水印（手动单段验证用）。body: video_path / project_name / config(临时覆盖)"""
    data = request.json or {}
    src = str(data.get("video_path") or "").strip()
    project_name = _safe_project(data.get("project_name") or "watermark")
    cfg = video_watermark.load_config(WATERMARK_CONFIG_PATH)
    if isinstance(data.get("config"), dict):
        cfg = video_watermark.normalize(data["config"], base=cfg)
    if not src:
        return jsonify({"success": False, "error": "缺少 video_path"}), 400
    if not os.path.isfile(src):
        return jsonify({"success": False, "error": f"视频不存在：{src}"}), 400
    if data.get("enabled") is not None:
        cfg["enabled"] = bool(data.get("enabled"))
    res = video_watermark.apply_watermark(src, cfg=cfg,
                                          out_dir=os.path.join(WATERMARK_DIR, project_name))
    out_path = res.get("out_path") or ""
    if res.get("skipped"):
        return jsonify({"success": True, "skipped": True, "config": cfg,
                        "message": "水印未开启，未产出带水印文件"})
    if not res.get("ok"):
        return jsonify({"success": False, "config": cfg, "error": res.get("error")}), 500
    return jsonify({"success": True, "config": cfg, "output_path": out_path,
                    "url": f"/api/watermark/file/{project_name}/{os.path.basename(out_path)}",
                    "mode": cfg.get("mode"),
                    "mode_label": video_watermark.MODES_LABEL.get(cfg.get("mode")),
                    "elapsed": res.get("elapsed"),
                    "message": "水印已烧写"})


@app.route('/api/watermark/list/<path:project_name>', methods=['GET'])
def api_watermark_list(project_name):
    folder = project_store.project_dirs(WATERMARK_DIR, project_name)
    files = []
    for d in folder:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.lower().endswith((".mp4", ".mov", ".mkv")):
                    files.append(os.path.join(d, f))
    return jsonify({"success": True, "files": files, "count": len(files),
                    "output_dir": os.path.abspath(WATERMARK_DIR)})


@app.route('/api/watermark/file/<path:filename>', methods=['GET'])
def api_watermark_file(filename):
    return _serve_safe(WATERMARK_DIR, filename, conditional=True,
                       as_attachment=request.args.get('download') == '1')


# ===== 视频超分（FlashVSR 真实实现，成片/片段 → 高分辨率） =====

upscale_tasks = {}
upscale_lock = threading.Lock()

UPSCALE_URL_PREFIXES = [("/api/final/", FINAL_DIR),
                        ("/api/videos/", VIDEOS_DIR),
                        ("/api/upscale/", UPSCALE_DIR)]

# ComfyUI 侧产出目录（项目里真实生成的镜头视频默认落在 ComfyUI/output/video，
# 成片前的素材也需要能直接超分，故一并列为候选来源；播放走 ComfyUI /view 重定向）
COMFY_VIDEO_DIRS = [("ComfyUI片段", os.path.join(COMFYUI_OUTPUT_DIR, "video")),
                    ("ComfyUI超分", os.path.join(COMFYUI_OUTPUT_DIR, "upscale"))]


def _comfy_view_url(filename: str, subfolder: str = "") -> str:
    """生成后端代理 URL（/api/upscale/comfyview），实际播放时 302 到 ComfyUI /view"""
    from urllib.parse import quote
    return (f"/api/upscale/comfyview?filename={quote(filename)}"
            f"&subfolder={quote(subfolder)}")


def _upscale_resolve_comfyview(query: dict) -> str:
    """解析 /api/upscale/comfyview?... 形式的 ComfyUI 产出视频为本地绝对路径"""
    filename = (query.get("filename") or "").replace("\\", "/").lstrip("/")
    subfolder = (query.get("subfolder") or "").replace("\\", "/").strip("/")
    if not filename or ".." in filename.split("/") or ".." in subfolder.split("/"):
        raise UpscaleError("非法的 ComfyUI 文件参数")
    candidate = os.path.abspath(os.path.join(COMFYUI_OUTPUT_DIR, subfolder, filename))
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if not candidate.startswith(root + os.sep):
        raise UpscaleError("非法路径：不允许跳出 ComfyUI 输出目录")
    if not os.path.exists(candidate):
        raise UpscaleError(f"ComfyUI 产出文件不存在: {candidate}")
    return candidate


def _upscale_resolve_video(data: dict) -> str:
    """解析待超分视频的真实本地路径：支持 video_path（绝对路径）或 video_url（/api/... 前缀）"""
    video_path = (data.get("video_path") or "").strip()
    if video_path:
        video_path = os.path.abspath(video_path)
        if not os.path.exists(video_path):
            raise UpscaleError(f"视频文件不存在: {video_path}")
        return video_path

    url = (data.get("video_url") or "").strip()
    if url.startswith("/api/upscale/comfyview"):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        return _upscale_resolve_comfyview({k: v[0] for k, v in qs.items()})
    url = url.split("?")[0]
    if url:
        for prefix, base in UPSCALE_URL_PREFIXES:
            if url.startswith(prefix):
                rel = url[len(prefix):]
                candidate = os.path.abspath(os.path.join(base, rel))
                if not candidate.startswith(os.path.abspath(base)):
                    raise UpscaleError("非法路径：不允许跳出输出目录")
                if not os.path.exists(candidate):
                    raise UpscaleError(f"URL 对应文件不存在: {candidate}")
                return candidate
        raise UpscaleError(f"不支持的视频 URL 前缀: {url}")

    raise UpscaleError("请提供 video_path（绝对路径）或 video_url（如 /api/final/<项目>/<文件>）")


def _upscale_url_for_path(path: str) -> str:
    """把输出目录下的绝对路径反查为可播放 URL（用于前端对比预览）"""
    try:
        p = os.path.abspath(path)
    except Exception:
        return ""
    for prefix, base in UPSCALE_URL_PREFIXES:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            return prefix + rel
    for _label, base in COMFY_VIDEO_DIRS:
        b = os.path.abspath(base)
        if p.startswith(b + os.sep):
            rel = os.path.relpath(p, b).replace(os.sep, "/")
            sub = os.path.relpath(b, os.path.abspath(COMFYUI_OUTPUT_DIR)).replace(os.sep, "/")
            return _comfy_view_url(rel, "" if sub == "." else sub)
    # ComfyUI 侧任意子目录产出（video / v5video / upscale / 自定义工作流目录等）：
    # 统一走 comfyview 代理，保证「超分前」对比预览有可播放地址
    root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    if p.startswith(root + os.sep):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        return _comfy_view_url(os.path.basename(rel), os.path.dirname(rel).replace("\\", "/"))
    return ""


def _upscale_worker(task_id: str, video_path: str, project_name: str, params: dict):
    """后台线程：执行真实超分链路，进度/错误全部写入 upscale_tasks"""
    def progress(msg, pct=None):
        with upscale_lock:
            t = upscale_tasks.get(task_id)
            if not t:
                return
            if pct is not None:
                t["progress"] = max(int(t.get("progress") or 0), int(pct))
            t["message"] = msg
            t["updated_at"] = time.time()

    with upscale_lock:
        upscale_tasks[task_id].update({"status": "running", "progress": 2,
                                       "message": "正在准备超分…"})
    try:
        result = VideoUpscaler().upscale(video_path, project_name=project_name,
                                         progress_cb=progress, **params)
        result["output_url"] = _upscale_url_for_path(result.get("output_path") or "")
        result["input_url"] = _upscale_url_for_path(video_path)

        # —— 超分成品质检（2026-09-24 补上，此前超分是唯一「产出后零质检」的环节）——
        # 超分是成品链路的最后一环（4K 输出），若不质检，超分导致的闪烁/撕裂/糊化会直接
        # 进成片无人拦。这里对「超分后的成品」跑一次视频质检（抽帧 + 多模态判定）。
        # 口径与整集视频质检一致，但**不阻断**（fail-open）：超分是增值环节，质检接口
        # 未就绪 / 成品不达标时只标记 qc_passed=False 并留痕，不把任务判 error ——
        # 用户仍能拿到超分成品，同时能看到质检结论。
        _qc_result = {"checked": False, "passed": None, "reason": ""}
        try:
            _qc_cfg = _qc_load_cfg()
            if qc_client.video_qc_ready(_qc_cfg):
                _out_path = result.get("output_path") or ""
                _before = probe_video_info(video_path) or {}
                _dur = _before.get("duration")
                _style = _project_style(project_name)
                _verdict = qc_client.check_video(
                    _out_path, "超分成品质检（对超分后的成品抽帧，检查是否引入闪烁/撕裂/糊化/色块）",
                    _qc_cfg, style=_style,
                    expected_duration=float(_dur) if _dur else None)
                _gate = _qc_gate(_verdict)
                _qc_result["checked"] = bool(_verdict.get("ok"))
                _qc_result["passed"] = bool(_gate.get("accept", False))
                _qc_result["reason"] = (_gate.get("reason") or _verdict.get("reason")
                                        or _verdict.get("error") or "")
                _qc_result["score"] = _verdict.get("score")
                if _verdict.get("ok"):
                    try:
                        _qc_record_verdict(
                            project_name, "video", "upscale", "超分成品质检",
                            1, None, _out_path, _verdict, style=_style)
                    except Exception as _re:  # noqa: BLE001 质检记录失败不影响超分交付
                        app.logger.warning("超分质检落盘失败（不影响交付）：%s", _re)
                if _qc_result["passed"] is False:
                    app.logger.warning("[超分质检] 成品未通过质检：%s", _qc_result["reason"])
                else:
                    app.logger.info("[超分质检] 成品质检%s：%s",
                                    "通过" if _qc_result["passed"] else "未执行/不可判定",
                                    _qc_result["reason"])
            else:
                app.logger.info("[超分质检] 视频质检未就绪（开关/接口），跳过（fail-open）")
        except Exception as _qe:  # noqa: BLE001 质检自身异常绝不拖垮超分交付
            _qc_result["reason"] = f"质检异常：{_qe}"
            app.logger.warning("[超分质检] 质检异常（不影响交付）：%s", _qe)
        result["qc"] = _qc_result

        with upscale_lock:
            upscale_tasks[task_id].update({
                "status": "done", "progress": 100, "message": "超分完成",
                "result": result, "finished_at": time.time(),
            })
    except Exception as e:
        import traceback
        traceback.print_exc()
        with upscale_lock:
            upscale_tasks[task_id].update({
                "status": "error", "message": str(e), "error": str(e),
                "finished_at": time.time(),
            })


@app.route('/api/upscale/env', methods=['GET'])
def api_upscale_env():
    """超分环境自检：ComfyUI 在线、节点、FlashVSR 模型文件、TE-Speed 加速模板是否就位"""
    env = upscale_env_check()
    env["defaults"] = dict(TE_UPSCALE_DEFAULT_PARAMS) if env.get("te_ready") \
        else dict(UPSCALE_DEFAULT_PARAMS)
    env["legacy_defaults"] = dict(UPSCALE_DEFAULT_PARAMS)
    env["te_defaults"] = dict(TE_UPSCALE_DEFAULT_PARAMS)
    env["te_lowvram"] = dict(TE_UPSCALE_LOWVRAM_PARAMS)
    env["default_engine"] = UPSCALE_ENGINE
    return jsonify(env)


@app.route('/api/upscale/sources', methods=['GET'])
def api_upscale_sources():
    """列出可作为超分输入的候选视频（成片 final / 片段 videos / 已有超分产物 / ComfyUI 侧产出）"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    dirs = [("成片", FINAL_DIR), ("视频片段", VIDEOS_DIR), ("超分产物", UPSCALE_DIR)]
    items = []
    prefix_by_base = {os.path.abspath(b): p for p, b in UPSCALE_URL_PREFIXES}
    for label, base in dirs:
        d = os.path.join(base, project_name)
        if not os.path.isdir(d):
            continue
        url_prefix = prefix_by_base.get(os.path.abspath(base), "")
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            items.append({
                "kind": label,
                "name": name,
                "path": os.path.abspath(p),
                "url": f"{url_prefix}{project_name}/{name}",
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    # ComfyUI 侧产出（项目真实生成的镜头视频默认落在 ComfyUI/output 各子目录，
    # 如 video / v5video / 自定义工作流目录；成片前也能直接选中超分）
    comfy_root = os.path.abspath(COMFYUI_OUTPUT_DIR)
    comfy_dirs = [(comfy_root, "ComfyUI")]
    if os.path.isdir(comfy_root):
        try:
            for name in sorted(os.listdir(comfy_root)):
                sub = os.path.join(comfy_root, name)
                if os.path.isdir(sub):
                    comfy_dirs.append((sub, f"ComfyUI/{name}"))
        except OSError as e:
            app.logger.debug("扫描 ComfyUI 子目录失败（忽略）：%s", e)
    for base, label in comfy_dirs:
        if not os.path.isdir(base):
            continue
        subfolder = os.path.relpath(base, comfy_root).replace(os.sep, "/")
        if subfolder == ".":
            subfolder = ""
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
                continue
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            items.append({
                "kind": label,
                "name": name,
                "path": os.path.abspath(p),
                "url": _comfy_view_url(name, subfolder),
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    items.sort(key=lambda it: (it["kind"] not in ("成片", "视频片段"), it["kind"], it["name"]))
    return jsonify({"success": True, "project_name": project_name, "items": items[:300]})


@app.route('/api/upscale/comfyview')
def api_upscale_comfyview():
    """代理播放 ComfyUI 侧产出视频：302 重定向到 ComfyUI /view（仅允许 output 目录内文件）"""
    filename = request.args.get('filename') or ''
    subfolder = request.args.get('subfolder') or ''
    try:
        _upscale_resolve_comfyview({'filename': filename, 'subfolder': subfolder})
    except UpscaleError as e:
        app.logger.warning(f"comfyview 拒绝访问: {e}")
        abort(404)
    from urllib.parse import urlencode
    q = urlencode({'filename': filename, 'subfolder': subfolder, 'type': 'output'})
    return redirect(f"{COMFYUI_URL.rstrip('/')}/view?{q}")


@app.route('/api/upscale/list', methods=['GET'])
def api_upscale_list():
    """列出某项目已生成的超分产物"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    d = os.path.join(UPSCALE_DIR, project_name)
    items = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d), reverse=True):
            if not name.lower().endswith(".mp4"):
                continue
            p = os.path.join(d, name)
            items.append({
                "name": name, "path": p,
                "url": f"/api/upscale/{project_name}/{name}",
                "size_mb": round(os.path.getsize(p) / 1048576, 2),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    return jsonify({"success": True, "project_name": project_name, "items": items})


@app.route('/api/upscale/video', methods=['POST'])
def api_upscale_video():
    """发起视频超分（异步任务）：body 支持 project_name / video_path|video_url / scale / mode 等"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（前端契约必填 project_name，
    # 缺省只会静默写进共享 'project' 命名空间造成串项目）
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err

    try:
        video_path = _upscale_resolve_video(data)
    except UpscaleError as e:
        return jsonify({"error": str(e)}), 400

    before = probe_video_info(video_path)
    if not before.get("ok"):
        return jsonify({"error": f"输入视频无法解析: {before.get('error')}"}), 400

    env = upscale_env_check()
    if not env.get("available"):
        return jsonify({"error": "超分环境不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    try:
        scale = int(data.get('scale') or TE_UPSCALE_DEFAULT_PARAMS.get('scale', 2))
    except (TypeError, ValueError):
        return jsonify({"error": "scale 参数非法，仅支持 2 / 3 / 4"}), 400
    if scale not in (2, 3, 4):
        return jsonify({"error": "倍率仅支持 2 / 3 / 4（FlashVSR 支持范围）"}), 400

    # 引擎：默认 TE-Speed-flashVSR 加速链路，可显式指定 legacy-flashvsr
    engine = str(data.get('engine') or UPSCALE_ENGINE or "te-speed-flashvsr").strip().lower()
    if engine not in ("te-speed-flashvsr", "legacy-flashvsr"):
        return jsonify({"error": "engine 仅支持 te-speed-flashvsr / legacy-flashvsr"}), 400
    if engine == "te-speed-flashvsr" and not env.get("te_ready"):
        return jsonify({"error": "TE-Speed 加速链路不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    # 参数：TE-Speed 加速链路（sparse_sage2 + 分块）与旧链路字段都接受，按引擎生效
    params = {k: data.get(k) for k in (
        # TE-Speed-flashVSR 加速参数
        "mode", "precision", "device", "quality_profile", "intensity",
        "spatial_strategy", "memory_policy", "attention_backend",
        "attention_budget", "kv_retention", "local_radius",
        "max_tile_edge", "blend_overlap", "preprocess_batch",
        "quality_value", "color_fix", "frame_load_cap", "skip_first_frames", "free_vram",
        "seed", "timeout",
        # ⚠️ 音轨旁路开关：TE-Speed 链路默认 attach_audio=False，对「成片」超分时
        # 不显式打开会把已合成的配音丢掉，产出无声视频。
        "attach_audio",
        # 旧 FlashVSR 链路参数（回退时生效）
        "tile_size", "tile_overlap", "tiled_vae", "tiled_dit", "unload_dit",
        "sparse_ratio", "kv_ratio", "local_range", "attention_mode", "force_offload",
    ) if data.get(k) is not None}
    params["scale"] = scale
    params["engine"] = engine

    task_id = f"upscale_{int(time.time() * 1000)}"
    with upscale_lock:
        upscale_tasks[task_id] = {
            "task_id": task_id, "status": "pending", "progress": 0,
            "message": "任务已创建", "project_name": project_name,
            "input_path": video_path, "input_probe": before,
            "scale": scale, "engine": engine, "params": params, "created_at": time.time(),
        }
    # B-01 P1-12：GPU 并发闸门
    def _upscale_worker_gated():
        with gpu_task_gate.run_gpu_task(task_id, f"超分({engine} {scale}x)"):
            _upscale_worker(task_id, video_path, project_name, params)
    threading.Thread(target=_upscale_worker_gated, daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "input_path": video_path,
                    "input_probe": before, "scale": scale, "engine": engine,
                    "params": params})


@app.route('/api/upscale/status/<task_id>', methods=['GET'])
def api_upscale_status(task_id):
    """查询超分任务进度/结果"""
    with upscale_lock:
        task = upscale_tasks.get(task_id)
        task = dict(task) if task else None
    if not task:
        return jsonify({"error": f"未找到超分任务 {task_id}"}), 404
    return jsonify({"success": True, **task})


@app.route('/api/upscale/tasks', methods=['GET'])
def api_upscale_tasks():
    """列出全部超分任务（按创建时间倒序）"""
    with upscale_lock:
        items = [dict(t) for t in upscale_tasks.values()]
    items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return jsonify({"success": True, "items": items[:50]})


@app.route('/api/upscale/<path:filename>')
def api_upscale_file(filename):
    """提供超分产物访问（支持 Range 拖动进度条与下载）"""
    return _serve_safe(UPSCALE_DIR, filename, conditional=True,
                      as_attachment=request.args.get('download') == '1')


@app.route('/api/script/fallback', methods=['POST'])
def api_fallback_script():
    """一键加载本地兜底剧本（免 API Key 演示）"""
    try:
        script = script_gen.load_fallback_script()
        if not script:
            return jsonify({"error": "未找到本地兜底剧本（output/scripts/剑心初醒_兼容版.json）"}), 404
        data = request.json or {}
        project_name = _safe_project(data.get('project_name') or script.get("title", "fallback"))
        script_path = script_gen.save_script(script, project_name)
        script.setdefault("metadata", {})["script_path"] = script_path
        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", [])),
            "fallback": True
        })
    except Exception as e:
        app.logger.error(f"加载兜底剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


# =====================================================================
# 新增模块 A：自定义 LLM API 配置
# =====================================================================

app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 单次上传上限 200MB

LLM_NOT_CONFIGURED_GUIDE = (
    "尚未配置自定义 AI 接口。请点击页面右上角「AI 设置」，切换到对应模块后依次填写：\n"
    "① base_url：OpenAI 兼容接口地址，例如 https://api.deepseek.com/v1\n"
    "② api_key：接口密钥（保存后页面只显示脱敏结果）\n"
    "③ model：模型名，例如 deepseek-chat / gpt-4o-mini\n"
    "填写后先点「测试连接」，成功再点「保存配置」。文本分析 / 质检 / 对话总控 三个模型相互独立。"
)

LLM_NOT_CONFIGURED_GUIDE_MAP = {
    "text": ("尚未配置「文本分析模型」。请在「AI 设置」中填写 base_url / api_key / model 并保存，"
             "之后即可使用小说转剧本与提示词分析。"),
    "qc": ("尚未配置「质检模型」。请在「AI 设置 → 质检模型」中填写独立的 base_url / api_key / model"
           "（需支持图像输入的多模态模型）。未配置时生成流程会自动跳过质检，不会报错。"),
    "chat": ("尚未配置「对话总控模型」。请在「AI 设置 → 对话总控模型」中填写 base_url / api_key / model，"
             "之后即可使用 AI 对话来确定创作设定。"),
}

AI_MODULE_LABEL = {m: ai_config.MODULE_META[m]["label"] for m in AI_MODULES}


def _ai_client_for_module(module: str, base_url: str = None, api_key: str = None,
                          model: str = None, timeout: int = None,
                          reasoning_effort: str = None) -> LLMClient:
    """构造某个 AI 模块的客户端；传参可用于「测试连接」（不落盘）

    ⚠️ reasoning_effort 必须一起透传：它决定请求体注入哪个思考档位。
    漏掉它会导致两种事故——(1) 测试连接时按「不注入档位」探测，与保存后的真实行为不一致；
    (2) always-on reasoning 的模型（GLM-5.3-Flash）拿不到额度下限，max_tokens 太小 →
    正文全空只剩 reasoning_content，被误判成「模型不支持」。
    """
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    _re = None if reasoning_effort is None else str(reasoning_effort).strip()
    ep = {
        "base_url": (base_url or "").strip() or saved["base_url"],
        "api_key": (api_key or "").strip() or saved["api_key"],
        "model": (model or "").strip() or saved["model"],
        # 档位：本次显式传参优先，否则沿用已保存值（None 表示「不改动」语义）
        "reasoning_effort": (saved.get("reasoning_effort") or "") if _re is None else _re,
    }
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        raise LLMError(f"「{AI_MODULE_LABEL.get(module, module)}」尚未配置"
                       "（base_url / api_key / model 均为必填）")
    return LLMClient(AI_CONFIG_PATH, config=ep, timeout=timeout or LLM_REQUEST_TIMEOUT)


def _ai_gate_or_400(action: str, probe: bool = True):
    """开跑前 AI 前置门禁（P0-5 收尾）。

    通过 → 返回 None（调用方继续）；未通过 → 返回可直接 `return` 的 (response, 400) 元组。

    为什么放在每个生产入口而不是散在内部：修复前的故障是「配置页测试通过、运行时 401、
    整条流水线静默失败」，用户看不到任何原因。门禁要在**进入执行前**就把话说明白
    （缺什么、去哪修），而不是跑 4 小时后再炸。

    门禁自身异常按 fail-open 放行并响亮告警 —— 门禁是护栏，不是业务本身，
    绝不能因为护栏故障把整条线堵死。
    """
    try:
        import ai_selfcheck
        rep = ai_selfcheck.gate(action, probe=probe)
    except Exception as e:  # noqa: BLE001
        app.logger.error(f"AI 门禁执行异常（按放行处理，action={action}）：{type(e).__name__}: {e}")
        return None
    if rep.get("ok"):
        return None
    return jsonify({
        "success": False,
        "error": rep.get("message") or "AI 前置自检未通过，已阻断执行",
        "message": rep.get("message") or "",
        "hint": rep.get("hint") or "",
        "ai_selfcheck": {
            "action": action,
            "ok": False,
            "blocked_modules": rep.get("blocked_modules") or [],
            "blocked_labels": rep.get("blocked_labels") or [],
            "modules": rep.get("modules") or {},
            "hint": rep.get("hint") or "",
            "gate_off": False,
            "fix_url": "/api/ai/selfcheck?probe=1",
        },
    }), 400


def _current_llm_client() -> LLMClient:
    """文本分析链路（小说转剧本 / 章节转剧本 / 提示词分析）专用客户端：只用「文本分析模型」"""
    return _ai_client_for_module("text")


def _optional_llm_client():
    """best-effort 取「文本分析模型」客户端：已配置返回 LLMClient，未配置/异常返回 None。

    用于「LLM 锦上添花但不可阻断主流程」的场景（如上传时用 LLM 归纳章节标题正则，
    失败则退回纯正则切分）。与 _current_llm_client 的区别是**不抛 LLMError**。"""
    try:
        return _current_llm_client()
    except LLMError:
        return None


def _apply_project_settings(style: str, project_name: str = "") -> str:
    """把「AI 对话 → 应用设定」落盘的创作设定并入风格描述，供剧本 / 提示词 / 分镜链路引用。

    - 命中项目则用该项目的生效设定；未命中则退回最近一次应用的设定；
    - 未应用过任何设定时原样返回 style，行为与改造前一致。
    """
    base = (style or "").strip()
    try:
        brief = (ai_chat.settings_view(AI_SETTINGS_PATH, project_name).get("style_brief") or "").strip()
        if not brief and (project_name or "").strip():
            brief = (ai_chat.settings_view(AI_SETTINGS_PATH, "").get("style_brief") or "").strip()
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"创作设定读取失败（忽略，沿用原风格）：{e}")
        return base
    if not brief:
        return base
    return f"{base}；{brief}" if base else brief


def _sync_project_config_style(project_name: str, brief: str) -> bool:
    """把 AI 总控敲定的风格纲要同步到项目 config.json 的 style 字段（2026-09-22 P-2）。

    总控 apply 设定原本只写 ai_chat/project_settings.json，项目 config.json 的 style
    停在「建项目时的默认值（3D动漫渲染）」，用户看到「配置要求国漫2D 实际却还是 3D」
    即由此。这里把生效的 style_brief 回写进 config.json，让两份配置口径一致。

    - 仅当项目在 project_store 里真实存在、且 brief 非空时才写；
    - 失败只降级告警、不阻断 apply（config.json 同步属后置簿记，主链路必须成功）。
    返回是否真正落盘。
    """
    brief = (brief or "").strip()
    if not brief:
        return False
    rec = project_store.get_project(project_name or "")
    if not rec:
        return False
    try:
        project_store.update_config(rec["dir_key"], {"style": brief})
        return True
    except Exception as e:  # noqa: BLE001
        app.logger.warning("AI 总控风格同步到项目 config.json 失败（不影响设定应用）：%s", e)
        return False


def _ai_config_view() -> dict:
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.public_view(cfg)
    view["config_path"] = os.path.abspath(AI_CONFIG_PATH)
    view["legacy_path"] = os.path.abspath(LLM_CONFIG_PATH)
    view["modules_meta"] = ai_config.module_meta()
    # ComfyUI 地址如实下发：它由环境变量 COMFYUI_URL 决定，写进配置文件也没有任何
    # 代码读取（历史遗留的死配置）。前端据此只做只读展示，不再给一个「改了没用」的输入框。
    view["comfyui"] = {
        "url": COMFYUI_URL,
        "source": "环境变量 COMFYUI_URL",
        "editable": False,
    }
    # 网关熔断状态：上游整体挂掉时前端要能一眼看到「不是模型配错，是网关没算力」，
    # 否则用户会反复改 base_url/model 却越改越乱。
    view["gateways"] = {
        m: {"base_url": (v or {}).get("base_url") or "",
            "circuit": LLMClient.gateway_circuit_state((v or {}).get("base_url") or "")}
        for m, v in (view.get("modules") or {}).items()
    }
    return view


def _ai_guide_response(message: str, code: int = 400, module: str = "text"):
    label = AI_MODULE_LABEL.get(module, module)
    return jsonify({
        "success": False,
        "error": message,
        "code": "LLM_NOT_CONFIGURED",
        "module": module,
        "guide": (f"请点击顶部「AI 设置」→「{label}」，填写 ① base_url ② api_key ③ model，"
                  "先点「测试连接」通过后再点「保存」。三个模型相互独立配置，互不影响。"),
        "config": ai_config.public_view(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)),
    }), code


# =====================================================================
# AI 设置：文本分析 / 质检 / 对话总控 三个独立模块
# =====================================================================

@app.route('/api/ai/config', methods=['GET'])
def api_ai_config_get():
    """读取统一 AI 设置（三模块，api_key 一律脱敏）"""
    return jsonify({"success": True, "config": _ai_config_view()})


@app.route('/api/ai/config/reveal', methods=['GET'])
def api_ai_config_reveal():
    """按需回显某个模块已保存的 api_key 明文（前端「眼睛」按钮点开时调用）。

    默认 GET /api/ai/config 仍一律脱敏（module_public_view 永不含明文）；
    本端点只在用户显式点「显示」时被调用，把明文回填进输入框——否则已保存的
    密钥在前端只是 placeholder 圆点，切 type 什么都显不出来。
    本应用是本地单用户工具（仅 127.0.0.1），密钥明文本就只存在本机。
    """
    module = (request.args.get('module') or '').strip()
    if module not in ai_config.MODULES:
        return jsonify({"success": False, "error": f"未知的 AI 模块：{module}"}), 400
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, module)
    key = ep.get("api_key") or ""
    return jsonify({"success": True, "module": module,
                    "has_api_key": bool(key), "api_key": key})


def _ai_credentials_verify(module: str, base_url: str = "", model: str = "",
                           new_key: str = "", cleared: bool = False) -> tuple:
    """保存/清空后**读回核对**：任务实际读的那份凭证（tasks.db）是否等于本次提交的值。

    职责分工：写侧由 `ai_config.save_module` / `clear_module` 内部镜像闭合
    （任何调用方都自动同步，见 `ai_config._mirror_credentials_db` 的长注释）。
    这里**只做独立核对**，因为最危险的故障恰恰是「写没成功但接口照样返回 200」——
    磁盘满 / tasks.db 不可写 / env 覆盖，都会让「前端配的」与「任务实际用的」分叉，
    而页面上完全看不出来（用户会以为配置已生效，直到任务 401 或跑出别的账号的结果）。

    返回 `(note, error)`：`error` 非空 → 响应必须响亮告警。
    """
    try:
        import ai_credentials_db
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    if cleared:
        # module=None = 整体重置 → 三个模块都要核对（`get_credentials(None)` 会抛 ValueError）
        mods = [module] if module else list(AI_MODULES)
        stuck = []
        for m in mods:
            try:
                if ai_credentials_db.get_credentials(m).get("api_key"):
                    stuck.append(m)
            except Exception as e:  # noqa: BLE001
                return "", f"{type(e).__name__}: {e}"
        if stuck:
            return "", (f"AI 凭证库（tasks.db）中 {'、'.join(stuck)} 模块的密钥未被清空 → "
                        "任务仍会读到旧凭证，请检查 output/tasks.db 可写性后重试")
        return "，AI 凭证库已同步清空", ""

    try:
        db = ai_credentials_db.get_credentials(module)
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {e}"

    # env 覆盖是**设计内**的最高优先级（运维部署用），但它同样意味着「页面填的不作数」，
    # 必须明说而不是报成错误。
    try:
        import secret_store
        env_name = secret_store.ENV_KEY_MAP.get(f"ai.{module}") or ""
    except Exception:  # noqa: BLE001
        env_name = ""
    env_key = ((os.getenv(env_name) or "").strip() if env_name else "")
    if env_key and env_key != new_key:
        return (f"注意：{module} 模块密钥被环境变量 {env_name} 覆盖，"
                "任务实际使用的是该环境变量的值，不是页面上填写的值", "")

    def _norm(u):
        return (u or "").strip().rstrip("/").lower()

    diff = []
    if _norm(db.get("base_url")) != _norm(base_url):
        diff.append("base_url")
    if (db.get("model") or "") != (model or ""):
        diff.append("model")
    if new_key and (db.get("api_key") or "") != new_key:
        diff.append("api_key")
    if diff:
        return "", (f"AI 凭证库（tasks.db）与本次保存不一致（{'、'.join(diff)}）→ "
                    "任务可能仍用旧凭证。请检查 output/tasks.db 是否可写、磁盘是否已满后重试")
    return "，已写入 AI 凭证库（任务下次调用立即生效，无需重启）", ""


def _save_ai_module(data: dict):
    """保存单个 AI 模块的核心实现（/api/ai/config 与兼容路由 /api/llm/config 共用）"""
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False,
                        "error": f"unknown module：{module or '(空)'}，可选 {list(AI_MODULES)}"}), 400
    base_url = (data.get("base_url") or '').strip()
    model = (data.get("model") or '').strip()
    api_key = data.get("api_key")
    # 思考档位（可选项）：字段缺失 = 不改动；传空串 = 清空。非法值由 ai_config 归一化成 ""
    has_reasoning_effort = "reasoning_effort" in data
    reasoning_effort = data.get("reasoning_effort")
    if not base_url or not model:
        return jsonify({"success": False, "error": "base_url 与 model 均为必填项"}), 400

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    old = ai_config.get_module(cfg, module)
    key = "" if api_key is None else str(api_key).strip()
    keep = (not key) or ("*" in key)   # 留空或脱敏回显 → 不改动原密钥
    if keep and not old.get("api_key"):
        return jsonify({"success": False, "error": "该模块首次配置必须填写 api_key"}), 400

    cfg = ai_config.save_module(AI_CONFIG_PATH, module, base_url=base_url, model=model,
                                api_key=None if keep else key, legacy_path=LLM_CONFIG_PATH,
                                reasoning_effort=(reasoning_effort if has_reasoning_effort else None))
    # ⭐ 凭证单一事实源由 `ai_config.save_module` **内部**镜像闭合（json + 加密库 + tasks.db
    # 一次写完，见其 `_mirror_credentials_db` 长注释）—— 这里不再重复写库，只**读回核对**，
    # 避免同一份值有两个写点（将来谁改一处就会漂移）。核对能抓到「接口返回成功但写没落地」
    # 以及 env 覆盖这类页面上看不出来的分叉。
    db_note, db_error = _ai_credentials_verify(
        module, base_url=base_url, model=model,
        new_key="" if keep else key)
    if db_error:
        app.logger.error("AI 凭证库核对不通过（module=%s）：%s", module, db_error)
    # 配置刚变 → 清掉前置自检的端点探测缓存，避免出现「明明确认改好了，开跑还是被拦」
    try:
        import ai_selfcheck
        ai_selfcheck.reset_probe_cache()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("重置 AI 前置自检探测缓存失败（忽略）：%s", e)
    view = ai_config.module_public_view(ai_config.get_module(cfg, module))
    return jsonify({
        "success": True,
        "module": module,
        "module_config": view,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "credentials_db_updated": bool(not db_error),
        "credentials_db_error": db_error,
        "message": (f"{AI_MODULE_LABEL.get(module, module)}配置已保存"
                    + ("（api_key 保持不变）" if keep else "")
                    + db_note
                    + (f"；但凭证核对未通过：{db_error}" if db_error else "")),
    })


# （历史说明）保存 qc 模块曾靠 qc_client.set_endpoint 把密钥从 ai.qc 槽 best-effort 复制到
# qc 槽，任何绕过该路由的修改都会双槽漂移 → 401。现已由 ai_credentials_db（tasks.db）
# 单一事实源取代：qc_client.load_config 直接读 DB 的 qc 模块，旧桥接退役。


@app.route('/api/ai/config', methods=['POST'])
def api_ai_config_save():
    """保存单个模块：{module: text|qc|chat, base_url, model, api_key?}"""
    return _save_ai_module(request.json or {})


@app.route('/api/ai/config/clear', methods=['POST'])
def api_ai_config_clear():
    """清空单个模块；不传 module 则整体重置三个模块"""
    data = request.json or {}
    module = (data.get("module") or "").strip() or None
    if module and module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module}"}), 400
    # A-22（M5）：clear_module 有落盘副作用（清空模块配置 + 同步清密钥库 + **镜像清 DB**），
    # 必须保留调用；返回值此前被赋给 cfg 却从未使用（响应改由下方 _ai_config_view() 重新取整份视图），故去掉赋值。
    ai_config.clear_module(AI_CONFIG_PATH, module=module, legacy_path=LLM_CONFIG_PATH)
    # ⭐ 与「保存」对称：清库同样由 `ai_config.clear_module` 内部闭合（`_mirror_credentials_db(
    # clear=True)`），这里只**读回核对** —— 避免「AI 设置显示已清空，任务却仍读到 DB 旧凭证」
    # 这种页面上看不出来的漂移（清库失败会让任务继续用旧密钥跑）。
    db_clear_note, db_clear_error = _ai_credentials_verify(module, cleared=True)
    if db_clear_error:
        app.logger.error("AI 凭证库清空核对不通过（module=%s）：%s", module, db_clear_error)
    reset_note, reset_error = "", ""
    if module in (None, "qc"):
        try:
            qc_client.reset_endpoint(QC_CONFIG_PATH)
            reset_note = "，质检接口已同步重置"
        except Exception as e:  # noqa: BLE001
            reset_error = f"{type(e).__name__}: {e}"
            app.logger.warning(f"质检接口重置失败（AI 设置已清空，质检可能仍用旧接口）：{reset_error}")
    return jsonify({
        "success": True,
        "module": module,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "credentials_db_cleared": bool(not db_clear_error),
        "credentials_db_error": db_clear_error,
        "qc_reset": bool(module in (None, "qc") and not reset_error),
        "qc_reset_error": reset_error,
        "message": ((f"{AI_MODULE_LABEL.get(module, module)}配置已清除" if module else "AI 设置已整体重置")
                    + db_clear_note
                    + reset_note
                    + (f"；但凭证清空核对未通过：{db_clear_error}" if db_clear_error else "")
                    + (f"；但质检接口重置失败：{reset_error}" if reset_error else "")),
    })


@app.route('/api/ai/selfcheck', methods=['GET'])
def api_ai_selfcheck():
    """AI 前置自检（P0-5）：三个模块（text/qc/chat）的配置完整性 + 可选端点可达性。

    query:
      probe=1  附带端点可达性探测（GET {base_url}/models，结果带 60s 缓存）
      fresh=1  强制绕过探测缓存（刚改完配置时用）

    返回的 `ok=False` 即「当前配置一开跑就会失败」，前端据此在 AI 设置页 / 项目页
    显示红字阻断提示；`modules[*].hint` 给出逐模块的修复指引。
    """
    _probe = str(request.args.get("probe") or "").strip().lower() in ("1", "true", "yes", "on")
    _fresh = str(request.args.get("fresh") or "").strip().lower() in ("1", "true", "yes", "on")
    try:
        import ai_selfcheck
        rep = ai_selfcheck.check_modules(probe=_probe, force_probe=_fresh)
        _gate_off = ai_selfcheck.gate_disabled()
    except Exception as e:  # noqa: BLE001  自检失败按 500 明确报出，不假装通过
        return jsonify({"success": False,
                        "error": f"自检执行失败：{type(e).__name__}: {e}"}), 500
    return jsonify({
        "success": True,
        "ok": rep["ok"],
        "probed": rep["probed"],
        "message": rep["message"],
        "hint": rep["hint"],
        "blocked_modules": rep["blocked_modules"],
        "blocked_labels": rep["blocked_labels"],
        "modules": rep["modules"],
        "gate_off": _gate_off,
    })


@app.route('/api/ai/test', methods=['POST'])
def api_ai_test():
    """测试单个模块连通性（可用页面暂存参数，不落盘）。

    - text / chat：文本连通性对话测试
    - qc：默认做「视觉能力」探测（发一张极小图片），确认模型支持图像输入；
          也可传 probe="text" 只测文本连通性。
    """
    data = request.json or {}
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module or '(空)'}"}), 400

    # 用户点了「测试连接」= 刚改完配置想验证 → 必须清掉旧熔断。
    # 否则网关恢复/换好网关后，这里会被上一轮的熔断状态直接拦下并报"不可用"，
    # 用户会以为新配置也没用。
    LLMClient.reset_gateway_circuits()
    # 同理清掉前置自检的端点探测缓存：测试通过后马上开跑，门禁不该还拿着改之前的旧结论
    try:
        import ai_selfcheck
        ai_selfcheck.reset_probe_cache()
    except Exception as e:  # noqa: BLE001
        app.logger.debug("重置 AI 前置自检探测缓存失败（忽略）：%s", e)

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    base_url = (data.get("base_url") or saved["base_url"] or "").strip()
    model = (data.get("model") or saved["model"] or "").strip()
    api_key = data.get("api_key")
    if not api_key or not str(api_key).strip() or "*" in str(api_key):
        api_key = saved["api_key"] or ""
    # 思考档位：页面暂存值优先，否则沿用已保存值（必须带上，见 _ai_client_for_module 的说明）
    _re = data.get("reasoning_effort")
    if _re is None:
        _re = saved.get("reasoning_effort") or ""
    ep = {"base_url": base_url, "api_key": str(api_key).strip(), "model": model,
          "reasoning_effort": str(_re).strip()}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return jsonify({"success": False, "module": module, "endpoint": {k: v for k, v in ep.items() if k != "api_key"},
                        "error": "base_url / api_key / model 均为必填，请填写完整后再测试"}), 400

    probe = (data.get("probe") or ("vision" if module == "qc" else "text")).strip()
    if module == "qc" and probe == "vision":
        result = qc_client.test_vision(ep, timeout=int(data.get("timeout") or 60))
        result.update({"module": module, "probe": "vision", "model": ep["model"],
                       "base_url": ep["base_url"]})
        if not result.get("success"):
            result["guide"] = ("该接口或模型不支持图像输入（或不可达）。质检需要多模态模型，"
                               "请改用支持视觉的模型（如 gpt-4o-mini / qwen-vl-max / glm-4v）。")
        return jsonify(result), (200 if result.get("success") else 400)

    try:
        # ⭐ P0-5 第②项收尾：探针与运行态**必须走同一条客户端构造路径**。
        # 此前这里自建 LLMClient(AI_CONFIG_PATH, config=ep)，运行态走 _ai_client_for_module，
        # 两边各写一份「参数取谁 / reasoning_effort 怎么注入 / 默认超时多少」的推导 ——
        # 一旦分叉就是「测试连接通过、运行时行为不一致」的经典温床。现在只此一条路径。
        client = _ai_client_for_module(
            module, base_url=ep["base_url"], api_key=ep["api_key"], model=ep["model"],
            timeout=int(data.get("timeout") or 60), reasoning_effort=ep["reasoning_effort"])
        result = dict(client.test_connection() or {})
    except LLMError as e:
        # 与运行态同一个报错：未配置时的提示语完全一致，不再出现"测试说没问题、跑起来报未配置"
        result = {"success": False, "error": str(e)}
    except LLMGatewayUnavailable as e:
        # 网关整体不可用：明确区分于「密钥写错」
        result = {"success": False, "verdict": "gateway_unavailable",
                  "error": str(e), "hint": e.hint, "streak": e.streak,
                  "guide": "连通测试已跳过重试（上游报无可用算力时重试无意义）。"
                           "请更换 base_url 或模型，本项目的 AI 设置是三个模块各自独立的。"}
    except Exception as e:  # noqa: BLE001
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}
    result.update({"module": module, "probe": "text", "model": ep["model"], "base_url": ep["base_url"],
                   "circuit": LLMClient.gateway_circuit_state(ep["base_url"])})
    return jsonify(result), (200 if result.get("success") else 400)


# ---- 兼容旧接口：/api/llm/config 系列（等价于「文本分析模型」模块） ----

@app.route('/api/llm/config', methods=['GET'])
def api_llm_config_get():
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
    view["guide"] = None if view["configured"] else LLM_NOT_CONFIGURED_GUIDE
    view["deprecated"] = "该接口为兼容旧前端保留，等价于 /api/ai/config 的 text 模块"
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(AI_CONFIG_PATH)})


@app.route('/api/llm/config', methods=['POST'])
def api_llm_config_save():
    """兼容旧前端：强制保存 text 模块（= 文本分析模型 = LLM 引擎）

    旧实现是 `data["module"]="text"; return api_ai_config_save()`，
    但 `api_ai_config_save` 内部会重新从 `request.json` 取值，本地 dict 的修改
    完全无效 —— 结果是旧接口永远存不进 text 模块（调用方不传 module 时直接报
    "unknown module：(空)"）。这里改为把改写后的 data 显式传进共用的实现。
    """
    data = dict(request.json or {})
    data["module"] = "text"
    return _save_ai_module(data)


@app.route('/api/llm/config/clear', methods=['POST'])
def api_llm_config_clear():
    cfg = ai_config.clear_module(AI_CONFIG_PATH, module="text", legacy_path=LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(AI_CONFIG_PATH),
                    "message": "文本分析模型配置已清除"})


@app.route('/api/llm/test', methods=['POST'])
def api_llm_test():
    data = request.json or {}
    data["module"] = "text"
    data.setdefault("probe", "text")
    return api_ai_test()


# =====================================================================
# AI 对话（创作总控）：多轮对话敲定创作设定 →「应用设定」落盘
# =====================================================================

# 归档根目录注入：ai_chat.load_all_messages 未显式传 root 时用它（见 ai_chat.set_archive_root）
ai_chat.set_archive_root(os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH)))


def _chat_project(data: dict = None, history: dict = None) -> str:
    data = data or {}
    # ⚠️ Ưu tiên request field (project_name hoặc project), sau đó mới fallback history
    name = (data.get("project_name") or data.get("project") or "").strip()
    if not name and isinstance(history, dict):
        name = (history.get("active_project") or "").strip()
    return ai_chat.project_key(name or "default")


def _chat_state(project: str = "") -> dict:
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = ai_chat.project_key(project or history.get("active_project") or "default")
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    model_view = ai_config.module_public_view(ai_config.get_module(cfg, "chat"))
    return {
        "project_name": project,
        "messages": ai_chat.project_messages(history, project),
        "draft": ai_chat.get_draft(history, project),
        "settings": ai_chat.settings_view(AI_SETTINGS_PATH, project),
        "fields": ai_chat.fields_meta(),
        "model": model_view,
        "history_file": os.path.abspath(AI_CHAT_HISTORY_PATH),
        "settings_file": os.path.abspath(AI_SETTINGS_PATH),
    }


@app.route('/api/ai/chat/history', methods=['GET'])
def api_ai_chat_history():
    """读取会话历史 + 当前草稿 + 已生效设定（供界面恢复）"""
    project = (request.args.get("project") or "").strip()
    return jsonify({"success": True, "state": _chat_state(project)})


def _archive_project_key(raw: str, history: dict) -> str:
    """归档接口的项目键：**看原始入参**决定是否回退到活跃项目。

    ⚠️ 不能用 `_safe_project(x) or <兜底>` 判空：`_safe_project('')` 返回字面量
    `'project'`（真值），兜底永不生效。必须看原始 query 是否为空。
    """
    raw = (raw or "").strip()
    if raw:
        return ai_chat.canonical_project_key(raw)
    return str(history.get("active_project") or "")


@app.route('/api/ai/chat/archive', methods=['GET'])
def api_ai_chat_archive():
    """列出某项目归档的日期与每日条数（只读；全量真相源的浏览入口）。

    query: project（可空 → 回退当前活跃项目）
    → {success, project, dates:[{date,count}], total}
    """
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    key = _archive_project_key(request.args.get("project") or "", history)
    if not key:
        return jsonify({"success": True, "project": "", "dates": [], "total": 0})
    root = os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH))
    dates = ai_chat.archive_dates(root, key)
    return jsonify({"success": True, "project": key, "dates": dates,
                    "total": sum(int(d.get("count") or 0) for d in dates)})


@app.route('/api/ai/chat/archive/<date>', methods=['GET'])
def api_ai_chat_archive_date(date):
    """读取某项目某天的归档消息（只读分页）。

    query: project（可空 → 回退当前活跃项目）/ limit（默认 0=全部）/ offset
    → {success, date, total, messages:[...]}
    """
    date = str(date or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return jsonify({"success": False, "error": "日期格式应为 YYYY-MM-DD"}), 400
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    key = _archive_project_key(request.args.get("project") or "", history)
    if not key:
        return jsonify({"success": True, "date": date, "total": 0, "messages": []})
    try:
        limit = max(0, int(request.args.get("limit", 0)))
    except (TypeError, ValueError):
        limit = 0
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    root = os.path.dirname(os.path.abspath(AI_CHAT_HISTORY_PATH))
    messages = ai_chat.load_archive(root, key, date, limit=limit, offset=offset)
    return jsonify({"success": True, "date": date,
                    "total": ai_chat.archive_count(root, key, date),
                    "messages": messages})


@app.route('/api/ai/chat/clear', methods=['POST'])
def api_ai_chat_clear():
    """清空会话（默认保留创作设定草稿与已生效设定）"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    history = ai_chat.clear_history(AI_CHAT_HISTORY_PATH,
                                    keep_settings=bool(data.get("keep_settings", True)),
                                    project=project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
    return jsonify({"success": True, "message": "会话已清空", "state": _chat_state(project)})


@app.route('/api/ai/chat', methods=['POST'])
def api_ai_chat():
    """一轮对话：调用「对话总控模型」，返回回复并同步更新创作设定草稿"""
    data = request.json or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message 不能为空"}), 400
    if len(message) > ai_chat.MAX_CHARS_PER_MESSAGE:
        message = message[:ai_chat.MAX_CHARS_PER_MESSAGE]

    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    history["active_project"] = project
    ai_chat.append_message(history, "user", message, project)

    draft = ai_chat.get_draft(history, project)
    if isinstance(data.get("draft"), dict) and data["draft"]:
        draft = ai_chat.merge_settings(draft, data["draft"])       # 界面手工补充的设定

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, "chat")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        ai_chat.drop_last_message(history, project)               # 未配置则不落用户消息，避免脏历史
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({
            "success": False,
            "error": "「对话总控模型」尚未配置（base_url / api_key / model）",
            "guide": LLM_NOT_CONFIGURED_GUIDE_MAP["chat"],
            "need_config": True,
            "state": _chat_state(project),
        }), 400

    messages = ai_chat.build_messages(history, draft, project)
    try:
        client = LLMClient(AI_CONFIG_PATH, config=ep, timeout=LLM_REQUEST_TIMEOUT)
        reply = client.chat(messages, temperature=0.7, max_tokens=2048)
    except LLMError as e:
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": f"对话总控模型调用失败：{e}",
                        "state": _chat_state(project)}), 400
    except Exception as e:  # noqa: BLE001
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": f"对话总控模型调用异常：{e}",
                        "state": _chat_state(project)}), 500

    new_settings = ai_chat.extract_settings(reply)
    if new_settings:
        draft = ai_chat.merge_settings(draft, new_settings)
        ai_chat.set_draft(history, project, draft)
    display = ai_chat.strip_json_block(reply)
    ai_chat.append_message(history, "assistant", display, project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    state = _chat_state(project)
    return jsonify({"success": True, "reply": display, "raw_reply": reply,
                    "new_settings": new_settings, "draft": draft,
                    "model": {"base_url": ep["base_url"], "model": ep["model"]},
                    "state": state})


@app.route('/api/ai/chat/apply', methods=['POST'])
def api_ai_chat_apply():
    """应用设定：把当前草稿（可叠加 patch）落盘为项目创作设定配置"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    draft = ai_chat.get_draft(history, project)
    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    merged = ai_chat.merge_settings(draft, patch)
    if not merged:
        return jsonify({"success": False, "error": "当前没有任何已确认的创作设定，无法应用",
                        "state": _chat_state(project)}), 400

    ai_chat.save_project_settings(AI_SETTINGS_PATH, project, merged)
    ai_chat.set_draft(history, project, merged)
    history["active_project"] = project
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    # 同步风格到项目 config.json：否则 config.json 的 style 停在建项目时的默认值
    # （如 3D动漫渲染），与总控刚敲定的设定不一致，用户会以为「设定没生效」。
    state = _chat_state(project)
    _sync_project_config_style(project, ai_chat.style_brief(state.get("settings") or {}))
    # ⚠️ normalize_settings 只保留白名单字段，其余**静默丢弃**。
    # 模型自造键名（实测出现过 color_tone / camera_language）时，
    # 用户以为「冷色调、克制镜头」已经写进去了，落盘却只剩 style —— 白沟通一场。
    # 这里把被丢弃的键如实回传，总控才能纠正键名并如实告知用户。
    dropped = sorted(k for k in (patch or {}).keys()
                     if k not in ai_chat.FIELD_LABELS)
    message = f"创作设定已应用（{len(merged)} 项）"
    if dropped:
        message += (f"；以下字段名不被支持，已忽略：{'、'.join(dropped)}"
                    "（合法字段见 allowed_fields，请用合法键名重试）")
    return jsonify({
        "success": True,
        "message": message,
        "dropped_fields": dropped,
        "allowed_fields": list(ai_chat.FIELD_LABELS.keys()),
        "settings": state["settings"],
        "draft": merged,
        "state": state,
    })


@app.route('/api/ai/chat/draft', methods=['POST'])
def api_ai_chat_draft():
    """直接补充 / 修改创作设定草稿（不调用模型，供界面表单编辑）"""
    data = request.json or {}
    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    project = _chat_project(data, history)
    patch = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    if data.get("replace"):
        history.setdefault("drafts", {})[project] = ai_chat.normalize_settings(patch)
    else:
        ai_chat.set_draft(history, project, ai_chat.merge_settings(
            ai_chat.get_draft(history, project), patch))
    history["active_project"] = project
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
    return jsonify({"success": True, "state": _chat_state(project)})


@app.route('/api/ai/chat/settings', methods=['GET'])
def api_ai_chat_settings():
    """当前已生效的创作设定（供界面查看，也供剧本 / 提示词 / 分镜链路引用）"""
    project = (request.args.get("project") or "").strip()
    return jsonify({"success": True, "settings": ai_chat.settings_view(AI_SETTINGS_PATH, project),
                    "settings_file": os.path.abspath(AI_SETTINGS_PATH)})


@app.route('/api/ai/settings', methods=['GET', 'POST'])
def api_ai_settings():
    """统一读取/保存创作设定（简版）"""
    project = (request.args.get("project") or "").strip()

    if request.method == 'POST':
        data = request.json or {}
        message = "设置已保存"

        # 「LLM 引擎」= 「文本分析模型」——同一个模块、同一份配置。
        # 后端自己也标注了：/api/llm/config 返回的 `deprecated` 字段写着
        # 「等价于 /api/ai/config 的 text 模块」。
        #
        # 旧实现在这里调 `load_llm_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)`，
        # 但这个别名来自 **llm_client**（只需 1 个 path 参数），却被按 ai_config 的
        # 2 参签名调用 → TypeError → 整个「保存系统设置」按钮必然 500
        # （实测报错：load_config() takes 1 positional argument but 2 were given）。
        # 现在统一落到 text 模块，不再往 llm_config 形态的扁平键里写死配置。
        if 'llm_api_key' in data or 'llm_provider' in data:
            cur = ai_config.get_module(ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH), "text")
            key = str(data.get('llm_api_key') or '').strip()
            if key and '*' not in key and cur.get("base_url") and cur.get("model"):
                ai_config.save_module(AI_CONFIG_PATH, "text",
                                      base_url=cur["base_url"], model=cur["model"],
                                      api_key=key, legacy_path=LLM_CONFIG_PATH)
                message = "LLM 引擎密钥已更新（与「文本分析模型」是同一份配置）"
            else:
                message = ("「LLM 引擎」就是「文本分析模型」，"
                           "请在上方「文本分析模型」卡片里填写 base_url / model / api_key")

        # ComfyUI 地址：全项目只有这里写、没有任何地方读（各 ComfyUI 客户端都直接取
        # 模块级常量 COMFYUI_URL，来源是环境变量）。所以旧实现只是「假装保存成功」。
        # 这里如实告知，避免用户以为改了地址就生效。
        if 'comfyui_url' in data:
            message = (f"ComfyUI 地址由环境变量 COMFYUI_URL 决定，当前为 {COMFYUI_URL}；"
                       "如需修改请改环境变量后重启服务")

        # Save watermark config if provided
        if 'watermark_enabled' in data or 'watermark_text' in data:
            wm_cfg = _wm_load_cfg()
            if 'watermark_enabled' in data:
                wm_cfg['enabled'] = data['watermark_enabled']
            if 'watermark_text' in data:
                wm_cfg['text'] = data['watermark_text']
            video_watermark.save_config(WATERMARK_CONFIG_PATH, wm_cfg)
            message = "水印设置已保存"

        return jsonify({"success": True, "message": message})

    view = ai_chat.settings_view(AI_SETTINGS_PATH, project)
    return jsonify({"success": True, "project_name": view.get("project_name"),
                    "active": view.get("active"), "settings": view.get("settings"),
                    "style_brief": view.get("style_brief"),
                    "settings_file": view.get("settings_file")})


# =====================================================================
# 新增模块 C：图片 / 视频 AI 质检（可开关、不达标自动重生成）
# =====================================================================

def _qc_load_cfg() -> dict:
    return qc_client.load_config(QC_CONFIG_PATH)


def _qc_brief(kind: str) -> dict:
    """任务级质检摘要（随状态接口下发，供前端展示徽标与开关状态）"""
    try:
        cfg = _qc_load_cfg()
        view = qc_client.public_view(cfg)
        active = view["image_qc_active"] if kind == "image" else view["video_qc_active"]
        return {"enabled": view["enabled"], "active": active,
                "declared": bool(view["enabled"] and (cfg.get("image_enabled") if kind == "image"
                                                      else cfg.get("video_enabled"))),
                "pass_score": cfg.get("pass_score"), "max_retries": cfg.get("max_retries"),
                "video_frame_count": cfg.get("video_frame_count"),
                "model": view["effective_model"], "endpoint_source": view["endpoint_source"]}
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检摘要生成失败（忽略）：{e}")
        return {"enabled": False, "active": False, "declared": False}


def _qc_test_override(data: dict) -> dict:
    """测试用的临时接口参数（不落盘）；返回空 dict 表示完全用已保存配置"""
    ep = {k: str(data.get(k) or "").strip() for k in ("base_url", "model")}
    key = str(data.get("api_key") or "").strip()
    if "*" in key:
        key = ""          # 脱敏回显 → 用已保存密钥
    ep["api_key"] = key
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {}
    return ep


class _PromptQCBlocked(RuntimeError):
    """提示词预检未通过（内部信号）

    批处理循环里每个镜头是一大段嵌套代码，用异常跳出比「把生成段整体再缩进一层」
    安全得多；异常会被同一层的 ``except Exception`` 接住，该镜头照常记入 manifest
    （状态为失败），不会从产物清单里消失。
    """


def _prompt_preflight(kind: str, prompt: str, *, ctx=None, style: str = "",
                      ref_count=None, expect_refs=None, project_name: str = "",
                      cfg: dict = None) -> tuple:
    """生成前提示词预检 + 确定性自愈（统一入口）。

    返回 ``(应交给生成端的提示词, preflight 结果, 闸门结论)``。

    ``project_name``：可选；给出时会在预检**之前**先召回 ``kind="prompt"`` 的历史教训
    并叠加，把「历史上被预检判死的输入模式」变成显式补丁后再进预检（US-7）。

    ``cfg``：G13（P1）可选，传入 worker 级已读取的质检配置则直接复用，避免逐镜再
    触发一次 ``_qc_load_cfg()``（读 JSON + Fernet 解密）；不传则按需自读（向后兼容）。

    ⚠️ **一律 fail-open**：预检自身异常时按「原样放行」处理并记 warning。
    这一层是新增的保险，绝不能因为它自己出问题就把整集生产卡死。
    """
    text = str(prompt or "")
    try:
        cfg = cfg if cfg is not None else _qc_load_cfg()
        if project_name:
            # 召回叠加在原始提示词上（自愈前）；调用方已保留 orig_prompt 作稳定 phash 键。
            text = prompt_memory.learned_prompt(
                kind="prompt", prompt=text, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=style)
        pf = prompt_qc.preflight(kind, text, ctx=ctx, style=style, cfg=cfg,
                                 ref_count=ref_count, expect_refs=expect_refs)
        gate = prompt_qc.prompt_qc_gate(pf, cfg)
        if pf.get("repairs") or pf["verdict"].get("issues"):
            app.logger.info(
                "提示词预检[%s] %s｜自愈 %s 项｜issue %s 项｜accept=%s",
                kind, pf.get("label"), len(pf.get("repairs") or []),
                len(pf["verdict"].get("issues") or []), pf.get("accept"))
        return pf["prompt"], pf, gate
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"提示词预检异常（{kind}），按放行处理：{e}")
        skip = {"ok": False, "skipped": True, "accept": True, "blocked": False,
                "label": "提示词预检异常", "reason": str(e), "repairs": [],
                "verdict": {"issues": [], "critical_issues": [], "reason": str(e)}}
        return text, skip, {"accept": True, "blocked": False, "skipped": True,
                            "label": "提示词预检异常", "reason": str(e),
                            "critical_issues": [], "repairs": []}


def _qc_repeat_features(rec: dict) -> frozenset:
    """取一条质检记录的「缺陷特征集合」，用于判断连续两次重试是否毫无变化。

    G1 修复：委托给 qc_client.qc_retry_features（共享叶子模块），app 侧再叠加
    style_issues 维度（app 特有的风格缺陷，不进入通用模块）。
    seed 与 score 每次都会变（换了种子必然抖动），不能当特征，否则永远判不出「没变化」。
    """
    base = qc_client.qc_retry_features(rec)
    extra = set()
    for item in (rec.get("style_issues") or []):
        text = str(item).strip()
        if text:
            extra.add(text[:200])
    return frozenset(base | extra)


def _qc_retry_hopeless(attempts: list, streak: int = 2) -> tuple:
    """连续 ``streak`` 次重试的缺陷特征**完全相同** → 判定「改提示词 + 换种子」没有产生
    任何变化，继续重试只是重复烧 GPU（实测 ep02 shot_13 连烧 6 次全败，缺陷一字不差）。

    返回 ``(True, "缺陷摘要")`` 或 ``(False, "")``。

    G1 修复：把本函数抽到 qc_client.qc_retry_hopeless（共享叶子模块），
    供 comfyui_client / keyframe 以注入回调（qc_stop_cb）方式复用，
    规避 app ↔ comfyui_client 的循环依赖。app 侧把 style_issues 合并进
    issues 再委托 —— 通用模块不认识 app 特有字段。

    ⚠️ 刻意保守（宁可多试一次，也不要误停）：
      · 特征为**空**时一律不判定 —— 质检没给出可用信息 ≠ 缺陷相同；
      · 必须最近 streak 条**逐条集合相等**（多一条少一条都不算）；
      · **不改变闸门结论**：该镜仍算未通过、仍不进正式目录，只是不再继续重试；
        用户可直接改这一镜的剧本字段（如 camera / description）后单独重跑该镜。
    """
    merged = []
    for r in (attempts or []):
        if not isinstance(r, dict):
            continue
        issues = list(r.get("issues") or [])
        style = list(r.get("style_issues") or [])
        if style:
            issues = issues + style
        merged.append({"issues": issues, "critical_issues": r.get("critical_issues") or []})
    return qc_client.qc_retry_hopeless(merged, streak)


def _qc_prune_attempts(scratch_dir: str, keep: int = 4) -> None:
    """G8：质检暂存区 try 产物滚动保留 —— 每个 shot/asset 只保留最近 keep 个尝试。

    背景（审计 G8）：图片链路每次都把产物 copy 进暂存区 `output/qc/<项目>/{assets,storyboard,
    video}_scratch/`，不达标越多残留越多，实测 qc 目录膨胀到 1.1GB。临时产物（`*_tryN`）
    只有「最近几轮」对续跑/排障有意义，更早的纯浪费。这里按 (前缀, 尝试号, 扩展名) 归组，
    每组只留最大的 keep 个 try，其余删除。

    设计取舍：
      - 只清「带 _try 后缀的临时产物」，正式交付图（base.png/shot_XX.png）绝不动；
      - 单文件失败静默跳过（清理是优化而非功能，绝不能因清错文件阻断生产）；
      - 保留策略对 `.png`/`.mp4`/`.srt`/`.list` 通用，三类暂存区都能复用。
    """
    if not os.path.isdir(scratch_dir):
        return
    try:
        import re as _re
        groups = {}   # (前缀, 扩展名) -> [尝试号]
        info = {}      # 尝试号 -> 完整路径
        for fn in os.listdir(scratch_dir):
            # ⚠️ 单镜重跑落盘名是 `shot_NN_retry.png`（**无** `_tryN` 数字后缀），
            # 旧正则 `^(.+)_try(\d+)(\.\w+)$` 匹配不到 → 该文件永不被滚动清理、只增不减。
            # 这里把 `_retry` 也纳入：归到 num=0（比任何 `_tryN` 都旧 → 优先被清）。
            m = _re.match(r"^(.+)_try(\d+)(\.\w+)$", fn)
            if not m:
                m = _re.match(r"^(.+)_retry(\.\w+)$", fn)
                if m:
                    prefix, num, ext = m.group(1), 0, m.group(2)
                else:
                    continue   # 非 try/retry 命名（正式产物/杂项）一律不动
            else:
                prefix, num, ext = m.group(1), int(m.group(2)), m.group(3)
            groups.setdefault((prefix, ext), []).append(num)
            info[(prefix, ext, num)] = os.path.join(scratch_dir, fn)
        removed = 0
        for (prefix, ext), nums in groups.items():
            nums = sorted(nums)   # P2-2（A-15）：listdir 无序，必须按尝试号排序，
                                  # 否则 `nums[-keep:]` 保留的是「最早遍历到」而非「最新尝试号」
            keep_set = set(nums[-keep:]) if len(nums) > keep else set(nums)
            for num in nums:
                if num in keep_set:
                    continue
                p = info.get((prefix, ext, num))
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass   # 文件被占用/权限问题：跳过，不阻断
        if removed:
            app.logger.info("G8 暂存区滚动清理 %s：删 %d 个过期 try（每组保留最近 %d）",
                            scratch_dir, removed, keep)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("G8 暂存区清理失败（不影响生产）：%s: %s",
                           type(e).__name__, e)


def _write_artifact_meta(artifact_path: str, *, kind: str, project_name: str,
                         seed=None, prompt=None, workflow_key=None,
                         elapsed=None, qc=None, shot_id=None, asset_name=None,
                         extra=None) -> None:
    """O2：产物旁路元数据 —— 在正式产物旁写 `<产物>.meta.json`（可追溯/可复现）。

    记录：seed / prompt / 工作流文件名 + SHA256（内容指纹，而非仅文件名）/ 耗时 /
    生效质检结论。此前 manifest 只记工作流**文件名**，无法校验"当初到底用哪版工作流
    出的这张图"；SHA256 让产物与生成时点的工作流内容一一对应。

    纯旁路（绝不阻断生产）：任何异常静默降级、只留 debug 日志 —— meta 缺失不影响主流程。
    """
    try:
        import hashlib
        import config as _cfg
        meta = {
            "artifact": os.path.basename(artifact_path),
            "kind": kind,
            "project": project_name,
            "seed": seed,
            "prompt": (str(prompt)[:2000] if prompt else None),
            "workflow": None,
            "workflow_sha256": None,
            "elapsed_sec": elapsed,
            "qc": qc,
            "extra": extra,
            "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if shot_id is not None:
            meta["shot_id"] = shot_id
        if asset_name is not None:
            meta["asset"] = asset_name
        # 工作流内容指纹（O2 核心：文件名 → 名 + SHA256）
        if workflow_key:
            try:
                tpl_name = _cfg.WORKFLOW_TEMPLATE.get(workflow_key)
                if tpl_name:
                    wf_path = os.path.join(_cfg.COMFYUI_WORKFLOWS_DIR, tpl_name)
                    if os.path.isfile(wf_path):
                        h = hashlib.sha256()
                        with open(wf_path, "rb") as _f:
                            for _chunk in iter(lambda: _f.read(65536), b""):
                                h.update(_chunk)
                        meta["workflow"] = tpl_name
                        meta["workflow_sha256"] = h.hexdigest()
            except Exception as e:  # noqa: BLE001  工作流指纹算不出不影响 meta 主体
                app.logger.warning("工作流指纹计算失败（不影响 meta 主体）：%s", e)
        out = os.path.splitext(artifact_path)[0] + ".meta.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001  旁路兜底：meta 写失败绝不阻断入库
        try:
            app.logger.debug("O2 产物元数据旁路写失败（不影响入库）：%s: %s",
                            type(e).__name__, e)
        except Exception:  # noqa: BLE001
            pass


def _qc_gate(verdict: dict) -> dict:
    """统一质检入库闸门（P0）：ok=false 或 不达标 一律不得静默入库。
    - skipped=True   → 质检未执行（总开关/类型开关关闭、接口未配置），按「放行」处理并明确标注；
    - ok=False       → 质检调用异常，结果不可判定，一律阻断（不得静默入库）；
    - accepted=False → 不通过；命中关键缺陷时 blocked=True（关键缺陷阻断）。

    P0 加固（不盲信 verdict.accepted）：无论上游 verdict 的 accepted / passed / blocked
    给什么值，本函数都会用本地关键缺陷词表对 issues（并集上游显式 critical_issues）做一次
    独立复核；一旦命中关键缺陷（画面崩坏 / 拼接 / 人物重复 等），**强制阻断**，不得因为
    模型自评 accepted=True 而放行。放行条件是 accepted 与 passed **同时**为真（任一为假即阻断）。
    """
    verdict = verdict or {}
    if verdict.get("skipped"):
        return {"accept": True, "blocked": False, "skipped": True, "label": "质检未执行",
                "reason": verdict.get("reason") or "质检未执行（跳过）", "critical_issues": []}
    if not verdict.get("ok"):
        # P0-2：区分「接口级故障」与「内容不合格」。
        # interface_fault=True（鉴权 401/403、超时、网络抖动、未配置 key）= 根本没拿到
        # 模型判定，**不等于**产物不合格——ComfyUI 已出好的图/片不能因质检 key 失效被丢弃。
        # 此时 fail-open：放行已产出资产 + 响亮告警；但若客观层已命中致命缺陷
        # （全黑/无音轨等确定性闸门，见 critical_issues）仍强制阻断，不放行真坏帧。
        if verdict.get("interface_fault"):
            crit = [str(x) for x in (verdict.get("critical_issues") or [])]
            if crit:
                return {"accept": False, "blocked": True, "skipped": False,
                        "fault_open": False, "label": "客观层致命缺陷（AI 质检接口不可用）",
                        "reason": "；".join(crit[:3]), "critical_issues": crit}
            app.logger.warning(
                "质检接口故障，已 fail-open 放行本资产（结果不可判定）：%s",
                verdict.get("error") or "质检接口不可用")
            return {"accept": True, "blocked": False, "skipped": False,
                    "fault_open": True, "label": "质检接口故障·已放行",
                    "reason": (verdict.get("error") or "质检接口不可用")
                              + "（接口级故障，未做内容判定，已放行）",
                    "critical_issues": []}
        return {"accept": False, "blocked": True, "skipped": False, "label": "质检调用异常",
                "reason": verdict.get("error") or "质检调用失败，结果不可判定", "critical_issues": []}

    # ---- 独立复核：本地词表命中 ∪ 上游显式 critical_issues（不依赖模型自评结论） ----
    local_hits = qc_client.find_critical_issues(verdict.get("issues") or [])
    declared = [str(x) for x in (verdict.get("critical_issues") or [])]
    crit = list(dict.fromkeys(list(local_hits) + declared))
    if crit:
        app.logger.warning(f"质检闸门独立复核命中关键缺陷，强制阻断：{crit[:3]}")
        return {"accept": False, "blocked": True, "skipped": False,
                "label": "关键缺陷阻断（独立复核）" if local_hits else "关键缺陷阻断",
                "reason": verdict.get("reason") or ("命中关键缺陷：" + "；".join(crit[:3])),
                "critical_issues": crit}

    accepted = verdict.get("accepted")
    passed = verdict.get("passed")
    if accepted is None:
        accepted = bool(passed)
    if passed is None:
        passed = bool(accepted)
    if not (bool(accepted) and bool(passed)):
        if verdict.get("style_mismatch"):
            return {"accept": False, "blocked": bool(verdict.get("blocked")),
                    "skipped": False, "style_blocked": True, "label": "风格不达标",
                    "reason": verdict.get("reason") or "画面风格与目标风格不符",
                    "critical_issues": [],
                    "style_issues": verdict.get("style_issues") or []}
        return {"accept": False, "blocked": bool(verdict.get("blocked")), "skipped": False,
                "label": "质检不达标", "reason": verdict.get("reason") or "质检未达标",
                "critical_issues": []}
    return {"accept": True, "blocked": False, "skipped": False, "label": "质检达标",
            "reason": verdict.get("reason") or "", "critical_issues": []}


def _qc_record_verdict(project_name: str, kind: str, shot_key, stage: str,
                       attempt: int, seed, file_path: str, verdict: dict,
                       extra: dict = None, style: str = "") -> dict:
    """把一次质检结论整理成历史记录并落盘，返回该记录（含 history_file）

    style：本次质检所用的目标风格串。连同 style_mismatch / style_issues 一并落盘，
    供教训库识别「风格不达标」并触发改写提示词重生成。
    """
    rec = {"attempt": attempt, "seed": seed, "file": file_path, "stage": stage,
           "ok": bool(verdict.get("ok")), "passed": bool(verdict.get("passed")),
           "accepted": bool(verdict.get("accepted") if verdict.get("accepted") is not None
                            else verdict.get("passed")),
           "blocked": bool(verdict.get("blocked")),
           "score": verdict.get("score"), "reason": verdict.get("reason"),
           "issues": verdict.get("issues") or [],
           "critical_issues": verdict.get("critical_issues") or [],
           "style_mismatch": bool(verdict.get("style_mismatch")),
           "style_issues": verdict.get("style_issues") or [],
           "style": style_kit.normalize_style(style),
           "error": verdict.get("error"), "latency_ms": verdict.get("latency_ms"),
           # P0-2：接口级故障（鉴权/超时/网络）标记，供 _qc_summary 区分「故障放行」与「内容不合格」
           "interface_fault": bool(verdict.get("interface_fault"))}
    if extra:
        rec.update(extra)
    rec["history_file"] = _qc_record(project_name, kind, shot_key, rec)
    return rec


def _qc_lesson_from_record(rec: dict) -> dict:
    """从一条质检历史记录里取出「缺陷」，供教训库沉淀。

    ⚠️ `_qc_record_verdict` 返回的记录把 score/reason/issues 放在**顶层**，
    **没有** `verdict` 子对象。此前写入教训时误读 `rec["verdict"]`（恒为 None → {}），
    于是教训库里躺着的全是 score=0 / reason="" / issues=[] 的空记录，
    召回时自然什么建议都给不出来 —— 重试就变成了「换种子瞎撞」。
    这里对两种形态都做兼容，避免再被字段形态坑一次。
    """
    if not isinstance(rec, dict):
        return {}
    inner = rec.get("verdict") if isinstance(rec.get("verdict"), dict) else {}
    score = rec.get("score")
    if score is None:
        score = inner.get("score")
    issues = list(rec.get("issues") or inner.get("issues") or [])
    issues += list(rec.get("critical_issues") or inner.get("critical_issues") or [])
    issues += list(rec.get("style_issues") or inner.get("style_issues") or [])
    issues = [str(x).strip() for x in issues if str(x).strip()]
    reason = str(rec.get("reason") or inner.get("reason") or "").strip()
    if not issues and reason:
        issues = [reason]
    return {"score": score if score is not None else 0, "issues": issues[:20], "reason": reason}


def _record_qc_lesson(project_name: str, kind: str, prompt: str, rec: dict) -> dict:
    """把一次「质检不达标」沉淀成教训（供下次重试时改写提示词）。

    风格由 ``_project_style()`` 内部取（plan 的 style > AI 设定 > config.style），
    这样 6 个调用点不用各自找 style —— 它们本来就都在同一个项目上下文里。
    """
    lesson_src = _qc_lesson_from_record(rec)
    # 记录当时的视觉风格：召回时按「同风格加权 / 异风格降权」使用。
    # 没有它就无法回答「生成相同风格的提示词时有没有参考历史教训」——
    # 旧教训一律 context={}，跨画风的缺陷会串味（用 A 画风的标准要求 B 画风的图）。
    try:
        _qc_style = _project_style(project_name) or ""
    except Exception as _se:  # noqa: BLE001
        app.logger.warning("取项目风格失败（教训按无风格记录）：%s", _se)
        _qc_style = ""
    # 风格不达标：额外注入一条「明确的风格强化指令」，确保召回时能直接指导模型修正风格，
    # 而不是只给一条「风格不符」的缺陷描述。
    # ⚠️ 风格名必须写成占位符 {style}，**不能在记录时把项目风格写死**：
    #    教训库是跨项目复用的，写死会让 A 项目（中国古风玄幻）的教训被 B 项目
    #    （国漫偏写实）召回时强行要求 B 采用 A 的风格 —— 那是主动伤害。
    #    实际替换发生在 prompt_memory.suggestions(..., style=当前项目风格)。
    if (rec or {}).get("style_mismatch"):
        style_hint = ("画面风格与目标风格不符，必须严格采用「{style}」"
                      "的视觉风格、画风、渲染方式与配色，不得偏离")
        existing = lesson_src.get("issues") or []
        lesson_src["issues"] = [style_hint] + [i for i in existing if i != style_hint]
    if not lesson_src.get("issues") and not lesson_src.get("reason"):
        return {}
    try:
        got = prompt_memory.record(project=project_name, kind=kind, prompt=prompt,
                                   issues=lesson_src["issues"], reason=lesson_src["reason"],
                                   score=lesson_src.get("score"),
                                   root_dir=PROJECT_OUTPUT_DIR, style=_qc_style)
        if got:
            app.logger.info("[教训库] %s 记录 %d 条缺陷（kind=%s score=%s style=%s）：%s",
                            project_name, len(lesson_src["issues"]), kind,
                            lesson_src.get("score"), _qc_style or "-",
                            lesson_src["issues"][:2])
        return got or {}
    except Exception as mem_err:  # noqa: BLE001
        app.logger.warning("记录质检教训失败：%s", mem_err)
        return {}


def _optimize_prompt_from_qc(kind: str, prompt: str, rec: dict, style: str = "") -> str:
    """质检不达标后，用「文本分析模型」针对**本次这张图**的缺陷即时改写提示词。

    与 ``prompt_memory.learned_prompt``（召回**历史泛化**教训）的区别：
    这里把本次 verdict 的具体 issues 直接喂给 LLM，让它针对「这张图为什么没过」给出
    一条精准的提示词修正——而不是拼一条可能跨项目、可能过时的历史建议。

    返回优化后的提示词；任何失败（模型未配置 / 调用异常 / 返回空）都返回 None，
    由调用方回落原逻辑（换 seed / 召回历史教训），**绝不让优化环节阻断重生成**。
    """
    try:
        lesson = _qc_lesson_from_record(rec)
        issues = [str(x).strip() for x in (lesson.get("issues") or []) if str(x).strip()]
        reason = str(lesson.get("reason") or "").strip()
        if not issues and not reason:
            return None
        if not (prompt or "").strip():
            return None
        client = _optional_llm_client()
        if client is None:
            app.logger.info("[提示词优化] 文本分析模型未配置，跳过即时优化（回落历史召回/换种子）")
            return None
        kind_label = {"asset": "参考图", "storyboard": "分镜图",
                      "keyframe": "尾帧", "h3": "视频"}.get(kind, kind)
        issues_text = "\n".join(f"  - {i}" for i in issues[:6])
        style_text = (f"\n目标风格：{style}" if style else "")
        system = (
            "你是漫剧生成系统的提示词优化器。用户给出一段「生成图片用的提示词」和「质检判定它"
            "不达标的具体问题」，你要输出一段**修正后的提示词**，让重新生成能通过质检。\n"
            "要求：\n"
            "1. 只输出修正后的提示词正文，不要任何解释、前言、标号或 Markdown；\n"
            "2. 保留原提示词里仍然有效的描述（主体、外貌、材质、风格等），只针对列出的问题做精准修补；\n"
            "3. 用中文输出；\n"
            "4. 不要新增与问题无关的内容，不要改变原有画面主体；\n"
            "5. 修正要具体可执行（例如「去掉文字」就写「画面中不得出现任何文字/字幕/水印」）。"
        )
        user = (
            f"原提示词：\n{prompt.strip()}\n\n"
            f"质检判定不达标的问题：\n{issues_text}"
            f"{'（结论：' + reason + '）' if reason else ''}{style_text}\n\n"
            f"请输出修正后的提示词："
        )
        reply = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=0.4, max_tokens=1024,
        )
        optimized = (reply or "").strip()
        if not optimized or optimized == prompt.strip():
            app.logger.info("[提示词优化] %s 本次优化无变化或为空，回落原逻辑", kind_label)
            return None
        app.logger.info("[提示词优化] %s 针对本次缺陷改写提示词（%d 条问题）：%s → %s",
                        kind_label, len(issues), prompt[:24], optimized[:40])
        return optimized
    except Exception as e:  # noqa: BLE001
        app.logger.warning("质检后即时优化提示词失败（回落原逻辑）：%s", e)
        return None


def _record_preflight_lesson(project_name: str, prompt_original: str, pf: dict,
                             gate: dict) -> dict:
    """把一次「提示词预检不通过 / 有缺陷」沉淀成 ``kind="prompt"`` 教训。

    ``prompt_original`` 必须是**自愈前**（也**不含召回叠加块**）的原始提示词，作为稳定
    phash 键。收敛 keyframe / asset / storyboard 三处预检的沉淀逻辑，避免复制粘贴。
    """
    pf = pf if isinstance(pf, dict) else {}
    verdict = pf.get("verdict") if isinstance(pf.get("verdict"), dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in (verdict.get("issues") or []) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(pf.get("reason") or gate.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "stage": "prompt_preflight",
        "label": str(pf.get("label") or gate.get("label") or ""),
    }
    if not rec["issues"] and rec["reason"]:
        rec["issues"] = [rec["reason"]]
    if not rec["issues"] and not rec["reason"]:
        return {}
    return _record_qc_lesson(project_name, "prompt", prompt_original or "", rec)


def _qc_style_of(project_name: str) -> str:
    """取项目**当前**风格，供教训召回替换建议里的 ``{style}`` 占位符。

    数据源优先用 autopilot 计划（用户与总控敲定的创作设定，最权威），
    其次退回项目级创作设定的 style；都取不到就返回空串
    （此时 ``prompt_memory`` 会主动丢弃带 ``{style}`` 的建议，而不是把别的项目的风格安上来）。
    """
    try:
        plan = autopilot.get_plan(project_name) or {}
        s = style_kit.normalize_style(plan.get("style"))
        if s:
            return s
    except Exception as e:  # noqa: BLE001
        app.logger.debug("读取托管计划风格失败（忽略）：%s", e)
    try:
        for ep in (novel_to_script.list_episodes(SCRIPT_DIR, project_name, project_name) or []):
            path = ep.get("path") or ""
            if path and os.path.isfile(path):
                data = project_store._read_json(path, {}) or {}
                s = style_kit.normalize_style(data.get("style"))
                if s:
                    return s
    except Exception as e:  # noqa: BLE001
        app.logger.debug("读取剧本风格失败（忽略）：%s", e)
    return ""


def _keyframe_recall_cb(project_name: str):
    """尾帧重试召回回调（注入 ``keyframe.generate_keyframes`` 的 ``recall_cb``）。

    返回闭包 ``(orig_prompt, shot, item) -> str``：用 preflight 自愈**之前**的原始尾帧
    提示词召回 ``kind="keyframe"`` 历史教训并叠加；无教训时原样返回（零行为变更）。
    """
    def _recall(orig_prompt: str, shot: dict, item: dict) -> str:
        try:
            return prompt_memory.learned_prompt(
                kind="keyframe", prompt=orig_prompt or "", project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=_qc_style_of(project_name))
        except Exception as e:  # noqa: BLE001 - 召回失败绝不影响生成
            app.logger.warning(f"尾帧教训召回失败（忽略）：{e}")
            return orig_prompt or ""
    return _recall


def _episode_frame_ratios(segs: list, max_frames: int = None) -> list:
    """D-05（P1）整集按段抽帧的占比列表 —— 实现见 ``qc_coverage.episode_frame_ratios``。

    抽成独立零依赖模块（``app/qc_coverage.py``）以便离线单测
    （``verify_episode_qc_coverage.py``）无需 Flask/requests 即可验证覆盖率。
    """
    return qc_coverage.episode_frame_ratios(segs, max_frames=max_frames)


def _episode_qc_desc(shots: list, limit: int = qc_coverage.DEFAULT_DESC_LIMIT) -> str:
    """构造整集质检用的「镜头信息」摘要 —— 实现见 ``qc_coverage.episode_qc_desc``。

    为什么不用 comfyui_client 传进来的 ``shot_desc``：整集模式下它传的是
    **所有段的 H3 提示词全文拼接**（每段都是六段式结构，几十段叠在一起），
    又长又难判读，还挤占上下文。整片质检真正需要的是「这一集有哪些镜头、
    各自什么景别和内容」，这里按镜头生成紧凑摘要。

    D-05（P1）：``limit`` 由 12 提到 60 —— 原来 40 镜的整集只把前 12 镜给模型，
    中后段镜头对模型**完全不可见**，与抽帧漏检叠加后整集质检形同虚设。
    另：一旦真的截断，必须在串里**显式声明「其余未提供」**，让模型知道信息不完整，
    而不是误以为整集只有 limit 个镜头。
    """
    return qc_coverage.episode_qc_desc(shots, limit=limit, warn=app.logger.warning)


def _qc_shot_desc(shot: dict) -> str:
    """构造交给质检模型的「镜头信息」。

    ⚠️ 景别必须带上**判定标准**，不能只给裸词。
    生成端用 ``SHOT_CAMERA_SPECS[camera]`` 的精确定义写提示词，而质检端此前只传
    「机位：中景跟拍」——模型只能凭自己的理解判「中景」，与生成端标准不一致，
    实测分镜图通过率仅 57%、失败原因几乎全是「景别不符」（把腰部以上的中景判成不合规）。
    这里改为引用 :func:`comfyui_client.camera_spec`（经模块顶部的 ``_camera_spec`` 别名调用，
    因为本文件里 ``comfyui_client`` 是实例而非模块），保证两端**同一份标准**。
    """
    parts = []
    # 景别放在最前：描述较长时 [:900] 截断会吃掉尾部，判定标准必须优先保住
    if shot.get("camera"):
        cam = str(shot["camera"]).strip()
        # ⚠️ 必须显示「解析后的景别」而不是只给裸词：camera 常是「景别+机位+运镜」的复合写法。
        # 且**景别未给时不许编**（旧实现回落中景 → 拿中景标准去判脚部俯拍图，必然判不符，
        # 该镜永远过不了；实测 ep02 shot_13 因此白烧 6 次 GPU）。
        cam_k = _camera_key(cam)
        parts.append(f"景别：{cam_k or '未指定'}（camera 原值「{cam}」；判定标准：{_camera_spec(cam)}）")
        # 机位与景别正交：只给景别不给机位的话，「要求俯拍却给了平视」没人能判出来。
        ang = _camera_angle(cam)
        if ang:
            parts.append(f"机位：{ang}（须与画面一致）")
    if shot.get("location"):
        parts.append(f"场景：{shot['location']}")
    if shot.get("description"):
        parts.append(str(shot["description"]).strip())
    if shot.get("emotion"):
        parts.append(f"情绪：{shot['emotion']}")
    if shot.get("dialogue"):
        parts.append(f"台词：{str(shot['dialogue']).strip()[:80]}")
    return "；".join(parts)[:900] or "（无镜头描述）"


def _qc_ref_images(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                   fallback_refs: list = None) -> list:
    """为「图片质检」收集**本镜出现**的角色 / 物品 / 场景设定图 → [(label, path)]。

    为什么要单独收集，而不直接复用生成侧的 `_allocate_storyboard_refs`：
    生成侧只有 3 个参考图槽位（主角色 / 次要 / 场景），最多带 3 张；而质检的目的是
    **逐个核对画面里的每个角色、每件物品有没有变形、是否与设定一致**，所以按
    `shot.characters_in_shot` / `shot.items_in_shot` 全量收集（总数上限
    `qc_client.MAX_REF_IMAGES`，在 check_image 内还会按路径去重）。

    历史缺陷：分镜质检只把成品图单独送检，模型手里没有任何设定锚点，
    「这个角色长得像不像设定」「这柄剑的形制对不对」只能靠它自己猜 ——
    「角色不像设定 / 道具走形」这类问题要么被放过、要么被误判。
    """
    out, seen = [], set()

    def _add(label, path):
        if not path or path in seen:
            return
        seen.add(path)
        out.append((label, path))

    for n in (shot.get("characters_in_shot") or []):
        _add(f"角色「{n}」的外貌、服装与发型", (char_idx.get(n) or {}).get("image"))
    for n in (shot.get("items_in_shot") or []):
        _add(f"物品「{n}」的形状、材质与配色", (item_idx.get(n) or {}).get("image"))
    loc = shot.get("location")
    if loc in scene_idx:
        _add(f"场景「{loc}」的环境与氛围", (scene_idx.get(loc) or {}).get("image"))
    if not out:
        # 兜底：本镜没登记角色/物品时，用生成侧实际用的那几张（至少保住场景锚点）
        for r in (fallback_refs or []):
            if isinstance(r, (list, tuple)) and len(r) >= 3:
                _add(str(r[1]), r[2])
    return out[:qc_client.MAX_REF_IMAGES]


def _keyframe_qc_verifier(project_name: str, script: dict = None):
    """尾帧质检回调（供 keyframe.generate_keyframes 的 verify_cb 注入）

    返回 (verify_cb, max_retries)；质检未开启或不可用时返回 (None, 0)，
    此时尾帧链路与旧行为完全一致（不做任何质检）。

    背景：尾帧此前**完全不经过质检**（只有分镜图走），而链式模式下尾帧会直接
    成为下一镜的首帧，一张坏图会顺着链污染后面所有镜——必须拦在源头。
    """
    try:
        cfg = _qc_load_cfg()
    except Exception:  # noqa: BLE001
        return None, 0
    if not (cfg.get("enabled") and cfg.get("image_enabled")
            and cfg.get("keyframe_qc_enabled", True)):
        return None, 0
    if not qc_client.image_qc_ready(cfg):
        return None, 0

    # 尾帧同样要核对「角色/物品有没有变形、是否与设定一致」：尾帧在链式模式下
    # 会直接成为下一镜的首帧，一张走形的尾帧会顺着链污染后面所有镜。
    # 这里按剧本预建一次资产索引（只建一次，逐镜复用）。
    _kf_idx = None
    try:
        if script:
            _kf_idx = (
                _build_asset_index(script.get("characters") or [], project_name, "character"),
                _build_asset_index(script.get("items") or [], project_name, "item"),
                _build_asset_index(script.get("scenes") or [], project_name, "scene"),
            )
    except Exception as _e:  # noqa: BLE001
        app.logger.warning(f"尾帧质检构建资产索引失败（本轮不带设定图）：{_e}")
        _kf_idx = None

    def _verify(path: str, shot: dict, item: dict):
        desc = (_qc_shot_desc(shot)
                + f"；本图是该镜的「尾帧」（动作结束瞬间），"
                  f"须与首帧保持同一人物、同一服装、同一场景与同一画风"
                + ("，且须承接上一镜尾帧的画面" if item.get("chained") else ""))
        verdict = qc_client.check_image(path, desc, cfg, style=(shot.get("style") or ""),
                                        ref_images=(_qc_ref_images(shot, *_kf_idx) if _kf_idx else None))
        gate = _qc_gate(verdict)
        accepted = bool(gate.get("accept"))
        try:
            # A-17（P2-6）：尾帧质检结论**达标 / 不达标都落盘**一条 keyframe 质检记录 ——
            # 旧实现只在 `if not accepted` 里写记录，达标时无痕，用户无从确认「这集尾帧
            # 到底查没查」。这里两种结果都产生一条记录（record 内自带 ok/passed/accepted
            # 区分口径）；只有**不达标**才进一步沉淀教训（lesson 供下次重试改写提示词）。
            rec = _qc_record_verdict(
                project_name, "keyframe",
                f"shot_{item.get('seq') or item.get('shot_id')}", "尾帧质检",
                1, None, path, verdict,
                extra={"qc_outcome": "pass" if accepted else "fail"},
                style=(shot.get("style") or ""))
            if not accepted:
                # 尾帧质检不达标 → 沉淀 kind="keyframe" 教训，供下次重试 recall_cb 改写提示词。
                # 提示词键用 preflight 自愈**之前**的确定性串（build_end_frame_prompt），与
                # keyframe.generate_keyframes 里的 orig_prompt 同键，保证 phash 稳定。
                orig_prompt = keyframe.build_end_frame_prompt(
                    shot, chained=bool(item.get("chained")))
                _record_qc_lesson(project_name, "keyframe", orig_prompt, rec)
        except Exception as e:  # noqa: BLE001 - 落盘/沉淀失败绝不影响质检结论
            app.logger.warning(f"尾帧质检记录/教训沉淀失败（忽略）：{e}")
        # P1-18：返回 4 元组（ok, reason, unavailable, critical_issues）——
        #  · verdict.ok=False 表示质检「不可判定」（接口 5xx / ffmpeg 缺失等，与内容无关），
        #    由 keyframe 侧据此 **不重试**并标记 qc_unavailable（口径与「不达标」分开）；
        #  · critical_issues（A-18）：本镜判定为致命的缺陷清单（gate 独立复核命中的关键
        #    缺陷 ∪ 上游显式 critical_issues），keyframe 侧写入 r["qc"] 供 G1 止损
        #    （_qc_retry_hopeless）比对「连续 N 次缺陷相同」。旧 3 元组下 keyframe 侧
        #    r["qc"]["critical_issues"] 恒空 → 止损永不判无望（纯换 seed 瞎撞）。
        return (accepted, gate.get("reason") or "",
                not bool(verdict.get("ok")),
                list(gate.get("critical_issues") or []))

    return _verify, min(2, int(cfg.get("max_retries") or 0))


def _keyframe_prompt_preflight(project_name: str):
    """尾帧「生成前提示词预检」回调构建器（与 ``_keyframe_qc_verifier`` 同一注入风格）

    返回 ``(preflight_cb, enabled)``；``preflight_cb(prompt, shot, item) -> dict`` 直接返回
    ``prompt_qc.preflight`` 的结果（含自愈后的提示词），由
    ``keyframe.generate_keyframes`` 取其中的 ``prompt`` 去出图。

    ⚠️ 与尾帧质检（生成后、依赖质检接口）不同：预检是**纯确定性**的，所以只看
    ``prompt_enabled`` —— 质检接口没配好时它照样能拦住「提示词为空」「缺锚定参考图语义」
    这类必然废图的输入。这正是预检比事后质检便宜、且能兜住事后质检的原因。
    """
    try:
        cfg = _qc_load_cfg()
    except Exception:  # noqa: BLE001
        return None, False
    if not prompt_qc.prompt_qc_ready(cfg):
        return None, False

    # 项目级风格只解析一次：预检要按镜头逐个跑，不能在闭包里反复读盘
    _proj_style = ""
    try:
        _proj_style = _qc_style_of(project_name) or ""
    except Exception:  # noqa: BLE001
        _proj_style = ""

    def _pre(prompt: str, shot: dict, item: dict) -> dict:
        shot = shot or {}
        # 风格与生成端对齐：build_end_frame_prompt 只读 shot["style"]。镜头没带风格时退回
        # 项目当前风格 —— 这样预检能把「风格缺失」判出来并自愈补上，而不是直接放过。
        style = shot.get("style") or _proj_style
        ctx = dict(shot)
        ctx["chained"] = bool(item.get("chained"))
        # prompt 召回：预检前先叠加 kind="prompt" 历史教训。orig_prompt 是自愈**前**的
        # 原始串（也不含召回叠加块），作为下方沉淀的稳定 phash 键，避免指纹漂移。
        orig_prompt = prompt or ""
        try:
            prompt = prompt_memory.learned_prompt(
                kind="prompt", prompt=orig_prompt, project=project_name,
                root_dir=PROJECT_OUTPUT_DIR, style=style)
        except Exception as e:  # noqa: BLE001 - 召回失败不影响预检
            app.logger.warning(f"尾帧提示词召回失败（忽略）：{e}")
        pf = prompt_qc.preflight("keyframe", prompt, ctx=ctx, style=style, cfg=cfg)
        gate = prompt_qc.prompt_qc_gate(pf, cfg)
        _qc_record(project_name, "prompt",
                   f"shot_{item.get('seq') or item.get('shot_id')}",
                   {"stage": "keyframe",
                    "label": pf.get("label"),
                    "accept": bool(pf.get("accept")),
                    "repairs": list(pf.get("repairs") or []),
                    "rebuild_hint": pf.get("rebuild_hint") or "",
                    "verdict": pf.get("verdict") or {},
                    "prompt": pf.get("prompt") or ""})
        # prompt 沉淀：预检不通过或有缺陷时落 kind="prompt"（键用自愈前原始串）。
        if not gate.get("accept") or (pf.get("verdict") or {}).get("issues"):
            try:
                _record_preflight_lesson(project_name, orig_prompt, pf, gate)
            except Exception as e:  # noqa: BLE001 - 沉淀失败不影响预检
                app.logger.warning(f"尾帧提示词教训沉淀失败（忽略）：{e}")
        return pf

    return _pre, True


def _qc_record(project_name: str, kind: str, shot_id, payload: dict) -> str:
    """写一条质检/重试历史（失败也不影响主流程）"""
    try:
        return qc_client.append_history(QC_DIR, project_name, kind, shot_id, payload)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检历史写入失败（忽略）：{e}")
        return ""


def _qc_history_file_for(project_name: str, kind: str, shot_id) -> str:
    """推算某条质检历史的落盘路径（与 qc_client.history_path 同口径）。

    用途：产物被移入回收站后，把该历史里指向已删路径的 `file` 记为 null（断链修正），
    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据。
    """
    try:
        return qc_client.history_path(QC_DIR, project_name, kind, shot_id)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检历史路径推算失败（忽略）：{e}")
        return ""


def _qc_summary(attempts: list, enabled: bool, ready: bool, max_retries: int) -> dict:
    """汇总一次资产生成的全部质检尝试，供前端展示徽标 / 明细 / 重试次数"""
    if not enabled:
        return {"enabled": False, "status": "disabled", "label": "质检未开启",
                "attempts": 0, "max_retries": max_retries}
    if not ready:
        return {"enabled": True, "status": "unconfigured", "label": "质检未配置（未启用）",
                "attempts": 0, "max_retries": max_retries}
    passed_rec = next((a for a in attempts if a.get("passed")), None)
    last = attempts[-1] if attempts else {}
    # P0-2：接口级故障放行——最后一次尝试是 interface_fault（鉴权/超时/网络）且无客观层
    # 致命缺陷 → 资产已被 fail-open 写入，不算「error/不达标」，单独一档 fault_open。
    fault_open = bool(last.get("interface_fault")) and not passed_rec and \
        not (last.get("critical_issues") or [])
    if fault_open:
        status = "fault_open"
        blocked = False
    else:
        status = "passed" if passed_rec else ("error" if last.get("error") else "failed")
        blocked = status in ("failed", "error")
    crit = list((passed_rec or last).get("critical_issues") or [])
    # ★ 重试止损（_qc_retry_hopeless）：未通过且已提前停止重试时，把原因写进 label，
    #   否则用户只看到「质检不达标」，不知道系统其实已经主动止损（没在继续烧 GPU）。
    retry_stopped = bool((last or {}).get("retry_stopped")) and not passed_rec
    _label = {"passed": "质检达标", "failed": "质检不达标", "error": "质检调用异常",
              "fault_open": "质检接口故障·已放行"}.get(status, status)
    if retry_stopped:
        _label = "质检不达标（已停止重试：连续两次缺陷完全相同）"
    return {
        "enabled": True,
        "status": status,
        "label": _label,
        "fault_open": fault_open,
        "retry_stopped": retry_stopped,
        "retry_stopped_detail": (last or {}).get("retry_stopped_features") or "",
        "passed": bool(passed_rec),
        "blocked": blocked,
        "entry_blocked": blocked,
        "asset_written": (not blocked),
        "critical_issues": crit,
        "score": (passed_rec or last).get("score"),
        "reason": (passed_rec or last).get("reason") or (last.get("error") or ""),
        "issues": (passed_rec or last).get("issues") or [],
        "attempts": len(attempts),
        "regenerated": max(0, len(attempts) - 1),
        "max_retries": max_retries,
        "history": attempts,
        "history_file": last.get("history_file") or "",
    }


@app.route('/api/qc/config', methods=['GET'])
def api_qc_config_get():
    cfg = _qc_load_cfg()
    view = qc_client.public_view(cfg)
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "history_dir": os.path.abspath(QC_DIR),
                    "ai_settings_path": os.path.abspath(AI_CONFIG_PATH)})


@app.route('/api/qc/config', methods=['POST'])
def api_qc_config_save():
    data = request.json or {}
    data.pop("reuse_llm", None)   # 旧字段：质检不再复用文本分析 LLM，直接忽略
    cfg = qc_client.save_config(QC_CONFIG_PATH, data)
    view = qc_client.public_view(cfg)
    if view["enabled"] and not view["ready"]:
        return jsonify({"success": True, "config": view,
                        "config_path": os.path.abspath(QC_CONFIG_PATH),
                        "warning": "质检开关已开启，但质检接口信息不完整（base_url / api_key / model），"
                                   "生成流程将跳过质检且不会报错。",
                        "message": "配置已保存（接口未就绪）"})
    return jsonify({"success": True, "config": view,
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "质检配置已保存"})


@app.route('/api/qc/config/clear', methods=['POST'])
def api_qc_config_clear():
    cfg = qc_client.clear_config(QC_CONFIG_PATH)
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "质检配置已清除（质检总开关关闭）"})


@app.route('/api/qc/config/reset-endpoint', methods=['POST'])
def api_qc_config_reset_endpoint():
    """把质检接口恢复为「AI 设置 → 质检模型」的独立配置"""
    cfg = qc_client.reset_endpoint(QC_CONFIG_PATH)
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "message": "已清空质检页面内的接口覆盖，将使用「AI 设置 → 质检模型」"})


@app.route('/api/qc/config/sync-from-ai', methods=['POST'])
def api_qc_config_sync_from_ai():
    """一键把「AI 设置 → 质检模型」的接口同步到质检配置（可选，便于统一维护）"""
    ai_cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(ai_cfg, "qc")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return jsonify({"success": False,
                        "error": "「AI 设置 → 质检模型」尚未配置完整，请先在那里填写并保存"}), 400
    cfg = qc_client.set_endpoint(QC_CONFIG_PATH, ep["base_url"], ep["api_key"], ep["model"])
    return jsonify({"success": True, "config": qc_client.public_view(cfg),
                    "config_path": os.path.abspath(QC_CONFIG_PATH),
                    "endpoint": {"base_url": ep["base_url"], "model": ep["model"], "source": "ai_settings"},
                    "message": "已同步「AI 设置 → 质检模型」到质检配置"})


@app.route('/api/qc/test', methods=['POST'])
def api_qc_test():
    """测试质检接口：可用页面暂存参数直接测试，不落盘。
    传 image_path 时用真实图片走一次图片质检；传 video_path 走视频抽帧质检；
    否则做连通性测试（vision=true 时用极小图片探测视觉能力）。"""
    data = request.json or {}
    data.pop("reuse_llm", None)
    cfg = _qc_load_cfg()
    for k in ("image_prompt", "video_prompt", "pass_score",
              "max_retries", "video_frame_count"):
        if data.get(k) not in (None, ""):
            cfg[k] = data[k]
    cfg["enabled"] = True
    cfg["image_enabled"] = True
    cfg["video_enabled"] = True
    cfg = qc_client.load_config_dict(cfg)

    override = _qc_test_override(data)
    ep = qc_client.resolve_endpoint(cfg, override or None)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return jsonify({"success": False,
                        "error": "质检接口未配置完整（质检需独立配置 base_url / api_key / model）",
                        "endpoint_source": ep["source"]}), 400

    def _endpoint_view():
        return {"base_url": ep["base_url"], "model": ep["model"], "source": ep["source"]}

    image_path = data.get("image_path") or ""
    if not image_path:
        # 自动挑一张已有分镜图作为测试样张
        probe = _ep_read_dir(STORYBOARDS_DIR,
                             _safe_project(data.get("project_name") or "project"),
                             data.get("episode_no"))
        if os.path.isdir(probe):
            pngs = sorted([f for f in os.listdir(probe) if f.lower().endswith(".png")])
            if pngs:
                image_path = os.path.join(probe, pngs[0])

    video_path = data.get("video_path") or ""
    if video_path and os.path.isfile(video_path):
        verdict = qc_client.check_video(video_path, "接口连通性测试样张", cfg, override or None,
                                        frames_dir=os.path.join(QC_DIR, "_selftest", "frames"))
        ok = bool(verdict.get("ok"))
        return jsonify({
            "success": ok,
            "mode": "video",
            "video_path": os.path.abspath(video_path),
            "frame_count": verdict.get("frame_count"),
            "duration": verdict.get("duration"),
            "frames": verdict.get("frames"),
            "verdict": verdict,
            "endpoint": _endpoint_view(),
            "error": None if ok else (verdict.get("error") or verdict.get("reason")),
        }), (200 if ok else 400)

    if image_path and os.path.isfile(image_path):
        verdict = qc_client.check_image(image_path, "接口连通性测试样张", cfg, override or None)
        ok = bool(verdict.get("ok"))
        return jsonify({
            "success": ok,
            "mode": "image",
            "image_path": os.path.abspath(image_path),
            "verdict": verdict,
            "endpoint": _endpoint_view(),
            "error": None if ok else (verdict.get("error") or verdict.get("reason")),
        }), (200 if ok else 400)

    # 无样张：连通性测试。vision=true 或未指定时用极小图片探测视觉能力
    if data.get("vision", True):
        result = qc_client.test_vision(ep, timeout=60)
        return jsonify({"success": result.get("success"), "mode": "vision",
                        "vision": result.get("vision"), "reply": result.get("reply"),
                        "latency_ms": result.get("latency_ms"), "url": result.get("url"),
                        "endpoint": _endpoint_view(), "error": result.get("error")}), (
            200 if result.get("success") else 400)
    try:
        resp = qc_client._post_chat(ep, {
            "model": ep["model"],
            "messages": [{"role": "user", "content": "回复 JSON：{\"ok\": true}"}],
            "temperature": 0, "max_tokens": 64},
            cfg.get("timeout", 180))
        return jsonify({"success": True, "mode": "text", "raw": resp["content"][:300],
                        "latency_ms": resp["latency_ms"], "endpoint": _endpoint_view()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "mode": "text", "error": str(e),
                        "endpoint": _endpoint_view()}), 400


@app.route('/api/qc/history/<path:project_name>/<kind>/<path:shot_key>')
def api_qc_history(project_name, kind, shot_key):
    project = _safe_project(os.path.basename(project_name.rstrip('/')))
    data = qc_client.read_history(QC_DIR, project, kind, shot_key)
    path = qc_client.history_path(QC_DIR, project, kind, shot_key)
    return jsonify({"success": bool(data), "project_name": project, "kind": kind,
                    "shot_id": shot_key, "exists": bool(data), "history": data,
                    "history_file": os.path.abspath(path)})


@app.route('/api/qc/prompt', methods=['POST'])
def api_qc_prompt():
    """提示词预检（生成前质检）：按需检查一条提示词，并按配置自愈。

    body::

        {kind: "storyboard"|"h3"|"asset", prompt: "...", style?: "...",
         context?: <镜头/资产 dict>, ref_count?: int, expect_refs?: bool,
         repair?: true}   # repair=false 时只检查、不改写

    与图片/视频质检不同，这一层**不依赖质检接口**（纯确定性检查），因此未配置质检
    接口也能用；返回的 verdict 与 check_image/check_video 同构，便于前端统一展示。
    """
    data = _body()
    kind = str(data.get('kind') or '').strip().lower()
    if not kind:
        return jsonify({"success": False,
                        "error": f"缺少 kind（可选 {' / '.join(prompt_qc.PROMPT_KINDS)}）"}), 400
    if kind not in prompt_qc.PROMPT_KINDS:
        return jsonify({"success": False,
                        "error": f"不支持的 kind：{kind}（可选 {'/'.join(prompt_qc.PROMPT_KINDS)}）"}), 400
    prompt = str(data.get('prompt') or '')
    if not prompt.strip():
        return jsonify({"success": False, "error": "缺少 prompt"}), 400

    cfg = _qc_load_cfg()
    style = str(data.get('style') or '').strip()
    ctx = data.get('context') if isinstance(data.get('context'), dict) else None
    rc = data.get('ref_count')
    try:
        rc = int(rc) if rc is not None else None
    except (TypeError, ValueError):
        rc = None
    expect_refs = data.get('expect_refs')
    expect_refs = bool(expect_refs) if isinstance(expect_refs, (bool, int)) else None

    if bool(data.get('repair', True)):
        pf = prompt_qc.preflight(kind, prompt, ctx=ctx, style=style, cfg=cfg,
                                 ref_count=rc, expect_refs=expect_refs)
    else:
        v = prompt_qc.check_prompt(kind, prompt, ctx=ctx, style=style, cfg=cfg,
                                   ref_count=rc, expect_refs=expect_refs)
        blocked = bool(v.get("critical_issues"))
        pf = {"prompt": prompt, "verdict": v, "repairs": [], "accept": not blocked,
              "blocked": blocked, "skipped": not prompt_qc.prompt_qc_ready(cfg),
              "label": "提示词达标" if v.get("passed") and not v.get("issues") else "提示词不达标",
              "reason": v.get("reason") or "", "rebuild_hint": v.get("rebuild_hint") or ""}
    return jsonify({"success": True, "kind": kind,
                    "prompt": pf.get("prompt"), "verdict": pf.get("verdict"),
                    "repairs": pf.get("repairs") or [],
                    "gate": prompt_qc.prompt_qc_gate(pf, cfg),
                    "mode": prompt_qc.prompt_qc_mode(cfg),
                    "accept": bool(pf.get("accept")),
                    "label": pf.get("label"), "reason": pf.get("reason"),
                    "rebuild_hint": pf.get("rebuild_hint") or ""})


@app.route('/api/qc/audio', methods=['POST'])
def api_qc_audio():
    """音频质检（成品质检）：客观层（ffmpeg 指标）+ AI 层（频谱图/波形图送多模态）。

    body::

        {project_name?: "...", 
         path?: "output/dub/<项目>/lines/xxx.wav",   # 显式指定文件（必须位于 output/ 内）
         source?: "mix" | "merged" | "line",         # 未给 path 时按此推导（默认 mix > merged）
         expect_sec?: 3.2,          # 期望时长；不给则只做无声/削波判定，不做时长偏差
         line_text?: "三年了，我回来了。",
         check_speech_ratio?: true, # 「有声占比下限」判定。单句传 true；整轨必须 false
         with_ai?: true}            # false 时只跑客观层（毫秒级、零模型调用）

    与 ``/api/qc/prompt``（生成前预检）配套：那个管「台词写对没有」，这个管
    「录出来是不是真的有人声」。
    """
    data = _body()
    project = _safe_project(data.get('project_name') or '')
    cfg = _qc_load_cfg()
    if not qc_client.audio_qc_ready(cfg):
        return jsonify({"success": False,
                        "error": "音频质检未启用（请检查质检总开关与音频质检开关）",
                        "audio_qc_active": False}), 400

    # ---- 定位待检文件：显式 path 优先，否则按 source 推导 ----
    raw_path = str(data.get('path') or '').strip()
    source = str(data.get('source') or '').strip().lower()
    target, why = '', ''
    if raw_path:
        cand = os.path.abspath(os.path.join(PROJECT_ROOT_DIR, raw_path)) \
            if not os.path.isabs(raw_path) else os.path.abspath(raw_path)
        root = os.path.abspath(PROJECT_OUTPUT_DIR)
        # 只允许检查 output/ 内的产物：这是「回显用户自己的成品」，不是任意文件读取接口
        if not cand.startswith(root + os.sep):
            return jsonify({"success": False,
                            "error": f"只允许检查 output/ 目录内的文件：{raw_path}"}), 400
        if not os.path.isfile(cand):
            return jsonify({"success": False, "error": f"文件不存在：{cand}"}), 404
        target, source = cand, (source or 'path')
    elif project:
        target, source, why = _resolve_audio_qc_target(project, source)
        if not target:
            return jsonify({"success": False, "error": why or "未找到可质检的音频产物",
                            "project_name": project}), 404
    else:
        return jsonify({"success": False, "error": "需要 project_name 或 path"}), 400

    # ---- 期望时长 / 有声占比口径 ----
    expect = data.get('expect_sec')
    try:
        expect = float(expect) if expect not in (None, '') else None
    except (TypeError, ValueError):
        expect = None
    if expect is None and source == 'mix':
        try:
            expect = float(mix_probe_audio(target).get('duration') or 0) or None
        except Exception:  # noqa: BLE001
            expect = None
    # ⚠️ 整轨（成片 mp4 / 整集合成音轨）默认**关闭**有声占比判定：
    #    成片天然有大段无台词留白，拿单句的 50% 标准卡它必然误报「漏句」。
    #    判据是「整轨口径」而不是「调用方有没有传 source」—— 前端直接拖一个 mp4 过来
    #    检查（source 会是 path）时同样必须关掉，否则一进来就是满屏「静音过多」。
    is_whole_track = source in ('mix', 'merged') or target.lower().endswith(('.mp4', '.mkv', '.mov'))
    default_ratio = not is_whole_track
    check_ratio = data.get('check_speech_ratio')
    check_ratio = default_ratio if not isinstance(check_ratio, bool) else check_ratio

    stem = os.path.splitext(os.path.basename(target))[0]
    bucket = 'audio_mix' if (source == 'mix' or target.lower().endswith('.mp4')) else 'audio'
    visuals_dir = os.path.join(
        QC_DIR, bucket,
        _audio_qc_visuals_key(data.get('project_name'), target), stem)

    with_ai = bool(data.get('with_ai', True))
    if not with_ai:
        verdict = audio_qc.quick_check(
            target, expect_sec=expect,
            min_speech_ratio=(cfg.get("audio_min_speech_ratio", 0.50) if check_ratio else None),
            min_mean_db=cfg.get("audio_min_mean_db", -45.0),
            max_drift=cfg.get("audio_max_drift", 0.50))
        verdict["ai_skipped"] = True
        verdict["ai_skip_reason"] = "请求显式要求只做客观层（with_ai=false）"
    else:
        verdict = qc_client.check_audio(
            target, expect_sec=expect, line_text=str(data.get('line_text') or ''),
            cfg=cfg, visuals_dir=visuals_dir, check_speech_ratio=check_ratio)

    vis_urls = []
    for p in (verdict.get("visuals") or []):
        try:
            rel = os.path.relpath(p, QC_DIR).replace(os.sep, '/')
        except ValueError:
            continue
        if not rel.startswith('..'):
            vis_urls.append(f"/api/qc/frames/{rel}")
    return jsonify({
        "success": True,
        "project_name": project, "source": source, "path": os.path.abspath(target),
        "expect_sec": expect, "check_speech_ratio": check_ratio,
        "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
        "score": verdict.get("score"), "reason": verdict.get("reason"),
        "issues": verdict.get("issues") or [],
        "critical_issues": verdict.get("critical_issues") or [],
        "metrics": verdict.get("metrics") or {},
        "ai_used": bool(verdict.get("ai_used")),
        "ai_skipped": bool(verdict.get("ai_skipped")),
        "ai_skip_reason": verdict.get("ai_skip_reason") or "",
        "objective_only": bool(verdict.get("objective_only")),
        "visuals": vis_urls,
        "verdict": verdict,
        "audio_qc_active": True,
        "audio_ai_active": qc_client.audio_ai_ready(cfg),
        "file_url": _audio_qc_file_url(project, target),
    })


def _audio_qc_file_url(project: str, path: str) -> str:
    """尽量给出可直接播放的 URL（只对 tts/mix 两个既有静态路由下的产物）"""
    ap = os.path.abspath(path)
    try:
        rel_dub = os.path.relpath(ap, os.path.join(DUB_DIR, project or 'project'))
        if not rel_dub.startswith('..'):
            return f"/api/tts/file/{project or 'project'}/{rel_dub.replace(os.sep, '/')}"
        rel_mix = os.path.relpath(ap, mix_out_dir(project or 'project'))
        if not rel_mix.startswith('..'):
            return f"/api/mix/file/{project or 'project'}/{rel_mix.replace(os.sep, '/')}"
    except ValueError as e:
        app.logger.debug("混音相对路径解析失败（忽略）：%s", e)
    return ""


_AUDIO_QC_MEDIA_EXT = ('.mp4', '.mkv', '.mov', '.webm', '.m4v', '.avi')
_AUDIO_QC_AUDIO_EXT = ('.wav', '.mp3', '.flac', '.m4a', '.aac', '.ogg')


_AUDIO_QC_NON_PROJECT_DIRS = ('lines', 'frames', 'output', 'temp', 'qc', 'audio', 'audio_mix')


def _audio_qc_project_key(target: str) -> str:
    """按产物路径反推项目名，用于可视化图片的落盘目录。

    ⚠️ 不能退化成字面量（如 ``project``）：按 ``path`` 直接检查时拿不到项目名，
    所有项目就会挤进同一个目录，**不同项目的同名文件互相覆盖** —— 而 AI 读图是
    子进程/网络异步进行的，覆盖会变成竞态（读数项目的图）。
    布局：成片 ``output/final_dub/<项目>/x.mp4``、逐句 ``output/dub/<项目>/lines/x.wav``。
    """
    d = os.path.dirname(os.path.abspath(target))
    for _ in range(4):
        name = os.path.basename(d)
        if name and name.lower() not in _AUDIO_QC_NON_PROJECT_DIRS:
            return name
        parent = os.path.dirname(d)
        if parent == d:                       # 已到根，别再往上
            break
        d = parent
    return 'adhoc'


def _audio_qc_visuals_key(requested_project, target: str) -> str:
    """音频质检可视化图片用的项目键。

    ⚠️ 调用方**不能**写成 ``_safe_project(x) or _audio_qc_project_key(target)``：
    ``_safe_project('')`` 返回的是**字面量 'project'**（``project_store.safe_key``
    的空值兜底），恒为真值 → 兜底永不生效。后果是所有按 ``path`` 直接检查的请求都
    挤进同一个 ``.../project/`` 目录，**不同项目的同名产物互相覆盖** —— 而 AI 读图是
    异步进行的，覆盖会变成竞态（读到了别的项目的频谱图）。
    判「调用方到底有没有传项目名」必须看**原始入参**。
    """
    if str(requested_project or '').strip():
        return _safe_project(requested_project)
    return _audio_qc_project_key(target)


def _resolve_audio_qc_target(project: str, source: str = ''):
    """按项目推导待质检音频：mix（带配音成片）> merged（整集合成音轨）> line（单句）

    返回 ``(路径, source, 失败原因)``。

    ⚠️ 必须按扩展名过滤：``mix_out_dir`` 里除了成片还有 ``*_mix_report.json``
    等边车文件（且它们往往最新），不过滤就会把 JSON 报告当成成片送去解码，
    结论变成「文件无法解码」——假失败。
    """
    def _newest(paths):
        cands = [p for p in paths if os.path.isfile(p) and os.path.getsize(p) > 0]
        return max(cands, key=os.path.getmtime) if cands else ''

    def _media(d, exts):
        if not os.path.isdir(d):
            return []
        return [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(exts)]

    mix_dir = mix_out_dir(project)
    merged_dir = os.path.join(DUB_DIR, project)
    lines_dir = os.path.join(merged_dir, 'lines')

    if source in ('', 'mix'):
        p = _newest(_media(mix_dir, _AUDIO_QC_MEDIA_EXT))
        if p:
            return p, 'mix', ''
        if source == 'mix':
            return '', 'mix', f"项目 '{project}' 下没有带配音成片（请先做音画合成）"
    if source in ('', 'merged'):
        p = _newest(_media(merged_dir, _AUDIO_QC_AUDIO_EXT))
        if p:
            return p, 'merged', ''
        if source == 'merged':
            return '', 'merged', f"项目 '{project}' 下没有整集配音音轨"
    if source in ('', 'line'):
        p = _newest(_media(lines_dir, _AUDIO_QC_AUDIO_EXT))
        if p:
            return p, 'line', ''
        if source == 'line':
            return '', 'line', f"项目 '{project}' 下没有逐句配音文件"
    return '', source or 'mix', f"项目 '{project}' 下没有可质检的音频产物（先做配音/合成）"


@app.route('/api/qc/project-summary', methods=['GET'])
def api_qc_project_summary():
    """项目级 QC 聚合：列出所有质检项的最新结论，供前端总览页使用

    ⚠️ 审计 G3：本接口此前**恒返回空统计**（线上 149 个质检历史文件一个都统计不到），
    根因有三处，缺一不可：
      ① `project = _safe_project(request.args.get('project', ''))` 后面接
         `if not project:` —— `_safe_project('')` 返回**字面量 'project'**（真值），
         守卫恒不成立（死守卫）。漏传项目名不会报错，而是聚合到共享 `project` 命名空间。
         判空必须看**原始入参**。
      ② 它枚举的是 `QC_DIR` 下以 `shot_` 开头的**目录**，而真实落盘路径是
         `QC_DIR/<项目>/<kind>_<键>.json` —— 一个都匹配不到，于是总览页永远
         「0 通过 / 0 失败」，用户以为质检从未运行过。
      ③ `read_history` 返回 `{"records": [...]}` 字典、记录里的字段是
         `passed` / `time`，**没有** `verdict` / `timestamp`。旧代码把 dict 当 list 用
         （`hist[-1]`）并按 `verdict` 判通过 —— 即使目录判对了也统计不出来。
    """
    raw = (request.args.get('project') or '').strip()
    if not raw:
        return jsonify({"success": False, "error": "缺少 project 参数"}), 400
    project = _safe_project(raw)

    qc_cfg = _qc_load_cfg()
    qc_dir = os.path.join(QC_DIR, project)
    shots: list = []
    passed = failed = retry_count = 0

    if os.path.isdir(qc_dir):
        for fn in sorted(os.listdir(qc_dir)):
            if not fn.lower().endswith('.json'):
                continue
            # 文件名形如 <kind>_<键>.json（image_1.json / asset_image_七转蛊仙_base.json）
            kind, sep, shot_key = fn[:-5].partition('_')
            if not sep or not shot_key:
                continue
            try:
                with open(os.path.join(qc_dir, fn), 'r', encoding='utf-8') as f:
                    data = json.load(f) or {}
            except Exception as e:  # noqa: BLE001 - 单条坏文件不该拖垮总览
                app.logger.warning("质检历史读取失败（已跳过）：%s：%s", fn, e)
                continue
            records = data.get('records') or []
            latest = records[-1] if (records and isinstance(records[-1], dict)) else {}
            # 通过与否以记录里的 `passed` 为准；接口异常（ok=False）单独归入「待重试」
            if latest.get('passed') is True or data.get('last_passed') is True:
                verdict = 'pass'
            elif latest.get('ok') is False:
                verdict = 'error'
            elif latest.get('passed') is False or data.get('last_passed') is False:
                verdict = 'fail'
            else:
                verdict = 'unknown'
            shots.append({
                'shot_id': shot_key,
                'kind': kind,
                'stage': latest.get('stage') or '',
                'score': latest.get('score'),
                'verdict': verdict,
                'timestamp': latest.get('time') or data.get('updated_at') or '',
                'attempts': int(data.get('total_attempts') or len(records) or 0),
                'error': latest.get('error') or '',
                'reason': latest.get('reason') or '',
                'file': latest.get('file') or '',
            })
            if verdict == 'pass':
                passed += 1
            elif verdict == 'fail':
                failed += 1
            else:
                retry_count += 1

    view = qc_client.public_view(qc_cfg)
    return jsonify({
        "success": True,
        "project": project,
        "config": view,
        "stats": {"total": len(shots), "passed": passed, "failed": failed,
                  "retry_count": retry_count},
        "history": shots,
        "qc_dir": qc_dir,
    })


@app.route('/api/qc/frames/<path:filename>')
def api_qc_frame_file(filename):
    """回显视频质检抽帧图（只读）"""
    safe = filename.replace('\\', '/')
    target = os.path.abspath(os.path.join(QC_DIR, safe))
    root = os.path.abspath(QC_DIR)
    if not target.startswith(root):
        abort(403)
    if not os.path.exists(target):
        abort(404)
    return send_file(target, conditional=True)


# =====================================================================
# 新增模块 B：小说上传与解析
# =====================================================================

UPLOAD_TMP_DIR = os.path.join(NOVELS_DIR, "_uploads")


def _safe_upload_name(filename: str) -> str:
    """保留中文文件名，仅剥离路径与非法字符"""
    name = os.path.basename(str(filename or '').replace('\\', '/').split('/')[-1])
    name = re.sub(r'[<>:"|?*\x00-\x1f]', '_', name).strip().strip('.')
    return name or f"novel_{int(time.time())}.txt"


def _novels_stats(project_ref: str = None, include_unbound: bool = True):
    items = list_novels(NOVELS_DIR)
    # 标注每部小说当前归属的项目（一部小说 = 一个独立项目）
    for m in items:
        rec = project_store.find_by_novel(m.get("novel_id") or m.get("id"))
        m["project_id"] = (rec or {}).get("id", "")
        m["project_key"] = (rec or {}).get("dir_key", "")
        m["project_name"] = (rec or {}).get("name", "")
        m["bound"] = bool(rec)
    if project_ref:
        rec = project_store.get_project(project_ref)
        pid = (rec or {}).get("id") or project_ref
        filtered = [m for m in items if m["project_id"] == pid
                    or (include_unbound and not m["project_id"])]
    else:
        filtered = items
    return {
        "count": len(filtered),
        "total_chars": sum(int(m.get("char_count") or 0) for m in filtered),
        "novels": filtered,
        "all_count": len(items),
    }


@app.route('/api/novels/upload', methods=['POST'])
def api_upload_novels():
    """上传小说文件（支持多文件），解析为纯文本并入库"""
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False, "error": "未收到文件，请通过 file 字段上传"}), 400

    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    os.makedirs(NOVELS_DIR, exist_ok=True)
    # A：上传时可直接归属到某个项目（一部小说 = 一个独立项目）
    pref = (request.form.get('project_id') or request.args.get('project_id')
            or request.form.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else None
    # 无人值守生产要求「上传即建好项目」：未显式指定项目时，按小说自动建立独立项目，
    # 这样上传完成后即可直接进入「总控 AI 定风格 → 开启托管」，不需要用户手工建项目。
    auto_project = (request.form.get('auto_project') or request.args.get('auto_project')
                    or '1').strip() not in ('0', 'false', 'no')
    # 上传时用 LLM 归纳章节标题正则（每本书格式不同，纯正则易漏检）。
    # 取「文本分析模型」客户端是 best-effort：未配置/异常则传 None，退回纯正则切分，
    # 绝不阻断上传。
    novel_llm_client = _optional_llm_client()
    results, ok_count = [], 0
    for f in files:
        raw_name = _safe_upload_name(f.filename)
        if not raw_name:
            results.append({"filename": f.filename, "success": False, "error": "文件名为空"})
            continue
        ext = os.path.splitext(raw_name)[1].lower()
        tmp_path = os.path.join(
            UPLOAD_TMP_DIR,
            f"{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext or '.txt'}"
        )
        try:
            f.save(tmp_path)
            meta = ingest_novel(tmp_path, raw_name, NOVELS_DIR,
                                llm_client=novel_llm_client)
            ok_count += 1
            item_proj = proj
            if item_proj is None and auto_project:
                try:
                    item_proj = _resolve_novel_project({}, meta) or None
                except Exception as e:  # noqa: BLE001  建项目失败不该让上传整体失败
                    app.logger.warning(f"自动建项目失败（小说已入库）：{e}")
            if item_proj:
                item_proj = project_store.update_project(
                    item_proj["id"], novel_id=meta.get("novel_id") or "",
                    novel_name=meta.get("name") or raw_name) or item_proj
                proj = proj or item_proj
            results.append({
                "filename": raw_name, "success": True,
                "novel": {k: v for k, v in meta.items() if k != "chapters"},
                "chapter_preview": (meta.get("chapters") or [])[:5],
                "project_id": (item_proj or {}).get("id", ""),
                "project_key": (item_proj or {}).get("dir_key", ""),
                "project": {k: (item_proj or {}).get(k) for k in
                            ("id", "name", "dir_key", "novel_id")} if item_proj else None,
            })
        except NovelParseError as e:
            results.append({"filename": raw_name, "success": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            app.logger.error(f"小说解析失败 {raw_name}: {e}")
            results.append({"filename": raw_name, "success": False, "error": f"解析失败：{e}"})
        finally:
            # 清理临时文件属于「收尾」，绝不能因为它失败而让一个已经成功的上传变成
            # 无响应。除 OSError（Windows 杀软占用、共享冲突）外，某些运行环境注入的
            # 安全守卫会直接抛 SystemExit —— 它继承自 BaseException 而非 Exception，
            # Flask 不会把它转成 500，而是会掐断这次请求（客户端表现为挂起后空响应）。
            # 这里放宽到 BaseException，但保留 KeyboardInterrupt 的语义。
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except KeyboardInterrupt:
                raise
            except BaseException as _e:  # noqa: BLE001
                app.logger.warning(f"上传临时文件清理失败（忽略，不影响本次上传）：{_e!r}")

    return jsonify({
        "success": ok_count > 0,
        "uploaded": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
        "supported_exts": sorted(SUPPORTED_EXTS),
        "stats": _novels_stats(),
    }), (200 if ok_count else 400)


@app.route('/api/novels', methods=['GET'])
def api_list_novels():
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    include_unbound = (request.args.get('include_unbound', '1') not in ('0', 'false', 'no'))
    return jsonify({"success": True, **_novels_stats(pref or None, include_unbound),
                    "project_id": pref,
                    "supported_exts": sorted(SUPPORTED_EXTS)})


@app.route('/api/novels/<novel_id>', methods=['GET'])
def api_novel_detail(novel_id):
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({"success": True, "novel": meta,
                    "text_path": os.path.abspath(
                        os.path.join(NOVELS_DIR, meta.get("text_file") or f"{novel_id}.txt"))})


@app.route('/api/novels/<novel_id>/preview', methods=['GET'])
def api_novel_preview(novel_id):
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', NOVEL_PREVIEW_CHARS, type=int)
    try:
        data = preview_novel(NOVELS_DIR, novel_id, offset=offset, limit=limit)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({"success": True, **data})


# =====================================================================
# 新增模块 C：小说 → A 版剧本（全量分块 + 原文覆盖率校验：不删减原文，只做体裁改写）
# =====================================================================

def _novel_convert_worker(task_id: str, novel_meta: dict, style: str, episodes: int,
                          target_shots: int, project_key: str = None):
    def cb(phase, current, total, message, percent):
        with lock:
            generation_state[task_id].update({
                "phase": phase, "current": current, "total": total,
                "message": message, "progress": percent,
            })

    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        client = _current_llm_client()
        script = novel_to_script.convert_novel_to_script(
            client, novel_meta, text, style=style, episodes=episodes,
            target_shots=target_shots, progress_cb=cb,
            continuity_dir=CONTINUITY_DIR, project_key=project_key,
        )
        path = novel_to_script.save_generated_script(script, SCRIPT_DIR, project_key=project_key)
        if project_key:
            try:
                project_store.bind_script(project_key, path, project_store.script_stats(path))
            except Exception as be:  # noqa: BLE001
                app.logger.warning(f"项目剧本登记失败（{project_key}）：{be}")
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "script_path": path, "script": script,
                "project_name": script.get("metadata", {}).get("project_name"),
                "project_key": project_key,
                # 原文覆盖率摘要（含遗漏清单预览 / 补生成镜头数 / 复检轨迹），前端可直接展示
                "coverage": (script.get("metadata") or {}).get("coverage") or {},
                "coverage_report_path": (script.get("metadata") or {}).get("coverage_report_path"),
                "warnings": (script.get("metadata") or {}).get("warnings") or [],
                "episode_stats": {
                    "shot_count": script.get("shot_count") or len(script.get("shots") or []),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                    "episode_plan": script.get("episode_plan"),
                },
                "stats": {
                    "characters": len(script.get("characters") or []),
                    "items": len(script.get("items") or []),
                    "scenes": len(script.get("scenes") or []),
                    "shots": len(script.get("shots") or []),
                    "shot_count": script.get("shot_count"),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                },
            })
    except (LLMError, NovelParseError) as e:
        app.logger.error(f"小说转剧本失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("小说转剧本异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"转换异常：{e}"})


@app.route('/api/novels/<novel_id>/convert', methods=['POST'])
def api_novel_convert(novel_id):
    """用自定义 API 把已上传小说转成 A 版剧本（后台任务）"""
    try:
        novel_meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法把小说转成剧本")

    data = request.json or {}
    style = (data.get('style') or '3D动漫渲染').strip()
    episodes = max(1, min(int(data.get('episodes') or 1), 12))
    target_shots = max(4, min(int(data.get('target_shots') or NOVEL_DEFAULT_SHOTS), 40))
    # A：小说 → 独立项目（未指定则自动建立该项目，数据落在项目自己的目录）
    proj = _resolve_novel_project(data, novel_meta)
    style = _apply_project_settings(style, data.get('project_name') or novel_id)

    task_id = f"novel2script_{novel_id}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": "正在准备分块…", "novel_id": novel_id,
            "project_id": proj["id"], "project_key": proj["dir_key"],
            "chunk_chars": NOVEL_CHUNK_CHARS, "max_chunks": NOVEL_MAX_CHUNKS,
        }
    threading.Thread(target=_novel_convert_worker,
                     args=(task_id, novel_meta, style, episodes, target_shots,
                           proj["dir_key"]),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "novel_id": novel_id, "style": style, "episodes": episodes,
                    "target_shots": target_shots,
                    "project_id": proj["id"], "project_key": proj["dir_key"],
                    "project_name": proj["name"],
                    "char_count": novel_meta.get("char_count"),
                    "chapter_count": novel_meta.get("chapter_count")})


# =====================================================================
# 新增模块 C2：按章节分集生成（每章一集，独立落盘 output/scripts/<小说名>/第N集.json）
# =====================================================================

EPISODE_BATCH_LIMIT = 30          # 单次批量生成集数上限（保护后台任务）


def _novel_key(novel_meta: dict, project_ref: str = None) -> str:
    """剧集目录/项目名前缀所用的稳定键：优先取项目注册表分配的项目键。

    一部小说 = 一个独立项目 → 剧本落盘 output/scripts/<项目键>/，与其它小说彻底隔离。
    """
    rec = project_store.get_project(project_ref) if project_ref else None
    if not rec:
        rec = project_store.find_by_novel(novel_meta.get("novel_id") or novel_meta.get("id"))
    if rec:
        return rec["dir_key"]
    raw = (novel_meta.get("name") or novel_meta.get("title")
           or novel_meta.get("novel_id") or "novel")
    cleaned = re.sub(r"[《》〈〉【】「」『』\s]+", "", str(raw)).strip()
    return cleaned or str(novel_meta.get("novel_id") or "novel")


def _estimate_subchunks(char_count: int) -> int:
    """不读全文的二次分块数量估算（用于章节列表）"""
    return novel_to_script.estimate_subchunks(char_count)


def _resolve_novel_project(data: dict, novel_meta: dict) -> dict:
    """把当前操作绑定到项目：显式指定优先，否则按小说自动建立/复用独立项目。"""
    data = data or {}
    ref = (data.get('project_id') or data.get('project_name') or '').strip()
    rec = project_store.get_project(ref) if ref else None
    if rec is None:
        rec = project_store.ensure_project_for_novel(
            novel_meta.get("novel_id") or novel_meta.get("id") or "",
            novel_meta.get("name") or novel_meta.get("title") or "")
    return rec


def _episode_map(novel_meta: dict, project_ref: str = None) -> dict:
    """章节序号 -> 已生成剧集信息"""
    eps = novel_to_script.list_episodes(SCRIPT_DIR, _novel_key(novel_meta, project_ref))
    out = {}
    for e in eps:
        key = e.get("chapter_index")
        out[int(key if key is not None else e.get("episode_no"))] = e
    return out


@app.route('/api/novels/<novel_id>/chapters', methods=['GET'])
def api_novel_chapters(novel_id):
    """章节列表（章名 / 字数 / 规模提示 / 已生成剧集状态）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    pkey = proj["dir_key"] if proj else None
    key = _novel_key(meta, pkey)
    ep_map = _episode_map(meta, pkey)
    raw = meta.get("chapters") or []
    chapters = []
    for c in raw:
        idx = int(c.get("index") or (len(chapters) + 1))
        cnt = int(c.get("char_count") or 0)
        sub = _estimate_subchunks(cnt)
        gen = ep_map.get(idx)
        chapters.append({
            "index": idx,
            "episode_no": idx,          # 每章一集：集号 = 章序号
            "title": c.get("title") or f"第{idx}章",
            "char_count": cnt,
            "start": c.get("start"), "end": c.get("end"),
            "est_subchunks": sub,
            "too_short": cnt < novel_to_script.CHAPTER_MIN_CHARS,
            "advice": novel_to_script.chapter_advice(cnt, sub)["message"],
            "generated": bool(gen),
            "episode": gen or None,
        })

    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "title": meta.get("title") or meta.get("name"),
        "name": meta.get("name"),
        "char_count": meta.get("char_count"),
        "chapter_count": len(chapters),
        "fallback": not chapters,
        "fallback_hint": ("未识别到章节标记，无法按章分集；可先用「AI 转成剧本」整本处理，"
                          "或上传带「第N章」标题的小说") if not chapters else "",
        "min_chars": novel_to_script.CHAPTER_MIN_CHARS,
        "chunk_chars": novel_to_script.CHAPTER_CHUNK_CHARS,
        "max_subchunks": novel_to_script.CHAPTER_MAX_SUBCHUNKS,
        "batch_limit": EPISODE_BATCH_LIMIT,
        "generated_count": len(ep_map),
        "chapters": chapters,
    })


def _salvage_episode_script(path: str, episode_no: int):
    """任务报错后检查剧本产物是否其实可用（**以产物为准**，避免误报失败）

    2026-09-17 E2E 实测：分集任务报「模型未返回有效分镜…未知错误」，
    但 `第1集.json` 其实已落盘、6 镜有效、覆盖率 100%、也已被 /api/episodes 收录 ——
    用户看到 "失败" 会以为白跑一趟。产物存在且镜头非空时，按成功回填统计字段。

    返回可直接并入 results 的统计 dict；产物缺失/不可用则返回 None。
    """
    try:
        if not (path and os.path.isfile(path) and os.path.getsize(path) > 200):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    shots = data.get("shots") or []
    if not shots:
        return None
    meta = data.get("metadata") or {}
    try:
        stats = meta.get("episode_stats") or novel_to_script.build_episode_stats(shots)
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "shots": len(shots),
        "shot_count": int(data.get("shot_count") or stats.get("shot_count") or len(shots)),
        "episode_duration_sec": data.get("episode_duration_sec") or stats.get("duration_sec"),
        "characters": len(data.get("characters") or []),
        "items": len(data.get("items") or []),
        "scenes": len(data.get("scenes") or []),
        "elapsed_sec": meta.get("elapsed_sec"),
        "warnings": meta.get("warnings") or [],
    }


def _episode_units_for_chapters(novel_meta: dict, chapters: list) -> list:
    """把「用户选中的章」展开成**拍摄单元**（超长章会拆成多集）。

    ⚠️ 单元编号必须基于**全量章节**展开（口径 = ``autopilot.episode_units``），
    不能用传入的子集 —— 否则同一章在「手动选集生成」与「托管」两条链路上会拿到
    不同的集号，产物（``第N集.json`` / 成片 / 验收记录）互相错位。
    """
    selected = [int(c.get("index") or 0) for c in (chapters or [])]
    if not selected:
        return []
    try:
        all_chapters, text = autopilot.chapters_and_text(novel_meta)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("章节列表读取失败（按一章一集处理）：%s", e)
        all_chapters, text = [], ""
    if all_chapters:
        try:
            units = autopilot.episode_units(all_chapters, {"episodes": selected}, text)
            if units:
                return units
        except Exception as e:  # noqa: BLE001
            app.logger.warning("拆章失败（按一章一集处理）：%s", e)
    # 兜底：拿不到全量章节时退回「一章一集」（与历史行为一致）
    return [{"episode_no": int(c.get("index") or i + 1),
             "chapter_index": int(c.get("index") or i + 1),
             "part": 1, "parts": 1, "chapter": c}
            for i, c in enumerate(chapters or [])]


def _episodes_worker(task_id: str, novel_meta: dict, chapters: list, style: str,
                     target_shots: int, overwrite: bool, project_key: str = None):
    key = _novel_key(novel_meta, project_key)
    # 拍摄单元：超长章按语义边界拆成多集（单集镜头数硬上限见 novel_to_script）
    units = _episode_units_for_chapters(novel_meta, chapters)
    total = len(units)

    def report(ep_ordinal, chapter, phase, message, inner_percent):
        overall = int(((ep_ordinal - 1) + (inner_percent or 0) / 100.0) / total * 100)
        with lock:
            generation_state[task_id].update({
                "phase": phase, "progress": min(99, max(1, overall)),
                "current": ep_ordinal, "total": total,
                "current_chapter": chapter.get("title"),
                "current_episode": chapter.get("index"),
                "message": message,
            })

    try:
        text = read_novel_text(NOVELS_DIR, novel_meta["novel_id"])
        client = _current_llm_client()
        if not client.configured:
            raise LLMError("尚未配置自定义 AI 接口")

        results = []
        for i, unit in enumerate(units):
            ep = int(unit["episode_no"])
            chapter = unit["chapter"]
            ch_title = chapter.get("title") or f"第{ep}集"
            out_path = novel_to_script.episode_script_path(SCRIPT_DIR, key, ep)

            if os.path.isfile(out_path) and not overwrite:
                info = None
                for e in novel_to_script.list_episodes(SCRIPT_DIR, key):
                    if int(e.get("episode_no")) == ep:
                        info = e
                        break
                results.append({"episode_no": ep, "chapter_title": ch_title,
                                "status": "skipped", "path": out_path,
                                "project_key": key,
                                "message": "该集已存在，跳过（可勾选覆盖重新生成）",
                                "shots": (info or {}).get("shots", 0),
                                "shot_count": (info or {}).get("shot_count"),
                                "episode_duration_sec": (info or {}).get("episode_duration_sec")})
                report(i + 1, chapter, "skip", f"第{ep}集已存在，跳过", 100)
                continue

            report(i + 1, chapter, "chapter",
                   f"第{ep}集《{ch_title}》：准备章节正文…", 2)

            def cb(phase, cur, tot, msg, pct, _ep=ep, _ord=i + 1, _ch=chapter, _t=ch_title):
                report(_ord, _ch, phase, f"第{_ep}集《{_t}》 {msg}", pct)

            try:
                # 跨集连贯性（A/B/C/D）：项目级 bible + 上集摘要卡 + 衔接契约 + state 锚点
                # + 相邻集六类校验 + 命中高危问题时的局部重写，全部由 continuity 编排
                conv = continuity.convert_chapter_with_continuity(
                    client, novel_meta, text, chapter, key, CONTINUITY_DIR,
                    style=style, target_shots=target_shots, episode_no=ep,
                    save_dir=SCRIPT_DIR, progress_cb=cb,
                )
                script = conv["script"]
                validation = conv.get("validation") or {}
                path = (conv.get("script_path")
                        or novel_to_script.save_episode_script(script, SCRIPT_DIR, key, ep, key))
                # 项目登记：把剧本及其镜头数/每集时长统计写回项目注册表
                if project_key:
                    try:
                        project_store.bind_script(
                            project_key, path, project_store.script_stats(path))
                    except Exception as be:  # noqa: BLE001
                        app.logger.warning(f"项目剧本登记失败（{project_key}）：{be}")
                # 剧本体检：兜底镜头（dialogue=[] 且 prompt_h3=""）会在配音环节变成
                # 「一句都合不出来」，但这里看起来是「生成成功」，必须把缺口显式带出
                _audit = dialogue_utils.audit_script(script)
                _meta_warnings = list(script["metadata"].get("warnings") or [])
                if _audit["warnings"]:
                    _meta_warnings.extend(_audit["warnings"])
                    app.logger.warning(f"第{ep}集剧本存在内容缺口：{_audit['warnings']}")
                # P0-3 剧本↔原著一致性（三件套 + 定向修复）结果，随生成结果带出给前端
                _sc = script["metadata"].get("script_consistency") or {}
                results.append({
                    "episode_no": ep, "chapter_title": ch_title, "status": "success",
                    "path": path, "project_name": script["metadata"]["project_name"],
                    "project_key": key,
                    "shots": len(script.get("shots") or []),
                    "shot_count": script.get("shot_count") or len(script.get("shots") or []),
                    "episode_duration_sec": script.get("episode_duration_sec"),
                    "characters": len(script.get("characters") or []),
                    "items": len(script.get("items") or []),
                    "scenes": len(script.get("scenes") or []),
                    "chunks_total": script["metadata"].get("chunks_total"),
                    "chunks_used": script["metadata"].get("chunks_used"),
                    "elapsed_sec": script["metadata"].get("elapsed_sec"),
                    "warnings": _meta_warnings,
                    "script_audit": _audit["stats"],
                    "script_audit_ok": _audit["ok"],
                    "continuity_score": (script["metadata"].get("continuity") or {}).get("validation_score"),
                    "continuity_issues": len(validation.get("issues") or []),
                    "continuity_issue_stats": validation.get("issue_stats") or {},
                    "continuity_rewrite": bool((conv.get("rewrite") or {}).get("triggered")),
                    "continuity_rewrite_shots": (conv.get("rewrite") or {}).get("rewritten_shot_ids") or [],
                    "continuity_dir": continuity.continuity_root(CONTINUITY_DIR, key),
                    "coverage_percent": (conv.get("coverage") or {}).get("coverage_percent"),
                    "coverage_plot_percent": (conv.get("coverage") or {}).get("plot_coverage_percent"),
                    "coverage_detail_percent": (conv.get("coverage") or {}).get("detail_coverage_percent"),
                    "coverage_detail_passed": (conv.get("coverage") or {}).get("detail_passed"),
                    "coverage_passed": (conv.get("coverage") or {}).get("passed"),
                    "coverage_threshold_percent": (conv.get("coverage") or {}).get("threshold_percent"),
                    "coverage_missing": (conv.get("coverage") or {}).get("missing_count"),
                    "coverage_zero_omission": (conv.get("coverage") or {}).get("zero_omission"),
                    "coverage_supplement_shots": (conv.get("coverage") or {}).get("supplement_shots"),
                    "coverage_supplement_rounds": (conv.get("coverage") or {}).get("supplement_rounds"),
                    "coverage_report_path": (conv.get("coverage") or {}).get("report_path"),
                    # P0-3 剧本↔原著一致性：三件套 + 定向修复闭环
                    "consistency_passed": _sc.get("passed"),
                    "consistency_chapter_index": _sc.get("chapter_index"),
                    "consistency_anchor_checked": _sc.get("anchor_checked"),
                    "consistency_anchor_ok": _sc.get("anchor_ok"),
                    "consistency_anchor_deviation": _sc.get("anchor_deviation"),
                    "consistency_anchor_reason": _sc.get("anchor_reason"),
                    "consistency_leak_count": _sc.get("leak_count"),
                    "consistency_leak_shot_ids": _sc.get("leak_shot_ids") or [],
                    "consistency_element_percent": _sc.get("element_coverage_percent"),
                    "consistency_element_missing_count": _sc.get("element_missing_count"),
                    "consistency_element_missing": [e.get("name") for e in (_sc.get("element_missing") or [])],
                    "consistency_fix_rounds": _sc.get("fix_rounds"),
                    "consistency_fixed": _sc.get("fixed"),
                    "consistency_issue_count": _sc.get("issue_count"),
                    "consistency_issue_stats": _sc.get("issue_stats") or {},
                    "consistency_report_path": _sc.get("report_path"),
                    "message": "生成完成",
                })
            except Exception as e:  # noqa: BLE001
                app.logger.error(f"第{ep}集生成失败: {e}")
                # 以产物为准：模型抖动/校验失败时报错，但剧本可能已经落盘且可用
                salvaged = _salvage_episode_script(out_path, ep)
                if salvaged:
                    app.logger.warning(
                        f"第{ep}集虽报错但剧本产物可用，已按成功回填：{out_path}")
                    results.append({
                        "episode_no": ep, "chapter_title": ch_title,
                        "status": "success", "degraded": True,
                        "path": out_path, "project_key": key, **salvaged,
                        "message": f"生成过程报错，但剧本已落盘且可用（已按产物回填为成功）：{e}",
                        "error": str(e),
                    })
                else:
                    results.append({"episode_no": ep, "chapter_title": ch_title,
                                    "status": "failed", "path": out_path,
                                    "message": str(e)})

        ok = [r for r in results if r["status"] == "success"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]
        degraded = [r for r in ok if r.get("degraded")]
        episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok or skipped else "failed",
                "progress": 100, "current": total, "total": total,
                "message": (f"批量完成：成功 {len(ok)} 集"
                            + (f"（其中 {len(degraded)} 集过程报错但产物可用）" if degraded else "")
                            + f" / 跳过 {len(skipped)} 集 / 失败 {len(failed)} 集"),
                "degraded_count": len(degraded),
                "error": "" if (ok or skipped) else (failed[0]["message"] if failed else "全部失败"),
                "results": results, "episodes": episodes,
                "novel_id": novel_meta.get("novel_id"),
                "project_key": key,
                "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
            })
    except (LLMError, NovelParseError) as e:
        app.logger.error(f"分集生成失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("分集生成异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分集生成异常：{e}"})


@app.route('/api/novels/<novel_id>/episodes/generate', methods=['POST'])
def api_novel_episodes_generate(novel_id):
    """按章节分集生成：单章生成 / 批量生成多集（每章一集）

    body: {chapters:[1,2,3] | start:1,end:3, style, target_shots, overwrite}
    """
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法按章生成剧本")

    all_chapters = meta.get("chapters") or []
    if not all_chapters:
        return jsonify({"success": False,
                        "error": "该小说未识别到章节标记，无法按章分集；请改用「AI 转成剧本」整本处理"}), 400

    data = request.json or {}
    style = (data.get('style') or '3D动漫渲染').strip() or '3D动漫渲染'
    target_shots = max(4, min(int(data.get('target_shots') or NOVEL_DEFAULT_SHOTS), 40))
    overwrite = bool(data.get('overwrite'))
    style = _apply_project_settings(style, data.get('project_name') or novel_id)

    by_index = {}
    for c in all_chapters:
        by_index[int(c.get("index") or 0)] = c

    requested = data.get('chapters')
    if isinstance(requested, (str, int)):
        requested = [requested]
    picks = []
    if isinstance(requested, list) and requested:
        for x in requested:
            try:
                xi = int(x)
            except (TypeError, ValueError):
                continue
            if xi in by_index and xi not in [p.get("index") for p in picks]:
                picks.append(by_index[xi])
    else:
        start = data.get('start')
        end = data.get('end')
        try:
            s = int(start) if start is not None else None
            e = int(end) if end is not None else None
        except (TypeError, ValueError):
            s = e = None
        if s is not None or e is not None:
            lo = s if s is not None else 1
            hi = e if e is not None else max(by_index)
            picks = [c for idx, c in sorted(by_index.items()) if lo <= idx <= hi]

    if not picks:
        return jsonify({"success": False, "error": "未选择有效章节（chapters 或 start/end 至少提供一项）"}), 400
    if len(picks) > EPISODE_BATCH_LIMIT:
        return jsonify({"success": False,
                        "error": f"单次批量最多 {EPISODE_BATCH_LIMIT} 集，本次选择了 {len(picks)} 集；请缩小范围"}), 400

    picks.sort(key=lambda c: int(c.get("index") or 0))
    # A：绑定/自动建立该项目，剧本与后续产物全部落在该项目目录
    proj = _resolve_novel_project(data, meta)
    key = _novel_key(meta, proj["dir_key"])
    ep_dir = os.path.abspath(os.path.join(SCRIPT_DIR, key))

    # ⚠️ 互斥（2026-09-17 E2E 实测教训）：同一篇小说若已有分集生成任务在跑，
    # 再派一次会让两轮并发处理同一章 —— 互相抢模型配额（实测把接口打成 503 风暴），
    # 结果「一个成功写盘 + 一个 failed」，用户看到失败但产物其实是好的（覆盖率 100%）。
    # 规则：章节有重叠 → 直接复用正在跑的那个任务；章节不重叠 → 允许并发。
    # 旧任务没记 picks（历史数据）时保守视为冲突。
    want = {int(c.get("index") or 0) for c in picks}
    with lock:
        for _tid, _st in list(generation_state.items()):
            if not (isinstance(_st, dict) and _st.get("status") == "running"
                    and str(_tid).startswith("episodes_")):
                continue
            if _st.get("novel_id") != meta.get("novel_id"):
                continue
            _running = {int(x) for x in (_st.get("picks") or [])}
            if _running and not (_running & want):
                continue                      # 章节不重叠，互不干扰
            return jsonify({
                "success": True, "reused": True, "task_id": _tid, "status": "running",
                "novel_id": meta.get("novel_id"),
                "message": (f"该小说已有分集生成任务在跑"
                            f"（{_st.get('current') or 0}/{_st.get('total') or 0} 集），"
                            "本次请求已复用它 —— 避免同一章被并发生成两次"),
                "chapters": [{"index": c.get("index"), "title": c.get("title"),
                              "char_count": c.get("char_count")} for c in picks],
                "episodes": [c.get("index") for c in picks],
                "total": len(picks),
                "project_id": proj["id"], "project_key": key,
                "episode_dir": ep_dir,
            })

    task_id = f"episodes_{meta.get('novel_id')}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": f"准备生成 {len(picks)} 集…", "current": 0, "total": len(picks),
            "novel_id": meta.get("novel_id"), "results": [],
            # 记录本任务负责的章节，供上面的互斥判断比对重叠
            "picks": sorted(int(c.get("index") or 0) for c in picks),
            "project_id": proj["id"], "project_key": key,
            "episode_dir": ep_dir,
        }
    threading.Thread(target=_episodes_worker,
                     args=(task_id, meta, picks, style, target_shots, overwrite, key),
                     daemon=True).start()
    return jsonify({
        "success": True, "task_id": task_id, "status": "started",
        "novel_id": meta.get("novel_id"), "style": style, "target_shots": target_shots,
        "project_id": proj["id"], "project_key": key, "project_name": proj["name"],
        "episode_dir": ep_dir,
        "chapters": [{"index": c.get("index"), "title": c.get("title"),
                      "char_count": c.get("char_count")} for c in picks],
        "episodes": [c.get("index") for c in picks],
        "total": len(picks),
    })


def _episode_progress(project_name: str, episode_no: int, shot_count: int = 0) -> dict:
    """从磁盘真实产物推导单集的进度与状态（界面回显的唯一依据）

    背景（P1 线上问题）
    ------------------
    `novel_to_script.list_episodes` 只返回镜头数/覆盖率等字段，**从不返回 status / completed_shots**。
    前端 `EpisodeInfo.status` 因此恒为 undefined，`completed_shots` 恒为 undefined，于是：
      - 状态徽标一律落到兜底分支 → 第 1 集跑完 4 小时（含 final 失败）仍显示「○ 待生产」；
      - 进度显示「0 / 24 镜头」；
      - 概览统计「已完成 0 / 生产中 0」；
    用户完全无法从界面判断任务是否结束、成功还是失败，体验等同卡死。

    修复：以磁盘产物 + 生产历史为准推导状态，前端拿到的是真实进度。
    判定顺序（先看终态，再看进行中，最后看未开始）：
      1) 成片存在                  → done
      2) 最近一次生产记录为失败      → failed（并把错误原文回显）
      3) 托管正在跑这一集 或 已有中间产物 → producing
      4) 其余                      → pending
    """
    proj = _safe_project(project_name or "")
    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    def _count(d: str, pattern: str) -> int:
        try:
            return sum(1 for f in os.listdir(d) if re.match(pattern, f))
        except OSError:
            return 0

    sb_dir = _ep_read_dir(STORYBOARDS_DIR, proj, ep)
    vid_dir = _ep_read_dir(VIDEOS_DIR, proj, ep)
    storyboards = _count(sb_dir, r"^shot_\d+\.png$")
    shot_videos = _count(vid_dir, r"^shot_\d+\.mp4$")
    full_video = ""
    for cand in (f"ep{ep:02d}_full.mp4", "episode_full.mp4"):
        p = os.path.join(vid_dir, cand)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            full_video = p
            break
    final_file = os.path.join(FINAL_DIR, proj, f"ep{ep:02d}_final.mp4")
    final_ready = os.path.isfile(final_file) and os.path.getsize(final_file) > 0

    # 生产历史：取该集**最后一条**记录（不按时间窗过滤，否则老失败会被漏掉）
    last_run = None
    try:
        hp = os.path.join(PROJECT_OUTPUT_DIR, "autopilot", proj, "history.jsonl")
        if os.path.isfile(hp):
            with open(hp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(row.get("episode_no") or 0) == ep:
                        last_run = row
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"读取生产历史失败（{proj} 第{ep}集）：{e}")

    # 托管是否正在跑这一集
    running_this = False
    try:
        cur = (autopilot.status(proj) or {}).get("current") or {}
        running_this = (cur.get("project") == proj and int(cur.get("episode") or 0) == ep
                        and bool((autopilot.status(proj) or {}).get("running")))
    except Exception:  # noqa: BLE001
        running_this = False

    total = int(shot_count or 0)
    completed = storyboards
    if final_ready:
        status = "done"
    elif last_run is not None and not last_run.get("ok") and \
            str(last_run.get("status") or "") == "failed":
        status = "failed"
    elif running_this or storyboards or shot_videos or full_video:
        status = "producing"
    else:
        status = "pending"

    # 成片已出但镜头没画齐，同样视为「完成」但标注不齐（与交付物登记口径一致）
    incomplete = bool(final_ready and total and completed < total)
    return {
        "status": status,
        "completed_shots": min(completed, total) if total else completed,
        "storyboard_count": storyboards,
        "shot_video_count": shot_videos,
        "full_video": bool(full_video),
        "final_ready": final_ready,
        "final_file": final_file if final_ready else "",
        "incomplete_shots": incomplete,
        "last_run": ({
            "ok": last_run.get("ok"),
            "status": last_run.get("status"),
            "elapsed_sec": last_run.get("elapsed_sec"),
            "error": last_run.get("error") or "",
            "at": last_run.get("at"),
            "steps": last_run.get("steps") or {},
        } if last_run else None),
    }


@app.route('/api/episodes/<novel_id>', methods=['GET'])
def api_list_episodes(novel_id):
    """某小说已生成的剧集清单（含从磁盘产物推导的真实状态与进度）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    # 支持 ?project_id= 指定项目查看（不传时自动按小说归属的项目）
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
    # 逐集补齐状态 / 进度（前端 EpisodeInfo.status、completed_shots 的数据源）。
    # ⚠️ 产物目录（storyboards/videos/final/autopilot）用的是项目 dir_key（= key），
    #    而非剧本 meta.project_name（那是「每集独立项目名」，如 极短小说_雨夜归人_第1集）。
    for row in episodes:
        try:
            row.update(_episode_progress(key, int(row.get("episode_no") or 1),
                                         int(row.get("shot_count") or 0)))
        except Exception as e:  # noqa: BLE001  单集探测失败不拖垮整表
            app.logger.warning(f"第{row.get('episode_no')}集进度探测失败：{e}")
            row.setdefault("status", "pending")
            row.setdefault("completed_shots", 0)
    done = sum(1 for r in episodes if r.get("status") == "done")
    failed = sum(1 for r in episodes if r.get("status") == "failed")
    producing = sum(1 for r in episodes if r.get("status") == "producing")
    return jsonify({
        "success": True, "novel_id": novel_id, "name": key,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "novel_title": meta.get("title") or meta.get("name"),
        "chapter_count": meta.get("chapter_count"),
        "count": len(episodes),
        "total": len(episodes),
        "stats": {"total": len(episodes), "done": done, "failed": failed,
                  "producing": producing,
                  "pending": len(episodes) - done - failed - producing},
        "episode_dir": os.path.abspath(os.path.join(SCRIPT_DIR, key)),
        "episodes": episodes,
    })


@app.route('/api/episodes/<novel_id>/<int:episode_no>', methods=['GET'])
def api_get_episode(novel_id, episode_no):
    """读取单集剧本（供切集预览 / 载入后续步骤）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    try:
        script = novel_to_script.load_episode_script(SCRIPT_DIR, key, episode_no)
    except novel_to_script.EpisodeNotFoundError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 500

    meta_i = script.get("metadata") or {}
    dir_info = os.path.abspath(os.path.join(SCRIPT_DIR, key))
    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "episode_no": episode_no,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "episode_title": script.get("episode_title") or meta_i.get("chapter_title"),
        "chapter_index": meta_i.get("chapter_index"),
        "project_name": meta_i.get("project_name") or novel_to_script.episode_project_name(key, episode_no),
        "script_path": novel_to_script.episode_script_path(SCRIPT_DIR, key, episode_no),
        "episode_dir": dir_info,
        "script": script,
        "stats": {
            "characters": len(script.get("characters") or []),
            "items": len(script.get("items") or []),
            "scenes": len(script.get("scenes") or []),
            "shots": len(script.get("shots") or []),
            "shot_count": script.get("shot_count") or len(script.get("shots") or []),
            "episode_duration_sec": script.get("episode_duration_sec"),
            "duration_per_shot_sec": script.get("duration_per_shot_sec"),
        },
        "episode_plan": script.get("episode_plan"),
        "warnings": meta_i.get("warnings") or [],
        "chunks_total": meta_i.get("chunks_total"),
        "chunks_used": meta_i.get("chunks_used"),
        "chapter_char_count": meta_i.get("chapter_char_count"),
        "generated_at": meta_i.get("generated_at"),
        "continuity_meta": meta_i.get("continuity") or None,
        "coverage_meta": meta_i.get("coverage") or None,
        "coverage_report_path": meta_i.get("coverage_report_path"),
        "state_in": script.get("state_in"),
        "state_out": script.get("state_out"),
        "continuity": continuity.episode_continuity_view(CONTINUITY_DIR, key, episode_no),
    })


# =====================================================================
# 跨集连贯性（相邻两章转剧本改进 A/B/C/D）：项目级设定库 / 摘要卡 / state / 校验 查询
# =====================================================================

def _resolve_continuity_key(novel_id):
    """小说 → (meta, 项目记录, 项目键)，供连贯性查询接口复用"""
    meta = get_novel(NOVELS_DIR, novel_id)
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    return meta, proj, _novel_key(meta, proj["dir_key"] if proj else None)


@app.route('/api/continuity/<novel_id>', methods=['GET'])
def api_continuity_overview(novel_id):
    """项目级连贯性总览：bible / style_guide / 金句清单 / 口吻词典 / 运镜术语表 / 各集文件清单"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.continuity_overview(CONTINUITY_DIR, key)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", ""),
                 "novel_title": meta.get("title") or meta.get("name")})
    return jsonify(data)


@app.route('/api/continuity/<novel_id>/<int:episode_no>', methods=['GET'])
def api_continuity_episode(novel_id, episode_no):
    """单集连贯性：上集摘要卡 / 本集 state_in·state_out / 跨集一致性校验结果"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.episode_continuity_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)


@app.route('/api/coverage/<novel_id>', methods=['GET'])
def api_coverage_overview(novel_id):
    """项目级原文覆盖率总览（④⑤）：逐集覆盖率百分比 / 阈值 / 是否达标 / 遗漏数 / 补生成镜数"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
    rows = []
    for e in eps:
        ep = int(e.get("episode_no") or 0)
        rep = coverage.load_coverage_report(CONTINUITY_DIR, key, ep)
        if rep:
            row = coverage.summary_for_meta(rep)
            row.update({"episode_no": ep, "available": True,
                        "chapter_title": e.get("chapter_title"),
                        "script_path": e.get("path")})
        else:
            row = {"episode_no": ep, "available": False,
                   "coverage_percent": None, "passed": None,
                   "chapter_title": e.get("chapter_title"),
                   "script_path": e.get("path"),
                   "note": "该集尚无覆盖率报告（未按新流程重跑）"}
        rows.append(row)
    return jsonify({
        "success": True, "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "novel_title": meta.get("title") or meta.get("name"),
        "project_key": key,
        "threshold_percent": round(float(getattr(novel_to_script, "COVERAGE_THRESHOLD", 0.95)) * 100, 2),
        "coverage_dir": os.path.abspath(os.path.join(CONTINUITY_DIR, key, "episodes")),
        "count": len(rows),
        "episodes": rows,
    })


@app.route('/api/coverage/<novel_id>/<int:episode_no>', methods=['GET'])
def api_coverage_episode(novel_id, episode_no):
    """单集原文覆盖率详情（⑤）：覆盖率 / 阈值 / 遗漏清单 / 补生成记录 / 逐轮复检轨迹"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = continuity.episode_coverage_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)


@app.route('/api/script-consistency/<novel_id>', methods=['GET'])
def api_script_consistency_overview(novel_id):
    """P0-3 剧本↔原著一致性总览：逐集三件套结论 / 泄漏数 / 要素覆盖率 / 是否通过 / 报告路径
    （注意与 /api/consistency/*「资产多视图一致性」区分：本组专指剧本 ↔ 本章原著）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    eps = novel_to_script.list_episodes(SCRIPT_DIR, key)
    rows = []
    for e in eps:
        ep = int(e.get("episode_no") or 0)
        rep = script_consistency.load_consistency_report(CONTINUITY_DIR, key, ep)
        if rep:
            row = script_consistency.summary_for_meta(
                rep, script_consistency.consistency_report_path(CONTINUITY_DIR, key, ep))
            row.update({"episode_no": ep, "available": True,
                        "chapter_title": e.get("chapter_title"),
                        "script_path": e.get("path")})
        else:
            row = {"episode_no": ep, "available": False, "passed": None,
                   "chapter_title": e.get("chapter_title"),
                   "script_path": e.get("path"),
                   "note": "该集尚无一致性校验报告（未按新流程重跑）"}
        rows.append(row)
    return jsonify({
        "success": True, "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "novel_title": meta.get("title") or meta.get("name"),
        "project_key": key,
        "algorithm": script_consistency.SCRIPT_CONSISTENCY_VERSION,
        "consistency_dir": os.path.abspath(os.path.join(CONTINUITY_DIR, key, "episodes")),
        "count": len(rows),
        "episodes": rows,
    })


@app.route('/api/script-consistency/<novel_id>/<int:episode_no>', methods=['GET'])
def api_script_consistency_episode(novel_id, episode_no):
    """单集 P0-3 一致性详情：章节锚定 / 元信息泄漏 / 要素覆盖 / 问题清单 / 定向修复轨迹"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    data = script_consistency.episode_consistency_view(CONTINUITY_DIR, key, episode_no)
    data.update({"success": True, "novel_id": novel_id,
                 "project_id": (proj or {}).get("id", "")})
    return jsonify(data)


@app.route('/api/continuity/<novel_id>/<int:episode_no>/revalidate', methods=['POST'])
def api_continuity_revalidate(novel_id, episode_no):
    """对已落盘剧本重跑「相邻集六类一致性校验」（D⑨ 可选闸门；body.rewrite=true 时命中问题会局部重写）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法执行一致性校验")
    try:
        script = novel_to_script.load_episode_script(SCRIPT_DIR, key, episode_no)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"剧本读取失败：{e}"}), 404

    state = continuity.load_state(CONTINUITY_DIR, key, episode_no) or {
        "state_in": script.get("state_in") or {},
        "state_out": script.get("state_out") or {},
        "key_events": (script.get("continuity") or {}).get("key_events") or [],
    }
    prev_state = continuity.load_state(CONTINUITY_DIR, key, episode_no - 1)
    prev_script = continuity._load_prev_script(SCRIPT_DIR, key, episode_no - 1)
    rule_issues = continuity.rule_timeline_check(prev_state, state, script) if prev_state else []
    events = []
    validation = continuity.validate_continuity(
        client, script, prev_script, prev_state, state, episode_no,
        events=events, rule_issues=rule_issues)
    validation["quotes"] = continuity.check_quotes_in_script(CONTINUITY_DIR, key, script, episode_no)

    rewrite = {"triggered": False, "rounds": 0, "rewritten_shot_ids": [], "notes": []}
    body = request.json or {}
    if body.get("rewrite") and validation.get("rewrite_needed") and int(episode_no) > 1:
        issues = [i for i in (validation.get("issues") or [])
                  if i.get("severity") in ("high", "medium")]
        ctx = continuity.build_continuity_context(
            CONTINUITY_DIR, key, episode_no, script.get("style") or "3D动漫渲染")
        rw = continuity.rewrite_shots_for_issues(client, script, issues, episode_no,
                                                 continuity_ctx=ctx, events=events,
                                                 shot_ids=validation.get("rewrite_shot_ids"))
        if rw.get("rewritten_shot_ids"):
            rewrite.update({"triggered": True, "rounds": 1,
                            "rewritten_shot_ids": rw["rewritten_shot_ids"],
                            "notes": rw.get("notes") or []})
            novel_to_script.save_episode_script(script, SCRIPT_DIR, key, episode_no, key)
            state = continuity.extract_episode_state(client, script, prev_state, episode_no,
                                                     events=events)
            script["state_in"], script["state_out"] = state["state_in"], state["state_out"]
            rule_issues = continuity.rule_timeline_check(prev_state, state, script) if prev_state else []
            validation = continuity.validate_continuity(
                client, script, prev_script, prev_state, state, episode_no,
                events=events, rule_issues=rule_issues)
            validation["quotes"] = continuity.check_quotes_in_script(
                CONTINUITY_DIR, key, script, episode_no)
            continuity.save_state(CONTINUITY_DIR, key, state)
            novel_to_script.save_episode_script(script, SCRIPT_DIR, key, episode_no, key)
        else:
            rewrite["error"] = rw.get("error") or "未产生修改"

    validation["rewrite"] = rewrite
    validation["generated_at"] = continuity._now()
    continuity.save_json(continuity.validation_path(CONTINUITY_DIR, key, episode_no), validation)
    # P0-3 剧本↔原著一致性摘要：直接复用生成时写入剧本的结论（三件套为确定性计算，
    # 不在本接口重算，避免无章节正文时误报）
    consistency = (script.get("metadata") or {}).get("script_consistency") or {}
    return jsonify({"success": True, "novel_id": novel_id, "episode_no": episode_no,
                    "project_key": key, "validation": validation, "rewrite": rewrite,
                    "consistency": consistency,
                    "events": events})


# =====================================================================
# 新增模块 D：剧本提示词分析（prompt_h3 + 参考图提示词）
# =====================================================================

def _ensure_script_file(script: dict, script_path: str, project_name: str) -> str:
    # P0-4 纵深防御：worker（save_script_inplace 覆盖写盘）只认「项目输出目录内」的既有剧本；
    # 越界/空路径一律视为未提供，改在 SCRIPT_DIR 内另存，绝不对项目外文件落笔。
    if script_path and project_store.is_path_inside_output(script_path) \
            and os.path.isfile(script_path):
        return script_path
    return novel_to_script.save_generated_script(script, SCRIPT_DIR, project_name)


def _analyze_worker(task_id: str, script: dict, script_path: str, mode: str,
                    shot_ids, project_name: str, extra: str):
    def cb(phase, current, total, message, percent):
        with lock:
            generation_state[task_id].update({
                "phase": phase, "current": current, "total": total,
                "message": message, "progress": percent,
            })

    try:
        client = _current_llm_client()
        result = analyze_script_prompts(
            client, script, mode=mode, shot_ids=shot_ids,
            storyboards_dir=STORYBOARDS_DIR, project_name=project_name,
            extra_instruction=extra, progress_cb=cb,
        )
        path = _ensure_script_file(script, script_path, project_name)
        save_script_inplace(script, path)
        with lock:
            generation_state[task_id].update({
                "status": "completed", "progress": 100,
                "result": result, "script_path": path, "script": script,
            })
    except (LLMError, OSError) as e:
        app.logger.error(f"提示词分析失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("提示词分析异常")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": f"分析异常：{e}"})


@app.route('/api/scripts/analyze-prompts', methods=['POST'])
def api_analyze_prompts():
    """为剧本生成/优化 prompt_h3 与参考图提示词（后台任务）

    body: {script, script_path, mode: shots|assets|all, shot_ids: [...], project_name, extra_instruction}
    单镜头重写：mode=shots 且 shot_ids=[该镜头号]
    """
    data = request.json or {}
    script = data.get('script')
    if not isinstance(script, dict) or not script.get('shots'):
        return jsonify({"success": False, "error": "缺少剧本数据（或剧本中没有镜头）"}), 400

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法生成提示词")

    mode = data.get('mode') or 'all'
    if mode not in ('shots', 'assets', 'all'):
        return jsonify({"success": False, "error": "mode 必须是 shots / assets / all"}), 400
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]
    project_name = _safe_project(data.get('project_name')
                                 or (script.get('title') or 'project'))
    script_path = data.get('script_path') or script.get('metadata', {}).get('script_path') or ''
    # P0-4：这条链路会把分析结果**写回** script_path（_ensure_script_file → save_script_inplace
    # 覆盖式 json.dump），是「剧本路径越界」的写盘面 —— 与 /api/final/video、_dub_resolve_script、
    # bind_script 同一校验口径。越界一律 400 拒写（空值仍允许：worker 会在 SCRIPT_DIR 内另存）。
    if script_path and not project_store.is_path_inside_output(script_path):
        return jsonify({"success": False,
                        "error": "剧本路径必须在项目输出目录内（output/），越界路径已拒写"}), 400
    extra = str(data.get('extra_instruction') or '')[:500]

    task_id = f"prompts_{project_name}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": "正在准备提示词生成…", "mode": mode,
            "project_name": project_name,
            "total_shots": len(script.get('shots') or []),
        }
    threading.Thread(target=_analyze_worker,
                     args=(task_id, script, script_path, mode, shot_ids, project_name, extra),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "mode": mode, "project_name": project_name,
                    "script_path": script_path, "shot_ids": shot_ids})


# =====================================================================
# 视频配音（QwenTTS 真实链路：剧本台词 → 逐角色语音 → output/dub/<项目>/）
# =====================================================================

dub_tasks = {}
dub_lock = threading.Lock()


def _dub_project_dir(project_name: str) -> str:
    d = os.path.join(DUB_DIR, project_name)
    os.makedirs(d, exist_ok=True)
    return d


def _dub_resolve_script(data: dict) -> dict:
    """解析配音所用剧本：优先 body.script，其次 script_path（限项目输出目录内），最后自动匹配"""
    script = data.get("script")
    if isinstance(script, dict) and script.get("shots"):
        return {"script": script, "script_path": (data.get("script_path") or "").strip(),
                "source": "body"}

    script_path = (data.get("script_path") or "").strip()
    if script_path:
        # P0-4：与 project_store.bind_script / /api/final/video 同一校验函数
        if not project_store.is_path_inside_output(script_path):
            raise TTSError(f"剧本路径必须在项目输出目录内：{os.path.abspath(PROJECT_OUTPUT_DIR)}")
        p = os.path.abspath(script_path)
        if not os.path.exists(p):
            raise TTSError(f"剧本文件不存在：{p}")
        with open(p, "r", encoding="utf-8") as f:
            return {"script": json.load(f), "script_path": p, "source": "path"}

    # 自动匹配 output/scripts 下的剧本（优先路径含项目名，其次 episode_no 命中，最后取最新）
    project_name = data.get("project_name") or ""
    episode = data.get("episode")
    cands = []
    for dp, _dn, fn in os.walk(SCRIPT_DIR):
        for name in fn:
            if not name.lower().endswith(".json"):
                continue
            p = os.path.join(dp, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    s = json.load(f)
            except Exception:
                continue
            if not isinstance(s, dict) or not s.get("shots"):
                continue
            cands.append({"path": p, "mtime": os.path.getmtime(p), "script": s,
                          "episode_no": s.get("episode_no") or (s.get("metadata") or {}).get("episode_no")})
    if not cands:
        raise TTSError("未找到可用剧本（output/scripts 下无含 shots 的 JSON），请先生成剧本")

    # 严格匹配：有 project_name 时必须属于该项目，禁止跨项目回退
    if project_name:
        project_cands = [c for c in cands if project_name in c["path"]]
        if not project_cands:
            raise TTSError(f"项目 '{project_name}' 暂无剧本，请先生成剧本后再使用 TTS 功能")
        hit = project_cands
    else:
        hit = cands

    if episode:
        hit2 = [c for c in hit if str(c["episode_no"]) == str(episode)]
        if hit2:
            hit = hit2
    best = max(hit, key=lambda c: c["mtime"])
    return {"script": best["script"], "script_path": best["path"], "source": "auto"}


def _dub_audio_url(project_name: str, rel_path: str) -> str:
    rel = os.path.relpath(rel_path, _dub_project_dir(project_name)).replace(os.sep, "/")
    return f"/api/tts/file/{project_name}/{rel}" if not rel.startswith("..") else ""


# =====================================================================
# 音频质检接线（提示词预检 + 成品质检）
# =====================================================================
# 两层都在「生成前后」各管一段，与图片/视频质检的三层结构（预检 → 成品质检 → 重试）对齐：
#   ① 配音台词预检（零模型依赖，默认开启）：挡住会被念出来的结构化残留、空台词、错配音色；
#   ② 配音成品质检（ffmpeg 客观层 + 频谱/波形 AI 层）：挡住「合成成功但整段无声」这类
#      在旧流程里要等到成片验收才暴露的问题。
# 两者都**不阻断生成**：整集生产不能被单句质检拖死，结论如实记录、逐句可定位即可。

def _record_audio_qc_lesson(project_name: str, ln: dict, verdict: dict) -> dict:
    """把一句「配音成品质检不达标」沉淀成 ``kind="audio"`` 教训。

    提示词键用**自愈前**的台词原文（``audio_orig_text``，回退当前 ``ln["text"]``）：
    它正是 TTS 的实际输入，phash 稳定；预检已自愈过 text 时取自愈前的原文，避免指纹漂移。
    ⚠️ 沉淀的 issues **只进教训库，绝不改台词**（音频类召回是计划级纠偏，见 _apply_audio_hints）。
    """
    text_key = ln.get("audio_orig_text") or ln.get("text") or ""
    if not text_key:
        return {}
    verdict = verdict if isinstance(verdict, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in
                   (list(verdict.get("issues") or []) +
                    list(verdict.get("critical_issues") or [])) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(verdict.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "audio": True,
    }
    if not rec["issues"] and not rec["reason"]:
        return {}
    try:
        return _record_qc_lesson(project_name, "audio", text_key, rec)
    except Exception as e:  # noqa: BLE001 - 沉淀失败绝不影响配音
        app.logger.warning(f"配音教训沉淀失败（忽略）：{e}")
        return {}


def _dub_line_speaker_from_script(ln: dict, project_name: str) -> str:
    """从剧本里找该句所属镜头登记的 speaker（dialogue[].speaker / shot.speaker）。

    取不到返回空串（调用方不做回填）。纯只读，永不抛异常。
    """
    shot_id = ln.get("shot_id")
    if not project_name or shot_id is None:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
    except Exception:  # noqa: BLE001
        return ""
    sid_str = str(shot_id)
    text = str(ln.get("text") or "").strip()
    shots = []
    for sc in (script.get("scenes") or []):
        shots.extend(sc.get("shots") or [])
    if not shots:
        shots = script.get("shots") or []
    for shot in shots:
        if str(shot.get("shot_id") or "") != sid_str:
            continue
        dlg_speaker = ""
        for row in (shot.get("dialogue") or []):
            if str(row.get("text") or "").strip() == text:
                dlg_speaker = str(row.get("speaker") or "").strip()
                if dlg_speaker:
                    break
        return dlg_speaker or str(shot.get("speaker") or "").strip()
    return ""


def _dub_character_desc(character: str, project_name: str) -> str:
    """取角色音色底稿描述（供 design 模式 instruct）；取不到返回空串。"""
    if not character or not project_name:
        return ""
    try:
        resolved = _dub_resolve_script({"project_name": project_name})
        script = resolved.get("script") or {}
        for ch in (script.get("characters") or []):
            if str(ch.get("name") or "") == str(character):
                return str(ch.get("description") or ch.get("tts_voice") or "")
    except Exception as e:  # noqa: BLE001
        app.logger.debug("角色描述取值失败（忽略）：%s", e)
    return ""


def _apply_audio_hints(ln: dict, hints: list, project_name: str = "") -> None:
    """音频类召回的**计划级纠偏**（设计 D4：音频建议绝不拼进 ``ln["text"]``，会被 TTS 念出来）。

    逐条扫描 hints（缺陷描述），按特征做确定性纠偏，只动 plan 的说话人/音色模式/期望时长：
      - 含「旁白」「speaker」「角色」：若本句说话人是旁白兜底（剧本 dialogue 没登记 speaker），
        且能拿到该镜在剧本里登记的 speaker，则回填 ``ln["character"]``，避免角色台词被旁白念；
      - 含「情绪」「语气」「instruct」：``voice.mode == "preset"`` 时切到 ``design``，
        并确保 ``instruct`` 携带该句情绪（preset 的 CustomVoice 会忽略 instruct，只有
        VoiceDesign 真正按 instruct 控制语气）；
      - 含「时长」「截断」：记录 ``ln["audio_expect_sec"]``（期望时长）供后续质检比对，不阻断；
      - 其它：仅留痕（hints 由调用方写入 ``ln["audio_hints"]`` 审计），不改 plan。

    纯就地修改、永不抛异常、不改 tts_client.py（build_dub_plan 保持纯计划构建）。
    """
    hints = [str(h).strip() for h in (hints or []) if str(h).strip()]
    if not hints:
        return
    try:
        joined = " ".join(hints)
        voice = ln.get("voice") or {}
        # —— 说话人回填：旁白兜底 + hint 提示该句其实是角色台词 → 按剧本登记的 speaker 纠偏 ——
        if (("旁白" in joined or "speaker" in joined or "角色" in joined)
                and str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER):
            speaker = ""
            try:
                speaker = _dub_line_speaker_from_script(ln, project_name)
            except Exception:  # noqa: BLE001
                speaker = ""
            if speaker and speaker != tts_client.NARRATION_SPEAKER:
                ln["character"] = speaker
        # —— 情绪/语气：preset 忽略 instruct → 切 design 并携带情绪 ——
        if ("情绪" in joined or "语气" in joined or "instruct" in joined.lower()):
            emotion = str(ln.get("emotion") or "").strip()
            if emotion and not tts_client._is_neutral_emotion(emotion):
                desc = ""
                try:
                    desc = _dub_character_desc(ln.get("character"), project_name)
                except Exception:  # noqa: BLE001
                    desc = ""
                voice = dict(voice, mode="design",
                             instruct=tts_client._emotion_instruct(emotion, desc))
                ln["voice"] = voice
        # —— 时长/截断：记录期望时长供质检比对（不阻断）——
        if "时长" in joined or "截断" in joined:
            expect = _audio_line_expect_sec(ln)
            if expect > 0:
                ln["audio_expect_sec"] = round(float(expect), 2)
    except Exception as e:  # noqa: BLE001 - 纠偏失败绝不影响配音
        app.logger.warning(f"配音教训纠偏失败（忽略）：{e}")


def _apply_audio_lessons(plan_lines: list, project_name: str) -> int:
    """配音计划构建后、逐句合成前的音频教训召回（设计 §2.3.2）。

    对每句按 ``kind="audio"`` 召回历史教训（键 = 该句 text）：
      - ``ln["audio_hints"]`` 只存审计，**绝不进台词**；
      - 调 ``_apply_audio_hints`` 做计划级纠偏（说话人回填 / preset→design / 期望时长）。
    无教训时零行为变更；召回失败静默忽略（保险不影响配音）。返回产生 hints 的句数。
    """
    if not project_name or not plan_lines:
        return 0
    hit_lines = 0
    for ln in plan_lines:
        text = str(ln.get("text") or "")
        # 键用 build_dub_plan 刚构建、**尚未被预检自愈过**的台词原文（TTS 实际输入），
        # 保证 _record_audio_qc_lesson 里 phash 稳定、与自愈后的 text 不漂移。
        ln.setdefault("audio_orig_text", text)
        if not text:
            continue
        try:
            hints = prompt_memory.suggest(kind="audio", prompt=text, project=project_name,
                                          root_dir=PROJECT_OUTPUT_DIR)
        except Exception:  # noqa: BLE001 - 召回失败绝不影响配音
            hints = []
        if not hints:
            continue
        ln["audio_hints"] = list(hints)
        _apply_audio_hints(ln, hints, project_name)
        hit_lines += 1
    return hit_lines


def _audio_line_expect_sec(line: dict) -> float:
    """该句配音的期望时长（由台词字数推算；推算不出时退回镜头时长）

    只用于「时长偏差」这一条软判据，因此宁松勿紧：优先用字数推算（能发现「被截断」），
    推算不出（空台词）时才退回剧本给的镜头时长，避免拿 0 当期望值把一切都判成偏差。
    """
    est = audio_qc.estimate_speech_sec(line.get("text"))
    if est > 0:
        return est
    try:
        return max(0.0, float(line.get("duration_hint") or 0))
    except (TypeError, ValueError):
        return 0.0


def _dub_prompt_preflight(lines: list, project_name: str = "") -> dict:
    """配音台词生成前预检：就地自愈 ``lines[i]["text"]``，结论写入 ``lines[i]["prompt_qc"]``。

    永不抛异常（预检是保险，保险本身出问题不能耽误配音）。
    """
    stats = {"enabled": False, "checked": 0, "repaired": 0, "blocked": 0,
             "issue_lines": 0, "repaired_lines": 0, "problem_lines": []}
    if not lines:
        return stats
    try:
        cfg = _qc_load_cfg()
        if not prompt_qc.prompt_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        mode = prompt_qc.prompt_qc_mode(cfg)
        for ln in lines:
            text = ln.get("text") or ""
            ctx = {
                "project_name": project_name,
                "shot_id": ln.get("shot_id"),
                "character": ln.get("character"),
                "emotion": ln.get("emotion"),
                # preset（CustomVoice）会忽略 instruct → 情绪送不进 TTS，预检据此提示
                "voice_mode": (ln.get("voice") or {}).get("mode"),
                # 剧本没写 speaker（或写了未登记角色）时 build_dub_plan 落到「旁白」音色，
                # 角色台词会被旁白念 —— 用「最终音色是不是旁白兜底」判定，而不是旧写法
                # `source == "narration"`（旁白通道关闭后 source 恒为 dialogue，那个判据永远为假，
                # 等于这条预检规则静默失效）。
                "speaker_fallback": str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER,
            }
            pf = prompt_qc.preflight("audio", text, ctx=ctx, cfg=cfg)
            verdict = pf.get("verdict") or {}
            stats["checked"] += 1
            if pf.get("repairs"):
                stats["repaired"] += 1
            if verdict.get("issues"):
                stats["issue_lines"] += 1
            # ⚠️ 自愈结果为空时**保留原文**：把台词改成空串会让该句直接合成失败/静音，
            #    比「带一点噪音」更糟。空台词交给调用方按 rebuild_hint 从剧本重建。
            new_text = pf.get("prompt") or ""
            if new_text and new_text != text:
                ln["text"] = new_text
                stats["repaired_lines"] += 1
            ln["prompt_qc"] = {
                "mode": mode,
                "passed": bool(verdict.get("passed")),
                "blocked": bool(pf.get("blocked")),
                "score": verdict.get("score"),
                "issues": list(verdict.get("issues") or []),
                "critical_issues": list(verdict.get("critical_issues") or []),
                "repairs": list(pf.get("repairs") or []),
                "label": pf.get("label") or "",
                "reason": pf.get("reason") or "",
                "rebuild_hint": pf.get("rebuild_hint") or "",
            }
            if pf.get("blocked") or verdict.get("issues"):
                if pf.get("blocked"):
                    stats["blocked"] += 1
                if len(stats["problem_lines"]) < 20:
                    stats["problem_lines"].append({
                        "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
                        "character": ln.get("character"),
                        "blocked": bool(pf.get("blocked")),
                        "issues": list(verdict.get("issues") or [])[:3]
                                  + list(verdict.get("critical_issues") or [])[:2],
                        "repairs": list(pf.get("repairs") or []),
                        "rebuild_hint": pf.get("rebuild_hint") or "",
                    })
    except Exception as e:  # noqa: BLE001 - 预检失败绝不影响配音
        app.logger.warning(f"配音台词预检异常（已跳过，不影响配音）：{e}")
    return stats


def _audio_qc_lines(project_name: str, lines: list, results: list, cfg: dict,
                    retry_cb=None) -> dict:
    """配音成品逐句质检（客观层 + AI 层），结论写入 ``results[i]["audio_qc"]``。

    ``retry_cb(line, result) -> dict|None``：可选的重配合回调。**只对客观层判致命的句子
    调用**（整段无声/空文件）—— 这类失败属于「合成出了东西但不是人声」，重配一次是最有效
    的补救；软扣分项（音量偏小、时长偏差）不重配，交由用户决定。

    永不抛异常；批量口径为「记录 + 有限重配」，不阻断整集。
    """
    stats = {"enabled": False, "checked": 0, "passed": 0, "failed": 0, "blocked": 0,
             "ai_used": 0, "retried": 0, "recovered": 0, "problems": []}
    if not results:
        return stats
    try:
        if not qc_client.audio_qc_ready(cfg):
            return stats
        stats["enabled"] = True
        by_id = {str(l.get("line_id")): l for l in (lines or [])}
        visuals_root = os.path.join(QC_DIR, "audio", _safe_project(project_name or "project"))
        for r in results:
            if not r.get("ok") or not r.get("out_path"):
                continue
            ln = by_id.get(str(r.get("line_id"))) or {}
            expect = _audio_line_expect_sec(ln)
            verdict = qc_client.check_audio(
                r["out_path"], expect_sec=expect or None,
                line_text=ln.get("text") or r.get("text") or "", cfg=cfg,
                visuals_dir=os.path.join(visuals_root,
                                         os.path.splitext(os.path.basename(r["out_path"]))[0]))
            stats["checked"] += 1
            # 致命（整段无声/空文件）→ 重配一次。⚠️ 计数必须在重配之后按**最终**结论统计：
            # 先记 blocked 再重配会出现「致命 1 句 / 未通过 0 句」这种自相矛盾的汇总，
            # 前端与任务消息都在读这两个数，口径必须一致。
            if verdict.get("blocked") and retry_cb is not None:
                try:
                    stats["retried"] += 1
                    again = retry_cb(ln, r)
                    if again:
                        verdict = again
                        if not verdict.get("blocked"):
                            stats["recovered"] += 1
                except Exception as e:  # noqa: BLE001 - 重配失败不影响已有结论
                    app.logger.warning(f"音频质检重配失败（{r.get('line_id')}）：{e}")
            if verdict.get("blocked"):
                stats["blocked"] += 1
            if verdict.get("ai_used"):
                stats["ai_used"] += 1
            if verdict.get("passed"):
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                # T03b：配音成品质检不达标 → 沉淀 kind="audio" 教训（键 = 该句 TTS 输入原文，
                # 自愈前用 audio_orig_text）。verdict.ok=false（接口异常）时 verdict 无有效缺陷，
                # 不沉淀，避免把「质检调用失败」记成「这句配音有问题」。
                if verdict.get("ok", True) and ln:
                    _record_audio_qc_lesson(project_name, ln, verdict)
                # ★ 用户需求：质检不合格的配音不留本地。⚠️ **仅在最终 failed（重配也失败）时删**：
                # `blocked`（整段无声/空文件）已由 retry_cb 重配过一次，重配若恢复则 verdict
                # 被替换、不会走到这里；能走到这里说明**最终结论仍不合格**。删除条件是
                # 「质检成功返回（ok 非 False）且最终 not passed」—— ok=False（接口故障）不删。
                # 删：该句 wav（P9）+ 其可视化目录（P10 output/qc/audio/<项目>/<stem>/）。
                if verdict.get("ok", True) and r.get("out_path"):
                    try:
                        _stem = os.path.splitext(os.path.basename(r["out_path"]))[0]
                        _purge_rejected_artifacts(
                            [r["out_path"], os.path.join(visuals_root, _stem)],
                            project=project_name,
                            reason=f"配音质检不合格（{verdict.get('reason') or ''}）"[:120],
                            kind="audio_line")
                    except Exception as _pe:  # noqa: BLE001
                        app.logger.warning(f"不合格配音清理失败（忽略）：{_pe}")
                if len(stats["problems"]) < 20:
                    stats["problems"].append({
                        "line_id": r.get("line_id"), "shot_id": r.get("shot_id"),
                        "character": r.get("character"),
                        "blocked": bool(verdict.get("blocked")),
                        "score": verdict.get("score"),
                        "reason": str(verdict.get("reason") or "")[:200],
                        "metrics": verdict.get("metrics") or {},
                        "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                                    f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                                    for p in (verdict.get("visuals") or [])],
                    })
            r["audio_qc"] = {
                "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
                "score": verdict.get("score"),
                "reason": str(verdict.get("reason") or "")[:300],
                "issues": list(verdict.get("issues") or [])[:5],
                "critical_issues": list(verdict.get("critical_issues") or [])[:3],
                "metrics": verdict.get("metrics") or {},
                "ai_used": bool(verdict.get("ai_used")),
                "ai_skipped": bool(verdict.get("ai_skipped")),
                "ai_skip_reason": verdict.get("ai_skip_reason") or "",
                "visuals": [f"/api/qc/frames/audio/{_safe_project(project_name or 'project')}/"
                            f"{os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}"
                            for p in (verdict.get("visuals") or [])],
            }
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响配音产物
        app.logger.warning(f"配音成品质检异常（已跳过，不影响配音）：{e}")
    if stats["enabled"]:
        app.logger.info(f"配音质检（{project_name}）：检查 {stats['checked']} 句，"
                        f"通过 {stats['passed']}，未通过 {stats['failed']}，"
                        f"致命 {stats['blocked']}，重配 {stats['retried']}，"
                        f"恢复 {stats['recovered']}，AI 层 {stats['ai_used']}")
    return stats


def _mix_audio_qc(report: dict, cfg: dict) -> dict:
    """带配音成片的音频质检（整轨口径）。

    ⚠️ 必须关掉「有声占比下限」：成片天然有大段无台词留白（无台词镜头/纯环境音），
    拿单句的 50% 标准去卡它必然误报「漏句」。整轨真正要挡的是**整条音轨近乎无声**
    （amix 失败 / 全部条目静音）与**不含音频流** —— 这两条都在客观层的硬闸里。
    """
    try:
        if not qc_client.audio_qc_ready(cfg):
            return {"enabled": False, "reason": "音频质检开关未开启"}
        out = report.get("output_path") or ""
        before = report.get("video_before") or {}
        expect = 0.0
        try:
            expect = float(before.get("duration") or 0)
        except (TypeError, ValueError):
            expect = 0.0
        project_name = _safe_project(report.get("project") or "project")
        stem = os.path.splitext(os.path.basename(out))[0]
        verdict = qc_client.check_audio(
            out, expect_sec=expect or None, cfg=cfg,
            check_speech_ratio=False,
            visuals_dir=os.path.join(QC_DIR, "audio_mix", project_name, stem))
        metrics = verdict.get("metrics") or {}
        out_v = {
            "enabled": True,
            "passed": verdict.get("passed"), "blocked": bool(verdict.get("blocked")),
            "score": verdict.get("score"),
            "reason": str(verdict.get("reason") or "")[:300],
            "issues": list(verdict.get("issues") or [])[:5],
            "critical_issues": list(verdict.get("critical_issues") or [])[:3],
            "metrics": metrics,
            "ai_used": bool(verdict.get("ai_used")),
            "ai_skipped": bool(verdict.get("ai_skipped")),
            "ai_skip_reason": verdict.get("ai_skip_reason") or "",
            "visuals": [f"/api/qc/frames/audio_mix/{project_name}/{stem}/"
                        f"{os.path.basename(p)}" for p in (verdict.get("visuals") or [])],
            "coverage_sec": report.get("coverage_sec"),
            "video_duration": expect or None,
        }
        # 配音覆盖率：逐句音频总时长 / 视频时长。**两端都要看**：
        #   偏低（<50%）→ 大量镜头没有配音落点；
        #   偏高（>115%）→ 台词总长超过画面，末尾整段被 `-shortest` **静默截掉**
        #     （成片仍「有声音」所以客观层查不出来，但台词已经丢了一大半）。
        #   ⚠️ 曾只写「偏低」这一个方向，实测把 ep04（台词 606.4s / 画面 85.2s，
        #      21 句里 17 句落在片外）这条最该拦的缺陷直接放过了 —— 覆盖率是**比值**，
        #      单向判定等于漏掉一半语义。
        try:
            cov = float(report.get("coverage_sec") or 0)
            if expect > 0:
                ratio = cov / expect
                out_v.setdefault("issues", [])
                if ratio < 0.5:
                    out_v["issues"].append(
                        f"配音覆盖偏低：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                        f"（{ratio * 100:.0f}%）")
                elif ratio > 1.15:
                    entries = report.get("entries") or []
                    dropped = 0
                    for e in entries:
                        try:
                            if float(e.get("start") or 0) >= expect:
                                dropped += 1
                        except (TypeError, ValueError):
                            continue
                    detail = (f"，其中 {dropped}/{len(entries)} 句起始点已在片长之外、放不出来"
                              if dropped else "")
                    msg = (f"配音总长超出画面：逐句音频合计 {cov:.1f}s / 视频 {expect:.1f}s"
                           f"（{ratio * 100:.0f}%）{detail} —— 超出部分会被合成命令静默截断")
                    out_v["issues"].append(msg)
                    out_v.setdefault("critical_issues", [])
                    out_v["critical_issues"].append(msg)
                    # 单向收紧：客观层/AI 层说通过也翻不回来
                    out_v["blocked"] = True
                    out_v["passed"] = False
                    try:
                        out_v["score"] = min(int(out_v.get("score") or 0), 40)
                    except (TypeError, ValueError) as e:
                        app.logger.debug("评分字段解析失败（忽略）：%s", e)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            app.logger.debug("评分归一化计算失败（忽略）：%s", e)
        # ★ 用户需求：质检不合格的混音不留本地（P10 可视化目录）。仅当最终「质检成功返回
        # 且不合格」（ok 非 False 且 passed=False）时删；ok=False（接口故障）不删。
        if verdict.get("ok") is not False and not out_v.get("passed"):
            try:
                _purge_rejected_artifacts(
                    [os.path.join(QC_DIR, "audio_mix", project_name, stem)],
                    project=project_name,
                    reason=f"混音质检不合格（{out_v.get('reason') or ''}）"[:120],
                    kind="audio_mix")
            except Exception as _pe:  # noqa: BLE001
                app.logger.warning(f"不合格混音清理失败（忽略）：{_pe}")
        return out_v
    except Exception as e:  # noqa: BLE001 - 质检失败绝不影响合成结果
        app.logger.warning(f"成片音频质检异常（已跳过）：{e}")
        return {"enabled": True, "passed": None, "error": f"{type(e).__name__}: {e}"}


def _dub_worker(task_id: str, project_name: str, plan: dict, out_dir: str,
                fmt: str, episode: int):
    """后台配音：批量合成逐句音频 → 合并整集音轨 → 落盘清单"""
    try:
        lines = plan.get("lines") or []
        if not lines:
            raise TTSError("配音计划为空（剧本中没有可朗读台词，或所选镜头无台词）")

        lines_dir = os.path.join(out_dir, "lines")
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        for ln in lines:
            ln["project_tag"] = project_name

        # ---- ① 生成前提示词预检（配音台词）----
        # 台词会被 TTS 逐字念出来：结构化残留（`(S1) 说：[Chinese] …`）、舞台指示
        # （`（转身冷笑）`）都会原样进成片；空台词/纯标点则合成出静音却显示「成功」。
        # 这一层零模型依赖、默认开启，在消耗 GPU 之前把确定性缺陷挡住/修掉。
        qc_cfg = _qc_load_cfg()
        pre = _dub_prompt_preflight(lines, project_name)
        if pre.get("blocked") or pre.get("repaired_lines"):
            with dub_lock:
                dub_tasks[task_id].update({"prompt_qc": pre})

        def _cb(done, total, last, note):
            with dub_lock:
                dub_tasks[task_id].update({
                    "current": done, "total": total,
                    "progress": int(done / max(1, total) * 90),
                    "phase": f"配音合成中（{done}/{total}）",
                    "message": f"最新：{last.get('character') or ''} {str(last.get('text') or '')[:18]}",
                })

        results = client.synthesize_lines(lines, lines_dir, progress_cb=_cb)
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
            })

        # ---- ② 成品质检（音频客观层 + 频谱/波形 AI 层）----
        # 「合成成功」不等于「念出来了」：节点正常返回、文件也落盘，但整段可以是静音
        # （漏配音 / 模型未发声）。旧流程要等到成片验收才发现整集缺一句。
        # 这里逐句实测，致命的（整段无声/空文件）当场重配一次，软扣分项只记录。
        def _retry_line(ln, rec):
            """重配单句并重新质检（只对客观层判致命的句子调用）"""
            import copy as _copy
            one = _copy.deepcopy(ln)
            one["project_tag"] = project_name
            res = client.synthesize_lines([one], lines_dir)
            if not res or not res[0].get("ok"):
                return None
            rec.update({k: v for k, v in res[0].items() if k != "audio_qc"})
            return qc_client.check_audio(
                rec["out_path"], expect_sec=_audio_line_expect_sec(ln) or None,
                line_text=ln.get("text") or "", cfg=qc_cfg,
                visuals_dir=os.path.join(QC_DIR, "audio", _safe_project(project_name),
                                         os.path.splitext(os.path.basename(rec["out_path"]))[0]))

        if qc_cfg.get("enabled") and qc_cfg.get("audio_enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"phase": "配音质检中（客观指标 + 频谱波形送检）",
                                           "progress": 92})
        aqua = _audio_qc_lines(project_name, lines, results, qc_cfg, retry_cb=_retry_line)
        if aqua.get("enabled"):
            with dub_lock:
                dub_tasks[task_id].update({"audio_qc": aqua})
        ok_items = [r for r in results if r.get("ok")]
        with dub_lock:
            dub_tasks[task_id].update({
                "results": [
                    dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                    for r in results
                ],
                "success_count": len(ok_items), "failed_count": len(results) - len(ok_items),
                "audio_qc_failed": aqua.get("failed", 0),
            })

        # 合并整集音轨（按剧本镜头顺序）
        merged = None
        merged_probe = {}
        if ok_items:
            with dub_lock:
                dub_tasks[task_id].update({"phase": "合并整集音轨", "progress": 94})
            order = {l.get("line_id"): i for i, l in enumerate(lines)}
            ok_sorted = sorted(ok_items, key=lambda r: order.get(r.get("line_id"), 9999))
            merged_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub.{fmt}")
            merged = concat_audio([r["out_path"] for r in ok_sorted], merged_path, fmt=fmt)
            merged_probe = probe_audio_info(merged)

        manifest = {
            "project": project_name, "episode": episode,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "script_path": plan.get("script_path") or "",
            "voice_map": plan.get("voice_map") or {},
            "characters": plan.get("characters") or [],
            "merged_audio": merged,
            "merged_info": merged_probe,
            # 质检结论随清单落盘：成片验收时能回溯「这句当时是怎么判的」
            "prompt_qc": pre if pre.get("enabled") else {},
            "audio_qc": aqua if aqua.get("enabled") else {},
            "lines": [dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                      for r in results],
        }
        manifest_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        # A-3 P1：manifest 原子写改走 fs_atomic（唯一临时名 + flush/fsync + .bak 快照 +
        # os.replace 重试），替代旧「固定 .tmp + os.replace」
        atomic_write_json(manifest_path, manifest)

        _msg = f"成功 {len(ok_items)} 句 / 失败 {len(results) - len(ok_items)} 句"
        if aqua.get("enabled") and aqua.get("checked"):
            _msg += f"；质检通过 {aqua['passed']}/{aqua['checked']} 句"
            if aqua.get("recovered"):
                _msg += f"（重配恢复 {aqua['recovered']} 句）"
        with dub_lock:
            dub_tasks[task_id].update({
                "status": "completed" if ok_items else "failed",
                "progress": 100, "phase": "配音完成" if ok_items else "配音失败",
                "message": _msg,
                "merged_audio": merged,
                "merged_url": _dub_audio_url(project_name, merged) if merged else "",
                "merged_info": merged_probe,
                "manifest": manifest_path,
                "error": "" if ok_items else "全部句子合成失败，请查看 results 中的错误原因",
            })
    except (TTSError, OSError) as e:
        app.logger.error(f"配音任务失败: {e}")
        # B-16 P2-11：配音失败 → 清理本任务产生的中间产物（lines 目录、merged 半成品）
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), app.logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": str(e), "phase": "失败"})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("配音任务异常")
        # B-16 P2-11：配音异常 → 清理中间产物
        _cleanup_scratch_dir(os.path.join(out_dir, "lines"), app.logger)
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": f"异常：{e}", "phase": "失败"})


@app.route('/api/tts/env', methods=['GET'])
def api_tts_env():
    """配音环境自检：ComfyUI 在线 / Qwen-TTS 节点 / 模型权重 / 可用音色"""
    env = tts_env_check()
    env["voices"] = tts_list_voices()
    env["dub_dir"] = DUB_DIR
    env["out_dir"] = _dub_project_dir(_safe_project(request.args.get('project_name') or 'project'))
    return jsonify(env)


@app.route('/api/tts/plan', methods=['GET', 'POST'])
def api_tts_plan():
    """生成配音计划预览（逐句说话人 + 音色 + 落盘文件名，不合成）"""
    data = request.json or {} if request.method == 'POST' else dict(request.args)
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    try:
        resolved = _dub_resolve_script(dict(data, project_name=project_name))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 1)
    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = data.get('voice_map') or load_voice_map(vm_path) \
        or default_voice_map(script.get('characters') or [], project_name, episode)
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]

    try:
        plan = build_dub_plan(script, voice_map, project_name, episode,
                              shot_ids=shot_ids,
                              only_missing=bool(data.get('only_missing')),
                              out_dir_wav=os.path.join(out_dir, "lines"))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # T03b：配音计划构建后、预览/合成前，按 kind="audio" 召回历史教训做计划级纠偏
    # （说话人回填 / preset→design / 期望时长），绝不进台词。无教训时零行为变更。
    _apply_audio_lessons(plan.get("lines") or [], project_name)

    for ln in plan["lines"]:
        ln["url"] = _dub_audio_url(project_name, ln.get("out_path") or "")
        ln["exists"] = bool(ln.get("out_path") and os.path.exists(ln["out_path"]))
    # 剧本体检：兜底镜头（dialogue=[] 且 prompt_h3=""）会让配音一句都合不出来，
    # 但脚本生成阶段看起来是「成功」的 —— 这里把缺口提前摆到用户面前。
    audit = dialogue_utils.audit_script(script)
    plan.update({"success": True, "script_path": resolved["script_path"],
                 "script_source": resolved["source"], "voice_map_path": vm_path,
                 "out_dir": out_dir,
                 "audit": audit["stats"], "warnings": audit["warnings"],
                 "warnings_ok": audit["ok"]})
    return jsonify(plan)


@app.route('/api/tts/voice-map', methods=['POST'])
def api_tts_voice_map_save():
    """保存角色音色配置（所有角色固定 speaker/seed，保证全剧音色一致）"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    voice_map = data.get('voice_map')
    if not isinstance(voice_map, dict):
        return jsonify({"success": False, "error": "缺少 voice_map"}), 400
    try:
        for name, v in (voice_map.get("characters") or {}).items():
            normalize_voice(v)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    path = os.path.join(_dub_project_dir(project_name), "voice_map.json")
    save_voice_map(voice_map, path)
    return jsonify({"success": True, "path": path, "voice_map": voice_map})


@app.route('/api/tts/preview', methods=['POST'])
def api_tts_preview():
    """单句试听合成：指定 text + 音色（或角色），返回可播放音频与时长"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err
    text = clean_line_text(data.get('text') or '', str(data.get('character') or ''))
    if not text:
        return jsonify({"success": False, "error": "缺少待合成文本 text"}), 400

    # 试听前先跑一遍台词预检：用户在这里就能看到「这句会被念成什么样」以及为什么，
    # 不必等到整集配完才发现结构残留被念了出来。
    _pf = None
    try:
        _pcfg = _qc_load_cfg()
        _pf = prompt_qc.preflight(
            "audio", text,
            ctx={"character": str(data.get('character') or '') or None,
                 "emotion": data.get('emotion'),
                 "voice_mode": (data.get('voice') or {}).get('mode')
                               if isinstance(data.get('voice'), dict) else None},
            cfg=_pcfg)
        if _pf.get("prompt"):
            text = _pf["prompt"]
    except Exception as e:  # noqa: BLE001 - 预检失败不影响试听
        app.logger.debug(f"试听台词预检跳过：{e}")

    env = tts_env_check()
    if not env.get("available"):
        return jsonify({"success": False, "error": "TTS 环境不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = load_voice_map(vm_path) or {}
    char_name = str(data.get('character') or '').strip()
    try:
        base = (voice_map.get("characters") or {}).get(char_name) if char_name else None
        if base is None and char_name:
            base = default_voice_map([{"name": char_name}], project_name, 1)["characters"].get(char_name)
        voice = normalize_voice(data.get('voice'), base)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    preview_dir = os.path.join(out_dir, "preview")
    os.makedirs(preview_dir, exist_ok=True)
    out_path = os.path.join(preview_dir, f"preview_{int(time.time())}_"
                                         f"{tts_client.safe_name(char_name or 'line', 12)}.wav")
    try:
        client = QwenTTSClient(out_root=DUB_DIR, params=TTS_DEFAULT_PARAMS)
        rec = client.synthesize_one(text, voice, out_path)
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 502
    if not rec.get("ok"):
        return jsonify({"success": False, "error": rec.get("error") or "合成失败", "result": rec}), 502

    info = probe_audio_info(rec["out_path"])
    # 试听只跑**客观层**音频质检：纯 ffmpeg、毫秒级，不花模型调用，保证试听依然「点一下就响」。
    # 需要含 AI 层（频谱/波形送检）的完整结论时走 POST /api/qc/audio。
    _objs = None
    try:
        _ocfg = _qc_load_cfg()
        if qc_client.audio_qc_ready(_ocfg):
            # ⚠️ quick_check 的 verdict 里已经带了 metrics，不要再单独 probe 一次 ——
            #    那会重复解码一遍音频（白等一次 ffmpeg）。
            _objs = audio_qc.quick_check(
                rec["out_path"], expect_sec=audio_qc.estimate_speech_sec(text) or None,
                min_speech_ratio=_ocfg.get("audio_min_speech_ratio", 0.50),
                min_mean_db=_ocfg.get("audio_min_mean_db", -45.0),
                max_drift=_ocfg.get("audio_max_drift", 0.50))
    except Exception as e:  # noqa: BLE001
        app.logger.debug(f"试听音频客观质检跳过：{e}")
    return jsonify({"success": True, "result": dict(rec, url=_dub_audio_url(project_name, rec["out_path"])),
                    "audio": info, "voice": voice, "url": _dub_audio_url(project_name, rec["out_path"]),
                    "prompt_qc": (_pf or {}).get("verdict"),
                    "prompt_qc_label": (_pf or {}).get("label"),
                    "prompt_qc_repairs": (_pf or {}).get("repairs") or [],
                    "text_used": text,
                    "audio_qc": _objs})


@app.route('/api/tts/generate', methods=['POST'])
def api_tts_generate():
    """发起批量配音（异步任务）：逐句/逐角色合成 + 整集合并"""
    data = request.json or {}
    # P2-T2：写盘路由统一走 _project_or_400（缺省/越界 project_name → 400，
    # 不再静默回落共享 'project' 命名空间造成串项目）。前端契约必填。
    project_name, err = _project_or_400((data.get('project_name') or '').strip())
    if err is not None:
        return err

    env = tts_env_check()
    if not env.get("available"):
        return jsonify({"success": False,
                        "error": "TTS 环境不可用，无法配音：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503

    try:
        resolved = _dub_resolve_script(dict(data, project_name=project_name))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 1)
    out_dir = _dub_project_dir(project_name)
    vm_path = os.path.join(out_dir, "voice_map.json")
    voice_map = data.get('voice_map') or load_voice_map(vm_path) \
        or default_voice_map(script.get('characters') or [], project_name, episode)
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]
    fmt = str(data.get('format') or 'wav').lower()
    if fmt not in ('wav', 'mp3'):
        return jsonify({"success": False, "error": "format 仅支持 wav / mp3"}), 400

    try:
        plan = build_dub_plan(script, voice_map, project_name, episode,
                              shot_ids=shot_ids,
                              only_missing=bool(data.get('only_missing')),
                              out_dir_wav=os.path.join(out_dir, "lines"))
    except TTSError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    if not plan.get("lines"):
        # 兜底剧本（dialogue=[]、prompt_h3=""）会走到这里，但用户看到「没有台词」
        # 完全不知道是自己没写、还是模型兜底了 —— 把体检结论一并给出。
        audit = dialogue_utils.audit_script(script)
        stats = audit["stats"]
        return jsonify({
            "success": False,
            "error": "剧本中没有可朗读台词（或所选镜头无台词）",
            "hint": ("该集可能是「模型失败后按原文兜底」生成的：镜头内容是原文照搬，"
                     "没有任何台词，因此配音无从合成。请先人工润色该集剧本（补台词），"
                     "或重新生成剧本。"
                     if stats.get("fallback_shot_count") else
                     "请检查剧本该集是否确实没有台词内容。"),
            "audit": stats, "problem_shots": audit["problem_shots"],
        }), 400

    # T03b：合成前同样召回 kind="audio" 历史教训做计划级纠偏（与 /api/tts/plan 一致），
    # 避免用户「预览没纠偏、合成又纠偏」的不一致；无教训时零行为变更。
    _apply_audio_lessons(plan.get("lines") or [], project_name)

    # 保存音色映射，保证同角色跨轮次音色一致
    try:
        save_voice_map(plan["voice_map"], vm_path)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"音色映射保存失败：{e}")

    plan_summary = {"script_path": resolved["script_path"], "script_source": resolved["source"],
                    "episode": episode, "line_count": plan["line_count"],
                    "characters": [{"name": c["name"], "voice": c["voice"],
                                    "line_count": c["line_count"]} for c in plan["characters"]]}
    with dub_lock:
        # G5：同项目已有 running 的配音任务 → 复用（uuid 任务 ID 防止同秒覆盖）
        _existing_dub = next((tid for tid, t in dub_tasks.items()
                               if t.get("status") == "running"
                               and t.get("project_name") == project_name), None)
        if _existing_dub:
            return jsonify({"success": True, "task_id": _existing_dub, "status": "started",
                            "reused": True, "project_name": project_name, "episode": episode,
                            "line_count": plan["line_count"], "plan": plan_summary,
                            "out_dir": out_dir})
        task_id = f"dub_{project_name}_{uuid.uuid4().hex[:12]}"
        dub_tasks[task_id] = {
            "status": "running", "progress": 0, "phase": "准备配音",
            "message": "正在准备配音…", "project_name": project_name,
            "total": plan["line_count"], "current": 0, "results": [],
            "plan": plan_summary, "out_dir": out_dir,
        }
    plan["script_path"] = resolved["script_path"]
    threading.Thread(target=_dub_worker,
                     args=(task_id, project_name, plan, out_dir, fmt, episode),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "project_name": project_name, "episode": episode,
                    "line_count": plan["line_count"], "plan": plan_summary,
                    "out_dir": out_dir})


@app.route('/api/tts/status/<task_id>', methods=['GET'])
def api_tts_status(task_id):
    with dub_lock:
        task = dub_tasks.get(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        return jsonify(dict(task, success=True, task_id=task_id))


@app.route('/api/tts/tasks', methods=['GET'])
def api_tts_tasks():
    with dub_lock:
        items = [dict(t, task_id=k) for k, t in dub_tasks.items()]
    items.sort(key=lambda t: t.get("task_id", ""), reverse=True)
    return jsonify({"success": True, "items": items[:50]})


@app.route('/api/tts/list', methods=['GET'])
def api_tts_list():
    """列出某项目已生成的配音产物（单句 + 合并音轨）"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    out_dir = os.path.join(DUB_DIR, project_name)
    lines, merged = [], []
    if os.path.isdir(out_dir):
        lines_dir = os.path.join(out_dir, "lines")
        for base, bucket in ((lines_dir, lines), (out_dir, merged)):
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                if not name.lower().endswith((".wav", ".mp3", ".flac")):
                    continue
                p = os.path.join(base, name)
                if not os.path.isfile(p):
                    continue
                info = probe_audio_info(p)
                bucket.append({
                    "name": name, "path": os.path.abspath(p),
                    "url": _dub_audio_url(project_name, p),
                    "duration": info.get("duration"), "size_kb": round((info.get("size_bytes") or 0) / 1024, 1),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
                })
    merged.sort(key=lambda x: x["name"], reverse=True)
    vm_path = os.path.join(out_dir, "voice_map.json")
    return jsonify({"success": True, "project_name": project_name, "out_dir": out_dir,
                    "lines": lines, "merged": merged,
                    "voice_map": load_voice_map(vm_path)})


@app.route('/api/tts/file/<project_name>/<path:filename>')
def api_tts_file(project_name, filename):
    """播放 / 下载配音产物（支持 Range 拖动试听，download=1 触发下载）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(DUB_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, filename.replace("\\", "/").lstrip("/")))
    if not filepath.startswith(base + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')


# =====================================================================
# 音画对齐与混音合成（配音轨 × 成片视频 → 带配音成片 output/final_dub/）
# =====================================================================

mix_tasks = {}
mix_lock = threading.Lock()


def _mix_resolve_video(data: dict, project_name: str) -> str:
    """定位待合成的成片：显式 video_path/video_url 优先，否则在项目成片目录自动匹配最新 mp4"""
    if (data.get("video_path") or "").strip() or (data.get("video_url") or "").strip():
        return _upscale_resolve_video(data)
    cands = []
    for d in project_store.project_dirs(FINAL_DIR, project_name):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".mp4"):
                p = os.path.join(d, name)
                cands.append((os.path.getmtime(p), p))
    if not cands:
        raise DubMixError(
            "未找到成片视频：请先在步骤6完成成片合成，或显式提供 video_path / video_url")
    cands.sort(reverse=True)
    return cands[0][1]


def _mix_segments_dir(project_name: str, episode: int = 0) -> str:
    """定位该集（episode 给定）或该项目的镜头分段视频目录（用于按真实分段时长对齐时间轴）

    B-10 P1-6：带集号过滤。第 2 集起不再取到第 1 集素材，避免时间轴/成片源系统性错配。
    优先匹配该集专属目录（``<key>_第N集`` 或 ``epNN`` 子目录），找不到再回退到项目级目录。
    """
    ep_tag = f"ep{int(episode):02d}" if episode else ""
    best, best_key = "", (-1, 0)
    # 优先找该集专属目录（第 2 集起视频通常落在 <项目键>_第N集/ 或 epNN/ 子目录）
    ep_dir = ""
    if episode:
        for cand in (os.path.join(VIDEOS_DIR, project_name, ep_tag),
                     os.path.join(VIDEOS_DIR, f"{project_name}_第{episode}集")):
            if os.path.isdir(cand):
                ep_dir = cand
                break
    if ep_dir:
        # 该集目录直接采用
        vids = [f for f in os.listdir(ep_dir) if f.lower().endswith(".mp4")]
        if vids:
            return ep_dir
    # 回退：项目级目录（第 1 集或整集模式）
    for d in project_store.project_dirs(VIDEOS_DIR, project_name):
        if not os.path.isdir(d):
            continue
        vids = [f for f in os.listdir(d) if f.lower().endswith(".mp4")]
        if not vids:
            continue
        key = (len(vids), max(os.path.getmtime(os.path.join(d, f)) for f in vids))
        if key > best_key:
            best, best_key = d, key
    return best


def _mix_manifest(project_name: str, episode: int = 0) -> dict:
    """读取配音清单（优先指定集数，其次最新）"""
    out_dir = os.path.join(DUB_DIR, project_name)
    if not os.path.isdir(out_dir):
        raise DubMixError(f"尚未生成配音（目录不存在）：{out_dir}")
    if episode:
        p = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return {"manifest": json.load(f), "path": p}
    cands = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
             if f.endswith("_dub_manifest.json")]
    if not cands:
        raise DubMixError("未找到配音清单（*_dub_manifest.json），请先完成配音合成")
    cands.sort(key=os.path.getmtime, reverse=True)
    p = cands[0]
    with open(p, "r", encoding="utf-8") as f:
        return {"manifest": json.load(f), "path": p}


def _mix_audio_url(project_name: str, rel_path: str) -> str:
    base = os.path.abspath(mix_out_dir(project_name))
    p = os.path.abspath(rel_path)
    if not p.startswith(base + os.sep):
        return ""
    rel = os.path.relpath(p, base).replace(os.sep, "/")
    return f"/api/mix/file/{project_name}/{rel}"


# ===== 成片自动登记交付物 =====
# 用户视角的痛点：手工点击 / AI 总控跑出来的成片不会出现在「成品验收」页，页面永远是空的
# （只有 /api/autopilot/run-once 与托管轮转会登记）。这里在成片真正落盘处统一登记。


def _episode_video_stats(project_name: str, episode_no) -> dict:
    """该集镜头视频就绪度：剧本镜头数 vs 已落盘视频数（>1KB 才算数）"""
    script = _load_script_for(project_name, episode_no)
    shots = [s for s in (script.get('shots') or []) if isinstance(s, dict)]
    d = _ep_dir(os.path.join(VIDEOS_DIR, project_name), episode_no)
    ready = 0
    if os.path.isdir(d):
        for fn in os.listdir(d):
            if not fn.lower().endswith('.mp4'):
                continue
            try:
                if os.path.getsize(os.path.join(d, fn)) > 1024:
                    ready += 1
            except OSError:
                continue
    return {"total": len(shots), "ready": ready, "dir": d}


def register_final_deliverable(project_name: str, episode_no, video_path: str,
                               meta: dict = None) -> dict:
    """把整集成片登记进「待验收」队列（幂等）。

    硬闸门（不满足就完全不登记，避免验收页被垃圾塞满）：
      0) 成片不存在 / < 100KB；1) 探不到时长或 < 2s。

    软闸门（镜头覆盖）：剧本镜头数 vs 已落盘镜头视频数。
    这里刻意不做「时长 >= 镜头数 x N 秒」的硬判定 —— 漫剧单镜常常不到 1s
    （实测 6 镜合并成片只有 4.46s），按时长否决会把真成片误判成半成品。
    镜头不齐时仍然登记，但在 meta 里打 `incomplete_shots` + `warning`，
    让「成品验收」页能显示「可能不完整」提醒，用户可据此打回。

    返回 {"registered": bool, "reason": str, "stats": {...}, "item": {...}}
    """
    if not video_path or not os.path.exists(video_path):
        return {"registered": False, "reason": "成片文件不存在", "stats": {}}
    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        return {"registered": False, "reason": f"成片不可读：{e}", "stats": {}}
    if size < 100 * 1024:
        return {"registered": False, "reason": f"成片过小（{size} 字节），疑似半成品",
                "stats": {"size": size}}

    try:
        ep = int(episode_no or 1)
    except (TypeError, ValueError):
        ep = 1

    try:
        vinfo = probe_video_info(video_path) or {}
    except Exception:  # noqa: BLE001 - 探测失败不代表成片不可用，走宽松分支
        vinfo = {}
    duration = float(vinfo.get("duration") or 0)
    if duration and duration < 2.0:
        return {"registered": False, "reason": f"成片仅 {duration:.1f}s，疑似片段",
                "stats": {"duration": duration, "size": size}}

    stats = _episode_video_stats(project_name, ep)
    stats["size"], stats["duration"] = size, duration
    total, ready = int(stats.get("total") or 0), int(stats.get("ready") or 0)
    incomplete = bool(total > 0 and ready < total)

    item_meta = dict(meta or {})
    item_meta.setdefault("source", "final")
    item_meta.setdefault("duration_sec", round(duration, 2) if duration else None)
    item_meta.setdefault("size_bytes", size)
    item_meta["shots_total"] = total
    item_meta["shots_ready"] = ready
    if incomplete:
        item_meta["incomplete_shots"] = True
        item_meta["warning"] = (f"该集剧本 {total} 镜，仅发现 {ready} 个镜头视频，"
                                f"成片可能不完整，建议核对后再验收")
    try:
        item = pipeline.record_deliverable(project_name, ep, video_path, meta=item_meta)
    except Exception as e:  # noqa: BLE001 - 登记失败不能影响出片主流程
        app.logger.warning(f"成片登记交付物失败（{project_name} 第{ep}集）：{e}")
        return {"registered": False, "reason": f"登记失败：{e}", "stats": stats}
    if incomplete:
        app.logger.warning(f"成片已登记但镜头疑似不全：{project_name} 第{ep}集 "
                           f"({ready}/{total}) -> {os.path.basename(video_path)}")
        return {"registered": True,
                "reason": f"已登记（镜头覆盖 {ready}/{total}，可能不完整）",
                "stats": stats, "item": item, "incomplete": True}
    app.logger.info(f"成片已登记待验收：{project_name} 第{ep}集 -> {os.path.basename(video_path)}")
    return {"registered": True, "reason": "已登记", "stats": stats, "item": item}


def _isolate_shot_sfx(video_path: str, project: str, episode, shot_id) -> dict:
    """对单镜音轨做人声分离，只留音效（H3 原生音效，剔掉它自带的说话声）。

    **fail-open**：任何异常都只返回 ok=False，绝不打断出片流程。
    """
    try:
        import sfx_isolate
        return sfx_isolate.isolate_sfx(
            video_path, project, f"ep{int(episode):02d}_shot{int(shot_id):02d}")
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _mix_prepare(data: dict) -> dict:
    """公共准备：解析项目 / 视频 / 剧本 / 配音清单 / 时间轴 / 逐句条目（不合成）"""
    # P2-T2：mix 的 project 解析唯一事实源在 _mix_prepare（被 /mix/plan 与 /mix/generate 共用）。
    # 缺省/越界 project_name → 抛 DubMixError（两条路由均已 catch 并回 400），
    # 不再静默回落共享 'project' 命名空间造成串项目。前端契约必填。
    project_name, _mix_err = _project_or_400((data.get('project_name') or '').strip())
    if _mix_err is not None:
        raise DubMixError("缺少 project_name")
    video_path = _mix_resolve_video(data, project_name)

    resolved = _dub_resolve_script(dict(data, project_name=project_name))
    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 0)

    mf = _mix_manifest(project_name, episode)
    manifest = mf["manifest"]
    episode = episode or int(manifest.get("episode") or 1)

    seg_dir = _mix_segments_dir(project_name, episode)
    timeline = shot_timeline(script, videos_dir=seg_dir)

    params = dict(MIX_DEFAULT_PARAMS)
    params.update(data.get('params') or {})
    mode = (data.get('mode') or params.get("mode") or "timeline").strip()

    lines = [ln for ln in (manifest.get("lines") or []) if ln.get("ok") and ln.get("out_path")]
    if mode == "concat":
        merged = manifest.get("merged_audio") or ""
        if not merged or not os.path.exists(merged):
            raise DubMixError("concat 模式需要整集合并音轨，但配音清单中没有有效 merged_audio")
        entries = [{"line_id": "merged", "shot_id": None, "character": "",
                    "text": "", "audio_path": os.path.abspath(merged),
                    "audio_dur": float((manifest.get("merged_info") or {}).get("duration") or 0),
                    "start": 0.0, "fit_ratio": 1.0}]
        warnings = ["concat 模式：整集音轨从 0 秒顺次铺设，不做逐镜头对齐"]
    else:
        built = build_entries(lines, timeline, params, mode=mode)
        entries, warnings = built["entries"], list(built["warnings"])
    if not entries:
        raise DubMixError("没有可用的配音音频：请先完成配音合成，或检查配音文件是否存在")

    # ---- 音效轨：H3 原生音效经人声分离后垫底（2026-09-17 新增）----
    # 为什么需要：H3 是音视频联合模型，原音轨里既有打斗/雨声等音效，也有它自己生成的
    # 说话声。直接保留原音轨会让两套人声重叠；完全丢弃又会让成片没有任何音效。
    # 折中：用 sfx_isolate 分离出「纯音效」，作为独立条目按同一条时间轴垫底。
    sfx_entries = []
    if H3_SFX_ISOLATE and mode != "concat":
        try:
            import sfx_isolate
            sfx_entries = sfx_isolate.build_sfx_entries(
                project_name, episode, timeline,
                volume=float(params.get("original_audio_volume") or 0.3))
        except Exception as e:                                  # noqa: BLE001
            warnings.append(f"音效轨装配失败（本集跳过音效）：{type(e).__name__}: {e}")
    if sfx_entries:
        entries = list(entries) + sfx_entries
        # 已用「分离后的纯音效」→ 关掉视频原音轨，否则人声会回来、音效也会叠双份
        params["keep_original_audio"] = False
        warnings.append(f"已叠加 {len(sfx_entries)} 条镜头音效（H3 音轨已做人声分离，"
                        f"垫底音量 {params.get('original_audio_volume')}）")
    elif H3_SFX_ISOLATE and params.get("keep_original_audio"):
        warnings.append("未找到可用的分离音效轨，将直接使用视频原音轨垫底"
                        "（其中可能含 H3 生成的说话声）")

    vinfo = probe_video_info(video_path)
    if vinfo.get("duration") and entries[-1].get("end", 0) > float(vinfo["duration"]) + 0.5:
        warnings.append(
            f"末句结束 {entries[-1].get('end')}s 超出视频时长 {vinfo.get('duration')}s，超出部分会被截断")

    return {
        "project_name": project_name, "video_path": video_path, "video_info": vinfo,
        "script_path": resolved["script_path"], "script_source": resolved["source"],
        "episode": episode, "manifest_path": mf["path"], "manifest": manifest,
        "segments_dir": seg_dir, "timeline": timeline,
        "entries": entries, "warnings": warnings, "mode": mode, "params": params,
    }


def _mix_worker(task_id: str, prepared: dict, out_name: str):
    """后台合成：逐句对齐混音 → 落盘带配音成片 + 报告"""
    try:
        project_name = prepared["project_name"]
        out_dir = mix_out_dir(project_name)
        out_path = os.path.join(out_dir, out_name)
        with mix_lock:
            mix_tasks[task_id].update({"phase": "音画对齐混音中", "progress": 30})
        report = mix_video_with_entries(prepared["video_path"], prepared["entries"],
                                        out_path, prepared["params"])
        # ---- 成品音频质检：成片音轨是不是真的有人声 ----
        # ffmpeg 返回成功、文件也有音频流，并不代表「配音真的混进去了」：
        # 条目路径错、amix 被压成静音、源片段本身无声，都能产出一条「合法但没声音」的
        # 音轨。这里对**最终成片**实测一遍（整轨口径，不做有声占比判定）。
        report["audio_qc"] = _mix_audio_qc(report, _qc_load_cfg())
        report.update({
            "task_id": task_id, "project": project_name, "episode": prepared["episode"],
            "mode": prepared["mode"], "video_source": prepared["video_path"],
            "script_path": prepared["script_path"], "manifest_path": prepared["manifest_path"],
            "segments_dir": prepared["segments_dir"], "warnings": prepared["warnings"],
            "timeline": prepared["timeline"], "entries": prepared["entries"],
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        report_path = os.path.join(out_dir, f"{os.path.splitext(out_name)[0]}_mix_report.json")
        write_mix_report(report, report_path)
        _aq = report.get("audio_qc") or {}
        _aq_msg = ""
        if _aq.get("enabled") and _aq.get("passed") is not None:
            _aq_msg = "；音频质检通过" if _aq.get("passed") else \
                f"；音频质检未通过（{str(_aq.get('reason') or '')[:60]}）"
        with mix_lock:
            mix_tasks[task_id].update({
                "status": "completed", "progress": 100, "phase": "合成完成",
                "message": (f"已合成 {report['entry_count']} 句配音，"
                            f"耗时 {report['elapsed_sec']}s{_aq_msg}"),
                "output_path": report["output_path"],
                "report_path": report_path,
                "url": _mix_audio_url(project_name, report["output_path"]),
                "audio_qc": _aq,
                "result": report,
            })
        # 带配音成片＝用户真正要验收的成品：自动登记进「成品验收」队列
        reg = register_final_deliverable(
            project_name, prepared["episode"], report["output_path"],
            meta={"source": "mix", "mode": prepared["mode"],
                  "entry_count": report.get("entry_count"),
                  "video_source": os.path.basename(prepared["video_path"] or ""),
                  "report": os.path.basename(report_path)})
        with mix_lock:
            mix_tasks[task_id]["deliverable"] = {
                "registered": bool(reg.get("registered")),
                "reason": reg.get("reason") or "",
                "episode_no": int(prepared["episode"] or 1),
                "path": report["output_path"],
            }
        if not reg.get("registered"):
            app.logger.info(f"成片未登记待验收（{project_name} 第{prepared['episode']}集）："
                            f"{reg.get('reason')}")
    except DubMixError as e:
        app.logger.warning(f"混音合成失败: {e}")
        # B-16 P2-11：混音失败 → 清理本任务产生的中间产物（未完成的 report / 半成品）
        _cleanup_scratch_dir(out_dir, app.logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成失败",
                                       "message": str(e), "progress": 100})
    except Exception as e:  # pragma: no cover - 兜底
        app.logger.exception("音画合成异常")
        # B-16 P2-11：混音异常 → 清理中间产物
        _cleanup_scratch_dir(out_dir, app.logger)
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成异常",
                                       "message": f"{type(e).__name__}: {e}", "progress": 100})


@app.route('/api/mix/env', methods=['GET'])
def api_mix_env():
    """音画合成环境自检：ffmpeg/ffprobe + 默认参数 + 最近一次配音清单"""
    env = mix_ffmpeg_check()
    project_name = _safe_project(request.args.get('project_name') or 'project')
    env["default_params"] = MIX_DEFAULT_PARAMS
    env["out_dir"] = mix_out_dir(project_name)
    try:
        mf = _mix_manifest(project_name)
        env["dub_manifest"] = mf["path"]
        env["dub_lines_ok"] = sum(1 for l in (mf["manifest"].get("lines") or [])
                                  if l.get("ok") and l.get("out_path"))
        env["merged_audio"] = mf["manifest"].get("merged_audio") or ""
        env["episode"] = mf["manifest"].get("episode")
    except DubMixError as e:
        env["dub_manifest"] = ""
        env["dub_reason"] = str(e)
    return jsonify(env)


@app.route('/api/mix/plan', methods=['GET', 'POST'])
def api_mix_plan():
    """合成计划预览（dry-run）：镜头时间轴 + 逐句落点 + 告警，不做 ffmpeg 合成"""
    data = request.json or {} if request.method == 'POST' else dict(request.args)
    try:
        prepared = _mix_prepare(data)
    except (DubMixError, TTSError, UpscaleError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({
        "success": True,
        "project_name": prepared["project_name"],
        "video_path": prepared["video_path"], "video_info": prepared["video_info"],
        "script_path": prepared["script_path"], "manifest_path": prepared["manifest_path"],
        "segments_dir": prepared["segments_dir"], "mode": prepared["mode"],
        "episode": prepared["episode"],
        "timeline": prepared["timeline"],
        "entries": prepared["entries"], "line_count": len(prepared["entries"]),
        "coverage_sec": round(sum(e.get("audio_dur") or 0 for e in prepared["entries"]), 3),
        "warnings": prepared["warnings"], "params": prepared["params"],
    })


@app.route('/api/mix/generate', methods=['POST'])
def api_mix_generate():
    """发起合成（异步任务）：产出自定义命名的带配音成片"""
    data = request.json or {}
    env = mix_ffmpeg_check()
    if not env.get("available"):
        return jsonify({"success": False,
                        "error": "ffmpeg/ffprobe 不可用：" + "；".join(env.get("reasons") or []),
                        "env": env}), 503
    try:
        prepared = _mix_prepare(data)
    except (DubMixError, TTSError, UpscaleError) as e:
        body = {"success": False, "error": str(e)}
        # 「没有可用的配音音频」最常见原因不是没跑 TTS，而是剧本本身没有台词
        # （模型兜底生成的镜头 dialogue=[] ）—— 补一句体检结论，别让用户白跑。
        if "配音" in str(e):
            try:
                _pj = _safe_project(data.get('project_name') or 'project')
                _audit = dialogue_utils.audit_script(_load_script_for(_pj, data.get('episode')))
                if _audit["stats"].get("fallback_shot_count") or _audit["stats"].get(
                        "silent_shot_count"):
                    body["audit"] = _audit["stats"]
                    body["hint"] = "；".join(_audit["warnings"][:2])
                    body["problem_shots"] = _audit["problem_shots"]
            except Exception as ae:  # noqa: BLE001 - 体检失败不影响主错误
                app.logger.debug(f"混音失败时的剧本体检跳过：{ae}")
        return jsonify(body), 400

    project_name = prepared["project_name"]
    out_name = (data.get("out_name") or "").strip()
    if not out_name:
        base = os.path.splitext(os.path.basename(prepared["video_path"]))[0]
        out_name = f"{base}_ep{int(prepared['episode']):02d}_dubbed.mp4"
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_name = os.path.basename(out_name.replace("\\", "/"))

    task_id = f"mix_{project_name}_{uuid.uuid4().hex[:12]}"
    with mix_lock:
        # G5：同一视频已有 running 的混音任务 → 复用（匹配 video_path：同项目可能有多个视频）
        _existing_mix = next((tid for tid, t in mix_tasks.items()
                               if t.get("status") == "running"
                               and t.get("video_path") == prepared["video_path"]), None)
        if _existing_mix:
            return jsonify({"success": True, "task_id": _existing_mix, "status": "started",
                            "reused": True, "project_name": project_name, "out_name": out_name,
                            "line_count": len(prepared["entries"]), "mode": prepared["mode"],
                            "warnings": prepared["warnings"],
                            "video_path": prepared["video_path"],
                            "out_dir": mix_out_dir(project_name)})
        mix_tasks[task_id] = {
            "task_id": task_id, "status": "running", "progress": 5,
            "phase": "准备音画对齐", "project_name": project_name,
            "video_path": prepared["video_path"], "out_name": out_name,
            "line_count": len(prepared["entries"]), "mode": prepared["mode"],
            "warnings": prepared["warnings"],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    threading.Thread(target=_mix_worker, args=(task_id, prepared, out_name),
                     daemon=True).start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "project_name": project_name, "out_name": out_name,
                    "line_count": len(prepared["entries"]), "mode": prepared["mode"],
                    "warnings": prepared["warnings"],
                    "video_path": prepared["video_path"],
                    "out_dir": mix_out_dir(project_name)})


@app.route('/api/mix/status/<task_id>', methods=['GET'])
def api_mix_status(task_id):
    with mix_lock:
        task = mix_tasks.get(task_id)
        if not task:
            return jsonify({"success": False, "error": "任务不存在"}), 404
        if task.get("output_path"):
            task = dict(task, url=_mix_audio_url(task["project_name"], task["output_path"]))
    return jsonify({"success": True, "task": task})


@app.route('/api/mix/tasks', methods=['GET'])
def api_mix_tasks():
    with mix_lock:
        items = [dict(t) for t in mix_tasks.values()]
    items.sort(key=lambda t: t.get("task_id", ""), reverse=True)
    return jsonify({"success": True, "items": items[:50]})


@app.route('/api/mix/list', methods=['GET'])
def api_mix_list():
    """列出某项目已生成的带配音成片 + 最近合成报告摘要"""
    project_name = _safe_project(request.args.get('project_name') or 'project')
    out_dir = os.path.join(DUB_MIX_DIR, project_name)
    items = []
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir), reverse=True):
            if not name.lower().endswith(".mp4"):
                continue
            p = os.path.join(out_dir, name)
            info = probe_video_info(p)
            items.append({
                "name": name, "path": os.path.abspath(p),
                "url": _mix_audio_url(project_name, p),
                "duration": info.get("duration"), "width": info.get("width"),
                "height": info.get("height"),
                "size_mb": round((info.get("size_bytes") or 0) / 1048576, 3),
                "has_audio": info.get("has_audio"), "audio_codec": info.get("audio_codec"),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
            })
    reports = []
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir), reverse=True):
            if name.endswith("_mix_report.json"):
                reports.append(os.path.join(out_dir, name))
    return jsonify({"success": True, "project_name": project_name, "out_dir": out_dir,
                    "items": items, "report_path": reports[0] if reports else "",
                    "default_params": MIX_DEFAULT_PARAMS})


@app.route('/api/mix/file/<project_name>/<path:filename>')
def api_mix_file(project_name, filename):
    """播放 / 下载带配音成片（支持 Range，download=1 触发下载）"""
    project_name = _safe_project(project_name)
    base = os.path.abspath(os.path.join(DUB_MIX_DIR, project_name))
    filepath = os.path.abspath(os.path.join(base, filename.replace("\\", "/").lstrip("/")))
    if not filepath.startswith(base + os.sep) or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')


# ============================================================================
# 无人值守托管（Autopilot）——「上传小说 → 敲定风格 → 电脑自己生产 → 人只验成品」
# ============================================================================
# 接口分工：
#   总览/自检 → status / ready / curve
#   托管开关 → plans / plan / enable / disable / pause / resume
#   进度视图 → progress / progress/<project>
#   成品验收 → deliverables / review / deliverable/file
#   人工介入 → exceptions / exceptions resolve
#   手动触发 → run-once（用于验证与补跑单集）


def _friendly_error(msg, fallback: str = "服务内部错误，请稍后重试（详情见后端日志）") -> str:
    """把后端异常整理成可安全展示给前端的文案（对应测试缺陷 D5）。

    前端错误框不应出现 traceback、文件路径、模块名等实现细节。
    这里做一次归一化：截掉 traceback 段、去掉 File/line 与模块来源、
    压缩空白并限长；若仍残留实现细节特征，则整体降级为通用文案。
    业务类友好错误（中文短句）会原样保留。
    """
    text = str(msg or "").strip()
    if not text:
        return fallback
    idx = text.find("Traceback (most recent call last)")
    if idx != -1:
        text = text[:idx].strip()
    text = text.splitlines()[-1].strip() if text else ""
    text = re.sub(r'File\s+"[^"]*",\s*line\s*\d+', "", text)
    text = re.sub(r"\s*from\s+'[^']*'", "", text)          # cannot import name 'X' from 'mod'
    text = re.sub(r"\s*\([^()]*\.py[^()]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # 仍残留实现细节（模块/导入语句/文件路径）→ 一律降级，避免外泄内部结构
    if re.search(r"\.py\b|\bimport\b|\bmodule\b|site-packages|[\\/]", text):
        return fallback
    if len(text) > 160:
        text = text[:160] + "…"
    return text or fallback


# 注：`_autopilot_guard` 已**前移**到本文件前段（首个使用点之前，见
# 「托管接口统一异常兜底」段）—— 原先定义在这里（文件后段），只能装饰其后
# 注册的路由，早注册的路由全部拿不到兜底（D1，2026-09-23）。


@app.errorhandler(BadRequest)
def _handle_bad_request(e):
    """请求体无法解析（非合法 JSON / Content-Type 不匹配）→ 400。

    没有这个兜底时，未被 _autopilot_guard 包裹的路由会直接把 werkzeug 的
    400 渲染成 HTML 错误页，前端拿到一坨 HTML 而无法解析成 JSON。
    """
    return jsonify({
        "success": False,
        "error": "请求体格式不正确：需要合法的 JSON（并带上 Content-Type: application/json）",
    }), 400


@app.route('/api/autopilot/status', methods=['GET'])
@_autopilot_guard
def api_autopilot_status():
    """托管总览：开关状态、当前在做什么、待验收数、异常数、24h 生产曲线"""
    project = request.args.get("project", "").strip()
    return jsonify({"success": True, **autopilot.status(project)})


@app.route('/api/autopilot/ready', methods=['GET'])
@_autopilot_guard
def api_autopilot_ready():
    """托管可行性自检：告诉用户还差什么才能真正无人值守"""
    return jsonify({"success": True, **autopilot.ready()})


@app.route('/api/autopilot/curve', methods=['GET'])
@_autopilot_guard
def api_autopilot_curve():
    """24 小时生产曲线（每小时完成/失败集数、平均耗时、吞吐）"""
    try:
        hours = max(1, min(int(request.args.get('hours') or 24), 168))
    except (TypeError, ValueError):
        hours = 24
    return jsonify({"success": True, **autopilot.production_curve(hours)})


@app.route('/api/autopilot/plans', methods=['GET'])
@_autopilot_guard
def api_autopilot_plans():
    """全部项目的托管计划"""
    plans = autopilot.list_plans()
    return jsonify({"success": True, "count": len(plans), "items": plans,
                    "defaults": autopilot.PLAN_DEFAULTS})


@app.route('/api/autopilot/plan/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autopilot_plan_get(project_name):
    project = _safe_project(project_name)
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.get_plan(project),
                    "defaults": autopilot.PLAN_DEFAULTS})


@app.route('/api/autopilot/plan/<project_name>', methods=['POST'])
@_autopilot_guard
def api_autopilot_plan_set(project_name):
    """设置某项目的自动生产计划（目标集数、镜头数、风格、质量阈值、重试上限…）"""
    project = _safe_project(project_name)
    data = request.json or {}
    novel_id = str(data.pop('novel_id', '') or '').strip()
    patch = {k: v for k, v in (data or {}).items() if k in autopilot.PLAN_DEFAULTS}
    if not patch and not novel_id:
        return jsonify({"success": False,
                        "error": f"没有可更新字段；可用字段：{sorted(autopilot.PLAN_DEFAULTS)}"}), 400
    plan = autopilot.set_plan(project, patch, novel_id=novel_id)
    return jsonify({"success": True, "project": project, "plan": plan})


@app.route('/api/autopilot/enable', methods=['POST'])
@_autopilot_guard
def api_autopilot_enable():
    """开启托管（可同时带生产配置）。novel_id 缺省时从项目注册表自动关联。"""
    # P0-5 门禁：托管是无人值守路径，配置不全时开闸只会白跑一整晚 —— 先在门口拦下
    _gate = _ai_gate_or_400("episode")
    if _gate is not None:
        return _gate
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project') or data.get('project_name') or '',
                                    field_name="project")
    if err is not None:
        return err
    novel_id = str(data.get('novel_id') or '').strip()
    if not novel_id:
        try:
            rec = project_store.get_project(project) or {}
            novel_id = (rec.get('novel_id') or '').strip()
        except Exception:  # noqa: BLE001
            novel_id = ''
    if not novel_id:
        # 退回「按项目名匹配同名小说」，尽量让用户少填一步
        try:
            for n in list_novels(NOVELS_DIR):
                if (n.get('title') or '').strip() == project:
                    novel_id = n.get('novel_id') or ''
                    break
        except Exception:  # noqa: BLE001
            novel_id = ''
    patch = {k: v for k, v in (data or {}).items()
             if k in autopilot.PLAN_DEFAULTS and k != 'enabled'}
    plan = autopilot.enable(project, patch)
    if novel_id:
        plan = autopilot.set_plan(project, {}, novel_id=novel_id)
    # 顺带校验输入是否齐备（小说正文 / 项目绑定），避免"开了但没活干"
    warn = ""
    if not plan.get('novel_id'):
        warn = "该项目未关联小说，托管不会生产；请先在「小说库」上传并建项目"
    return jsonify({"success": True, "project": project, "plan": plan,
                    "novel_id": plan.get('novel_id') or "", "warning": warn})


@app.route('/api/autopilot/disable', methods=['POST'])
@_autopilot_guard
def api_autopilot_disable():
    data = request.json or {}
    # G4：判空看原始入参（_safe_project('') 返回真值 'project'，死守卫）
    project, err = _project_or_400(data.get('project') or data.get('project_name') or '',
                                   field_name="project")
    if err is not None:
        return err
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.disable(project)})


@app.route('/api/autopilot/pause', methods=['POST'])
@_autopilot_guard
def api_autopilot_pause():
    """暂停托管（在当前步骤边界生效，不产生半成品）

    ⚠️ 这是**全局**开关：``autopilot.pause()`` 置的是全局 ``_STATE["paused"]``，
    **不区分项目**。此前接口既不读 ``project`` 也不提示，调用方（尤其是总控 AI）
    很容易以为「只暂停了某个项目」，实际把所有项目都停了 —— 静默越权。
    这里保留 reason 语义，并在收到 project 时**显式告知**它被忽略、给出替代做法。
    """
    data = request.json or {}
    result = autopilot.pause(str(data.get('reason') or '手动暂停'))
    ignored_project = str(data.get('project') or data.get('project_name') or '').strip()
    payload = {"success": True, **result}
    if ignored_project:
        payload["warning"] = (f"暂停托管是**全局**开关，已忽略 project='{ignored_project}'"
                              "（所有项目都会暂停）。若只想停某个项目，"
                              "请调用 /api/autopilot/disable 并带 project。")
        payload["scope"] = "global"
    return jsonify(payload)


@app.route('/api/autopilot/resume', methods=['POST'])
@_autopilot_guard
def api_autopilot_resume():
    """恢复托管（服务重启后也可用它手动拉起守护进程）"""
    return jsonify({"success": True, **autopilot.resume()})


@app.route('/api/autopilot/progress', methods=['GET'])
@_autopilot_guard
def api_autopilot_progress_all():
    """全部项目的分集进度（剧本/资产/分镜/视频/成片 逐集状态）"""
    items = autopilot.all_progress()
    return jsonify({"success": True, "count": len(items), "items": items})


@app.route('/api/autopilot/progress/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autopilot_progress_one(project_name):
    project = _safe_project(project_name)
    return jsonify({"success": True, **autopilot.project_progress(project)})


@app.route('/api/autopilot/deliverables', methods=['GET'])
@_autopilot_guard
def api_autopilot_deliverables():
    """成品验收队列——用户唯一需要重点看的清单（只含最终成片）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else ''
    items = pipeline.list_deliverables(project)
    return jsonify({"success": True, "count": len(items),
                    "pending": sum(1 for x in items if x.get('review') == 'pending'),
                    "items": items})


@app.route('/api/autopilot/deliverables/review', methods=['POST'])
@_autopilot_guard
def api_autopilot_review():
    """验收 / 打回成片（打回 = 该集在下次轮转时自动重做）"""
    data = request.json or {}
    # 口径统一（F-01 收口）：与 run-once 一致，改走 _project_or_400 拒绝越界/空/控制字符。
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    review = str(data.get('review') or '').strip().lower()
    if review not in ('accepted', 'rejected', 'pending'):
        return jsonify({"success": False, "error": "review 仅支持 accepted / rejected / pending"}), 400
    try:
        ep = int(data.get('episode_no') or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    item = pipeline.set_deliverable_review(project, ep, review, str(data.get('note') or ''))
    if not item:
        return jsonify({"success": False, "error": f"未找到 {project} 第{ep}集的成片记录"}), 404
    return jsonify({"success": True, "item": item})


@app.route('/api/autopilot/deliverable/file/<project_name>/<path:filename>')
@_autopilot_guard
def api_autopilot_deliverable_file(project_name, filename):
    """播放 / 下载成片（以交付物索引为准解析真实路径，防目录穿越）"""
    project = _safe_project(project_name)
    filepath = pipeline.deliverable_path(project, filename)
    if not filepath or not os.path.isfile(filepath):
        abort(404)
    return send_file(filepath, conditional=True,
                     as_attachment=request.args.get('download') == '1')


@app.route('/api/autopilot/exceptions', methods=['GET'])
@_autopilot_guard
def api_autopilot_exceptions():
    """需人工介入清单（超过重试上限 / 环境性缺失导致挂起）"""
    project = _safe_project(request.args.get('project') or '') if request.args.get('project') else ''
    projects = [project] if project else [p['project'] for p in autopilot.list_plans()]
    items = []
    for pj in projects:
        for d in pipeline.list_dead_letters(pj):
            if not d.get('resolved'):
                # P0-4：list_dead_letters 返回的是磁盘 dead_letter.json 里的原始项
                # （字段只有 episode_no/reason/detail/...，不含 project）——不传
                # ?project= 时每条无法归属项目、排序键 x.get('project') 形同虚设。
                # 用**拷贝**补 project（不就地改磁盘读出的对象，避免污染其他调用方），
                # 让 items.sort 的 project 排序键从此真正生效。
                items.append({**d, "project": pj})
    items.sort(key=lambda x: (x.get('project') or '', int(x.get('episode_no') or 0)))
    return jsonify({"success": True, "count": len(items), "items": items})


@app.route('/api/autopilot/exceptions/resolve', methods=['POST'])
@_autopilot_guard
def api_autopilot_exception_resolve():
    """处理异常：标记已解决 / 忽略（之后托管会重新尝试该集）"""
    data = request.json or {}
    project = _safe_project(data.get('project') or data.get('project_name') or '')
    try:
        ep = int(data.get('episode_no') or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    item = pipeline.resolve_dead_letter(project, ep, str(data.get('note') or ''))
    if not item:
        return jsonify({"success": False, "error": f"未找到 {project} 第{ep}集的异常记录"}), 404
    autopilot.wake()
    return jsonify({"success": True, "item": item})


@app.route('/api/autopilot/run-once', methods=['POST'])
@_autopilot_guard
def api_autopilot_run_once():
    """立即生产指定一集（同步返回结果；用于联调与补跑，不建议前端长等待）"""
    # P0-5 门禁：整集生产依赖文本分析模型，未配置直接阻断（不再「跑一半才 401」）
    _gate = _ai_gate_or_400("episode")
    if _gate is not None:
        return _gate
    data = request.json or {}
    # 口径统一（F-01 收口）：run-once / review 此前用 _safe_project，会把越界/空/控制字符
    # 项目名静默收敛成合法键，与全仓 46 处 _project_or_400 不一致；现改走同一入口。
    project, err = _project_or_400(
        data.get('project') or data.get('project_name') or '', field_name="project")
    if err is not None:
        return err
    try:
        ep = int(data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    plan = autopilot.get_plan(project)
    meta = autopilot._novel_meta(project, plan)
    if not meta:
        return jsonify({"success": False,
                        "error": "该项目未关联小说，无法生产（请先在小说库上传建项目）"}), 400
    chapters, novel_text = autopilot.chapters_and_text(meta)
    # 集号 ≠ 章号（超长章会拆成多集）→ 必须走单元表反查，不能按章号找
    unit = autopilot.find_episode_unit(chapters, plan, ep, novel_text)
    chapter = (unit or {}).get("chapter")
    if not chapter:
        _n_units = len(autopilot.episode_units(chapters, plan, novel_text))
        return jsonify({"success": False,
                        "error": f"该小说没有第{ep}集（共 {len(chapters)} 章 / {_n_units} 集）"}), 400
    cfg = pipeline.normalize_config({**plan, 'novel_id': meta.get('novel_id')},
                                    default_project_key=project)
    # P1-5：run-once 是同步阻塞执行，此前不接 progress_cb → 前端 `current` 状态全程不更新，
    # UI 只能看到「执行中」而看不到「当前在哪一步 / 百分之几 / 卡在重试」，用户干等 40 分钟无反馈。
    # 这里把进度实时写进 autopilot 的 current 状态，`/api/autopilot/status` 轮询即可拿到实时进度。
    _seen: list = []

    def _cb(message, percent, phase=None):
        try:
            import pipeline as _pl
            base = str(phase or "").split(":")[0]
            if base in _pl.STEP_SEQUENCE and base not in _seen:
                idx = _pl.STEP_SEQUENCE.index(base)
                _seen.extend(_pl.STEP_SEQUENCE[:idx])
                _seen.append(base)
            autopilot._set_current(project=project, episode=ep,
                                   title=chapter.get('title') or f"第{ep}章",
                                   step=base or "running", message=message,
                                   percent=int(percent or 0),
                                   steps_done=list(dict.fromkeys(_seen)),
                                   phase=phase or "start", started_at=autopilot._now())
        except Exception as e:  # noqa: BLE001  进度上报失败不得阻断生产
            app.logger.warning("run-once 进度上报失败：%s", e)

    try:
        result = pipeline.run_episode(cfg, project, ep, meta, chapter, progress_cb=_cb)
    finally:
        try:
            autopilot._clear_current()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("run-once 清理 current 失败：%s", e)
    if result.get('status') == 'busy':
        # B-02 P0-5：该集正被另一执行体（托管轮转）生产，集级锁拒绝双跑
        return jsonify({"success": False,
                        "error": result.get('error') or "该集正在生产中",
                        "retry_after_sec": 30}), 409
    if result.get('ok') and result.get('deliverable'):
        pipeline.record_deliverable(project, ep, result['deliverable'], meta={
            'title': chapter.get('title') or '', 'chapter_index': chapter.get('index'),
            'elapsed_sec': result.get('elapsed_sec')})
    return jsonify({"success": bool(result.get('ok')), "result": result})


@app.route('/api/autopilot/plan-from-settings/<project_name>', methods=['POST'])
@_autopilot_guard
def api_autopilot_plan_from_settings(project_name):
    """总控 AI 一键设定：把对话敲定的创作设定（风格/画风/镜头数/节奏…）落成托管计划

    这是「对话 → 自动生产」的接缝：用户在总控 AI 里谈好风格后点一下，
    风格纲要写进流水线配置，之后每集剧本生成都会带上它，无需再手工填表。
    """
    project = _safe_project(project_name)
    view = ai_chat.settings_view(AI_SETTINGS_PATH, project)
    brief = (view.get('style_brief') or '').strip()
    s = view.get('settings') or {}
    patch = {}
    if brief:
        patch['style'] = brief
    # 单集镜头数：从设定里解析数字（如 "12 个" → 12）
    shots = str(s.get('shots_per_episode') or '')
    digits = ''.join(ch for ch in shots if ch.isdigit())
    if digits:
        try:
            patch['target_shots'] = max(4, min(int(digits), 40))
        except ValueError as e:
            app.logger.debug("target_shots 字段解析失败（忽略）：%s", e)
    if not patch:
        return jsonify({"success": False,
                        "error": "总控 AI 还没有生效设定；请先在「总控 AI 对话」里谈好风格再一键设定"}), 400
    plan = autopilot.set_plan(project, patch)
    return jsonify({"success": True, "project": project, "plan": plan,
                    "applied": patch, "settings_filled": view.get('filled'),
                    "settings_missing": view.get('missing'),
                    "style_brief": brief})


# ==================== 全自动生产主控路由 ====================

@app.route('/api/autonomous/start', methods=['POST'])
@_autopilot_guard
def api_autonomous_start():
    """一键启动全自动生产：上传小说后，AI对话定风格，然后一键启动24h自动生产"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or '')
    novel_id = str(data.get('novel_id') or '').strip()
    plan_overrides = {k: v for k, v in data.items()
                      if k in ('style', 'target_shots', 'video_mode', 'enable_assets',
                               'enable_keyframe', 'enable_video', 'enable_final',
                               'enable_tts', 'enable_mix', 'step_max_retries')}

    if not novel_id and project_name:
        # 尝试从现有计划获取 novel_id
        import autopilot as _ap
        plan = _ap.get_plan(project_name)
        novel_id = plan.get('novel_id', '')

    if not novel_id:
        return jsonify({"success": False, "error": "缺少 novel_id，请先上传小说"}), 400

    _g = _style_aspect_guard(project_name, override_style=str(plan_overrides.get('style') or ''))
    if _g is not None:
        return _g

    result = autonomous.start_autonomous(project_name or novel_id, novel_id, plan_overrides)
    # D5：返回给前端的错误统一脱敏，避免把异常栈/模块名直接显示在错误框里
    if isinstance(result, dict) and result.get('error'):
        result['error'] = _friendly_error(result['error'])
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@app.route('/api/autonomous/stop', methods=['POST'])
@_autopilot_guard
def api_autonomous_stop():
    """停止全自动生产"""
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    result = autonomous.stop_autonomous(project)
    return jsonify(result)


@app.route('/api/autonomous/resume', methods=['POST'])
@_autopilot_guard
def api_autonomous_resume():
    """恢复全自动生产"""
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    result = autonomous.resume_autonomous(project)
    return jsonify(result)


@app.route('/api/autonomous/status', methods=['GET'])
@_autopilot_guard
def api_autonomous_status():
    """查询全自动生产状态"""
    project = _safe_project(request.args.get('project_name', ''))
    result = autonomous.status(project)
    return jsonify({"success": True, **result})


@app.route('/api/autonomous/chat', methods=['POST'])
@_autopilot_guard
def api_autonomous_chat():
    """AI 对话指令解析：将用户的自然语言指令转化为生产动作"""
    data = request.json or {}
    message = str(data.get('message') or '').strip()
    project = _safe_project(data.get('project_name') or '')

    if not message:
        return jsonify({"success": False, "error": "消息不能为空"}), 400

    result = autonomous.interpret_chat_command(message, project)
    return jsonify(result)


@app.route('/api/autonomous/report/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report(project_name):
    """获取生产报告"""
    project = _safe_project(project_name)
    episode_no = request.args.get('episode_no', type=int)
    result = autonomous.generate_report(project, episode_no)
    return jsonify(result)


@app.route('/api/autonomous/report/export/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_report_export(project_name):
    """导出生产报告"""
    project = _safe_project(project_name)
    fmt = request.args.get('format', 'json')
    filepath = autonomous.export_report(project, fmt)
    if not filepath:
        return jsonify({"success": False, "error": "暂无生产记录"}), 404
    return send_file(filepath, as_attachment=True,
                     download_name=os.path.basename(filepath))


@app.route('/api/autonomous/projects', methods=['GET'])
@_autopilot_guard
def api_autonomous_projects():
    """列出所有有生产记录的项目"""
    projects = autonomous.list_all_projects()
    return jsonify({"success": True, "projects": projects})


@app.route('/api/autonomous/deliverables/<project_name>', methods=['GET'])
@_autopilot_guard
def api_autonomous_deliverables(project_name):
    """获取项目的交付物列表"""
    project = _safe_project(project_name)
    deliverables = autonomous.get_deliverables(project)
    return jsonify({"success": True, "project": project, "deliverables": deliverables})


# ===================== 角色管理API =====================

@app.route('/api/characters', methods=['GET'])
@_autopilot_guard
def api_list_characters():
    """列出项目所有角色"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    characters = mgr.get_all_characters()
    return jsonify({"success": True, "characters": characters})


@app.route('/api/characters', methods=['POST'])
@_autopilot_guard
def api_add_character():
    """添加新角色"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    name = data.get('name', '')
    role = data.get('role', '配角')
    description = data.get('description', '')
    outfit = data.get('outfit', '')

    if not name:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    char_id = mgr.add_character(name, role, description, outfit)

    return jsonify({"success": True, "character_id": char_id, "name": name})


@app.route('/api/characters/<char_id>', methods=['PUT'])
@_autopilot_guard
def api_update_character(char_id):
    """更新角色信息"""
    data = request.get_json(silent=True) or {}
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(data.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    mgr.update_character(
        char_id,
        name=data.get('name'),
        role=data.get('role'),
        description=data.get('description'),
        outfit=data.get('outfit'),
        status=data.get('status')
    )
    return jsonify({"success": True})


@app.route('/api/characters/<char_id>/reference', methods=['POST'])
@_autopilot_guard
def api_upload_character_reference(char_id):
    """上传角色参考图"""
    project_name = request.form.get('project')
    view_type = request.form.get('view_type', 'front')
    file = request.files.get('image')

    if not file:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)

    # 保存上传的文件
    upload_dir = os.path.join(project_dir, "characters", "references")
    os.makedirs(upload_dir, exist_ok=True)
    ext = os.path.splitext(file.filename)[1]
    filename = f"{char_id}_{view_type}{ext}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    mgr.set_reference(char_id, view_type, filepath)
    return jsonify({"success": True, "path": filepath})


@app.route('/api/characters/<char_id>/prompt', methods=['GET'])
@_autopilot_guard
def api_get_character_prompt(char_id):
    """获取角色生成提示词"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err
    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    mgr = CharacterManager(project_name, project_dir)
    prompt = mgr.get_character_prompt(char_id)
    return jsonify({"success": True, "prompt": prompt})


# ===================== 九宫格分镜API =====================

@app.route('/api/storyboard/nine-grid', methods=['POST'])
@_autopilot_guard
def api_generate_nine_grid():
    """生成九宫格分镜"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    scene_description = data.get('scene_description', '')
    character_ids = data.get('character_ids', [])
    emotion = data.get('emotion', 'neutral')

    if not scene_description:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    sb = NineGridStoryboard(project_name, project_dir)
    nine_grid = sb.generate_nine_grid(scene_description, character_ids, emotion)

    # 关键：必须把 grid_id 返回给前端，且落盘文件名要与 grid_id 一致。
    # 之前 save_nine_grid() 存成 nine_grid_<时间戳>.json，却没有任何字段告诉前端这个名字，
    # 于是「选最佳构图」只能拼出 .../nine-grid/undefined/select → 404，功能等于不可用。
    grid_id = f"nine_grid_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    grid_dir = os.path.join(project_dir, "storyboards")
    os.makedirs(grid_dir, exist_ok=True)
    filepath = os.path.join(grid_dir, f"{grid_id}.json")
    nine_grid["grid_id"] = grid_id
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(nine_grid, f, ensure_ascii=False, indent=2)

    return jsonify({
        "success": True,
        "grid_id": grid_id,
        "project": project_name,
        "scene_description": scene_description,
        "created_at": nine_grid.get("created_at"),
        "filepath": filepath,
        "shots": nine_grid["shots"],
    })


@app.route('/api/storyboard/nine-grid/<grid_id>/select', methods=['POST'])
@_autopilot_guard
def api_select_nine_grid_shot(grid_id):
    """选择九宫格中的最佳镜头"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    selected_index = data.get('selected_index')

    if selected_index is None:
        return jsonify({"success": False, "error": "缺少 selected_index"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    sb = NineGridStoryboard(project_name, project_dir)
    # 加载九宫格数据
    grid_file = os.path.join(project_dir, "storyboards", f"{grid_id}.json")
    if not os.path.exists(grid_file):
        return jsonify({"success": False, "error": f"分镜文件不存在：{grid_id}"}), 404

    with open(grid_file, 'r', encoding='utf-8') as f:
        nine_grid = json.load(f)

    try:
        selected = sb.select_best_shot(nine_grid, int(selected_index))
    except (ValueError, IndexError, TypeError) as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"无效的选择：{e}"}), 400

    # 把选择结果落盘，否则「已选最佳构图」刷新后就丢了
    nine_grid["selected_index"] = int(selected_index)
    nine_grid["selected_at"] = datetime.now().isoformat()
    with open(grid_file, 'w', encoding='utf-8') as f:
        json.dump(nine_grid, f, ensure_ascii=False, indent=2)

    return jsonify({"success": True, "grid_id": grid_id, "shot": selected})


# ===================== 导出API =====================

def _timeline_for_export(project_name: str, episode_no=None,
                         provided: dict = None) -> dict:
    """把项目剧本适配成 ExportManager 需要的时间轴结构。

    为什么需要这层适配（对应「导出结果是空壳」缺陷）：
        ExportManager 期望 {project_name, duration, resolution, fps,
        sequences:[{clips:[{name,start,end}]}]}；
        而 nle_export.build_timeline 产出的是 {shots:[{start,end,video,...}], total_sec}。
        两者结构不同，前端又只发 formats 不发 timeline，
        于是 export_all({}) 写出的是 name="项目"、<clips/> 为空的无效文件，
        用户下载下来却以为导出成功。
    """
    if provided and provided.get("sequences"):
        provided.setdefault("project_name", project_name)
        return provided

    script = _load_script_for(project_name, episode_no)
    # B-17 P2-13：传集号给 build_timeline，按集号过滤视频目录
    tl = nle_export.build_timeline(script or {}, project_name, episode=episode_no)
    rows = tl.get("shots") or []
    clips = [
        {
            "name": f"shot_{int(r.get('index', i)) + 1:02d}",
            "start": r.get("start", 0),
            "end": r.get("end", 0),
            "asset_path": r.get("video") or "",
        }
        for i, r in enumerate(rows)
    ]
    return {
        "project_name": project_name,
        "duration": tl.get("total_sec", 0),
        "resolution": {
            "width": nle_export.JY_CANVAS["width"],
            "height": nle_export.JY_CANVAS["height"],
        },
        "fps": 30,
        "shots": rows,
        "sequences": [{"name": project_name, "clips": clips}],
    }


@app.route('/api/export/<project_name>', methods=['POST'])
@_autopilot_guard
def api_export_project(project_name):
    """导出项目为多种格式"""
    data = request.get_json(silent=True) or {}
    formats = data.get('formats', ['fcpml', 'edl', 'json'])
    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err
    # 前端不传 timeline（工作台 ExportTab 就只传 formats）。此处必须自己从剧本
    # 构建时间轴，否则会导出成空的占位文件。
    timeline = _timeline_for_export(project_name, data.get('episode_no'),
                                    data.get('timeline'))

    em = ExportManager(project_name, PROJECT_OUTPUT_DIR)
    exports = em.export_all(timeline)

    # 转换为前端期望的格式
    result_files = []
    for fmt in formats:
        if fmt in exports:
            filepath = exports[fmt]
            exists = os.path.isfile(filepath)
            filename = os.path.basename(filepath) if exists else f"{project_name}_{fmt}.xml"
            result_files.append({
                "format": fmt,
                "filename": filename,
                "exists": exists,
                "path": filepath
            })

    return jsonify({
        "success": True,
        "files": result_files,
        "shot_count": len(timeline.get("shots") or timeline.get("sequences", [{}])[0].get("clips", [])),
        "total_sec": timeline.get("duration", 0),
    })


@app.route('/api/export/<project_name>/<format>', methods=['GET'])
@_autopilot_guard
def api_get_export_file(project_name, format):
    """获取导出的文件"""
    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err
    export_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project_name)
    if format == 'fcpml':
        filename = f"{project_name}_fcpml.xml"
    elif format == 'edl':
        filename = f"{project_name}_edl.edl"
    elif format == 'json':
        filename = f"{project_name}_timeline.json"
    else:
        return jsonify({"success": False, "error": "不支持的格式"}), 400

    filepath = os.path.join(export_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({"success": False, "error": "文件不存在"}), 404

    return send_file(filepath, as_attachment=True)


@app.route('/api/export/current', methods=['POST'])
@_autopilot_guard
def api_export_current():
    """导出当前项目的所有格式"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project_name', '')
    formats = data.get('formats', ['fcpml', 'edl', 'json'])

    # A-01（F-01）：项目名统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project_name")
    if err is not None:
        return err

    export_dir = os.path.join(PROJECT_OUTPUT_DIR, "exports", project_name)
    os.makedirs(export_dir, exist_ok=True)

    result = {}
    for fmt in formats:
        if fmt == 'fcpml':
            filename = f"{project_name}_fcpml.xml"
        elif fmt == 'edl':
            filename = f"{project_name}_edl.edl"
        elif fmt == 'json':
            filename = f"{project_name}_timeline.json"
        else:
            continue
        filepath = os.path.join(export_dir, filename)
        if os.path.exists(filepath):
            result[fmt] = filepath

    return jsonify({"success": True, "files": result})


# ===================== 角色关系图谱API =====================

@app.route('/api/relations/graph', methods=['GET'])
@_autopilot_guard
def api_get_relation_graph():
    """获取角色关系图谱数据"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    graph = rmgr.get_graph_data()
    return jsonify({"success": True, "graph": graph})


@app.route('/api/relations', methods=['GET'])
@_autopilot_guard
def api_list_relations():
    """列出项目所有角色关系"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    relations = rmgr.get_all_relations()
    return jsonify({"success": True, "relations": relations})


@app.route('/api/relations', methods=['POST'])
@_autopilot_guard
def api_add_relation():
    """添加角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    char_a = data.get('char_a', '')
    char_b = data.get('char_b', '')
    rel_type = data.get('type', 'friend')
    strength = data.get('strength', 'medium')
    note = data.get('note', '')

    if not char_a or not char_b:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    rel_id = rmgr.add_relation(char_a, char_b, rel_type, strength, note)
    return jsonify({"success": True, "relation_id": rel_id})


@app.route('/api/relations/<rel_id>', methods=['PUT'])
@_autopilot_guard
def api_update_relation(rel_id):
    """更新角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    try:
        rmgr.update_relation(rel_id, **{k: v for k, v in data.items() if k != 'project'})
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404


@app.route('/api/relations/<rel_id>', methods=['DELETE'])
@_autopilot_guard
def api_delete_relation(rel_id):
    """删除角色关系"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    try:
        rmgr.delete_relation(rel_id)
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 404


@app.route('/api/relations/sync', methods=['POST'])
@_autopilot_guard
def api_sync_relations():
    """同步角色数据到关系库"""
    data = request.get_json(silent=True) or {}
    project_name = data.get('project')
    characters = data.get('characters', {})

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    rmgr.sync_characters(characters)
    return jsonify({"success": True})


@app.route('/api/relations/conflicts', methods=['GET'])
@_autopilot_guard
def api_check_relation_conflicts():
    """检测关系冲突"""
    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(request.args.get('project'), field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    detector = RelationConflictDetector(rmgr)
    summary = detector.get_conflict_summary()
    return jsonify({"success": True, "conflicts": summary})


@app.route('/api/relations/svg', methods=['GET'])
@_autopilot_guard
def api_export_relation_svg():
    """导出关系图谱为SVG"""
    project_name = request.args.get('project')
    width = int(request.args.get('width', 800))
    height = int(request.args.get('height', 600))

    # A-01（F-01）：统一走 _project_or_400（越界/缺失 → 400，不写盘）
    project_name, err = _project_or_400(project_name, field_name="project")
    if err is not None:
        return err

    project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
    rmgr = RelationManager(project_name, project_dir)
    svg_content = rmgr.export_svg(width, height)
    return jsonify({"success": True, "svg": svg_content})


# ==================== AI Memory API ====================

#: 质检教训库（prompt_memory）与 AI 记忆（ai_memory）是**两套数据**：
#:   - ai_memory：手动登记的经验/模式，供记忆页展示（本轮之前只有 3 条 test，是死壳）
#:   - prompt_memory：生成链路**自动沉淀**的质检教训（真正在驱动「不达标→改提示词」）
#: 记忆页此前只读前者，因此「看不到任何自动化学习成果」。下面这组接口把两者都暴露出来。


def _prompt_memory_view(kind: str = "", limit: int = 50) -> dict:
    """真实质检教训库的只读视图（供记忆页展示）"""
    try:
        m = prompt_memory.get_memory(PROJECT_OUTPUT_DIR)
        st = m.stats()
        return {
            "total": st.get("total", 0),
            "by_kind": st.get("by_kind") or {},
            "path": st.get("path", ""),
            "lessons": m.list(kind=kind, limit=limit),
        }
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取质检教训库失败：%s", e)
        return {"total": 0, "by_kind": {}, "path": "", "lessons": [], "error": str(e)}


def _prompt_memory_dead_count() -> int:
    """死教训数（use_count==0 的条数）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("dead_lessons") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _prompt_memory_used_total() -> int:
    """累计被生成链路召回次数（所有教训 use_count 之和）；读取失败返回 0。"""
    try:
        return int(prompt_memory.get_memory(PROJECT_OUTPUT_DIR).stats().get("used_total") or 0)
    except Exception:  # noqa: BLE001
        return 0


@app.route('/api/memory/lessons', methods=['GET'])
@_autopilot_guard
def api_memory_lessons():
    """质检教训库（generation 链路自动学习成果）

    query: kind（逗号分隔多值）/ project / since / until(ISO，只到日期按当天末闭区间) /
           q（关键词）/ limit（默认 50）/ offset（默认 0）/ prune_empty=1（顺手清理空记录）
    → {success, pruned, total, filtered, offset, limit, by_kind, dead_lessons, lessons[]}
    """
    kind = str(request.args.get('kind') or '')
    project = str(request.args.get('project') or '').strip()
    since = str(request.args.get('since') or '').strip()
    until = str(request.args.get('until') or '').strip()
    q = str(request.args.get('q') or '').strip()
    try:
        limit = max(0, min(500, int(request.args.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    removed = 0
    if str(request.args.get('prune_empty') or '') in ('1', 'true', 'yes'):
        try:
            removed = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).prune_empty()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("清理空教训失败：%s", e)
    try:
        page = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).query(
            kind=kind, project=project, since=since, until=until, q=q,
            limit=limit, offset=offset)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("读取质检教训库失败：%s", e)
        return jsonify({"success": False, "pruned": removed, "total": 0, "filtered": 0,
                        "offset": offset, "limit": limit, "by_kind": {}, "dead_lessons": 0,
                        "lessons": [], "error": str(e)})
    return jsonify({
        "success": True, "pruned": removed,
        "total": page["total"], "filtered": page["filtered"],
        "offset": offset, "limit": limit,
        "by_kind": page["by_kind"], "dead_lessons": page["dead_lessons"],
        "lessons": page["items"],
    })


@app.route('/api/memory/lessons/<lesson_id>', methods=['DELETE'])
@_autopilot_guard
def api_memory_lesson_delete(lesson_id):
    """删除单条教训（按确定性主键 lesson_id，"L"+sha1 前 16 位）。

    → {success, deleted, lesson_id}；未命中返回 404 {"success":false,"error":"未找到该教训"}。
    """
    lid = str(lesson_id or "").strip()
    if not lid:
        return jsonify({"success": False, "error": "缺少 lesson_id"}), 400
    try:
        ok = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).delete(lid)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("删除教训失败：%s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    if not ok:
        return jsonify({"success": False, "deleted": 0, "lesson_id": lid,
                        "error": "未找到该教训"}), 404
    return jsonify({"success": True, "deleted": 1, "lesson_id": lid})


@app.route('/api/memory/lessons/clear', methods=['POST'])
@_autopilot_guard
def api_memory_lessons_clear():
    """清空某一环节的全部教训（body {kind}；kind 为空 = 清空全部）。

    → {success, cleared, kind}。
    """
    data = request.json or {}
    kind = str(data.get("kind") or "").strip()
    try:
        cleared = prompt_memory.get_memory(PROJECT_OUTPUT_DIR).clear(kind)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("清空教训失败：%s", e)
        return jsonify({"success": False, "cleared": 0, "kind": kind,
                        "error": str(e)}), 500
    return jsonify({"success": True, "cleared": cleared, "kind": kind})


@app.route('/api/memory/lessons/search', methods=['GET'])
@_autopilot_guard
def api_memory_lessons_search():
    """按提示词召回教训建议（可视化「如果现在生成，会带上哪些历史修正」）

    query: kind（默认 storyboard）/ prompt（必填）/ project / style
    """
    kind = str(request.args.get('kind') or 'storyboard')
    prompt = str(request.args.get('prompt') or '').strip()
    project = str(request.args.get('project') or '').strip()
    style = str(request.args.get('style') or '').strip()
    if not prompt:
        return jsonify({"success": False, "error": "缺少 prompt 参数"}), 400
    try:
        hints = prompt_memory.suggest(kind=kind, prompt=prompt, project=project,
                                      root_dir=PROJECT_OUTPUT_DIR, style=style)
        learned = prompt_memory.learned_prompt(kind=kind, prompt=prompt, project=project,
                                               root_dir=PROJECT_OUTPUT_DIR, style=style)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"召回失败：{e}"}), 500
    return jsonify({"success": True, "kind": kind, "project": project, "style": style,
                    "hints": hints, "learned_prompt": learned,
                    "changed": learned != prompt})


@app.route('/api/memory/stats', methods=['GET'])
@_autopilot_guard
def api_memory_stats():
    """获取 AI 记忆统计（含**真实质检教训库**的条数，不再只报手动登记的几条）"""
    mem = get_memory_system()
    stats = mem.get_stats()
    trends = mem.evolution.analyze_trends()
    insights = mem.evolution.generate_insights(limit=5)
    lessons = _prompt_memory_view(limit=0)
    # 让前端「经验条数」反映真实学习成果，而不是 3 条测试数据
    stats = dict(stats or {})
    stats["prompt_lessons"] = lessons["total"]
    stats["prompt_lessons_by_kind"] = lessons["by_kind"]
    return jsonify({
        "success": True,
        "stats": stats,
        "trends": trends,
        "insights": insights,
        "lessons": {
            "total": lessons["total"],
            "by_kind": lessons["by_kind"],
            "dead_lessons": _prompt_memory_dead_count(),
            "used_total": _prompt_memory_used_total(),
        },
    })


@app.route('/api/memory/list', methods=['GET'])
@_autopilot_guard
def api_memory_list():
    """列出记忆"""
    mem_type = request.args.get('type')
    limit = int(request.args.get('limit', 50))
    mem = get_memory_system()
    entries = mem.list_all(mem_type=mem_type, limit=limit)
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in entries],
        "total": len(entries),
    })


@app.route('/api/memory/search', methods=['GET'])
@_autopilot_guard
def api_memory_search():
    """搜索记忆"""
    query = request.args.get('query', '')
    mem_type = request.args.get('type')
    limit = int(request.args.get('limit', 10))
    if not query:
        return jsonify({"success": False, "error": "缺少 query 参数"}), 400
    mem = get_memory_system()
    results = mem.search_by_pattern(query, mem_type=mem_type, limit=limit)
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in results],
        "total": len(results),
    })


@app.route('/api/memory/record', methods=['POST'])
@_autopilot_guard
def api_memory_record():
    """记录记忆"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    mem_type = data.get('type', 'insight')
    content = data.get('content', '')
    context = data.get('context', {})
    confidence = data.get('confidence', 1.0)
    tags = data.get('tags', [])
    source = data.get('source', 'manual')

    if not content:
        return jsonify({"success": False, "error": "缺少 content"}), 400

    mem = get_memory_system()
    entry = mem.add_memory(
        mem_type=mem_type,
        content=content,
        context=context,
        confidence=confidence,
        tags=tags,
        source=source,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})


@app.route('/api/memory/record-lesson', methods=['POST'])
@_autopilot_guard
def api_memory_record_lesson():
    """记录质检教训"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    project = data.get('project', '')
    episode = data.get('episode', 0)
    prompt = data.get('prompt', '')
    issues = data.get('issues', [])
    category = data.get('category', 'quality')

    if not project or not issues:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400

    mem = get_memory_system()
    entry = mem.record_lesson(
        project=project,
        episode=episode,
        prompt=prompt,
        issues=issues,
        category=category,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})


@app.route('/api/memory/record-success', methods=['POST'])
@_autopilot_guard
def api_memory_record_success():
    """记录成功经验"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    project = data.get('project', '')
    episode = data.get('episode', 0)
    prompt = data.get('prompt', '')
    highlights = data.get('highlights', [])
    category = data.get('category', 'quality')

    if not project or not highlights:
        return jsonify({"success": False, "error": "缺少必要参数"}), 400

    mem = get_memory_system()
    entry = mem.record_success(
        project=project,
        episode=episode,
        prompt=prompt,
        highlights=highlights,
        category=category,
    )
    return jsonify({"success": True, "memory": entry.to_dict()})


@app.route('/api/memory/optimize-prompt', methods=['POST'])
@_autopilot_guard
def api_memory_optimize_prompt():
    """基于记忆优化提示词"""
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "error": "缺少请求体"}), 400

    base_prompt = data.get('prompt', '')
    issues = data.get('issues', [])

    if not base_prompt:
        return jsonify({"success": False, "error": "缺少 prompt"}), 400

    mem = get_memory_system()
    optimized = mem.evolution.auto_optimize_prompt(base_prompt, issues)
    return jsonify({
        "success": True,
        "original": base_prompt,
        "optimized": optimized,
        "improvements": len(issues),
    })


@app.route('/api/memory/insights', methods=['GET'])
@_autopilot_guard
def api_memory_insights():
    """获取 AI 洞察和建议"""
    limit = int(request.args.get('limit', 5))
    mem = get_memory_system()
    insights = mem.evolution.generate_insights(limit=limit)
    return jsonify({
        "success": True,
        "insights": insights,
        "total": len(insights),
    })


@app.route('/api/memory/clear-old', methods=['POST'])
@_autopilot_guard
def api_memory_clear_old():
    """清理过期记忆"""
    data = request.get_json(silent=True) or {}
    days = int(data.get('days', 90))
    mem = get_memory_system()
    cleared = mem.clear_old(days=days)
    return jsonify({
        "success": True,
        "cleared": cleared,
        "days": days,
    })


@app.route('/api/memory/export', methods=['GET'])
@_autopilot_guard
def api_memory_export():
    """导出记忆数据"""
    mem_type = request.args.get('type')
    mem = get_memory_system()
    entries = mem.list_all(mem_type=mem_type) if mem_type else mem.list_all()
    return jsonify({
        "success": True,
        "memories": [e.to_dict() for e in entries],
        "total": len(entries),
        "exported_at": datetime.now().isoformat(),
    })


@app.route('/api/memory/trends', methods=['GET'])
@_autopilot_guard
def api_memory_trends():
    """获取趋势分析"""
    mem = get_memory_system()
    trends = mem.evolution.analyze_trends()
    return jsonify({
        "success": True,
        "trends": trends,
    })


if __name__ == '__main__':
    # P2-T4（F-02）：直跑分支也走同一套回环护栏（与 serve.main 共用 host_guard）。
    # 此前 `python app/app.py` 直跑完全绕过 serve.py 护栏 —— APP_HOST=0.0.0.0 时
    # 零鉴权的 debug 模式对同网段全裸暴露。现：非回环且未设 MJSCXT_ALLOW_NON_LOOPBACK
    # 时拒绝启动（fail-closed），与 serve.py 口径一致。
    from host_guard import _guard_host
    try:
        _guard_host(APP_HOST)
    except RuntimeError as e:
        app.logger.error("启动被安全护栏拦截（F-02 直跑分支）：%s", e)
        raise SystemExit(2)
    app.logger.info(f"漫剧生成系统启动: http://{APP_HOST}:{APP_PORT} (debug={APP_DEBUG})")
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG, threaded=True)


# ===================== 总控 AI 自主执行（function-calling agent） =====================
#
# 与 /api/ai/chat 的区别：
#   /api/ai/chat      —— 只聊天 + 抽取创作设定，不执行任何生产动作
#   /api/agent/chat   —— 由模型自己决定调用哪些工具，直接把活干完（无人确认）
#
# 安全不靠弹窗，靠 agent_core 里的自动护栏：工具白名单 / 昂贵动作配额 /
# 步数上限 / 同参数冷却 / 全局急停 / 单项目互斥 / 审计日志。

@app.route('/api/agent/tools', methods=['GET'])
def api_agent_tools():
    """列出总控 AI 可调用的工具与当前护栏状态（供前端展示能力边界）"""
    return jsonify({
        "success": True,
        "count": len(agent_core.TOOLS),
        "tools": agent_core.tool_index(),
        "guards": {
            "max_steps": agent_core.MAX_STEPS,
            "max_expensive": agent_core.MAX_EXPENSIVE,
            "max_turn_sec": agent_core.MAX_TURN_SEC,
            "cooldown_sec": agent_core.COOLDOWN_SEC,
        },
        "kill": agent_core.kill_state(),
    })


@app.route('/api/agent/kill', methods=['GET', 'POST'])
def api_agent_kill():
    """急停开关：一键中止所有正在跑的总控动作（无人值守时的刹车）"""
    if request.method == 'GET':
        return jsonify({"success": True, "kill": agent_core.kill_state()})
    data = request.json or {}
    on = bool(data.get("on", True))
    reason = str(data.get("reason") or "").strip() or ("手动急停" if on else "")
    return jsonify({"success": True, "kill": agent_core.set_kill(on, reason)})


@app.route('/api/agent/chat', methods=['POST'])
def api_agent_chat():
    """下发一条自然语言指令，总控 AI 自主决策并执行（异步任务，返回 job_id 供轮询）"""
    # P0-5 门禁：总控对话模型未配置 → 闸在门口，避免 issue 落库后才发现跑不动
    _gate = _ai_gate_or_400("chat")
    if _gate is not None:
        return _gate
    data = request.json or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "message 不能为空"}), 400
    if len(message) > ai_chat.MAX_CHARS_PER_MESSAGE:
        message = message[:ai_chat.MAX_CHARS_PER_MESSAGE]

    history = ai_chat.load_history(AI_CHAT_HISTORY_PATH)
    # ⚠️ _chat_project 只认 project_name，而本接口/前端传的是 project。
    # 不转换的话总控会静默落到「上一个活跃项目」上，把 A 项目的指令干到 B 项目头上。
    _explicit = (data.get("project_name") or data.get("project") or "").strip()
    project = _chat_project({"project_name": _explicit} if _explicit else (data or {}), history)

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    ep = ai_config.get_module(cfg, "chat")
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return jsonify({
            "success": False,
            "error": "「对话总控模型」尚未配置（base_url / api_key / model），总控无法自主执行",
            "guide": LLM_NOT_CONFIGURED_GUIDE_MAP["chat"],
            "need_config": True,
            "state": _chat_state(project),
        }), 400

    history["active_project"] = project
    ai_chat.append_message(history, "user", message, project)
    ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)

    started = agent_core.start_job(
        message=message,
        project=project,
        ep=ep,
        history=ai_chat.project_messages(history, project)[:-1],
        timeout=LLM_REQUEST_TIMEOUT,
    )
    if not started.get("ok"):
        ai_chat.drop_last_message(history, project)
        ai_chat.save_history(AI_CHAT_HISTORY_PATH, history)
        return jsonify({"success": False, "error": started.get("error"),
                        "state": _chat_state(project)}), 409
    return jsonify({"success": True, "job_id": started["job_id"], "project": project,
                    "state": _chat_state(project)})


@app.route('/api/agent/job/<job_id>', methods=['GET'])
def api_agent_job(job_id):
    """轮询总控任务进度（steps 逐条追加，status: running/done/failed/killed/timeout）"""
    job = agent_core.get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": f"未找到任务 {job_id}"}), 404
    # P1-1：最终回复已在 agent 线程内即时落盘（agent_core._finish → _persist_agent_reply）；
    # 这里仅作幂等兜底——线程内失败/尚未完成落盘时由 persist_job_reply 补写（共用同一份
    # 认领/去重逻辑，保证回复只追加一次，不重复不丢失）。前端刷新/离开不再导致回复丢失。
    if job.get("status") in ("done", "failed", "killed", "timeout") and job.get("reply"):
        agent_core.persist_job_reply(job_id)
    return jsonify({"success": True, **{k: v for k, v in job.items() if not k.startswith("_")}})


@app.route('/api/agent/log', methods=['GET'])
def api_agent_log():
    """查看总控 AI 的审计日志（干了什么、成功没有、花了多久）"""
    try:
        limit = max(1, min(int(request.args.get("limit") or 100), 1000))
    except (TypeError, ValueError):
        limit = 100
    path = os.path.join(agent_core.AUDIT_DIR,
                        f"audit-{datetime.now().strftime('%Y%m%d')}.jsonl")
    items = []
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
            for ln in lines:
                try:
                    items.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        except Exception as e:  # noqa: BLE001
            return jsonify({"success": False, "error": f"读取审计日志失败：{e}"}), 500
    return jsonify({"success": True, "count": len(items), "items": items})


# ===== SPA 路由（必须在所有 API 路由之后，Flask 默认静态路由之前）=====
# 显式注册每个 SPA 页面，避免与 Flask 默认 /static 路由冲突
_SPA_PAGES = ['projects', 'upload', 'auto', 'memory', 'characters', 'grid', 'deliver', 'export', 'settings']

for _page in _SPA_PAGES:
    _endpoint = f'spa_{_page}'
    def _make_spa_page(_p=_page, _ep=_endpoint):
        @app.route(f'/{_p}', endpoint=_ep)
        def _spa_page():
            return send_from_directory(_STATIC_DIR, 'index.html')
        return _spa_page
    _make_spa_page()

# 通用回退：任意未匹配路径返回 index.html（用于 SPA 客户端路由）
@app.route('/<path:path>')
def spa_fallback(path):
    """SPA 路由回退：非 API、非静态文件请求返回 Vite index.html"""
    # 放行 API 前缀
    if path.startswith('api/'):
        abort(404)
    # 若 static 目录下确实存在该文件（favicon.svg / robots.txt 等），直接返回真实文件，
    # 避免被下面的「带扩展名一律 404」误伤。
    # 用 normpath + 前缀校验防目录穿越；send_from_directory 自身也会做安全校验。
    safe = os.path.normpath(os.path.join(_STATIC_DIR, path))
    if safe.startswith(os.path.abspath(_STATIC_DIR)) and os.path.isfile(safe):
        return send_from_directory(_STATIC_DIR, path)
    # 放行带扩展名的静态资源
    if '.' in path.split('/')[-1]:
        abort(404)
    return send_from_directory(_STATIC_DIR, 'index.html')


# ==========================================================================
# D-11a（P2）：ComfyUI 输出目录滚动回收 —— 任务收尾接线
# --------------------------------------------------------------------------
# 判定与删除**全部委托**零第三方依赖的 disk_reclaim 模块（可用
# `MJSCXT_AUTOPILOT=0 python verify_comfyui_reclaim.py` 离线单测）；
# 这里只做三件事：
#   ① 从 config 常量推导「正式产物目录」清单（不硬编码任何盘符路径）；
#   ② 进程内 10 分钟节流（同一进程最多每 10 分钟真正扫描一次）；
#   ③ 全容错 —— 回收是**优化**不是功能，任何异常只留 warning，绝不阻断生产。
#
# 为什么把包装函数集中在文件末尾追加：本文件已逾万行，把新逻辑集中放在末尾
# 便于审阅与回滚，也避免与既有函数体交错。**注意不要再以「绝对行号」锚定任何
# 守卫** —— 本仓库吃过亏：`verify_silent_except.py` 的白名单原本写成
# `("app.py", 5691)`，D-11a 在上面插了两行就漂到 5693、守卫静默失效，被迫手工
# 同步。该白名单现已改为**内容锚点**（`app/verify_project_audit.py` G5 同口径），
# 行号扰动不再影响它。
#
# 调用点：`_generate_asset_task`（资产生成任务收尾）、`_storyboard_worker`
# （分镜生成任务收尾）；成片步骤收尾见 `app/pipeline.py step_final`。
# ==========================================================================
_COMFYUI_RECLAIM_LAST_TS = 0.0          # 上次真正扫描的时间戳（模块级节流状态）
_COMFYUI_RECLAIM_INTERVAL_SEC = 600.0   # 同一进程 10 分钟内只真正扫描一次
_COMFYUI_RECLAIM_LOCK = threading.Lock()

# ComfyUI「任务历史」自动清理（面板只增不减 → 易被误读成「生成了大量废图」）：
# 与上面的回收同构 —— 模块级节流 + 全容错，默认间隔取 config 值（5 分钟）。
_COMFYUI_CLEAR_HISTORY_LAST_TS = 0.0
_COMFYUI_CLEAR_HISTORY_LOCK = threading.Lock()


def _maybe_clear_comfyui_history(where: str = "") -> bool:
    """按节流清空 ComfyUI **任务历史列表**（不是磁盘产物）。

    为什么要做：ComfyUI 界面「任务历史」面板只增不减，质检每失败一次重跑就多一条
    记录，跑几轮后几百条 → 用户会以为「生成了大量废图」。实测面板 162 条时磁盘上
    真正残留的废弃分镜图 **0 张**（清之前 /history 162 条 → 清完 0 条）。

    语义边界（重要）：
      · 只调 `POST /history {"clear":true}`，**绝不删任何 output 文件**；
      · 不影响正在执行/排队中的任务（它们结束后会各自追加新记录）；
      · 只应在**任务收尾**调用 —— 有任务在飞时清掉历史，会让 `wait_for_completion`
        的轮询查不到自己那条记录而误判超时。

    开关 `MJSCXT_CLEAR_COMFYUI_HISTORY=0` 可整体关闭；节流 5 分钟（见 config）。
    永不抛异常、永不阻断生产。
    """
    global _COMFYUI_CLEAR_HISTORY_LAST_TS
    if not CLEAR_COMFYUI_HISTORY:
        return False
    try:
        now = time.time()
        with _COMFYUI_CLEAR_HISTORY_LOCK:
            if now - _COMFYUI_CLEAR_HISTORY_LAST_TS < CLEAR_COMFYUI_HISTORY_INTERVAL_SEC:
                return False
            # 先占用时间戳：真正清理失败也不要在同一分钟内反复重试刷屏。
            _COMFYUI_CLEAR_HISTORY_LAST_TS = now
        ok = comfyui_client.clear_history()
        if ok:
            app.logger.info("[任务历史] 已清空 ComfyUI 任务历史面板（收尾：%s）", where or "未知")
        return ok
    except Exception as e:  # noqa: BLE001  可观测性优化，绝不能阻断生产
        app.logger.warning("ComfyUI 任务历史清理异常（不影响生产）：%s: %s",
                           type(e).__name__, e)
        return False



def _comfyui_official_dirs() -> list:
    """正式产物目录清单（全部由 config 常量推导，不硬编码盘符路径）。"""
    return [d for d in (CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR,
                        KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR) if d]


def _maybe_reclaim_comfyui_output() -> None:
    """薄包装：带节流地回收 ``COMFYUI_OUTPUT_DIR`` 下的产物残留（D-11a）。

    只有**同时**满足下列条件的文件才会被删（判定细节与安全论证见
    ``app/disk_reclaim.py`` 模块 docstring）：

      ① 位于 ``COMFYUI_OUTPUT_DIR`` 下的 ``comic_drama*`` 产物目录内；
      ② 文件名是 ComfyUI 侧产物命名（带自动编号后缀 ``_00001_``，或含
         ``_retry``/``_try``）—— 交付件名 ``base.png`` 之类天然不匹配；
      ③ ``mtime`` 距今 > 24h；
      ④ 正式产物目录里已有 ``(size, sha256)`` **双匹配**的同内容副本；
      ⑤ ``st_nlink == 1``（硬链接删了不释放空间）；
      ⑥ 候选不在任何正式产物目录内（含大小写归一后的比较）。

    性能：内容指纹按 ``(路径, size, mtime_ns)`` 进程内缓存，稳态下只有**新增**的
    正式产物需要读盘；配合 10 分钟节流，同步调用不会给任务收尾带来可感知的延迟。
    """
    global _COMFYUI_RECLAIM_LAST_TS
    try:
        now = time.time()
        with _COMFYUI_RECLAIM_LOCK:
            if now - _COMFYUI_RECLAIM_LAST_TS < _COMFYUI_RECLAIM_INTERVAL_SEC:
                return
            _COMFYUI_RECLAIM_LAST_TS = now
        if not COMFYUI_OUTPUT_DIR or not os.path.isdir(COMFYUI_OUTPUT_DIR):
            return
        import disk_reclaim   # 延迟导入：与本文件其它叶子模块一致，避免加载期副作用
        stats = disk_reclaim.reclaim_comfyui_output(
            COMFYUI_OUTPUT_DIR, _comfyui_official_dirs(), logger=app.logger)
        if stats.get("delete"):
            app.logger.info("D-11a ComfyUI 输出回收：删 %d 个产物残留，释放 %.2f MB",
                            len(stats["delete"]),
                            (stats.get("removed_bytes") or 0) / 1048576.0)
    except Exception as e:  # noqa: BLE001  回收是优化，绝不能阻断生产
        app.logger.warning("D-11a ComfyUI 输出回收失败（不影响生产）：%s: %s",
                           type(e).__name__, e)
