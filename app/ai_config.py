"""
统一「AI 设置」配置管理 —— 文本分析 / 质检 / 对话总控 三个相互独立的模型模块

配置文件：项目根目录 ai_config.json
{
  "version": 1,
  "modules": {
    "text": {"base_url": "", "api_key": "", "model": "", "reasoning_effort": "", "updated_at": null},
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
- reasoning_effort（思考档位）只对「思考不可关闭」的模型有意义（如 GLM-5.3-Flash）：
  留空 = 不注入该参数，由服务端取默认档；low / high / max 为合法档位。
  它与 llm_client 的 disable_thinking 是**互斥的两套机制**：一旦设了档位，就不再尝试关思考。
- 兼容旧 llm_config.json：首次读取时若 text 模块为空而旧配置存在，自动迁移（不丢原有配置）。
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
from datetime import datetime

from llm_client import build_chat_url, mask_key
from llm_client import load_config as _load_legacy_config
from llm_client import REASONING_EFFORT_LEVELS
import secret_store

logger = logging.getLogger(__name__)

# 项目根目录（用于定位加密密钥库 output/secrets.enc 与主密钥 .secret_key）
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _store():
    return secret_store.get_store(_ROOT_DIR)


# =====================================================================
# 并发保护（审计 S8）
# ---------------------------------------------------------------------
# `save_module` / `clear_module` / `load_config` 的首次迁移都是
# 「load_config → 改内存 → _write_file」的读改写，此前**零锁**。
# 典型症状：快速连续保存「文本分析」和「质检」两个模块，后者用旧快照覆盖前者 ——
# 用户看到「保存成功了但没生效」。另外 `_write_file` 曾用固定的 `.tmp` 名，
# 并发写者会互相截断对方写了一半的临时文件，`os.replace` 发布出**损坏的 JSON**
# （而 `_read_file` 解析失败只告警、返回 {}）→ 表现为「AI 设置全空、密钥丢失」。
# =====================================================================

_CONFIG_LOCK = threading.RLock()


def _locked(fn):
    """把读改写放回同一把可重入锁的临界区（函数级装饰，签名/文档不变）"""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _CONFIG_LOCK:
            return fn(*args, **kwargs)
    return _wrapper

# 三个独立模块的键（顺序即前端展示顺序）
MODULES = ("text", "qc", "chat")

MODULE_META = {
    "text": {
        "label": "文本分析模型（= LLM 引擎）",
        "desc": "就是 LLM 引擎：小说转剧本、章节转剧本、提示词分析等所有纯文本任务都用这一份配置",
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
    return {"base_url": "", "api_key": "", "model": "",
            "reasoning_effort": "", "updated_at": None}


def _empty_config() -> dict:
    return {"version": 1, "modules": {m: _empty_module() for m in MODULES}, "updated_at": None}


def normalize_reasoning_effort(value) -> str:
    """思考档位归一化：合法值原样返回（小写），非法/空值一律返回 ""（=不注入任何档位参数）

    ⚠️ 必须在这里就把非法值挡掉。GLM-5.3 这类模型会把**非法档位静默解析成最高档（最贵）**，
    如果让用户的笔误原样发给服务端，就等于悄悄按 max 档烧 token。
    """
    v = str(value or "").strip().lower()
    return v if v in REASONING_EFFORT_LEVELS else ""


def _normalize_module(raw) -> dict:
    cfg = _empty_module()
    if isinstance(raw, dict):
        for k in ("base_url", "api_key", "model", "updated_at"):
            if raw.get(k) is not None:
                cfg[k] = str(raw[k])
        cfg["reasoning_effort"] = normalize_reasoning_effort(raw.get("reasoning_effort"))
    return cfg


def module_meta() -> dict:
    """模块元信息（前端渲染用，不含任何密钥）"""
    return {m: dict(MODULE_META[m], key=m) for m in MODULES}


# ===================== 读写 / 迁移 =====================

def _read_file(path: str) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    # ⚠️ Windows 上 `_atomic_replace` 换目录项的瞬间，另一线程 open() 会抛
    #    PermissionError（暂时拿不到 ≠ 文件坏了）。旧实现直接当读失败 → 返回 {}，
    #    表现为「AI 设置全空 / 密钥丢失」，刷新又回来。这里退避重试。
    err = None
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except PermissionError as e:
            err = e
            time.sleep(0.02 * (i + 1))
        except Exception as e:  # noqa: BLE001
            err = e
            break
    logger.warning(f"AI 设置读取失败（按未配置处理）：{err}")
    return {}


def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试。

    ⚠️ Windows 上若目标文件正被另一个线程/进程打开读取（例如 `load_index` 刚读完、
    句柄尚未释放），`os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这属于「暂时拿不到」而不是「文件坏了」：直接失败会让新建项目/保存设置偶发 500
    （实测并发读写时必现）。退避重试即可。
    """
    last = None
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:        # WinError 5 / 32：目标被占用
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


def _write_file(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # ⚠️ 临时名必须每次唯一（固定 `.tmp` 会被并发写者互相截断，见文件头并发注释）
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError as e:
            logger.debug("清理临时文件失败（忽略）：%s", e)
        raise


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


@_locked
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

    # P0-3：若 json 中仍残留明文密钥，自动迁移到加密库并清空 json 字段（只做一次）
    if _has_plaintext_key(raw):
        try:
            secret_store.scrub_plaintext_key(config_path, "llm", _ROOT_DIR)
            raw2 = _read_file(config_path)
            modules2 = raw2.get("modules") if isinstance(raw2.get("modules"), dict) else {}
            for m in MODULES:
                cfg["modules"][m] = _normalize_module(modules2.get(m))
            cfg["updated_at"] = raw2.get("updated_at") or cfg["updated_at"]
            logger.info(f"{os.path.basename(config_path)} 中的明文密钥已迁移至加密库")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"明文密钥迁移失败（暂不阻断）：{e}")

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
    """取某个模块的配置（含明文 api_key，仅供后端调用使用，切勿直接回传前端）

    密钥取值优先级：环境变量（MJSCXT_API_KEY_TEXT/QC/CHAT）> 本地加密库 > json 明文（遗留）。
    base_url / model 同样支持环境变量覆盖（MJSCXT_BASE_URL_* / MJSCXT_MODEL_*）。
    """
    if module not in MODULES:
        raise ValueError(f"未知的 AI 模块：{module}")
    modules = (cfg or {}).get("modules") or {}
    ep = _normalize_module(modules.get(module))
    ns = f"ai.{module}"
    # ⭐ 单一事实源：DB（ai_credentials 表）优先。get_credentials 内部已按
    # env > DB 解析，直接信任其结果；DB 有非空字段就覆盖 json 值。
    try:
        import ai_credentials_db
        db_ep = ai_credentials_db.get_credentials(module)
        if db_ep.get("base_url"):
            ep["base_url"] = db_ep["base_url"]
        if db_ep.get("model"):
            ep["model"] = db_ep["model"]
        if db_ep.get("api_key"):
            ep["api_key"] = db_ep["api_key"]
        if db_ep.get("reasoning_effort"):
            ep["reasoning_effort"] = db_ep["reasoning_effort"]
        return ep
    except Exception as e:  # noqa: BLE001  DB 不可用回落旧口径（json + 加密库槽 + env）
        logger.warning(f"AI 凭证 DB 读取失败，回落加密库/env：{e}")
    # 回落：非密钥字段环境变量优先
    env_base = secret_store.SecretStore.env_base_url(ns)
    env_model = secret_store.SecretStore.env_model(ns)
    if env_base:
        ep["base_url"] = env_base
    if env_model:
        ep["model"] = env_model
    # 密钥：环境变量 > 加密库 > json 明文
    secure = _store().get_api_key(ns)
    if secure:
        ep["api_key"] = secure
    return ep


@_locked
def save_module(config_path: str, module: str, base_url: str = None, model: str = None,
                api_key: str = None, legacy_path: str = None,
                reasoning_effort: str = None) -> dict:
    """保存单个模块。

    密钥处理（P0-3 加固）：
    - api_key 为 None / 空 / 含 * 的脱敏值时，视为「不改动原密钥」；
    - 有效新密钥写入**加密库**（output/secrets.enc），json 中该字段恒为空字符串；
    - 加密不可用时（未装 cryptography），拒绝保存明文并抛 ValueError，
      提示改用环境变量 MJSCXT_API_KEY_TEXT/QC/CHAT。

    reasoning_effort（思考档位，思考不可关闭的模型如 GLM-5.3-Flash 用）：
    - None = 不改动；"" = 清空（回到「不注入档位、由服务端默认」）；low/high/max = 设定档位。
    - 非法值**不报错但会被归一化成 ""**，避免把笔误原样发给服务端（部分模型会把非法值
      静默解析成最高档，即最贵的那档）。
    """
    if module not in MODULES:
        raise ValueError(f"未知的 AI 模块：{module}")
    cfg = load_config(config_path, legacy_path)
    ep = cfg["modules"][module]
    if base_url is not None:
        ep["base_url"] = (base_url or "").strip()
    if model is not None:
        ep["model"] = (model or "").strip()
    if reasoning_effort is not None:
        ep["reasoning_effort"] = normalize_reasoning_effort(reasoning_effort)
    key = "" if api_key is None else str(api_key).strip()
    if key and "*" not in key:
        if not _store().set_api_key(f"ai.{module}", key):
            raise ValueError(
                "密钥加密存储不可用（缺少 cryptography 或主密钥），已拒绝明文落盘。"
                "请安装 cryptography 后重试，或改用环境变量 "
                f"{secret_store.ENV_KEY_MAP.get(f'ai.{module}')} 配置密钥。")
    # json 中恒不保存明文密钥
    ep["api_key"] = ""
    ep["updated_at"] = _now()
    cfg["updated_at"] = _now()
    _write_file(config_path, cfg)
    return cfg


def _has_plaintext_key(raw: dict) -> bool:
    """判断配置里是否残留明文密钥（用于触发一次性迁移）"""
    if not isinstance(raw, dict):
        return False
    if isinstance(raw.get("api_key"), str) and raw["api_key"].strip():
        return True
    modules = raw.get("modules")
    if isinstance(modules, dict):
        for cfg in modules.values():
            if isinstance(cfg, dict) and isinstance(cfg.get("api_key"), str) and cfg["api_key"].strip():
                return True
    return False


@_locked
def clear_module(config_path: str, module: str = None, legacy_path: str = None) -> dict:
    """清空单个模块；module 为空则清空全部三个模块（整体重置）。同步清除加密库中的密钥。"""
    cfg = load_config(config_path, legacy_path)
    if module:
        if module not in MODULES:
            raise ValueError(f"未知的 AI 模块：{module}")
        cfg["modules"][module] = _empty_module()
        _store().clear_api_key(f"ai.{module}")
    else:
        cfg = _empty_config()
        for m in MODULES:
            _store().clear_api_key(f"ai.{m}")
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
        "reasoning_effort": normalize_reasoning_effort((ep or {}).get("reasoning_effort")),
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
        # 思考档位可选项（前端下拉）："" = 不注入（服务端默认）、其余为合法档位
        "reasoning_effort_options": ["", *REASONING_EFFORT_LEVELS],
    }
