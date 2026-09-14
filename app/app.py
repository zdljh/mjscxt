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
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, abort, redirect
from flask_cors import CORS

from config import (
    COMFYUI_URL, PROJECT_ROOT_DIR, PROJECT_OUTPUT_DIR, SCRIPT_DIR,
    CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR, KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR,
    NOVELS_DIR, LLM_CONFIG_PATH, NOVEL_CHUNK_CHARS, NOVEL_MAX_CHUNKS,
    NOVEL_DEFAULT_SHOTS, NOVEL_PREVIEW_CHARS, LLM_REQUEST_TIMEOUT,
    QC_CONFIG_PATH, QC_DIR,
    WATERMARK_CONFIG_PATH, WATERMARK_DIR,
    AI_CONFIG_PATH, AI_MODULES, AI_CHAT_HISTORY_PATH, AI_SETTINGS_PATH,
    UPSCALE_DIR, UPSCALE_DEFAULT_PARAMS, COMFYUI_OUTPUT_DIR,
    TE_UPSCALE_DEFAULT_PARAMS, TE_UPSCALE_LOWVRAM_PARAMS, UPSCALE_ENGINE,
    DUB_DIR, TTS_DEFAULT_PARAMS, H3_STRIP_AUDIO,
    DUB_MIX_DIR, MIX_DEFAULT_PARAMS, CONTINUITY_DIR,
    TASKS_DB_PATH, TASK_QUEUE_CONCURRENCY, TASK_UNIT_MIN_BYTES
)
from script_generator import ScriptGenerator
from comfyui_client import ComfyUIClient
from video_postprocess import VideoPostProcessor, ensure_no_audio
from novel_parser import (
    SUPPORTED_EXTS, NovelParseError, ingest_novel, list_novels,
    get_novel, preview_novel, read_novel_text, split_chapters
)
from llm_client import (
    LLMClient, LLMError, load_config as load_llm_config,
    save_config as save_llm_config, clear_config as clear_llm_config,
    public_view as llm_public_view
)
import ai_config
import ai_chat
import novel_to_script
import analytics
import autopilot
import consistency
import continuity
import coverage
import keyframe
import nle_export
import pipeline
import plugin_registry
import project_store
import providers
import qc_client
import task_store
import video_watermark
import watermark_cleanup
import upscale_client
import tts_client
import dub_mix
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

app = Flask(__name__)
CORS(app)

# 服务配置（可通过环境变量覆盖：APP_HOST / APP_PORT / APP_DEBUG）
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "5000"))
APP_DEBUG = os.getenv("APP_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# 模型自绘文字/水印清理：工作流本身无水印节点，标识由模型权重绘制（提示词无法可靠压制），
# 故在产物落盘前对固定画面区域（默认右下角比例区）做 ffmpeg delogo 插值修复。
WATERMARK_CLEANUP_ENABLED = os.getenv("WATERMARK_CLEANUP", "1").lower() not in ("0", "false", "no", "off")
WATERMARK_CLEANUP_BACKUP_DIR = os.path.join(QC_DIR, "_watermark_backup")

# 全局状态
generation_state = {}
lock = threading.Lock()

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

@app.route('/api/projects', methods=['GET'])
def api_projects_list():
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
    rec = project_store.create_project(
        name,
        novel_id=data.get('novel_id') or '',
        novel_name=data.get('novel_name') or name,
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

    scripts = [project_store.script_stats(sp) for sp in project_store.project_scripts(key)]
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
                     "prompt": shot_meta.get("prompt_h3") or manifest.get("prompt") or "",
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
                                   "assets": [{"name": name,
                                               "reference_prompt_zh": meta.get("reference_prompt_zh") or "",
                                               "reference_prompt_en": meta.get("reference_prompt_en") or "",
                                               "appearance": meta.get("appearance") or ""}]}},
        "downloads": [v["url"] for v in views],
    })


# ===== 页面 =====

@app.route('/')
def index():
    return render_template('index.html')


# ===== 状态 =====

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
        "task_queue": task_queue.status(),
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
                    "queue": task_queue.status()})


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

def _shot_num_key(key) -> str:
    """把各种镜头标识统一成纯数字字符串：'shot_01' / 2 / '2' / 'S001' → '1' / '2'

    注意与 _norm_shot_key 的区别：后者只处理纯数字字符串，无法把
    文件名形式（shot_01）与剧本 shot_id（1）对齐，一致性采集器必须用本函数。
    """
    m = re.search(r"\d+", str(key))
    if m:
        try:
            return str(int(m.group()))
        except ValueError:
            return m.group()
    return str(key).strip().lower()


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
    """按项目名（+可选集号）读取剧本；缺集号时取该项目第一集"""
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
    return script or {}


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
    """
    def _scan(root: str) -> list:
        out = []
        base = os.path.join(root, _safe_project(project))
        if not os.path.isdir(base):
            return out
        for name in sorted(os.listdir(base)):
            d = os.path.join(base, name)
            if not os.path.isdir(d):
                continue
            ref = ""
            for cand in ("front.png", "base.png", "front.jpg", "base.jpg"):
                p = os.path.join(d, cand)
                if os.path.isfile(p):
                    ref = p
                    break
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
    project = _safe_project(request.args.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    episode_no = request.args.get('episode_no')
    script = _load_script_for(project, episode_no)
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "该剧本没有镜头数据",
                        "project": project}), 404
    kf_dir = _keyframes_dir(project, episode_no)
    sb_map = _keyframe_sb_map(project, script, episode_no=episode_no)
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir,
                                  only_missing=(request.args.get('only_missing', '1') != '0'))
    return jsonify({"success": True, "project": project,
                    "shot_count": len(shots),
                    "keyframes_dir": kf_dir,
                    "start_frames_ready": sum(1 for p in plan if p["has_start"]),
                    "end_frames_ready": sum(1 for p in plan if p["has_end"]),
                    "to_generate": sum(1 for p in plan if p["need_gen"]),
                    "plan": plan})


@app.route('/api/keyframes/generate', methods=['POST'])
def api_keyframes_generate():
    """批量生成尾帧（Qwen Edit，以分镜图为首帧参考）——后台任务 + 断点续跑"""
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    script = _load_script_for(project, data.get('episode_no')) if not data.get('shots') \
        else {"shots": data.get('shots') or []}
    shots = script.get("shots") or []
    if not shots:
        return jsonify({"success": False, "error": "没有镜头数据"}), 400
    _kf_ep = _ep_of_script(script, data.get('episode_no'))
    kf_dir = _keyframes_dir(project, _kf_ep)
    sb_map = _keyframe_sb_map(project, script, data.get('storyboards'), episode_no=_kf_ep)
    only_missing = bool(data.get('only_missing', True))
    seed = data.get('seed')
    timeout = int(data.get('timeout') or 900)

    task_id = f"keyframe_{project}_{int(time.time())}"
    plan = keyframe.plan_keyframes(shots, sb_map, kf_dir, only_missing=only_missing)
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
            except Exception:  # noqa: BLE001
                pass

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
                except Exception:  # noqa: BLE001
                    pass

        try:
            report = keyframe.generate_keyframes(
                shots, sb_map, kf_dir, seed=seed, timeout=timeout,
                only_missing=only_missing, progress_cb=_progress)
            ok = int(report.get("succeeded") or 0)
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

    th = threading.Thread(target=_kf_worker, daemon=True)
    th.start()
    return jsonify({"success": True, "task_id": task_id, "status": "started",
                    "total": len([p for p in plan if p["need_gen"]]),
                    "keyframes_dir": kf_dir})


@app.route('/api/keyframes/file/<path:filename>')
def api_keyframes_file(filename):
    """关键帧图片访问：/api/keyframes/file/<项目>/shot_01_end.png"""
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(KEYFRAMES_DIR, safe_path)
    if os.path.isfile(filepath):
        return send_file(filepath)
    abort(404)


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
        idx = {str(s.get("shot_id")): i for i, s in enumerate(shots)}
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
    project = _safe_project(data.get('project_name') or '')
    order = data.get('order') or []
    if not project or not order:
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


@app.route('/api/storyboard/retry-shot', methods=['POST'])
def api_storyboard_retry_shot():
    """单镜分镜图重跑（同步返回；只影响该镜，不触碰其它镜头产物）

    body: {project_name, shot: {...}, seed?, episode_no?}
    未传 shot 时按 shot_id 从剧本取。
    """
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
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
    if not refs:
        return jsonify({"success": False,
                        "error": "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"}), 400
    labels = [r[1] for r in refs]
    prompt = comfyui_client.build_storyboard_prompt(shot, labels)
    seed = data.get('seed')
    try:
        result = comfyui_client.generate_storyboard(
            prompt_zh=prompt, ref_images=[r[2] for r in refs],
            filename_prefix=f"comic_drama_sb/{project}_shot_{seq:02d}_retry",
            seed=seed)
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "error": f"分镜图重跑失败：{e}"}), 500
    files = (result or {}).get("files") or []
    if not files:
        return jsonify({"success": False, "error": "ComfyUI 未返回分镜图"}), 500

    # 质检（若已开启）：不达标同样阻断入库（与批量链路一致）
    qc_cfg = _qc_load_cfg()
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
    if WATERMARK_CLEANUP_ENABLED:
        try:
            watermark_cleanup.clean_image(files[0], backup_dir=os.path.join(
                WATERMARK_CLEANUP_BACKUP_DIR, project))
        except Exception as e:  # noqa: BLE001
            app.logger.warning(f"单镜重跑去水印未生效：{e}")
    shutil.copy2(files[0], scratch)
    if qc_on:
        verdict = qc_client.check_image(scratch, _qc_shot_desc(shot), qc_cfg)
        gate = _qc_gate(verdict)
        _qc_record_verdict(project, "image", shot_id, "单镜重跑质检",
                           1, seed, scratch, verdict)
    if qc_on and not (gate or {}).get("accept"):
        return jsonify({"success": False, "qc_blocked": True,
                        "error": f"分镜图质检阻断（{(gate or {}).get('label')}）："
                                 f"{(gate or {}).get('reason')}；未写入正式目录",
                        "verdict": verdict, "scratch": scratch}), 200
    shutil.copy2(scratch, dst)
    # 同步更新 manifest 中该镜条目
    _update_storyboard_manifest_shot(project, shot_id, seq, dst, prompt, refs, verdict, gate,
                                     episode_no=_ep)
    _sub = f"ep{int(_ep):02d}/" if _ep and int(_ep) > 1 else ""
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "path": dst,
                    "url": f"/api/storyboards/file/{project}/{_sub}shot_{seq:02d}.png",
                    "prompt": prompt, "ref_count": len(refs),
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
        try:
            with open(mpath, "r", encoding="utf-8") as f:
                manifest = json.load(f) or {}
        except Exception:  # noqa: BLE001
            manifest = {}
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
        with open(mpath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"分镜 manifest 更新失败：{e}")


@app.route('/api/video/retry-shot', methods=['POST'])
def api_video_retry_shot():
    """单镜视频重跑（同步；只重生成该镜的 mp4）

    支持 mode：reference（默认，分镜图+主角锚点）/ keyframe（首尾帧插值）
    """
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
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
            prompt = shot.get('prompt_h3') or comfyui_client._build_h3_prompt(
                shot, char_refs, scene_refs)
        try:
            dur = float(shot.get('duration') or 5)
        except (TypeError, ValueError):
            dur = 5.0
        seg = {"prompt": prompt, "duration": dur, "reference_images": refs,
               "name": f"shot_{seq:02d}"}

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
    return jsonify({"success": True, "project": project, "shot_id": shot_id, "seq": seq,
                    "mode": mode, "path": dst,
                    "url": f"/api/videos/{project}/{_rs_sub}{os.path.basename(dst)}",
                    "ref_count": len(seg["reference_images"]), "duration": seg["duration"]})


# ==========================================================================
# P2-1 / P2-2  NLE 导出（剪映草稿 / FCPXML / SRT / 帧序列）
# ==========================================================================

@app.route('/api/export/run', methods=['POST'])
def api_export_run():
    """一键导出：剪映草稿 + FCPXML + SRT + 帧序列清单

    body: {project_name, episode_no?, formats?: ["jianying","fcpxml","srt","frames"]}
    """
    data = request.json or {}
    project = _safe_project(data.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project_name"}), 400
    script = _load_script_for(project, data.get('episode_no'))
    if not script:
        return jsonify({"success": False, "error": "剧本不存在"}), 404
    formats = data.get('formats')
    try:
        results = nle_export.export_all(project, script,
                                        formats=formats if isinstance(formats, list) else None)
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
    return jsonify({"success": True, "count": len(items), "items": items})


@app.route('/api/export/download/<path:filename>')
def api_export_download(filename):
    """导出产物下载（限导出根目录内）"""
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    root = nle_export.EXPORT_DIR
    filepath = os.path.join(root, safe_path)
    if os.path.isfile(filepath):
        return send_file(filepath, as_attachment=True,
                         download_name=os.path.basename(filepath))
    abort(404)


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
    data = request.json
    theme = data.get('theme', '')
    episodes = data.get('episodes', 1)
    duration = data.get('duration', 60)
    style = data.get('style', '古风仙侠')
    style = _apply_project_settings(style, _safe_project(data.get('project_name') or (theme or '')[:20] or 'project'))

    if not theme:
        return jsonify({"error": "请提供主题"}), 400

    try:
        script = script_gen.generate_script(
            theme=theme, episodes=episodes,
            duration_per_episode=duration, style=style
        )
        project_name = _safe_project(theme[:20])
        script_path = script_gen.save_script(script, project_name)
        script["metadata"]["script_path"] = script_path

        return jsonify({
            "success": True,
            "script_path": script_path,
            "script": script,
            "project_name": project_name,
            "characters_count": len(script.get("characters", [])),
            "items_count": len(script.get("items", [])),
            "scenes_count": len(script.get("scenes", [])),
            "shots_count": len(script.get("shots", []))
        })
    except Exception as e:
        app.logger.error(f"生成剧本失败: {e}")
        return jsonify({"error": str(e)}), 500


# ===== 步骤2/3/4：资产生成（角色/物品/场景 + 多视角） =====

def _generate_asset_task(task_id: str, assets: list, asset_type: str, project_name: str):
    """后台资产生成任务：基础图 + 多视角图

    P0 修复（④⑤）：全链路接入 AI 质检——基础图与每一张多视角图都必须送检；
    不达标自动重生成（换 seed），重试仍不达标 / 质检调用异常 → 阻断入库并标记 qc_blocked。
    """
    try:
        base_dir = {"character": CHARACTERS_DIR, "item": ITEMS_DIR, "scene": SCENES_DIR}[asset_type]
        gen_base = {
            "character": comfyui_client.generate_character_base,
            "item": comfyui_client.generate_item_base,
            "scene": comfyui_client.generate_scene_base,
        }[asset_type]

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
            qc_desc = f"资产类型：{asset_type}；资产名称：{name}；资产设定：{str(prompt_zh)[:400]}"

            # ---------- 阶段1：基础图（生成 → 质检 → 重生成 → 阻断判定） ----------
            base_dst = os.path.join(asset_dir, "base.png")
            base_attempts = []
            base_gate = None
            base_ok = False
            base_files = []
            seed = None
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    seed = random.randint(1, 2 ** 31 - 1)
                    _set_phase(f"{name} 基础图质检不达标，重新生成（第 {attempt}/{max_retries} 次）",
                               "regenerating")
                base_files = gen_base(prompt_zh, seed=seed)
                if not base_files:
                    base_attempts.append({"attempt": attempt + 1, "seed": seed, "stage": "基础图生成",
                                          "ok": False, "error": "基础图生成失败"})
                    base_gate = {"accept": False, "blocked": True, "skipped": False,
                                 "label": "生成失败", "reason": "基础图生成失败", "critical_issues": []}
                    break
                scratch_base = os.path.join(scratch_dir, f"base_try{attempt + 1}.png")
                shutil.copy2(base_files[0], scratch_base)
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
                verdict = qc_client.check_image(scratch_base, qc_desc, qc_cfg)
                app.logger.info(f"[资产质检] base {asset_type}/{name} 第{attempt + 1}次 → "
                                f"{verdict.get('call_url')} model={verdict.get('model')} "
                                f"ok={verdict.get('ok')} passed={verdict.get('passed')} "
                                f"score={verdict.get('score')} latency={verdict.get('latency_ms')}ms")
                base_attempts.append(_qc_record_verdict(project_name, "asset_image", f"{name}_base",
                                                        "资产基础图质检", attempt + 1, seed,
                                                        scratch_base, verdict))
                base_gate = _qc_gate(verdict)
                if base_gate["accept"]:
                    base_ok = True
                    break
                if not verdict.get("ok"):
                    break     # 质检接口异常，重生成无意义
            if not base_ok:
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
                shutil.copy2(base_files[0], base_dst)

            # ---------- 阶段2：多视角（逐视角质检 → 整组重生成 → 不达标阻断） ----------
            view_paths = {"base": base_dst}
            view_attempts = {}
            view_gate = {}
            view_src = {}
            views = {}
            vseed = None
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    vseed = random.randint(1, 2 ** 31 - 1)
                    _set_phase(f"{name} 多视角质检不达标，重新生成（第 {attempt}/{max_retries} 次）",
                               "regenerating")
                views = comfyui_client.generate_multiview(
                    base_image_path=base_dst, asset_type=asset_type, asset_name=name,
                    base_prompt_zh=prompt_zh, seed=vseed) or {}
                view_src = {}
                for vk, vp in views.items():
                    sp = os.path.join(scratch_dir, f"{vk}_try{attempt + 1}.png")
                    shutil.copy2(vp, sp)
                    view_src[vk] = sp
                if not views:
                    break
                if not qc_on:
                    if qc_declared:
                        # 已声明开启质检但接口不可用：明确阻断（图仅留在暂存区），不静默放行
                        for vk in views:
                            view_gate[vk] = {"accept": False, "blocked": True, "skipped": False,
                                             "label": "质检接口未就绪",
                                             "reason": "已开启图片质检但质检接口不可用"
                                                       "（qc_config.json 缺 base_url / api_key / model）",
                                             "critical_issues": []}
                        break
                    for vk in views:
                        view_gate[vk] = {"accept": True, "blocked": False, "skipped": True,
                                         "label": "质检未开启", "reason": "图片质检未开启（跳过）",
                                         "critical_issues": []}
                    break
                round_pass = True
                api_error = False
                for vk, sp in view_src.items():
                    _set_phase(f"{name} 多视角质检中（{vk} · 第 {attempt + 1} 次）", "checking")
                    verdict = qc_client.check_image(sp, qc_desc + f"；视角：{vk}", qc_cfg)
                    app.logger.info(f"[资产质检] view {asset_type}/{name}/{vk} 第{attempt + 1}次 → "
                                    f"{verdict.get('call_url')} model={verdict.get('model')} "
                                    f"ok={verdict.get('ok')} passed={verdict.get('passed')} "
                                    f"score={verdict.get('score')} latency={verdict.get('latency_ms')}ms")
                    view_attempts.setdefault(vk, []).append(
                        _qc_record_verdict(project_name, "asset_image", f"{name}_{vk}",
                                           f"资产多视角质检（{vk}）", attempt + 1, vseed, sp, verdict))
                    gate = _qc_gate(verdict)
                    view_gate[vk] = gate
                    if not gate["accept"]:
                        round_pass = False
                        if not verdict.get("ok"):
                            api_error = True
                if round_pass:
                    break
                if api_error:
                    break     # 质检接口异常，重生成无意义

            blocked_views = []
            saved_views = []
            for vk in view_src.keys():
                gate = view_gate.get(vk) or {"accept": False, "blocked": True, "skipped": False,
                                             "label": "生成失败", "reason": "多视角图生成失败",
                                             "critical_issues": []}
                if gate["accept"] and os.path.isfile(view_src[vk]):
                    view_dst = os.path.join(asset_dir, f"{vk}.png")
                    shutil.copy2(view_src[vk], view_dst)
                    view_paths[vk] = view_dst
                    saved_views.append(vk)
                else:
                    blocked_views.append({"view": vk, "label": gate["label"],
                                          "reason": gate["reason"],
                                          "critical_issues": gate["critical_issues"]})
            if not saved_views and not blocked_views:
                blocked_views.append({"view": "-", "label": "生成失败",
                                      "reason": "多视角图全部生成失败", "critical_issues": []})

            all_attempts = list(base_attempts)
            for recs in view_attempts.values():
                all_attempts.extend(recs)
            results.append({
                "name": name,
                "success": not blocked_views,
                "dir": asset_dir,
                "views": list(view_paths.keys()),
                "qc_blocked": bool(blocked_views),
                "qc_blocked_views": blocked_views,
                "error": ("多视角质检阻断：" + "、".join(f"{b['view']}（{b['label']}）"
                                                        for b in blocked_views)) if blocked_views else None,
                "qc": _qc_summary(all_attempts, qc_declared, qc_on,
                                  int(qc_cfg.get("max_retries", 0))),
                "qc_base": _qc_summary(base_attempts, qc_declared, qc_on,
                                       int(qc_cfg.get("max_retries", 0))),
                "qc_views": {vk: _qc_summary(recs, qc_declared, qc_on,
                                             int(qc_cfg.get("max_retries", 0)))
                             for vk, recs in view_attempts.items()},
            })

        blocked_count = sum(1 for r in results if r.get("qc_blocked"))
        with lock:
            generation_state[task_id].update({
                "status": "completed", "results": results,
                "success_count": sum(1 for r in results if r.get("success")),
                "qc_blocked_count": blocked_count,
            })
    except Exception as e:
        app.logger.error(f"资产生成失败: {e}")
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})


@app.route('/api/assets/generate', methods=['POST'])
def api_generate_assets():
    """生成资产（角色/物品/场景，含多视角）"""
    data = request.json
    asset_type = data.get('asset_type', '')  # character / item / scene
    project_name = _safe_project(data.get('project_name', 'project'))
    assets = data.get('assets', [])

    if asset_type not in ("character", "item", "scene"):
        return jsonify({"error": "asset_type 必须是 character/item/scene"}), 400
    if not assets:
        return jsonify({"error": "没有资产数据"}), 400

    task_id = f"{asset_type}_{project_name}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "asset_type": asset_type,
            "progress": 0, "total": len(assets), "current": 0,
            "phase": "基础图", "results": []
        }

    thread = threading.Thread(
        target=_generate_asset_task,
        args=(task_id, assets, asset_type, project_name)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "started"})


@app.route('/api/generation/status/<task_id>', methods=['GET'])
def api_generation_status(task_id):
    with lock:
        state = generation_state.get(task_id, {})
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
        derived = [os.path.join(project_dir, name, f) for f in _ASSET_VIEW_FILES[kind]]
        local = _first_existing(
            comfyui_client.resolve_local_path(payload.get("front") or ""),
            comfyui_client.resolve_local_path(payload.get("base") or ""),
            *derived,
        )
        index[name] = {"name": name, "image": local,
                       "url": f"/api/assets/{kind}s/{project_name}/{name}/front.png"}
    return index


def _allocate_storyboard_refs(shot: dict, char_idx: dict, item_idx: dict, scene_idx: dict,
                               project_name: str = None) -> list:
    """为单个镜头分配最多 3 张参考图（对应 分镜生成.json 的 image1/image2/image3）

    槽位语义（按重要性排序）：
      1) 主角色正视图 —— 人物外观锚点（必须有，否则该镜头直接跳过）
      2) 次要角色正视图，缺则用镜头内物品正视图（物品/道具锚点）
      3) 镜头场景正视图（环境氛围锚点）
    """
    chars_in = [n for n in (shot.get("characters_in_shot") or []) if n in char_idx]
    if not chars_in:
        chars_in = [n for n in char_idx.keys()][:1]
    items_in = [n for n in (shot.get("items_in_shot") or []) if n in item_idx]

    refs = []

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
    scene_name = loc if loc in scene_idx else (list(scene_idx.keys())[0] if scene_idx else None)
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


def _shot_seq(shot_id, fallback: int) -> int:
    """shot_id → 顺序号（用于文件名 shot_XX.png / shot_XX.mp4，兼容 'S001' 等字符串编号）"""
    m = re.search(r"\d+", str(shot_id))
    return int(m.group()) if m else fallback


def _storyboard_worker(task_id: str, project_name: str, shots: list,
                       char_idx: dict, item_idx: dict, scene_idx: dict,
                       episode_no=None):
    """后台分镜图生成任务：逐镜头生成并落盘 output/storyboards/<项目>[/epNN]/shot_XX.png"""
    out_dir = _ep_dir(os.path.join(STORYBOARDS_DIR, project_name), episode_no)
    os.makedirs(out_dir, exist_ok=True)
    try:
        _episode = int(episode_no) if str(episode_no or "").strip() else 1
    except (TypeError, ValueError):
        _episode = 1
    manifest_shots = []
    try:
        for i, shot in enumerate(shots):
            shot_id = shot.get("shot_id", i + 1)
            seq = _shot_seq(shot_id, i + 1)
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
                if not refs:
                    item["error"] = "该镜头无可用参考图（请先完成步骤2/3/4的资产生成）"
                else:
                    labels = [r[1] for r in refs]
                    prompt = comfyui_client.build_storyboard_prompt(shot, labels)
                    item["prompt"] = prompt
                    item["refs"] = {r[0]: os.path.basename(os.path.dirname(r[2])) for r in refs}

                    # ---------- 图片 AI 质检（不达标自动重生成） ----------
                    qc_cfg = _qc_load_cfg()
                    qc_on = qc_client.image_qc_ready(qc_cfg)
                    qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("image_enabled"))
                    max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
                    attempts = []
                    seed = None
                    dst = os.path.join(out_dir, f"shot_{seq:02d}.png")

                    for attempt in range(max_retries + 1):
                        if attempt > 0:
                            seed = random.randint(1, 2 ** 31 - 1)
                            with lock:
                                generation_state[task_id]["phase"] = \
                                    f"质检不达标，重新生成（第 {attempt}/{max_retries} 次）"
                                generation_state[task_id]["qc_phase"] = "regenerating"
                        result = comfyui_client.generate_storyboard(
                            prompt_zh=prompt,
                            ref_images=[r[2] for r in refs],
                            filename_prefix=f"comic_drama_sb/{project_name}_shot_{seq:02d}",
                            seed=seed,
                        )
                        if not result["files"]:
                            item["error"] = "ComfyUI 未返回分镜图（可能节点缺失或超时）"
                            break
                        # 模型自绘文字/水印修复：提示词已声明禁字，此处对残留标识做区域清理
                        if WATERMARK_CLEANUP_ENABLED:
                            wm_rec = watermark_cleanup.clean_image(
                                result["files"][0],
                                backup_dir=os.path.join(WATERMARK_CLEANUP_BACKUP_DIR, project_name))
                            item["watermark_cleanup"] = wm_rec
                            if not wm_rec.get("ok"):
                                app.logger.warning(f"分镜图去水印未生效: {wm_rec.get('error')}")
                        # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该图）
                        sb_scratch_dir = os.path.join(QC_DIR, project_name, "storyboard_scratch")
                        os.makedirs(sb_scratch_dir, exist_ok=True)
                        scratch_png = os.path.join(sb_scratch_dir,
                                                   f"shot_{seq:02d}_try{attempt + 1}.png")
                        shutil.copy2(result["files"][0], scratch_png)
                        item.update({
                            "success": True,
                            "file": dst,
                            "url": f"/api/storyboards/file/{project_name}/shot_{seq:02d}.png",
                            "ref_count": len(refs),
                        })
                        item.pop("error", None)
                        if not qc_on:
                            shutil.copy2(scratch_png, dst)   # 未开启质检：按原行为直接入库
                            break
                        with lock:
                            generation_state[task_id]["phase"] = f"图片质检中（镜头 {shot_id} · 第 {attempt + 1} 次）"
                            generation_state[task_id]["qc_phase"] = "checking"
                        verdict = qc_client.check_image(scratch_png, _qc_shot_desc(shot), qc_cfg)
                        rec = _qc_record_verdict(project_name, "image", shot_id, "图片质检",
                                                 attempt + 1, seed, scratch_png, verdict)
                        attempts.append(rec)
                        gate = _qc_gate(verdict)
                        item["qc_gate"] = gate
                        item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                        if gate["accept"]:
                            shutil.copy2(scratch_png, dst)   # 质检达标 → 写入正式交付目录
                            item["file"] = dst
                            break
                        if not verdict.get("ok"):
                            # 质检接口异常：保留暂存图，不盲目重生成（闸门会阻断入库）
                            break
                    if qc_declared or qc_on:
                        item["qc"] = _qc_summary(attempts, qc_declared, qc_on,
                                                 int(qc_cfg.get("max_retries", 0)))
                        gate = item.get("qc_gate")
                        if not gate or not gate.get("accept"):
                            # P0：质检不达标 / 调用异常 → 阻断入库（不写正式目录，仅暂存区留证，不计入成功）
                            item["success"] = False
                            item["qc_blocked"] = True
                            item.pop("file", None)
                            item.pop("url", None)
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
            "workflow": "分镜生成.json",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(manifest_shots),
            "success_count": ok,
            "qc_blocked_count": blocked,
            "shots": manifest_shots,
        }
        with open(os.path.join(out_dir, "storyboard_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

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


@app.route('/api/storyboards/generate', methods=['POST'])
def api_generate_storyboards():
    """为剧本的每个 shot 生成一张分镜图（参考角色/物品/场景资产图）"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name', 'project'))
    shots = data.get('shots', [])
    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400

    limit = data.get('limit')
    if isinstance(limit, int) and limit > 0:
        shots = shots[:limit]

    # ⑥ 自动引用剧本中已判定的「镜头数 / 每集时长」字段（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    char_idx = _build_asset_index(data.get('characters', []), project_name, "character")
    item_idx = _build_asset_index(data.get('items', []), project_name, "item")
    scene_idx = _build_asset_index(data.get('scenes', []), project_name, "scene")

    task_id = f"storyboard_{project_name}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "total": len(shots),
            "current": 0, "phase": "分镜图生成", "results": [],
            "qc": _qc_brief("image"),
            "refs_available": {
                "characters": {k: bool(v["image"]) for k, v in char_idx.items()},
                "items": {k: bool(v["image"]) for k, v in item_idx.items()},
                "scenes": {k: bool(v["image"]) for k, v in scene_idx.items()},
            },
        }

    thread = threading.Thread(target=_storyboard_worker,
                              args=(task_id, project_name, shots, char_idx, item_idx, scene_idx,
                                    data.get('episode_no')))
    thread.daemon = True
    thread.start()
    return jsonify({"task_id": task_id, "status": "started", "total": len(shots),
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
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(STORYBOARDS_DIR, safe_path)
    if os.path.exists(filepath):
        return send_file(filepath)
    abort(404)


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



def _norm_shot_key(key) -> str:
    """镜头编号归一化（1 / "1" / "01" 视为同一镜头）"""
    s = str(key).strip()
    return s.lstrip("0") or "0" if s.isdigit() else s


@app.route('/api/videos/generate', methods=['POST'])
def api_generate_videos():
    data = request.json
    project_name = _safe_project(data.get('project_name', 'project'))
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

    if not shots:
        return jsonify({"error": "没有镜头数据"}), 400

    # ⑥ 视频链路自动引用剧本自动判定的镜头时长（缺 duration 时按项目配置兜底）
    episode_stats = _episode_schema_defaults(project_name, shots)

    task_id = f"video_{project_name}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0,
            "total": len(shots), "current": 0, "results": [],
            "phase": "视频生成", "qc": _qc_brief("video"),
            "episode_stats": episode_stats,
        }

def _video_generate_worker(task_id, project_name, shots, character_refs,
                          scene_refs, storyboards, use_storyboard, mode,
                          timeout_per_segment, episode_tag, episode_no=None):
    """逐镜/整集/关键帧三种模式的视频生成（后台任务体，可被路由与流水线复用）

    从 /api/videos/generate 抽出的模块级实现：原闭包变量（项目名、镜头、参考图、
    模式等）改为显式参数，业务逻辑不变。抽出的目的是让自动生产流水线
    （pipeline.py / autopilot.py）能直接复用同一条视频生成链路，避免两套实现漂移。

    episode_no：集级目录隔离（第 1 集沿用平铺，第 2 集起写 epNN/），
    避免多集自动生产时 shot_NN.mp4 互相覆盖。
    """
    try:
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

        # 分镜图映射（步骤5产物）→ 作为 H3 的 <Picture 1> 构图基准
        # 修复：改用合并式映射（目录扫描 + manifest + 前端传入）。
        # 原实现只认前端传入的 storyboards，前端漏传某镜时该镜会静默退化为
        # 「无分镜图参考」，与用户所见不符。
        sb_map = _keyframe_sb_map(project_name, None, storyboards, episode_no=episode_no)
        app.logger.info(f"分镜图参考映射: {sorted(sb_map.keys())}")

        # 关键帧驱动模式：取该项目尾帧目录，并按镜登记 [首帧, 尾帧]
        kf_end_map = {}
        if mode == 'keyframe':
            kf_dir = _ep_dir(os.path.join(KEYFRAMES_DIR, project_name), episode_no)
            for i, _s in enumerate(shots):
                _sid = _s.get('shot_id', i + 1)
                _seq = _shot_seq(_sid, i + 1)
                _end = os.path.join(kf_dir, f"shot_{_seq:02d}_end.png")
                if os.path.isfile(_end):
                    kf_end_map[str(_sid)] = _end
                    kf_end_map[f"shot_{_seq:02d}"] = _end
            app.logger.info(f"[keyframe] 尾帧就绪 {len(set(kf_end_map.values()))}/{len(shots)} 镜")

        def _shot_segment(shot, seq):
            """把一个分镜转成 H3 工作流的一个「段」（提示词 + 时长 + 参考图）

            keyframe 模式：参考图 = [首帧(分镜图), 尾帧]，让 H3 在两端之间插值运动；
            缺尾帧时自动退化为首帧单锚（并在返回值中标记，便于前端提示）。
            """
            sid = shot.get('shot_id')
            sb_local = sb_map.get(_norm_shot_key(sid)) if use_storyboard else None
            if mode == 'keyframe' and sb_local:
                sb_local = sb_map.get(_norm_shot_key(seq)) or sb_map.get(
                    f"shot_{seq:02d}") or sb_local
                end_p = kf_end_map.get(str(sid)) or kf_end_map.get(f"shot_{seq:02d}")
                refs = [sb_local] + ([end_p] if end_p else [])
                prompt = comfyui_client._build_h3_prompt(
                    shot, character_refs, scene_refs,
                    storyboard_ref={"name": f"shot_{sid}"})
            elif sb_local:
                # 分镜图（构图/场景基准）+ 主角外观锚点，共 2 张（H3 参考图上限）
                refs = [sb_local] + main_char_img
                prompt = comfyui_client._build_h3_prompt(
                    shot, character_refs, scene_refs,
                    storyboard_ref={"name": f"shot_{sid}"})
            else:
                refs = ref_imgs
                prompt = shot.get('prompt_h3') or comfyui_client._build_h3_prompt(
                    shot, character_refs, scene_refs)
            try:
                dur = float(shot.get('duration') or 5)
            except (TypeError, ValueError):
                dur = 5.0
            return {"prompt": prompt, "duration": dur, "reference_images": refs,
                    "name": f"shot_{seq:02d}"}, sb_local

        # ---------- 模式 episode：整集一次提交，工作流段数 = 该集分镜数 ----------
        if mode == 'episode':
            segs, shot_meta_map = [], []
            for i, shot in enumerate(shots):
                shot_id = shot.get('shot_id', i + 1)
                seq = _shot_seq(shot_id, i + 1)
                seg, sb_local = _shot_segment(shot, seq)
                segs.append(seg)
                shot_meta_map.append({"shot_id": shot_id, "seq": seq,
                                      "duration": seg["duration"],
                                      "used_storyboard": bool(sb_local)})
            with lock:
                generation_state[task_id].update({
                    "current": 0, "progress": 5,
                    "phase": f"整集 {len(segs)} 段一次生成（每段=一个分镜）",
                    "segment_count": len(segs),
                })
            app.logger.info(f"[episode] 整集一次生成：{len(segs)} 段，"
                            f"分镜 {[s['name'] for s in segs]}")
            result = comfyui_client.generate_h3_sequence(
                segments=segs,
                filename_prefix=f"comic_drama/{project_name}_{episode_tag or 'episode'}",
                seed=None,
                timeout_per_segment=timeout_per_segment,
            )
            files = result.get('files') or []
            # 源文件存在性校验：ComfyUI 返回了路径但文件缺失时明确判失败
            missing_src = bool(files) and not os.path.isfile(files[0])
            if not files or missing_src:
                with lock:
                    generation_state[task_id]["results"].append({
                        "success": False, "mode": "episode",
                        "segment_count": len(segs),
                        "error": (f"ComfyUI 返回的视频文件不存在: {files[0]}" if missing_src
                                  else "ComfyUI 未返回视频文件（可能未安装 H3 节点或超时）"),
                    })
            else:
                src = files[0]
                ep_name = f"{episode_tag or 'episode'}_full.mp4"
                dst = os.path.join(videos_dir, ep_name)
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.move(src, dst)
                item = {"success": True, "mode": "episode",
                        "segment_count": len(segs),
                        "path": dst, "url": f"{_vurl}/{ep_name}",
                        "segments": [{"index": s.get("index"), "name": s.get("name"),
                                      "duration": s.get("duration"),
                                      "refs": len(s.get("refs") or [])}
                                     for s in (result.get("segments") or [])],
                        "shots": shot_meta_map,
                        "prompt_id": result.get("prompt_id"),
                        "timeout": result.get("timeout")}
                if H3_STRIP_AUDIO:
                    strip = ensure_no_audio(dst, backup=True)
                    item["audio"] = {
                        "has_audio_before": strip.get("has_audio_before"),
                        "has_audio_after": strip.get("has_audio_after"),
                        "changed": strip.get("changed"),
                        "method": strip.get("method"),
                        "backup": strip.get("backup"),
                        "message": strip.get("message") or ("已剥离音轨" if strip.get("changed") else ""),
                        "error": strip.get("error"),
                    }
                with lock:
                    generation_state[task_id]["results"].append(item)
                    generation_state[task_id]["progress"] = 100
                    generation_state[task_id]["phase"] = "整集视频生成完成"
                app.logger.info(f"[episode] 产物落盘: {dst}（{len(segs)} 段 → 1 条整集视频）")
            with lock:
                results = generation_state[task_id]["results"]
                ok = sum(1 for r in results if r.get("success"))
                generation_state[task_id].update({
                    "status": "completed" if ok else "failed",
                    "success_count": ok,
                    "error": "" if ok else "整集视频生成失败",
                })
            return

        for i, shot in enumerate(shots):
            shot_id = shot.get('shot_id', i + 1)
            seq = _shot_seq(shot_id, i + 1)
            with lock:
                generation_state[task_id].update({
                    "current": i + 1,
                    "progress": int((i + 1) / len(shots) * 100),
                    "current_shot": shot_id
                })

            try:
                seg, sb_local = _shot_segment(shot, seq)
                shot_refs = seg["reference_images"]
                prompt = seg["prompt"]

                # ---------- 视频 AI 质检（抽帧送检，不达标自动重生成） ----------
                qc_cfg = _qc_load_cfg()
                qc_on = qc_client.video_qc_ready(qc_cfg)
                qc_declared = bool(qc_cfg.get("enabled") and qc_cfg.get("video_enabled"))
                max_retries = int(qc_cfg.get("max_retries", 0)) if qc_on else 0
                attempts = []
                seed = None
                dst = os.path.join(videos_dir, f"shot_{seq:02d}.mp4")
                video_item = {"shot_id": shot_id, "success": False,
                              "mode": "per_shot", "segment_count": 1,
                              "duration": seg.get("duration"),
                              "used_storyboard": bool(sb_local)}

                for attempt in range(max_retries + 1):
                    if attempt > 0:
                        seed = random.randint(1, 2 ** 31 - 1)
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
                    )
                    if not result['files']:
                        video_item["error"] = "ComfyUI 未返回视频文件（可能未安装 H3 节点或超时）"
                        break
                    src = result['files'][0]
                    # 防御：源文件不存在时明确判失败（避免重试时对已 move 过的路径二次读取报错）
                    if not os.path.isfile(src):
                        video_item["error"] = f"ComfyUI 返回的视频文件不存在: {src}"
                        break
                    # 模型自绘文字/水印修复：逐帧区域插值，确保抽帧质检与交付一致
                    if WATERMARK_CLEANUP_ENABLED:
                        wm_rec = watermark_cleanup.clean_video(
                            src, backup_dir=os.path.join(WATERMARK_CLEANUP_BACKUP_DIR, project_name))
                        video_item["watermark_cleanup"] = wm_rec
                        if not wm_rec.get("ok"):
                            app.logger.warning(f"视频去水印未生效: {wm_rec.get('error')}")
                    # P0：先落「质检暂存区」，质检达标后才写入正式交付目录（阻断 ⇒ 正式目录不产生该视频）
                    v_scratch_dir = os.path.join(QC_DIR, project_name, "video_scratch")
                    os.makedirs(v_scratch_dir, exist_ok=True)
                    v_scratch = os.path.join(v_scratch_dir,
                                             f"shot_{seq:02d}_try{attempt + 1}.mp4")
                    if os.path.abspath(src) != os.path.abspath(v_scratch):
                        shutil.move(src, v_scratch)
                    video_item.update({"success": True, "path": dst,
                                       "url": f"{_vurl}/shot_{seq:02d}.mp4"})
                    video_item.pop("error", None)
                    if not qc_on:
                        if os.path.exists(dst):
                            os.remove(dst)
                        shutil.move(v_scratch, dst)   # 未开启质检：按原行为直接入库
                        break
                    with lock:
                        generation_state[task_id]["phase"] = \
                            f"视频质检中（镜头 {shot_id} · 抽帧 {qc_cfg.get('video_frame_count', 3)} 张 · 第 {attempt + 1} 次）"
                        generation_state[task_id]["qc_phase"] = "checking"
                    frames_dir = os.path.join(QC_DIR, project_name, "frames",
                                              f"shot_{seq:02d}_try{attempt + 1}")
                    verdict = qc_client.check_video(v_scratch, _qc_shot_desc(shot), qc_cfg,
                                                    frames_dir=frames_dir)
                    rec = _qc_record_verdict(
                        project_name, "video", shot_id, "视频质检",
                        attempt + 1, seed, v_scratch, verdict,
                        extra={
                            "duration": verdict.get("duration"),
                            "frames": [f"/api/qc/frames/{os.path.relpath(f, QC_DIR).replace(os.sep, '/')}"
                                       for f in (verdict.get("frames") or [])],
                            "frames_local": verdict.get("frames") or [],
                        })
                    attempts.append(rec)
                    gate = _qc_gate(verdict)
                    video_item["qc_gate"] = gate
                    video_item["qc"] = _qc_summary(attempts, qc_declared, True, max_retries)
                    if gate["accept"]:
                        if os.path.exists(dst):
                            os.remove(dst)
                        shutil.move(v_scratch, dst)   # 质检达标 → 写入正式交付目录
                        break
                    if not verdict.get("ok"):
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
                        video_item["error"] = ((f"视频质检阻断（{gate['label']}）：{gate['reason']}"
                                                "；未通过质检，未写入正式目录（暂存视频见质检历史）")
                                               if gate else (video_item.get("error")
                                                             or "视频生成失败，未写入正式目录"))
                elif "qc" not in video_item:
                    video_item["qc"] = {"enabled": False, "status": "disabled",
                                        "label": "质检未开启", "attempts": 0, "regenerated": 0}

                # ---------- 去音轨兜底（H3 生成阶段已断开音频输入，此处再做一次 ffprobe 实测校验） ----------
                if video_item.get("success") and H3_STRIP_AUDIO:
                    strip = ensure_no_audio(dst, backup=True)
                    video_item["audio"] = {
                        "has_audio_before": strip.get("has_audio_before"),
                        "has_audio_after": strip.get("has_audio_after"),
                        "changed": strip.get("changed"),
                        "method": strip.get("method"),
                        "backup": strip.get("backup"),
                        "message": strip.get("message") or ("已剥离音轨" if strip.get("changed") else ""),
                        "error": strip.get("error"),
                    }
                    if strip.get("has_audio_after"):
                        app.logger.warning(f"镜头 {shot_id} 去音轨后仍检测到音频流: {dst}")
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
        with lock:
            generation_state[task_id].update({"status": "failed", "error": str(e)})


    thread = threading.Thread(
        target=_video_generate_worker,
        args=(task_id, project_name, shots, character_refs, scene_refs,
              storyboards, use_storyboard, mode, timeout_per_segment, episode_tag,
              data.get('episode_no')),
    )
    thread.daemon = True
    thread.start()

    return jsonify({"task_id": task_id, "status": "started"})


# ===== 步骤6：成片合成 =====

@app.route('/api/final/video', methods=['POST'])
def api_generate_final():
    data = request.json
    project_name = _safe_project(data.get('project_name', 'project'))
    script_path = data.get('script_path', '')

    if not script_path or not os.path.exists(script_path):
        return jsonify({"error": "剧本文件不存在"}), 400

    try:
        output = video_processor.generate_final_video(script_path, project_name)
        if not output:
            return jsonify({"error": "没有可合并的视频片段，请先完成步骤5的视频生成"}), 400
        filename = os.path.basename(output)
        resp = {
            "success": True,
            "output_path": output,                       # 保留原字段（本地绝对路径）
            "filename": filename,
            "url": f"/api/final/{project_name}/{filename}"   # 新增：前端可直接播放/下载的 URL
        }
        # 视频水印（默认关闭；开启后额外产出一份带水印成片，不影响上面的无水印成片）
        resp["watermark"] = _wm_apply_to_final(output, project_name)
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== 静态资产访问 =====

@app.route('/api/assets/<path:filename>')
def api_asset_file(filename):
    """提供资产文件访问"""
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(PROJECT_OUTPUT_DIR, "assets", safe_path)
    if os.path.exists(filepath):
        return send_file(filepath)
    abort(404)


@app.route('/api/videos/<path:filename>')
def api_video_file(filename):
    """提供视频文件访问"""
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(VIDEOS_DIR, safe_path)
    if os.path.exists(filepath):
        return send_file(filepath, conditional=True)
    abort(404)


@app.route('/api/final/<path:filename>')
def api_final_file(filename):
    """提供最终成片文件访问（新增：使成片可在页面内联播放/下载）"""
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(FINAL_DIR, safe_path)
    if os.path.exists(filepath):
        # conditional=True 支持 Range 请求，视频可拖动进度条
        return send_file(filepath, conditional=True,
                         as_attachment=request.args.get('download') == '1')
    abort(404)


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
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(WATERMARK_DIR, safe_path)
    if os.path.exists(filepath):
        return send_file(filepath, conditional=True,
                         as_attachment=request.args.get('download') == '1')
    abort(404)


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
        except OSError:
            pass
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
    project_name = _safe_project(data.get('project_name') or 'project')

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
    threading.Thread(target=_upscale_worker,
                     args=(task_id, video_path, project_name, params),
                     daemon=True).start()
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
    safe_path = os.path.normpath(filename)
    if safe_path.startswith('..'):
        abort(403)
    filepath = os.path.join(UPSCALE_DIR, safe_path)
    if os.path.exists(filepath):
        return send_file(filepath, conditional=True,
                         as_attachment=request.args.get('download') == '1')
    abort(404)


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
                          model: str = None, timeout: int = None) -> LLMClient:
    """构造某个 AI 模块的客户端；传参可用于「测试连接」（不落盘）"""
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH) if not (
        base_url and api_key and model) else None
    ep = {"base_url": (base_url or "").strip(), "api_key": (api_key or "").strip(),
          "model": (model or "").strip()}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        saved = ai_config.get_module(cfg, module)
        ep = {"base_url": ep["base_url"] or saved["base_url"],
              "api_key": ep["api_key"] or saved["api_key"],
              "model": ep["model"] or saved["model"]}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        raise LLMError(f"「{AI_MODULE_LABEL.get(module, module)}」尚未配置"
                       "（base_url / api_key / model 均为必填）")
    return LLMClient(AI_CONFIG_PATH, config=ep, timeout=timeout or LLM_REQUEST_TIMEOUT)


def _current_llm_client() -> LLMClient:
    """文本分析链路（小说转剧本 / 章节转剧本 / 提示词分析）专用客户端：只用「文本分析模型」"""
    return _ai_client_for_module("text")


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


def _ai_config_view() -> dict:
    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    view = ai_config.public_view(cfg)
    view["config_path"] = os.path.abspath(AI_CONFIG_PATH)
    view["legacy_path"] = os.path.abspath(LLM_CONFIG_PATH)
    view["modules_meta"] = ai_config.module_meta()
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


@app.route('/api/ai/config', methods=['POST'])
def api_ai_config_save():
    """保存单个模块：{module: text|qc|chat, base_url, model, api_key?}"""
    data = request.json or {}
    module = (data.get("module") or "").strip()
    if module not in AI_MODULES:
        return jsonify({"success": False,
                        "error": f"unknown module：{module or '(空)'}，可选 {list(AI_MODULES)}"}), 400
    base_url = (data.get("base_url") or '').strip()
    model = (data.get("model") or '').strip()
    api_key = data.get("api_key")
    if not base_url or not model:
        return jsonify({"success": False, "error": "base_url 与 model 均为必填项"}), 400

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    old = ai_config.get_module(cfg, module)
    key = "" if api_key is None else str(api_key).strip()
    keep = (not key) or ("*" in key)   # 留空或脱敏回显 → 不改动原密钥
    if keep and not old.get("api_key"):
        return jsonify({"success": False, "error": "该模块首次配置必须填写 api_key"}), 400

    cfg = ai_config.save_module(AI_CONFIG_PATH, module, base_url=base_url, model=model,
                                api_key=None if keep else key, legacy_path=LLM_CONFIG_PATH)
    view = ai_config.module_public_view(ai_config.get_module(cfg, module))
    return jsonify({
        "success": True,
        "module": module,
        "module_config": view,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "message": f"{AI_MODULE_LABEL.get(module, module)}配置已保存" + ("（api_key 保持不变）" if keep else ""),
    })


@app.route('/api/ai/config/clear', methods=['POST'])
def api_ai_config_clear():
    """清空单个模块；不传 module 则整体重置三个模块"""
    data = request.json or {}
    module = (data.get("module") or "").strip() or None
    if module and module not in AI_MODULES:
        return jsonify({"success": False, "error": f"unknown module：{module}"}), 400
    cfg = ai_config.clear_module(AI_CONFIG_PATH, module=module, legacy_path=LLM_CONFIG_PATH)
    return jsonify({
        "success": True,
        "module": module,
        "config": _ai_config_view(),
        "config_path": os.path.abspath(AI_CONFIG_PATH),
        "message": (f"{AI_MODULE_LABEL.get(module, module)}配置已清除" if module else "AI 设置已整体重置"),
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

    cfg = ai_config.load_config(AI_CONFIG_PATH, LLM_CONFIG_PATH)
    saved = ai_config.get_module(cfg, module)
    base_url = (data.get("base_url") or saved["base_url"] or "").strip()
    model = (data.get("model") or saved["model"] or "").strip()
    api_key = data.get("api_key")
    if not api_key or not str(api_key).strip() or "*" in str(api_key):
        api_key = saved["api_key"] or ""
    ep = {"base_url": base_url, "api_key": str(api_key).strip(), "model": model}
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
        client = LLMClient(AI_CONFIG_PATH, config=ep, timeout=int(data.get("timeout") or 60))
        result = dict(client.test_connection() or {})
    except Exception as e:  # noqa: BLE001
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}
    result.update({"module": module, "probe": "text", "model": ep["model"], "base_url": ep["base_url"]})
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
    data = request.json or {}
    data["module"] = "text"
    return api_ai_config_save()


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

def _chat_project(data: dict = None, history: dict = None) -> str:
    data = data or {}
    name = (data.get("project_name") or "").strip()
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

    state = _chat_state(project)
    return jsonify({
        "success": True,
        "message": f"创作设定已应用（{len(merged)} 项）",
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


@app.route('/api/ai/settings', methods=['GET'])
def api_ai_settings():
    """统一读取创作设定（简版）：{project: 可空} → 生效设定 + style_brief"""
    project = (request.args.get("project") or "").strip()
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


def _qc_gate(verdict: dict) -> dict:
    """统一质检入库闸门（P0）：ok=false 或 不达标 一律不得静默入库。

    返回 {"accept", "blocked", "skipped", "label", "reason", "critical_issues"}
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
        return {"accept": False, "blocked": bool(verdict.get("blocked")), "skipped": False,
                "label": "质检不达标", "reason": verdict.get("reason") or "质检未达标",
                "critical_issues": []}
    return {"accept": True, "blocked": False, "skipped": False, "label": "质检达标",
            "reason": verdict.get("reason") or "", "critical_issues": []}


def _qc_record_verdict(project_name: str, kind: str, shot_key, stage: str,
                       attempt: int, seed, file_path: str, verdict: dict,
                       extra: dict = None) -> dict:
    """把一次质检结论整理成历史记录并落盘，返回该记录（含 history_file）"""
    rec = {"attempt": attempt, "seed": seed, "file": file_path, "stage": stage,
           "ok": bool(verdict.get("ok")), "passed": bool(verdict.get("passed")),
           "accepted": bool(verdict.get("accepted") if verdict.get("accepted") is not None
                            else verdict.get("passed")),
           "blocked": bool(verdict.get("blocked")),
           "score": verdict.get("score"), "reason": verdict.get("reason"),
           "issues": verdict.get("issues") or [],
           "critical_issues": verdict.get("critical_issues") or [],
           "error": verdict.get("error"), "latency_ms": verdict.get("latency_ms")}
    if extra:
        rec.update(extra)
    rec["history_file"] = _qc_record(project_name, kind, shot_key, rec)
    return rec


def _qc_shot_desc(shot: dict) -> str:
    parts = []
    if shot.get("location"):
        parts.append(f"场景：{shot['location']}")
    if shot.get("camera"):
        parts.append(f"机位：{shot['camera']}")
    if shot.get("description"):
        parts.append(str(shot["description"]).strip())
    if shot.get("emotion"):
        parts.append(f"情绪：{shot['emotion']}")
    if shot.get("dialogue"):
        parts.append(f"台词：{str(shot['dialogue']).strip()[:80]}")
    return "；".join(parts)[:600] or "（无镜头描述）"


def _qc_record(project_name: str, kind: str, shot_id, payload: dict) -> str:
    """写一条质检/重试历史（失败也不影响主流程）"""
    try:
        return qc_client.append_history(QC_DIR, project_name, kind, shot_id, payload)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"质检历史写入失败（忽略）：{e}")
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
    status = "passed" if passed_rec else ("error" if last.get("error") else "failed")
    blocked = status in ("failed", "error")
    crit = list((passed_rec or last).get("critical_issues") or [])
    return {
        "enabled": True,
        "status": status,
        "label": {"passed": "质检达标", "failed": "质检不达标", "error": "质检调用异常"}.get(status, status),
        "passed": bool(passed_rec),
        "blocked": blocked,
        "entry_blocked": blocked,
        "asset_written": not blocked,
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
            meta = ingest_novel(tmp_path, raw_name, NOVELS_DIR)
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
        return _llm_guide_response("尚未配置自定义 AI 接口，无法把小说转成剧本")

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


def _episodes_worker(task_id: str, novel_meta: dict, chapters: list, style: str,
                     target_shots: int, overwrite: bool, project_key: str = None):
    key = _novel_key(novel_meta, project_key)
    total = len(chapters)

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
        for i, chapter in enumerate(chapters):
            ep = int(chapter.get("index"))
            ch_title = chapter.get("title") or f"第{ep}章"
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
                    "warnings": script["metadata"].get("warnings") or [],
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
                    "message": "生成完成",
                })
            except Exception as e:  # noqa: BLE001
                app.logger.error(f"第{ep}集生成失败: {e}")
                results.append({"episode_no": ep, "chapter_title": ch_title,
                                "status": "failed", "path": out_path,
                                "message": str(e)})

        ok = [r for r in results if r["status"] == "success"]
        skipped = [r for r in results if r["status"] == "skipped"]
        failed = [r for r in results if r["status"] == "failed"]
        episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
        with lock:
            generation_state[task_id].update({
                "status": "completed" if ok or skipped else "failed",
                "progress": 100, "current": total, "total": total,
                "message": (f"批量完成：成功 {len(ok)} 集 / 跳过 {len(skipped)} 集 / 失败 {len(failed)} 集"),
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
        return _llm_guide_response("尚未配置自定义 AI 接口，无法按章生成剧本")

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
    task_id = f"episodes_{meta.get('novel_id')}_{int(time.time())}"
    with lock:
        generation_state[task_id] = {
            "status": "running", "progress": 0, "phase": "prepare",
            "message": f"准备生成 {len(picks)} 集…", "current": 0, "total": len(picks),
            "novel_id": meta.get("novel_id"), "results": [],
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


@app.route('/api/episodes/<novel_id>', methods=['GET'])
def api_list_episodes(novel_id):
    """某小说已生成的剧集清单"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    # 支持 ?project_id= 指定项目查看（不传时自动按小说归属的项目）
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    key = _novel_key(meta, proj["dir_key"] if proj else None)
    episodes = novel_to_script.list_episodes(SCRIPT_DIR, key)
    return jsonify({
        "success": True, "novel_id": novel_id, "name": key,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "novel_title": meta.get("title") or meta.get("name"),
        "chapter_count": meta.get("chapter_count"),
        "count": len(episodes),
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


@app.route('/api/continuity/<novel_id>/<int:episode_no>/revalidate', methods=['POST'])
def api_continuity_revalidate(novel_id, episode_no):
    """对已落盘剧本重跑「相邻集六类一致性校验」（D⑨ 可选闸门；body.rewrite=true 时命中问题会局部重写）"""
    try:
        meta, proj, key = _resolve_continuity_key(novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _current_llm_client()
    if not client.configured:
        return _llm_guide_response("尚未配置自定义 AI 接口，无法执行一致性校验")
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
    return jsonify({"success": True, "novel_id": novel_id, "episode_no": episode_no,
                    "project_key": key, "validation": validation, "rewrite": rewrite,
                    "events": events})


# =====================================================================
# 新增模块 D：剧本提示词分析（prompt_h3 + 参考图提示词）
# =====================================================================

def _ensure_script_file(script: dict, script_path: str, project_name: str) -> str:
    if script_path and os.path.isfile(script_path):
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
        return _llm_guide_response("尚未配置自定义 AI 接口，无法生成提示词")

    mode = data.get('mode') or 'all'
    if mode not in ('shots', 'assets', 'all'):
        return jsonify({"success": False, "error": "mode 必须是 shots / assets / all"}), 400
    shot_ids = data.get('shot_ids') or None
    if isinstance(shot_ids, (str, int)):
        shot_ids = [shot_ids]
    project_name = _safe_project(data.get('project_name')
                                 or (script.get('title') or 'project'))
    script_path = data.get('script_path') or script.get('metadata', {}).get('script_path') or ''
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
        p = os.path.abspath(script_path)
        root = os.path.abspath(PROJECT_OUTPUT_DIR)
        if not p.startswith(root + os.sep):
            raise TTSError(f"剧本路径必须在项目输出目录内：{root}")
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
        raise TTSError("未找到可用剧本（output/scripts 下无含 shots 的 JSON），请显式提供 script_path")
    hit = [c for c in cands if project_name and project_name in c["path"]] or cands
    if episode:
        hit2 = [c for c in hit if str(c["episode_no"]) == str(episode)]
        if hit2:
            hit = hit2
    best = max(hit, key=lambda c: c["mtime"])
    return {"script": best["script"], "script_path": best["path"], "source": "auto"}


def _dub_audio_url(project_name: str, rel_path: str) -> str:
    rel = os.path.relpath(rel_path, _dub_project_dir(project_name)).replace(os.sep, "/")
    return f"/api/tts/file/{project_name}/{rel}" if not rel.startswith("..") else ""


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
            "lines": [dict(r, url=_dub_audio_url(project_name, r.get("out_path") or ""))
                      for r in results],
        }
        manifest_path = os.path.join(out_dir, f"ep{int(episode):02d}_dub_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        with dub_lock:
            dub_tasks[task_id].update({
                "status": "completed" if ok_items else "failed",
                "progress": 100, "phase": "配音完成" if ok_items else "配音失败",
                "message": f"成功 {len(ok_items)} 句 / 失败 {len(results) - len(ok_items)} 句",
                "merged_audio": merged,
                "merged_url": _dub_audio_url(project_name, merged) if merged else "",
                "merged_info": merged_probe,
                "manifest": manifest_path,
                "error": "" if ok_items else "全部句子合成失败，请查看 results 中的错误原因",
            })
    except (TTSError, OSError) as e:
        app.logger.error(f"配音任务失败: {e}")
        with dub_lock:
            dub_tasks[task_id].update({"status": "failed", "error": str(e), "phase": "失败"})
    except Exception as e:  # noqa: BLE001
        app.logger.exception("配音任务异常")
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
    project_name = _safe_project(data.get('project_name') or 'project')
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

    for ln in plan["lines"]:
        ln["url"] = _dub_audio_url(project_name, ln.get("out_path") or "")
        ln["exists"] = bool(ln.get("out_path") and os.path.exists(ln["out_path"]))
    plan.update({"success": True, "script_path": resolved["script_path"],
                 "script_source": resolved["source"], "voice_map_path": vm_path,
                 "out_dir": out_dir})
    return jsonify(plan)


@app.route('/api/tts/voice-map', methods=['POST'])
def api_tts_voice_map_save():
    """保存角色音色配置（所有角色固定 speaker/seed，保证全剧音色一致）"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or 'project')
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
    project_name = _safe_project(data.get('project_name') or 'project')
    text = clean_line_text(data.get('text') or '', str(data.get('character') or ''))
    if not text:
        return jsonify({"success": False, "error": "缺少待合成文本 text"}), 400

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
    return jsonify({"success": True, "result": dict(rec, url=_dub_audio_url(project_name, rec["out_path"])),
                    "audio": info, "voice": voice, "url": _dub_audio_url(project_name, rec["out_path"])})


@app.route('/api/tts/generate', methods=['POST'])
def api_tts_generate():
    """发起批量配音（异步任务）：逐句/逐角色合成 + 整集合并"""
    data = request.json or {}
    project_name = _safe_project(data.get('project_name') or 'project')

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
        return jsonify({"success": False, "error": "剧本中没有可朗读台词（或所选镜头无台词）"}), 400

    # 保存音色映射，保证同角色跨轮次音色一致
    try:
        save_voice_map(plan["voice_map"], vm_path)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"音色映射保存失败：{e}")

    task_id = f"dub_{project_name}_{int(time.time())}"
    plan_summary = {"script_path": resolved["script_path"], "script_source": resolved["source"],
                    "episode": episode, "line_count": plan["line_count"],
                    "characters": [{"name": c["name"], "voice": c["voice"],
                                    "line_count": c["line_count"]} for c in plan["characters"]]}
    with dub_lock:
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


def _mix_segments_dir(project_name: str) -> str:
    """定位该项目的镜头分段视频目录（用于按真实分段时长对齐时间轴）"""
    best, best_key = "", (-1, 0)
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


def _mix_prepare(data: dict) -> dict:
    """公共准备：解析项目 / 视频 / 剧本 / 配音清单 / 时间轴 / 逐句条目（不合成）"""
    project_name = _safe_project(data.get('project_name') or 'project')
    video_path = _mix_resolve_video(data, project_name)

    resolved = _dub_resolve_script(dict(data, project_name=project_name))
    script = resolved["script"]
    episode = int(data.get('episode') or script.get('episode_no')
                  or (script.get('metadata') or {}).get('episode_no') or 0)

    mf = _mix_manifest(project_name, episode)
    manifest = mf["manifest"]
    episode = episode or int(manifest.get("episode") or 1)

    seg_dir = _mix_segments_dir(project_name)
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
        with mix_lock:
            mix_tasks[task_id].update({
                "status": "completed", "progress": 100, "phase": "合成完成",
                "message": f"已合成 {report['entry_count']} 句配音，耗时 {report['elapsed_sec']}s",
                "output_path": report["output_path"],
                "report_path": report_path,
                "url": _mix_audio_url(project_name, report["output_path"]),
                "result": report,
            })
    except DubMixError as e:
        with mix_lock:
            mix_tasks[task_id].update({"status": "failed", "phase": "合成失败",
                                       "message": str(e), "progress": 100})
    except Exception as e:  # pragma: no cover - 兜底
        logger.exception("音画合成异常")
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
        return jsonify({"success": False, "error": str(e)}), 400

    project_name = prepared["project_name"]
    out_name = (data.get("out_name") or "").strip()
    if not out_name:
        base = os.path.splitext(os.path.basename(prepared["video_path"]))[0]
        out_name = f"{base}_ep{int(prepared['episode']):02d}_dubbed.mp4"
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_name = os.path.basename(out_name.replace("\\", "/"))

    task_id = f"mix_{project_name}_{int(time.time())}"
    with mix_lock:
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


def _autopilot_guard(fn):
    """统一异常兜底：托管接口不应把 500 抛给前端，而是返回可读错误"""
    def _wrap(*a, **k):
        try:
            return fn(*a, **k)
        except KeyError as e:
            return jsonify({"success": False, "error": f"对象不存在：{e}"}), 404
        except Exception as e:  # noqa: BLE001
            app.logger.exception("托管接口异常")
            return jsonify({"success": False, "error": f"{type(e).__name__}: {e}"}), 500
    _wrap.__name__ = fn.__name__
    return _wrap


@app.route('/api/autopilot/status', methods=['GET'])
@_autopilot_guard
def api_autopilot_status():
    """托管总览：开关状态、当前在做什么、待验收数、异常数、24h 生产曲线"""
    return jsonify({"success": True, **autopilot.status()})


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
    data = request.json or {}
    project = _safe_project(data.get('project') or data.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project"}), 400
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
    project = _safe_project(data.get('project') or data.get('project_name') or '')
    if not project:
        return jsonify({"success": False, "error": "缺少 project"}), 400
    return jsonify({"success": True, "project": project,
                    "plan": autopilot.disable(project)})


@app.route('/api/autopilot/pause', methods=['POST'])
@_autopilot_guard
def api_autopilot_pause():
    """暂停托管（在当前步骤边界生效，不产生半成品）"""
    data = request.json or {}
    return jsonify({"success": True, **autopilot.pause(str(data.get('reason') or '手动暂停'))})


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
    project = _safe_project(data.get('project') or data.get('project_name') or '')
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
                items.append(d)
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
    data = request.json or {}
    project = _safe_project(data.get('project') or data.get('project_name') or '')
    try:
        ep = int(data.get('episode_no') or 1)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "episode_no 非法"}), 400
    plan = autopilot.get_plan(project)
    meta = autopilot._novel_meta(project, plan)
    if not meta:
        return jsonify({"success": False,
                        "error": "该项目未关联小说，无法生产（请先在小说库上传建项目）"}), 400
    chapters = autopilot.chapters_of(meta)
    chapter = next((c for c in chapters if c['index'] == ep), None)
    if not chapter:
        return jsonify({"success": False,
                        "error": f"小说里没有第{ep}章（共 {len(chapters)} 章）"}), 400
    cfg = pipeline.normalize_config({**plan, 'novel_id': meta.get('novel_id')},
                                    default_project_key=project)
    result = pipeline.run_episode(cfg, project, ep, meta, chapter)
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
        except ValueError:
            pass
    if not patch:
        return jsonify({"success": False,
                        "error": "总控 AI 还没有生效设定；请先在「总控 AI 对话」里谈好风格再一键设定"}), 400
    plan = autopilot.set_plan(project, patch)
    return jsonify({"success": True, "project": project, "plan": plan,
                    "applied": patch, "settings_filled": view.get('filled'),
                    "settings_missing": view.get('missing'),
                    "style_brief": brief})


if __name__ == '__main__':
    app.logger.info(f"漫剧生成系统启动: http://{APP_HOST}:{APP_PORT} (debug={APP_DEBUG})")
    app.run(host=APP_HOST, port=APP_PORT, debug=APP_DEBUG, threaded=True)
