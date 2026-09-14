"""
统一「AI 设置」配置管理 —— 文本分析 / 质检 / 对话总控 三个相互独立的模型模块

配置文件：项目根目录 ai_config.json
{
  "version": 1,
  "modules": {
    "text": {"base_url": "", "api_key": "", "model": "", "updated_at": null},
    "qc":   {...},
    "chat": {...}
  },
  "migrated_from": "llm_config.json",   # 可选，记录首次自动迁移来源
  "updated_at": "2026-09-11T09:21:03"
}

设计约束：
- 三个模块完全独立：各自的 base_url / api_key / model、独立连通性测试、独立脱敏回显、独立保存 / 清空；
  质检模块不再复用文本分析模型的配置（LLM 可能只配了纯文本模型，无法做视觉质检）。
- api_key 明文只落盘，永不回显（对外只给 api_key_masked + has_api_key）。
- 保存时 api_key 留空，或提交回的是脱敏值（含 *），一律视为「不改动原密钥」。
- 兼容旧 llm_config.json：首次读取时若 text 模块为空而旧配置存在，自动迁移（不丢原有配置）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from llm_client import build_chat_url, mask_key
from llm_client import load_config as _load_legacy_config

logger = logging.getLogger(__name__)

# 三个独立模块的键（顺序即前端展示顺序）
MODULES = ("text", "qc", "chat")

MODULE_META = {
    "text": {
        "label": "文本分析模型",
        "desc": "小说转剧本、剧本 / 提示词分析（纯文本能力即可）",
        "need_vision": False,
        "placeholder_model": "例如 deepseek-chat / qwen2.5:14b",
        "used_by": ["小说转剧本", "章节转剧本", "提示词分析", "剧本生成"],
    },
    "qc": {
        "label": "质检模型",
        "desc": "分镜图片 / 视频的视觉质检（需支持图像输入的多模态模型）",
        "need_vision": True,
        "placeholder_model": "例如 gpt-4o-mini / qwen-vl-max / glm-4v",
        "used_by": ["步骤5 分镜图片质检", "步骤6 视频质检"],
    },
    "chat": {
        "label": "对话总控模型",
        "desc": "AI 对话总控：通过多轮对话确定漫剧风格、题材、画风、角色等创作设定",
        "need_vision": False,
        "placeholder_model": "例如 deepseek-chat / gpt-4o",
        "used_by": ["AI 对话（创作总控）", "创作设定落盘"],
    },
}


# ===================== 基础结构 =====================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_module() -> dict:
    return {"base_url": "", "api_key": "", "model": "", "updated_at": None}


def _empty_config() -> dict:
    return {"version": 1, "modules": {m: _empty_module() for m in MODULES}, "updated_at": None}


def _normalize_module(raw) -> dict:
    cfg = _empty_module()
    if isinstance(raw, dict):
        for k in ("base_url", "api_key", "model", "updated_at"):
            if raw.get(k) is not None:
                cfg[k] = str(raw[k])
    return cfg


def module_meta() -> dict:
    """模块元信息（前端渲染用，不含任何密钥）"""
    return {m: dict(MODULE_META[m], key=m) for m in MODULES}


# ===================== 读写 / 迁移 =====================

def _read_file(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"AI 设置读取失败（按未配置处理）：{e}")
        return {}


def _write_file(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _legacy_migratable(legacy_path: str) -> dict:
    """旧 llm_config.json 若已配置完整，则作为迁移来源"""
    if not legacy_path or not os.path.isfile(legacy_path):
        return {}
    try:
        old = _load_legacy_config(legacy_path) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"旧 llm_config.json 读取失败（跳过迁移）：{e}")
        return {}
    if old.get("base_url") and old.get("api_key") and old.get("model"):
        return old
    return {}


def load_config(config_path: str, legacy_path: str = None) -> dict:
    """读取统一 AI 设置；首次读取且 text 模块为空时，自动从旧 llm_config.json 迁移并落盘。

    注意：迁移只做一次（迁移后 ai_config.json 中的 text 模块已有值），不会覆盖用户后续修改。
    """
    raw = _read_file(config_path)
    cfg = _empty_config()
    modules = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
    for m in MODULES:
        cfg["modules"][m] = _normalize_module(modules.get(m))
    if raw.get("migrated_from"):
        cfg["migrated_from"] = str(raw["migrated_from"])
    cfg["version"] = int(raw.get("version") or 1)
    cfg["updated_at"] = raw.get("updated_at")

    text = cfg["modules"]["text"]
    if not (text.get("base_url") and text.get("model")):
        legacy = _legacy_migratable(legacy_path)
        if legacy:
            text["base_url"] = (legacy.get("base_url") or "").strip()
            text["api_key"] = (legacy.get("api_key") or "").strip()
            text["model"] = (legacy.get("model") or "").strip()
            text["updated_at"] = legacy.get("updated_at") or _now()
            cfg["migrated_from"] = os.path.basename(legacy_path) if legacy_path else "llm_config.json"
            cfg["updated_at"] = _now()
            try:
                _write_file(config_path, cfg)
                logger.info(f"已从 {cfg['migrated_from']} 迁移文本分析模型配置到 {config_path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"AI 设置迁移落盘失败（内存中已生效）：{e}")
    return cfg


def get_module(cfg: dict, module: str) -> dict:
    """取某个模块的配置（含明文 api_key，仅供后端调用使用，切勿直接回传前端）"""
    if module not in MODULES:
        raise ValueError(f"未知的 AI 模块：{module}")
    modules = (cfg or {}).get("modules") or {}
    return _normalize_module(modules.get(module))


def save_module(config_path: str, module: str, base_url: str = None, model: str = None,
                api_key: str = None, legacy_path: str = None) -> dict:
    """保存单个模块：api_key 为 None / 空 / 含 * 的脱敏值时视为「不改动原密钥」"""
    if module not in MODULES:
        raise ValueError(f"未知的 AI 模块：{module}")
    cfg = load_config(config_path, legacy_path)
    ep = cfg["modules"][module]
    if base_url is not None:
        ep["base_url"] = (base_url or "").strip()
    if model is not None:
        ep["model"] = (model or "").strip()
    key = "" if api_key is None else str(api_key).strip()
    if key and "*" not in key:
        ep["api_key"] = key
    ep["updated_at"] = _now()
    cfg["updated_at"] = _now()
    _write_file(config_path, cfg)
    return cfg


def clear_module(config_path: str, module: str = None, legacy_path: str = None) -> dict:
    """清空单个模块；module 为空则清空全部三个模块（整体重置）"""
    cfg = load_config(config_path, legacy_path)
    if module:
        if module not in MODULES:
            raise ValueError(f"未知的 AI 模块：{module}")
        cfg["modules"][module] = _empty_module()
    else:
        cfg = _empty_config()
    cfg["updated_at"] = _now()
    _write_file(config_path, cfg)
    return cfg


# ===================== 对外视图 =====================

def module_public_view(ep: dict) -> dict:
    """单个模块的对外视图（绝不含 api_key 明文）"""
    key = (ep or {}).get("api_key") or ""
    base_url = (ep or {}).get("base_url") or ""
    model = (ep or {}).get("model") or ""
    return {
        "configured": bool(base_url and key and model),
        "ready": bool(base_url and key and model),
        "has_api_key": bool(key),
        "base_url": base_url,
        "model": model,
        "api_key_masked": mask_key(key),
        "chat_url": build_chat_url(base_url) if base_url else "",
        "updated_at": (ep or {}).get("updated_at"),
    }


def public_view(cfg: dict) -> dict:
    """整体视图：三个模块各自的脱敏配置 + 元信息"""
    modules = {}
    for m in MODULES:
        view = module_public_view(get_module(cfg, m))
        view.update({"key": m, "label": MODULE_META[m]["label"], "desc": MODULE_META[m]["desc"],
                     "need_vision": MODULE_META[m]["need_vision"],
                     "placeholder_model": MODULE_META[m]["placeholder_model"],
                     "used_by": MODULE_META[m]["used_by"]})
        modules[m] = view
    return {
        "modules": modules,
        "module_order": list(MODULES),
        "migrated_from": (cfg or {}).get("migrated_from"),
        "updated_at": (cfg or {}).get("updated_at"),
    }
