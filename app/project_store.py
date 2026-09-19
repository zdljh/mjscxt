"""
项目级隔离（每部小说 = 一个独立项目）
=====================================

设计原则
--------
1. **物理隔离靠「项目键（dir_key）」**：既有链路已把产物按 `output/<kind>/<项目名>/`
   落盘（assets / storyboards / videos / final / upscale / dub / qc / scripts）。
   本模块把「项目键」固定沉淀在注册表里，所有下游链路一律使用该键，
   从而做到「一部小说一个项目，数据互不混用」。
2. **配置分离**：每个项目在 `output/projects/<项目ID>/` 下拥有独立配置文件
   （config.json / ai_settings.json / chat_history.json），互不覆盖。
3. **注册表可重建**：`index.json` 是项目列表的唯一事实来源，但即使丢失，
   也能通过 `migrate_legacy()` 从磁盘产物目录重新扫描恢复（不丢数据）。
4. **删除可恢复**：删除项目不物理删除，而是把项目工作区 + 各产物目录整体
   移入 `output/projects/_trash/<时间戳>_<项目键>/`，随时可手工还原。

本模块只对 `output/projects/` 及 `output/<kind>/<项目键>/` 读写；
不触碰系统目录，不修改 ComfyUI 侧文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime

from config import (
    PROJECT_OUTPUT_DIR, SCRIPT_DIR, ASSETS_DIR, CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR,
    STORYBOARDS_DIR, VIDEOS_DIR, FINAL_DIR, UPSCALE_DIR, DUB_DIR, QC_DIR,
    KEYFRAMES_DIR, CONTINUITY_DIR, DUB_MIX_DIR, WATERMARK_DIR,
    PROJECTS_DIR, PROJECT_INDEX_PATH, PROJECT_TRASH_DIR, PROJECT_MIGRATE_REPORT,
    PROJECT_DEFAULT_CONFIG, AI_CHAT_DIR, AI_SETTINGS_PATH, AI_CHAT_HISTORY_PATH,
    NOVELS_DIR,
)

INDEX_VERSION = 1
KEY_MAX_LEN = 50

ASSET_KINDS = ("characters", "items", "scenes")


# =====================================================================
# 基础工具
# =====================================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_key(name: str) -> str:
    """项目名 → 项目键（与既有 _safe_project 规则一致，保证历史目录可直接命中）"""
    cleaned = re.sub(r"[《》〈〉【】「」『』]", "", str(name or "")).strip()
    key = "".join(c if c.isalnum() or c in "_-" else "_" for c in cleaned)
    key = key.strip("_") or "project"
    return key[:KEY_MAX_LEN]


def paths(key: str) -> dict:
    """项目键 → 该项目在各产物目录下的隔离路径 + 独立配置目录"""
    key = safe_key(key)
    root = os.path.join(PROJECTS_DIR, key)
    return {
        "key": key,
        "root": root,
        "config": os.path.join(root, "config.json"),
        "ai_settings": os.path.join(root, "ai_settings.json"),
        "chat_history": os.path.join(root, "chat_history.json"),
        "scripts": os.path.join(SCRIPT_DIR, key),
        "characters": os.path.join(CHARACTERS_DIR, key),
        "items": os.path.join(ITEMS_DIR, key),
        "scenes": os.path.join(SCENES_DIR, key),
        "storyboards": os.path.join(STORYBOARDS_DIR, key),
        "videos": os.path.join(VIDEOS_DIR, key),
        "final": os.path.join(FINAL_DIR, key),
        "upscale": os.path.join(UPSCALE_DIR, key),
        "dub": os.path.join(DUB_DIR, key),
        "qc": os.path.join(QC_DIR, key),
    }


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def new_project_id() -> str:
    return "p" + datetime.now().strftime("%Y%m%d%H%M%S") + os.urandom(2).hex()


# =====================================================================
# 注册表读写
# =====================================================================

def load_index() -> dict:
    data = _read_json(PROJECT_INDEX_PATH, None)
    if not isinstance(data, dict):
        data = {"version": INDEX_VERSION, "projects": [], "updated_at": now_str()}
    data.setdefault("version", INDEX_VERSION)
    projects = data.get("projects")
    if not isinstance(projects, list):
        data["projects"] = []
    return data


def save_index(index: dict) -> dict:
    index["version"] = INDEX_VERSION
    index["updated_at"] = now_str()
    _write_json(PROJECT_INDEX_PATH, index)
    return index


def _unique_key(index: dict, base: str) -> str:
    used = {p.get("dir_key") for p in index.get("projects", [])}
    key = base
    n = 2
    while key in used:
        suffix = f"_{n}"
        key = base[: KEY_MAX_LEN - len(suffix)] + suffix
        n += 1
    return key


def get_project(ref: str) -> dict | None:
    """按项目 ID 或项目键查找项目记录"""
    ref = str(ref or "").strip()
    if not ref:
        return None
    for p in load_index().get("projects", []):
        if p.get("id") == ref or p.get("dir_key") == ref or p.get("name") == ref:
            return p
    return None


def ensure_project_for_novel(novel_id: str, novel_name: str = "",
                             config: dict = None) -> dict:
    """小说 → 项目 一一对应：已有则复用，没有则按小说名新建独立项目。

    保证「一部小说 = 一个独立项目」，其剧本/资产/视频全部落在该项目自己的目录下，
    不会与其它小说混在全局目录。
    """
    rec = find_by_novel(novel_id)
    if rec:
        return rec
    display = (novel_name or novel_id or "").strip() or "未命名项目"
    return create_project(display, novel_id=novel_id, novel_name=display,
                          config=config, note="由小说转换自动建立的项目")


def find_by_novel(novel_id: str) -> dict | None:
    novel_id = str(novel_id or "").strip()
    if not novel_id:
        return None
    for p in load_index().get("projects", []):
        if p.get("novel_id") == novel_id:
            return p
    return None


# =====================================================================
# 项目 CRUD
# =====================================================================

def create_project(name: str, novel_id: str = "", novel_name: str = "",
                   config: dict = None, key: str = None, pid: str = None,
                   note: str = "") -> dict:
    """新建项目：注册表登记 + 建立独立配置目录与各产物隔离目录（均为空目录，不动任何已有文件）"""
    index = load_index()
    display = (name or "").strip() or "未命名项目"
    base_key = safe_key(key or display)
    dir_key = _unique_key(index, base_key)
    pid = pid or new_project_id()
    p = paths(dir_key)

    for d in (p["root"], p["scripts"], p["characters"], p["items"], p["scenes"],
              p["storyboards"], p["videos"], p["final"], p["upscale"], p["dub"], p["qc"]):
        os.makedirs(d, exist_ok=True)

    cfg = dict(PROJECT_DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update({k: v for k, v in config.items() if v is not None})
    _write_json(p["config"], cfg)
    if not os.path.exists(p["ai_settings"]):
        _write_json(p["ai_settings"], {"version": 1, "projects": {}, "active_project": dir_key})
    if not os.path.exists(p["chat_history"]):
        _write_json(p["chat_history"], {"version": 1, "messages": [],
                                        "drafts": {}, "active_project": dir_key})

    rec = {
        "id": pid,
        "name": display,
        "dir_key": dir_key,
        "novel_id": str(novel_id or ""),
        "novel_name": str(novel_name or ""),
        "created_at": now_str(),
        "updated_at": now_str(),
        "script_path": "",
        "script_paths": [],
        "episode_count": 0,
        "shot_count": 0,
        "episode_duration_sec": int(cfg.get("episode_duration_sec") or 0),
        "shots_per_episode": int(cfg.get("shots_per_episode") or 0),
        "from_migration": False,
        "note": note,
    }
    index.setdefault("projects", []).append(rec)
    save_index(index)
    return rec


def update_project(ref: str, **fields) -> dict | None:
    index = load_index()
    for rec in index.get("projects", []):
        if rec.get("id") == ref or rec.get("dir_key") == ref:
            for k, v in fields.items():
                if k in ("id", "dir_key"):
                    continue
                rec[k] = v
            rec["updated_at"] = now_str()
            save_index(index)
            return rec
    return None


def rename_project(ref: str, new_name: str) -> dict | None:
    """重命名项目（只改显示名，项目键与目录保持不变 → 已有产物无需搬动，绝不会丢数据）"""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("新项目名不能为空")
    rec = get_project(ref)
    if not rec:
        return None
    return update_project(rec["id"], name=new_name[:60], renamed_at=now_str())


def read_config(ref: str) -> dict:
    rec = get_project(ref)
    if not rec:
        return dict(PROJECT_DEFAULT_CONFIG)
    cfg = _read_json(paths(rec["dir_key"])["config"], None)
    merged = dict(PROJECT_DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def update_config(ref: str, patch: dict) -> dict:
    rec = get_project(ref)
    if not rec:
        raise KeyError("项目不存在")
    cfg = read_config(rec["dir_key"])
    cfg.update({k: v for k, v in (patch or {}).items() if v is not None})
    _write_json(paths(rec["dir_key"])["config"], cfg)
    fields = {}
    for src, dst in (("shots_per_episode", "shots_per_episode"),
                     ("episode_duration_sec", "episode_duration_sec")):
        if src in patch and patch[src] is not None:
            try:
                fields[dst] = int(patch[src])
            except (TypeError, ValueError):
                pass
    if fields:
        update_project(rec["id"], **fields)
    return cfg


#: 项目专属产物根目录（kind → 绝对根目录）。
#:
#: ⚠️ 2026-09-19 实测教训：这里曾**只列 11 类**，漏掉 final_dub（配音成片）、keyframes（尾帧）、
#: continuity（跨镜连续性）、autopilot（托管计划/历史/交付物索引）、autonomous（自主模式）、
#: comic_drama（漫画剧资产）、exports（导出件）、watermark（水印成片）。
#: 后果是「删掉项目」后 output/ 里仍残留 76MB 的 final_dub，用户以为已经删干净了。
#: 新增产物目录时**必须**同步登记到这里，否则删除会再次漏。
def project_kind_roots() -> list:
    """(kind, 根目录) 列表 —— 删除/清理项目时需要覆盖的**全部**产物目录。

    注意：其中若干目录（autopilot/autonomous/exports/export/comic_drama）没有独立
    config 常量，按名字从 PROJECT_OUTPUT_DIR 拼出，避免为了一个字符串去动 config 接口。
    """
    out = PROJECT_OUTPUT_DIR
    return [
        ("scripts", SCRIPT_DIR),
        ("characters", CHARACTERS_DIR),
        ("items", ITEMS_DIR),
        ("scenes", SCENES_DIR),
        ("storyboards", STORYBOARDS_DIR),
        ("videos", VIDEOS_DIR),
        ("final", FINAL_DIR),
        ("upscale", UPSCALE_DIR),
        ("dub", DUB_DIR),
        ("qc", QC_DIR),
        ("keyframes", KEYFRAMES_DIR),
        ("continuity", CONTINUITY_DIR),
        ("final_dub", DUB_MIX_DIR),
        ("watermark", WATERMARK_DIR),
        # 下面几个是「两层」结构：comic_drama/{characters,scenes}/<项目>
        ("comic_characters", os.path.join(out, "comic_drama", "characters")),
        ("comic_scenes", os.path.join(out, "comic_drama", "scenes")),
        ("autopilot", os.path.join(out, "autopilot")),
        ("autonomous", os.path.join(out, "autonomous")),
        ("exports", os.path.join(out, "exports")),
        ("export", os.path.join(out, "export")),
    ]


def _project_alias_names(rec: dict) -> list:
    """该项目在磁盘上可能出现过的目录名（项目键 + 显示名）"""
    names = []
    for v in (rec.get("dir_key"), rec.get("name")):
        v = str(v or "").strip()
        if v and v not in names:
            names.append(v)
    return names


def _match_project_entries(root: str, names: list) -> list:
    """在 root 下找出属于该项目的全部条目（子目录**和**文件）。

    一部小说 = 一个项目，但产物在磁盘上有多种形态：
    - ``<项目名>/``                —— 项目级产物目录
    - ``<项目名>_第N集/``          —— 分集时每集一个子目录，互不覆盖
    - ``<项目名>_<时间戳>.json``    —— 整本转换的单文件剧本（落在 output/scripts 下）
    - ``<项目名>_..._wm.mp4``      —— 水印成片（落在 output/watermark 下，是文件不是目录）
    统一按「精确名 / 前缀」聚合，保证既不串项目、也不漏分集目录与单文件产物。
    """
    if not root or not os.path.isdir(root):
        return []
    hit = []
    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if not (os.path.isdir(full) or os.path.isfile(full)):
            continue
        for n in names:
            if entry == n or entry.startswith(n + "_") or entry.startswith(n + "-"):
                hit.append(full)
                break
    return hit


def delete_project(ref: str, confirm: bool = False) -> dict:
    """删除项目 = 移入回收站（output/projects/_trash/…），非物理删除，可手工还原。

    需要显式 confirm=True（前端会弹出二次确认框），未确认一律拒绝。

    覆盖范围：项目工作区 + :func:`project_kind_roots` 里的**全部**产物目录
    （含分集变体 `<项目名>_第N集`）。漏登记的后果见 project_kind_roots 的注释。
    """
    rec = get_project(ref)
    if not rec:
        raise KeyError("项目不存在")
    if not confirm:
        raise PermissionError("删除项目需要明确确认（confirm=true）")

    key = rec["dir_key"]
    p = paths(key)
    names = _project_alias_names(rec)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, f"{stamp}_{key}")
    os.makedirs(trash_root, exist_ok=True)

    moved, skipped = [], []
    targets = [("projects_workspace", p["root"])]
    for kind, root in project_kind_roots():
        targets.extend((kind, d) for d in _match_project_entries(root, names))

    for kind, src in targets:
        if not os.path.exists(src):
            continue
        dst = os.path.join(trash_root, kind, os.path.basename(src))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved.append({"kind": kind, "from": src, "to": dst})
        except (OSError, shutil.Error) as e:  # 单个目录失败不影响其余
            skipped.append({"kind": kind, "path": src, "error": str(e)})

    index = load_index()
    index["projects"] = [x for x in index.get("projects", []) if x.get("id") != rec["id"]]
    save_index(index)

    return {"project": rec, "trash_dir": trash_root, "moved": moved,
            "skipped": skipped, "recoverable": True}


# =====================================================================
# 剧本绑定与统计
# =====================================================================

def project_scripts(ref: str) -> list:
    """项目内全部剧本文件（分集目录 + 单文件），按集号排序"""
    rec = get_project(ref)
    if not rec:
        return []
    p = paths(rec["dir_key"])
    out = []
    if os.path.isdir(p["scripts"]):
        for fn in sorted(os.listdir(p["scripts"])):
            if fn.lower().endswith(".json"):
                out.append(os.path.join(p["scripts"], fn))
    # 兼容历史：整本转换的剧本落在 output/scripts/<项目名>_<时间戳>.json
    if os.path.isdir(SCRIPT_DIR):
        for fn in sorted(os.listdir(SCRIPT_DIR)):
            if fn.lower().endswith(".json") and rec["dir_key"] in fn:
                out.append(os.path.join(SCRIPT_DIR, fn))
    return out


def script_stats(script_path: str) -> dict:
    """读取剧本文件，提取「镜头数 / 每集时长（秒）」等统计（优先用 Schema 字段，缺失时回算）"""
    data = _read_json(script_path, None)
    if not isinstance(data, dict):
        return {}
    shots = data.get("shots") or []
    ep = data.get("episode_stats") or data.get("metadata", {}).get("episode_stats") or {}
    shot_count = int(data.get("shot_count") or ep.get("shot_count") or len(shots) or 0)
    duration = data.get("episode_duration_sec") or ep.get("duration_sec")
    if not duration:
        try:
            duration = sum(float(s.get("duration") or 0) for s in shots) or shot_count * 5
        except (TypeError, ValueError):
            duration = shot_count * 5
    return {
        "episode_no": data.get("episode_no") or (data.get("metadata") or {}).get("episode_no"),
        "title": (data.get("metadata") or {}).get("title") or data.get("title") or "",
        "shot_count": shot_count,
        "episode_duration_sec": round(float(duration or 0), 2),
        "characters": len(data.get("characters") or []),
        "items": len(data.get("items") or []),
        "scenes": len(data.get("scenes") or []),
    }


def bind_script(ref: str, script_path: str, stats: dict = None) -> dict | None:
    """把剧本归属到项目：登记 script_path / 分集清单 / 镜头数与总时长（不移动文件）"""
    rec = get_project(ref)
    if not rec or not script_path:
        return None
    existing = list(rec.get("script_paths") or [])
    if script_path not in existing:
        existing.append(script_path)
    stats = stats or script_stats(script_path)
    fields = {"script_paths": existing, "script_path": script_path}
    if stats:
        fields["shot_count"] = int(stats.get("shot_count") or rec.get("shot_count") or 0)
        if stats.get("episode_duration_sec"):
            fields["episode_duration_sec"] = float(stats["episode_duration_sec"])
    if len(existing) > 1:
        fields["episode_count"] = len(existing)
    return update_project(rec["id"], **fields)


def project_stats(ref: str) -> dict:
    """项目磁盘现状统计（只读扫描，用于列表页展示隔离成果）"""
    rec = get_project(ref)
    if not rec:
        return {}
    p = paths(rec["dir_key"])

    def count_files(d, exts=None):
        if not os.path.isdir(d):
            return 0
        n = 0
        for _, _, files in os.walk(d):
            for f in files:
                if not exts or os.path.splitext(f)[1].lower() in exts:
                    n += 1
        return n

    scripts = project_scripts(rec["dir_key"])
    return {
        "scripts": len(scripts),
        "characters": len(os.listdir(p["characters"])) if os.path.isdir(p["characters"]) else 0,
        "items": len(os.listdir(p["items"])) if os.path.isdir(p["items"]) else 0,
        "scenes": len(os.listdir(p["scenes"])) if os.path.isdir(p["scenes"]) else 0,
        "asset_images": count_files(p["characters"], {".png", ".jpg", ".jpeg", ".webp"})
                        + count_files(p["items"], {".png", ".jpg", ".jpeg", ".webp"})
                        + count_files(p["scenes"], {".png", ".jpg", ".jpeg", ".webp"}),
        "storyboards": count_files(p["storyboards"], {".png", ".jpg", ".jpeg", ".webp"}),
        "videos": count_files(p["videos"], {".mp4", ".mov", ".webm"}),
        "final_videos": count_files(p["final"], {".mp4", ".mov", ".webm"}),
        "upscaled": count_files(p["upscale"], {".mp4", ".mov", ".webm"}),
        "dub_audio": count_files(p["dub"], {".wav", ".flac", ".mp3"}),
        "qc_records": count_files(p["qc"], {".json"}),
        "disk_paths": {k: p[k] for k in ("root", "scripts", "characters", "items", "scenes",
                                         "storyboards", "videos", "final", "upscale", "dub", "qc")},
    }


def summarize(ref: str, with_stats: bool = True) -> dict | None:
    rec = get_project(ref)
    if not rec:
        return None
    out = dict(rec)
    out["config"] = read_config(rec["dir_key"])
    out["paths"] = paths(rec["dir_key"])
    out["stats"] = project_stats(rec["dir_key"]) if with_stats else {}
    return out


def list_projects(with_stats: bool = True) -> list:
    return [summarize(p["id"], with_stats) for p in load_index().get("projects", [])]


# =====================================================================
# 资产画廊（角色 / 场景 / 物品 / 分镜）
# =====================================================================

def _views_of(dir_path: str) -> list:
    views = []
    if not os.path.isdir(dir_path):
        return views
    for fn in sorted(os.listdir(dir_path)):
        ext = os.path.splitext(fn)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        full = os.path.join(dir_path, fn)
        views.append({
            "view": os.path.splitext(fn)[0],
            "file": full,
            "size": os.path.getsize(full),
            "url": "/api/assets/" + os.path.relpath(full, ASSETS_DIR).replace(os.sep, "/"),
        })
    return views


def project_dirs(root: str, ref: str) -> list:
    """项目键在某个产物根目录下的全部隔离子目录。

    一部小说 = 一个项目，项目内的产物目录有两级形态：
    - ``output/<kind>/<项目键>/``            （整本转换 / 项目级产物）
    - ``output/<kind>/<项目键>_第N集/``      （按章节分集时每集独立子目录，互不覆盖）
    两者都属于同一个项目，统一按「项目键」或「项目键_*」前缀聚合，保证不串项目。
    """
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    out = []
    if not os.path.isdir(root):
        return out
    if os.path.basename(os.path.normpath(root)) == key:
        # 传入的已经是项目专属目录（如 paths(key)["characters"]），直接返回自身
        return [root]
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if name == key or name.startswith(key + "_") or name.startswith(key + "-"):
            out.append(full)
    return out


def find_kind_dir(ref: str, kind: str, name: str) -> dict:
    """在项目范围内定位某个资产的目录。

    返回 {"asset_dir": 绝对路径, "project_dir": 所属项目产物子目录名}

    ``kind`` 取 characters / items / scenes / storyboards。
    """
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    root = paths(key)[kind]
    for d in project_dirs(root, key):
        if not os.path.isdir(d):
            continue
        target = os.path.join(d, name)
        if os.path.isdir(target):
            return {"asset_dir": target, "project_dir": os.path.basename(d),
                    "kind_root": root}
    return {"asset_dir": "", "project_dir": "", "kind_root": root}


def find_asset_file(ref: str, kind: str, filename: str) -> str:
    """在项目范围内按文件名定位文件（分镜图等），返回绝对路径或空串。"""
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    root = paths(key)[kind]
    for d in project_dirs(root, key):
        cand = os.path.join(d, filename)
        if os.path.isfile(cand):
            return cand
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn == filename:
                    return os.path.join(d, fn)
    return ""


def asset_gallery(ref: str) -> dict:
    """项目资产索引：角色 / 物品 / 场景 各自的视图列表（含可直接访问的 URL）

    聚合该项目下所有产物子目录（项目键本身 + 全部分集目录），实现项目级隔离下的完整画廊。
    """
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    p = paths(key)
    out = {"characters": [], "items": [], "scenes": []}
    for kind in ASSET_KINDS:
        base = p[kind]
        items = []
        for d in project_dirs(base, key):
            pdir = os.path.basename(d)
            for name in sorted(os.listdir(d)):
                sub = os.path.join(d, name)
                if not os.path.isdir(sub):
                    continue
                views = _views_of(sub)
                base_view = next((v for v in views if v["view"] == "base"), None)
                items.append({
                    "kind": kind[:-1] if kind.endswith("s") else kind,  # characters → character
                    "name": name,
                    "dir": sub,
                    "project_dir": pdir,
                    "view_count": len(views),
                    "views": views,
                    "thumb": (base_view or (views[0] if views else None)),
                })
        out[kind] = items
    return out


def gallery_summary(ref: str) -> dict:
    """画廊汇总：角色/物品/场景/分镜/视频/成片/配音/超分的条目数（按项目聚合）"""
    g = asset_gallery(ref)
    rec = get_project(ref)
    key = rec["dir_key"] if rec else safe_key(ref)
    p = paths(key)
    out = {k: len(v) for k, v in g.items()}
    out["storyboards"] = sum(len(files) for _, _, files in
                             [(0, 0, [f for f in os.listdir(d) if f.lower().endswith(".png")])
                              for d in project_dirs(p["storyboards"], key)])
    out["videos"] = sum(len(files) for _, _, files in
                        [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv"))])
                         for d in project_dirs(p["videos"], key)])
    out["final"] = sum(len(files) for _, _, files in
                       [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv"))])
                        for d in project_dirs(p["final"], key)])
    out["upscale"] = sum(len(files) for _, _, files in
                         [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".mp4", ".mov", ".mkv"))])
                          for d in project_dirs(p["upscale"], key)])
    out["dub"] = sum(len(files) for _, _, files in
                     [(0, 0, [f for f in os.listdir(d) if f.lower().endswith((".wav", ".flac", ".mp3"))])
                      for d in project_dirs(p["dub"], key)])
    return out


# =====================================================================
# 历史数据归属迁移（非破坏性：只登记/复制配置，绝不移动或删除原文件）
# =====================================================================

def _novel_lookup() -> dict:
    """小说名/标题 → novel_id"""
    idx = _read_json(os.path.join(NOVELS_DIR, "index.json"), {})
    novels = idx.get("novels") if isinstance(idx, dict) else idx
    out = {}
    for n in (novels or []):
        if not isinstance(n, dict):
            continue
        for field in ("name", "title"):
            v = (n.get(field) or "").strip()
            if v:
                out[v] = n.get("novel_id") or n.get("id") or ""
    return out


def _strip_ts(name: str) -> str:
    """剑心初醒_20260910_151231.json → 剑心初醒"""
    stem = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r"_\d{8}_\d{6}$", "", stem)


def migrate_legacy(force: bool = False) -> dict:
    """扫描磁盘上的历史产物/剧本/配置，为尚未登记的数据补齐项目归属（不搬动任何文件）。"""
    index = load_index()
    known_keys = {p.get("dir_key") for p in index.get("projects", [])}
    created, existing, binds, sources = [], [], [], {}

    def ensure(key: str, name: str = "", novel_id: str = "", novel_name: str = "",
               from_migration: bool = True):
        nonlocal index
        key = safe_key(key)
        if not key:
            return None
        if key in known_keys:
            rec = next((p for p in index["projects"] if p.get("dir_key") == key), None)
            existing.append(key)
            return rec
        display = (name or key).strip()
        rec = create_project(display, novel_id=novel_id, novel_name=novel_name,
                             key=key, note="历史数据迁移")
        update_project(rec["id"], from_migration=bool(from_migration))
        index = load_index()
        known_keys.add(rec["dir_key"])
        created.append({"key": rec["dir_key"], "name": display,
                        "novel_id": novel_id, "novel_name": novel_name})
        return rec

    novels = _novel_lookup()

    # 1) 分集剧本目录 output/scripts/<小说名>/第N集.json
    if os.path.isdir(SCRIPT_DIR):
        for entry in sorted(os.listdir(SCRIPT_DIR)):
            full = os.path.join(SCRIPT_DIR, entry)
            if os.path.isdir(full):
                hits = [f for f in os.listdir(full) if f.lower().endswith(".json")]
                if not hits:
                    continue
                rec = ensure(entry, name=entry)
                sources.setdefault(entry, []).append(full)
                if rec:
                    for fn in sorted(hits):
                        binds.append((rec["dir_key"], os.path.join(full, fn)))
            elif entry.lower().endswith(".json"):
                # 2) 整本转换的单文件剧本 output/scripts/<小说名>_<时间戳>.json
                data = _read_json(full, {})
                meta = (data or {}).get("metadata") or {}
                key = safe_key(meta.get("project_name") or _strip_ts(entry))
                if not key:
                    continue
                rec = ensure(key, name=key)
                if rec:
                    binds.append((rec["dir_key"], full))

    # 3) 各产物目录 output/<kind>/<项目键>/…
    kind_dirs = [("characters", CHARACTERS_DIR), ("items", ITEMS_DIR), ("scenes", SCENES_DIR),
                 ("storyboards", STORYBOARDS_DIR), ("videos", VIDEOS_DIR), ("final", FINAL_DIR),
                 ("upscale", UPSCALE_DIR), ("dub", DUB_DIR), ("qc", QC_DIR)]
    for kind, d in kind_dirs:
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            full = os.path.join(d, entry)
            if not os.path.isdir(full):
                continue
            if entry.startswith("_"):
                continue
            ensure(entry, name=entry)
            sources.setdefault(entry, []).append(full)

    # 4) 全局 AI 设定 / 会话里出现过的项目键 → 补齐项目并复制其配置（不改动全局文件）
    for src_path, target_name in ((AI_SETTINGS_PATH, "ai_settings.json"),
                                  (AI_CHAT_HISTORY_PATH, "chat_history.json")):
        data = _read_json(src_path, None)
        if not isinstance(data, dict):
            continue
        keys = set((data.get("projects") or {}).keys()) | set((data.get("drafts") or {}).keys())
        if data.get("active_project"):
            keys.add(data["active_project"])
        for k in sorted(keys):
            ai_key = str(k).strip()
            k = safe_key(ai_key)
            if not k or k == "default":
                continue
            rec = ensure(k, name=k)
            if not rec:
                continue
            dst = (paths(rec["dir_key"])["chat_history"] if target_name == "chat_history.json"
                   else paths(rec["dir_key"])["ai_settings"])
            if os.path.exists(dst):
                blob = _read_json(dst, {})
                if blob.get("projects", {}).get(ai_key) or blob.get("drafts", {}).get(ai_key):
                    continue
            copy = {"version": 1, "projects": {}, "drafts": {}, "messages": []}
            if target_name == "ai_settings.json":
                s = (data.get("projects") or {}).get(ai_key)
                if s:
                    copy["projects"][k] = s
            else:
                dd = (data.get("drafts") or {}).get(ai_key)
                if dd:
                    copy["drafts"][k] = dd
            if copy.get("projects") or copy.get("drafts"):
                copy["active_project"] = k
                _write_json(dst, copy)

    # 5) 用小说索引反查 novel_id，补全小说绑定
    for rec in list(index["projects"]):
        if rec.get("novel_id"):
            continue
        for nm, nid in novels.items():
            if not nid:
                continue
            if nm and (nm in rec["dir_key"] or rec["dir_key"] in nm):
                update_project(rec["id"], novel_id=nid, novel_name=nm)
                break

    # 6) 剧本绑定 + 统计
    bound = []
    for key, sp in binds:
        rec = get_project(key)
        if not rec:
            continue
        st = script_stats(sp)
        bind_script(rec["id"], sp, st)
        bound.append({"key": key, "script": sp,
                      "shot_count": st.get("shot_count"),
                      "episode_duration_sec": st.get("episode_duration_sec")})

    report = {
        "generated_at": now_str(),
        "projects_total": len(load_index().get("projects", [])),
        "created": created,
        "already_existed": sorted(set(existing)),
        "scripts_bound": bound,
        "source_dirs": sources,
        "policy": "仅登记与复制配置；不移动、不删除任何历史产物，历史数据零丢失",
    }
    _write_json(PROJECT_MIGRATE_REPORT, report)
    return report
