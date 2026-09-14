"""
AI 质检模块（图片 / 视频）—— 可开关、可配置、不达标自动重生成

职责：
1) 质检配置持久化（qc_config.json）：总开关 / 图片·视频独立开关 / 质检独立接口（base_url / api_key / model，
   不复用文本分析 LLM 接口）/
   判定标准（合格线分数 + 自定义质检提示词）/ 最大重试次数 / 视频抽帧策略
2) 图片质检：把生成图交给「OpenAI 兼容 + 支持视觉输入」的多模态模型，返回
   {passed, score, reason, issues}
3) 视频质检：先用 ffmpeg 抽帧（首/中/尾等），再把多帧一并交给多模态模型判定
4) 质检与重试历史：output/qc/<项目>/<类型>_<镜头>.json（每次尝试一条记录，持续追加）

设计约束（重要）：
- 任何质检失败（未开启 / 未配置 / 网络异常 / 返回非法 JSON / ffmpeg 缺失）都不得抛异常打断生成流程，
  统一通过返回值里的 ok / skipped / error 字段表达。
- api_key 永不回显明文，只返回脱敏值。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# 项目根目录（定位加密密钥库 output/secrets.enc 与主密钥 .secret_key）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# ===================== 默认配置 =====================

# 质检判定口径：水印 / 角标 / 字幕 / 「AI 生成」标识一律不计为质量缺陷（B 项⑥）
# 该口径会追加到「默认提示词」与「用户自定义提示词」之后，保证历史配置同样生效。
WATERMARK_EXEMPT_NOTE = (
    "\n【判定口径·重要】画面中若仅存在水印、角标、logo、平台标识、字幕或「AI 生成」标识，"
    "一律不得视为质量缺陷，不得因此扣分，也不得据此判定不通过；"
    "请只针对画面本身的崩坏 / 畸变 / 糊化 / 闪烁 / 撕裂 / 一致性问题做判定。"
)

# 关键缺陷硬规则（P0：收紧放行）：模型偶尔给有明显崩坏的图打高分/放行，
# 因此在提示词里下达硬性规则，并在代码侧再加一道不可绕过的闸门（见 _finalize_verdict）。
CRITICAL_RULE_NOTE = (
    "\n【硬性判定规则·最高优先级】若画面存在下列任一关键缺陷：人物畸变/崩坏/五官错位、"
    "多手多脚/缺肢/手指异常、肢体错位或断裂、画面撕裂/闪烁/重影、严重糊化或大色块、"
    "黑屏/白屏、主体缺失或变形、与镜头描述明显不符 —— 必须输出 pass=false 且 score ≤ 50，"
    "并在 issues 中逐条写明。**禁止在存在上述缺陷时给出通过结论或高分**；"
    "宁可严格也不得放过有明显缺陷的画面。"
)

# 代码侧关键缺陷词表（命中即阻断，与模型分数无关）
# 注意：不含「水印/字幕/logo/文字」——这些按 WATERMARK_EXEMPT_NOTE 不计缺陷。
CRITICAL_ISSUE_KEYWORDS = (
    "畸变", "扭曲", "崩坏", "崩了", "脸部崩", "面部崩", "五官错位", "五官畸变", "五官错误",
    "多手", "多脚", "多头", "缺手", "缺脚", "断肢", "肢体错位", "肢体断裂", "手指异常", "六指",
    "融合", "粘连", "穿模", "黑屏", "白屏", "纯色块", "大色块", "严重糊", "糊化", "失焦",
    "撕裂", "闪烁", "重影", "鬼影", "变形", "比例失调", "主体缺失", "主体不完整", "残影",
    "拼接痕迹", "画面崩", "错位",
    # P0 独立复核补充（_qc_gate 侧会独立比对，命中即强制阻断，与模型 accepted 无关）：
    # 覆盖「画面崩坏 / 拼接 / 人物重复」三类高频漏检表述（更宽的表达形态）。
    "拼接", "画面异常", "人物重复", "重复人物", "人物多出", "多出人物", "多余人物",
    "人物多余", "人物重叠", "人物数量异常", "多个人物", "分身",
)


def find_critical_issues(issues) -> list:
    """从 issues 文本中筛出命中关键缺陷词的条目

    带否定语境过滤：命中词前 6 字内出现「无/没有/未/不存在/非/不」的（如"无明显畸变"）
    不视为关键缺陷，避免把「说明没有缺陷」的条目误判成阻断项。
    """
    hits: list = []
    for it in (issues or []):
        s = str(it)
        for k in CRITICAL_ISSUE_KEYWORDS:
            found = False
            start = 0
            while True:
                idx = s.find(k, start)
                if idx == -1:
                    break
                ctx = s[max(0, idx - 6):idx]
                if not any(neg in ctx for neg in ("无", "没有", "未", "不存在", "非")):
                    found = True
                    break
                start = idx + len(k)
            if found:
                hits.append(s[:200])
                break
    return hits


def _finalize_verdict(verdict: dict, pass_score: int) -> dict:
    """代码侧硬闸：单向收紧质检结论（只会把「通过」改成「不通过」，绝不反向放行）

    1) issues 命中关键缺陷 → passed=False、blocked=True、score 压到 ≤50；
    2) score < pass_score 时，即使模型给 pass=true 也强制 False；
    3) 补 accepted（= passed，供入库判断）与 blocked（关键缺陷阻断）字段。
    """
    issues = verdict.get("issues") or []
    hits = find_critical_issues(issues)
    score = verdict.get("score")
    blocked = False
    if hits:
        blocked = True
        verdict["passed"] = False
        if isinstance(score, int):
            verdict["score"] = min(score, 50)
    elif isinstance(score, int) and score < int(pass_score):
        verdict["passed"] = False
    verdict["critical_issues"] = hits
    verdict["blocked"] = blocked
    verdict["accepted"] = bool(verdict.get("passed"))
    if blocked:
        logger.warning(f"质检关键缺陷阻断（不通过）: {hits}")
    return verdict

DEFAULT_IMAGE_PROMPT = (
    "你是漫剧分镜图片质检员。请检查这张 AI 生成的分镜图是否达到可直接使用的标准：\n"
    "1) 人物：脸型/发型/服装/配饰与设定一致，无五官畸变、多手多脚、肢体错位；\n"
    "2) 画面：无严重糊化、噪点、色块、扭曲；\n"
    "3) 构图：主体完整清晰，场景与镜头描述相符。\n"
    "4) 水印/角标/logo/字幕即使存在，也不计入缺陷、不扣分。\n"
    "镜头信息：{shot_desc}\n"
    "请只输出一个 JSON 对象，不要任何解释文字，格式：\n"
    '{"score": 0-100 的整数, "pass": true 或 false, "reason": "一句话结论", "issues": ["具体问题1", "具体问题2"]}'
)

DEFAULT_VIDEO_PROMPT = (
    "你是漫剧视频质检员。下面按顺序给出同一镜头视频的若干抽帧图片（首帧/中间帧/尾帧）。\n"
    "请判断该视频片段是否达到可直接使用的标准：\n"
    "1) 画面：无严重闪烁、撕裂、糊化、崩坏或色块；\n"
    "2) 一致性：人物外观与场景在整段视频中保持稳定，无明显畸变；\n"
    "3) 内容：与镜头描述相符，主体清晰。\n"
    "4) 水印/角标/logo/字幕即使存在，也不计入缺陷、不扣分。\n"
    "镜头信息：{shot_desc}\n"
    "请只输出一个 JSON 对象，不要任何解释文字，格式：\n"
    '{"score": 0-100 的整数, "pass": true 或 false, "reason": "一句话结论", "issues": ["具体问题1", "具体问题2"]}'
)

CONFIG_KEYS = (
    "enabled", "image_enabled", "video_enabled",
    "base_url", "api_key", "model",
    "endpoint_override",   # 被其它模块（如分镜链路）自动写入的接口，记录以便「恢复为 AI 设置」
    "image_prompt", "video_prompt",
    "pass_score", "max_retries", "video_frame_count",
    "image_max_side", "timeout", "api_retries", "api_backoff", "updated_at",
)


def _empty_config() -> dict:
    return {
        "enabled": False,            # 质检总开关
        "image_enabled": True,       # 图片质检开关
        "video_enabled": True,       # 视频质检开关
        # 质检必须使用自己独立配置的 base_url / api_key / model（不再复用文本分析 LLM 接口）
        "base_url": "",
        "api_key": "",
        "model": "",
        "endpoint_override": {"base_url": "", "api_key": "", "model": ""},
        "image_prompt": DEFAULT_IMAGE_PROMPT,
        "video_prompt": DEFAULT_VIDEO_PROMPT,
        "pass_score": 70,            # 合格线（0-100），score >= pass_score 且 pass != false 视为达标
        "max_retries": 2,            # 不达标最大重试次数
        "video_frame_count": 3,      # 视频抽帧数量（1-6）
        "image_max_side": 1024,      # 送检前压缩的最长边（控制 token 与耗时）
        "timeout": 180,              # 单次质检请求读超时（秒）
        "api_retries": API_RETRY_ATTEMPTS,   # 网络层额外重试次数（瞬时故障时退避重试，与 max_retries 重画无关）
        "api_backoff": API_RETRY_BACKOFF,    # 网络重试退避基数（秒），按 2 的幂增长、单次上限见 API_RETRY_MAX_SLEEP
        "updated_at": None,
    }


def _normalize_override(raw) -> dict:
    if not isinstance(raw, dict):
        return {"base_url": "", "api_key": "", "model": ""}
    return {"base_url": (raw.get("base_url") or "").strip(),
            "api_key": (raw.get("api_key") or "").strip(),
            "model": (raw.get("model") or "").strip()}


# ===================== 配置读写 =====================

def load_config(config_path: str) -> dict:
    cfg = _empty_config()
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            for k in CONFIG_KEYS:
                if k == "endpoint_override":
                    continue
                if k in data and data[k] is not None:
                    cfg[k] = data[k]
            cfg["endpoint_override"] = _normalize_override(data.get("endpoint_override"))
            # P0-3：明文密钥自动迁移到加密库并清空 json 字段（只做一次）
            if data.get("api_key"):
                try:
                    import secret_store
                    _root = _PROJECT_ROOT
                    if secret_store.get_store(_root).set_api_key("qc", str(data["api_key"]).strip()):
                        data["api_key"] = ""
                        tmp = config_path + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        os.replace(tmp, config_path)
                        logger.info("质检配置中的明文密钥已迁移至加密库")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"质检密钥迁移失败（暂不阻断）：{e}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"质检配置读取失败（按默认值处理）：{e}")
    # 密钥取值：环境变量 > 加密库 > json（迁移后应为空）
    try:
        import secret_store
        secure = secret_store.get_store(_PROJECT_ROOT).get_api_key("qc")
        if secure:
            cfg["api_key"] = secure
        env_base = secret_store.SecretStore.env_base_url("qc")
        env_model = secret_store.SecretStore.env_model("qc")
        if env_base:
            cfg["base_url"] = env_base
        if env_model:
            cfg["model"] = env_model
    except Exception as e:  # noqa: BLE001
        logger.warning(f"质检密钥读取异常（回退 json）：{e}")
    # 类型兜底
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["image_enabled"] = bool(cfg.get("image_enabled", True))
    cfg["video_enabled"] = bool(cfg.get("video_enabled", True))
    try:
        cfg["pass_score"] = max(0, min(100, int(cfg.get("pass_score", 70))))
    except Exception:  # noqa: BLE001
        cfg["pass_score"] = 70
    try:
        cfg["max_retries"] = max(0, min(10, int(cfg.get("max_retries", 2))))
    except Exception:  # noqa: BLE001
        cfg["max_retries"] = 2
    try:
        cfg["video_frame_count"] = max(1, min(6, int(cfg.get("video_frame_count", 3))))
    except Exception:  # noqa: BLE001
        cfg["video_frame_count"] = 3
    try:
        cfg["image_max_side"] = max(256, min(2048, int(cfg.get("image_max_side", 1024))))
    except Exception:  # noqa: BLE001
        cfg["image_max_side"] = 1024
    try:
        cfg["timeout"] = max(10, min(900, int(cfg.get("timeout", 180))))
    except Exception:  # noqa: BLE001
        cfg["timeout"] = 180
    try:
        cfg["api_retries"] = max(0, min(5, int(cfg.get("api_retries", API_RETRY_ATTEMPTS))))
    except Exception:  # noqa: BLE001
        cfg["api_retries"] = API_RETRY_ATTEMPTS
    try:
        cfg["api_backoff"] = max(0.0, min(30.0, float(cfg.get("api_backoff", API_RETRY_BACKOFF))))
    except Exception:  # noqa: BLE001
        cfg["api_backoff"] = API_RETRY_BACKOFF
    return cfg


def save_config(config_path: str, patch: dict, keep_key_if_blank: bool = True) -> dict:
    cfg = load_config(config_path)
    for k, v in (patch or {}).items():
        if k not in CONFIG_KEYS or k == "updated_at":
            continue
        if k == "endpoint_override":
            cfg["endpoint_override"] = _normalize_override(v)
            continue
        if k == "api_key":
            if v is None:
                continue
            v = str(v).strip()
            if not v and keep_key_if_blank:
                continue          # 留空 = 不改动已保存密钥
            if v and set(v) == {"*"}:
                continue          # 误提交脱敏值，按不改动处理
            if v and "*" in v:
                continue          # 脱敏回显值（如 sk-a******wxyz），按不改动处理
            # P0-3：有效新密钥写入加密库，json 中不落明文
            try:
                import secret_store
                if not secret_store.get_store(_PROJECT_ROOT).set_api_key("qc", v):
                    raise RuntimeError("密钥加密存储不可用")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"质检密钥加密保存失败：{e}")
                raise ValueError(
                    "质检密钥加密存储不可用（缺少 cryptography 或主密钥），已拒绝明文落盘。"
                    f"请安装 cryptography 后重试，或改用环境变量 "
                    f"{secret_store.ENV_KEY_MAP.get('qc', 'MJSCXT_API_KEY_QC')} 配置密钥。")
            cfg["api_key"] = ""
            continue
        if k in ("enabled", "image_enabled", "video_enabled"):
            cfg[k] = bool(v)
        elif k in ("pass_score", "max_retries", "video_frame_count", "image_max_side",
                   "timeout", "api_retries"):
            try:
                cfg[k] = int(v)
            except Exception:  # noqa: BLE001
                continue
        elif k == "api_backoff":
            try:
                cfg[k] = float(v)
            except Exception:  # noqa: BLE001
                continue
        else:
            cfg[k] = str(v or "")
    cfg = load_config_dict(cfg)
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return cfg


def load_config_dict(raw: dict) -> dict:
    """把内存字典按 load_config 的同一套规则规范化（供 save / 测试复用）"""
    base = _empty_config()
    base.update({k: v for k, v in (raw or {}).items()
                 if k in CONFIG_KEYS and k not in ("endpoint_override",)})
    base["endpoint_override"] = _normalize_override((raw or {}).get("endpoint_override"))
    return _normalize(base)


def _normalize(cfg: dict) -> dict:
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["image_enabled"] = bool(cfg.get("image_enabled", True))
    cfg["video_enabled"] = bool(cfg.get("video_enabled", True))
    cfg["endpoint_override"] = _normalize_override(cfg.get("endpoint_override"))
    for key, default, lo, hi in (("pass_score", 70, 0, 100), ("max_retries", 2, 0, 10),
                                 ("video_frame_count", 3, 1, 6), ("image_max_side", 1024, 256, 2048),
                                 ("timeout", 180, 10, 900),
                                 ("api_retries", API_RETRY_ATTEMPTS, 0, 5)):
        try:
            cfg[key] = max(lo, min(hi, int(cfg.get(key, default))))
        except Exception:  # noqa: BLE001
            cfg[key] = default
    try:
        cfg["api_backoff"] = max(0.0, min(30.0, float(cfg.get("api_backoff", API_RETRY_BACKOFF))))
    except Exception:  # noqa: BLE001
        cfg["api_backoff"] = API_RETRY_BACKOFF
    return cfg


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def resolve_endpoint(cfg: dict, override: dict = None) -> dict:
    """生效的质检接口：只认质检自己的独立配置（base_url / api_key / model）。

    - 质检不再复用文本分析 LLM 接口（LLM 可能只配了纯文本模型，无法做视觉质检）。
    - override：可选的临时覆盖（仅用于页面「测试连通性」传暂存参数，不落盘）。
    - 若其它链路曾自动写入接口（endpoint_override 有值且与当前一致），标记为「自动写入」。
    """
    ov = _normalize_override(override) if override else {"base_url": "", "api_key": "", "model": ""}
    saved = _normalize_override(cfg.get("endpoint_override"))
    if ov["base_url"] and ov["api_key"] and ov["model"]:
        return {**ov, "source": "test_override",
                "auto_synced": bool(saved["base_url"]) and saved["base_url"] == ov["base_url"]}
    ep = {"base_url": (cfg.get("base_url") or "").strip(),
          "api_key": (cfg.get("api_key") or "").strip(),
          "model": (cfg.get("model") or "").strip()}
    auto = bool(saved["base_url"] and saved["base_url"] == ep["base_url"]
                and saved["api_key"] and saved["api_key"] == ep["api_key"]
                and saved["model"] and saved["model"] == ep["model"])
    return {**ep, "source": "qc_config", "auto_synced": auto}


def qc_endpoint_ready(cfg: dict, override: dict = None) -> bool:
    ep = resolve_endpoint(cfg, override)
    return bool(ep["base_url"] and ep["api_key"] and ep["model"])


def set_endpoint(config_path: str, base_url: str, api_key: str, model: str) -> dict:
    """被其它链路（如分镜加速自动写入）调用的质检接口同步：落盘并记录 endpoint_override"""
    cfg = load_config(config_path)
    ep = {"base_url": (base_url or "").strip(), "api_key": (api_key or "").strip(),
          "model": (model or "").strip()}
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return cfg
    cfg["base_url"], cfg["api_key"], cfg["model"] = ep["base_url"], ep["api_key"], ep["model"]
    cfg["endpoint_override"] = dict(ep)
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return cfg


def reset_endpoint(config_path: str) -> dict:
    """「恢复为 AI 设置」：清空自动写入的接口，交还给用户在 AI 设置里独立配置"""
    cfg = load_config(config_path)
    cfg["base_url"], cfg["api_key"], cfg["model"] = "", "", ""
    cfg["endpoint_override"] = {"base_url": "", "api_key": "", "model": ""}
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return cfg


def public_view(cfg: dict) -> dict:
    ep = resolve_endpoint(cfg)
    ready = bool(cfg.get("enabled") and ep["base_url"] and ep["api_key"] and ep["model"])
    view = {k: v for k, v in cfg.items() if k != "api_key"}
    view.update({
        "has_api_key": bool(cfg.get("api_key")),
        "api_key_masked": mask_key(cfg.get("api_key") or ""),
        "effective_base_url": ep["base_url"],
        "effective_model": ep["model"],
        "endpoint_source": ep["source"],
        "endpoint_auto_synced": ep.get("auto_synced", False),
        "ready": ready,
        "image_qc_active": bool(cfg.get("enabled") and cfg.get("image_enabled") and ep["base_url"] and ep["api_key"] and ep["model"]),
        "video_qc_active": bool(cfg.get("enabled") and cfg.get("video_enabled") and ep["base_url"] and ep["api_key"] and ep["model"]),
        "default_image_prompt": DEFAULT_IMAGE_PROMPT,
        "default_video_prompt": DEFAULT_VIDEO_PROMPT,
    })
    return view


def clear_config(config_path: str) -> dict:
    """清空配置：同时重置开关与独立接口（不含 prompt / 阈值以外的残留）"""
    cfg = _empty_config()
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ===================== 开关判定（生成流程调用） =====================

def image_qc_ready(cfg: dict, override: dict = None) -> bool:
    return bool(cfg.get("enabled") and cfg.get("image_enabled")
                and qc_endpoint_ready(cfg, override))


def video_qc_ready(cfg: dict, override: dict = None) -> bool:
    return bool(cfg.get("enabled") and cfg.get("video_enabled")
                and qc_endpoint_ready(cfg, override))


# ===================== 网络层重试与退避（仅针对瞬时故障） =====================
# 说明：这里处理的是「同一次质检请求」内部的网络重试，与「不达标重生成」
# （qc_cfg.max_retries，换 seed 重画）是两件事。
# 目的：外部质检服务偶发 read timeout / HTTP 520 / 空返回时，先在网络层做有限次
#       重试 + 指数退避，避免一次瞬时抖动就让资产被判为「质检调用异常」而阻断入库。
# 语义不变：重试全部失败后仍按原逻辑阻断（不静默放行、不跳过质检）。

API_RETRY_ATTEMPTS = 2      # 额外重试次数（总尝试 = 1 + 2），可被 qc_config.json 的 api_retries 覆盖
API_RETRY_BACKOFF = 1.5     # 退避基数（秒）：第 n 次重试等待 base * 2^(n-1)，可被 api_backoff 覆盖
API_RETRY_MAX_SLEEP = 8.0   # 单次退避等待上限（秒）
API_CONNECT_TIMEOUT = 10    # 连接超时（秒）；读超时沿用配置里的 timeout

# 可重试的 HTTP 状态码：限流 / 网关与上游瞬时故障（含 Cloudflare 系列 5xx）
RETRYABLE_HTTP_STATUS = (408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524)
# 明确不可重试的状态码：请求本身 / 鉴权 / 配置类错误，重试无意义
FATAL_HTTP_STATUS = (400, 401, 403, 404, 405, 413, 415, 422)


class QcApiError(RuntimeError):
    """质检接口调用错误。

    retryable=True 表示属瞬时故障（超时 / 可重试 5xx / 空返回 / 非 JSON），值得重试；
    retryable=False 表示请求或配置本身有问题，重试无意义。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int = None,
                 retry_after: float = None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status = status
        self.retry_after = retry_after
        self.attempts = 1
        self.retry_errors: list = []

    def with_attempts(self, attempts: int, retry_errors: list) -> "QcApiError":
        """补上重试统计后的最终错误（供上层留痕与展示：共尝试几次、失败原因）"""
        self.attempts = int(attempts)
        self.retry_errors = list(retry_errors or [])
        if self.attempts > 1:
            self.args = (f"{self.args[0]}（共尝试 {self.attempts} 次，"
                         f"含重试 {self.attempts - 1} 次）",)
        return self


# ===================== HTTP（OpenAI 兼容视觉输入） =====================

def _chat_url(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    path = urlparse(u).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return u
    if path in ("", "/"):
        return f"{u}/v1/chat/completions"
    return f"{u}/chat/completions"


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or host.endswith(".local")


def encode_image_data_url(path: str, max_side: int = 1024) -> str:
    """读取本地图片 → 压缩 → data:image/jpeg;base64,...（控制 token 与带宽）"""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, float(max_side) / max(w, h)) if max(w, h) else 1.0
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        raw = buf.getvalue()
        mime = "image/jpeg"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片压缩失败，按原图送检：{e}")
        ext = os.path.splitext(path)[1].lower()
        mime = {"png": "image/png", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _post_chat_once(ep: dict, payload: dict, timeout: int) -> dict:
    """单次请求（不做重试）。失败时抛 QcApiError，并标注该错误是否值得重试。"""
    url = _chat_url(ep["base_url"])
    session = requests.Session()
    if _is_local(url):
        session.trust_env = False
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {ep['api_key']}"}
    t0 = time.time()
    try:
        resp = session.post(url, headers=headers, json=payload,
                            timeout=(min(API_CONNECT_TIMEOUT, timeout), timeout))
    except requests.exceptions.Timeout as e:
        raise QcApiError(f"质检接口请求超时（连接 {min(API_CONNECT_TIMEOUT, timeout)}s / "
                         f"读取 {timeout}s）：{e}", retryable=True) from e
    except requests.exceptions.RequestException as e:
        # 连接被重置、TLS 抖动、分块传输中断等，均属瞬时故障
        raise QcApiError(f"质检接口网络异常：{e}", retryable=True) from e
    latency = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        if resp.status_code in RETRYABLE_HTTP_STATUS:
            retry_after = None
            try:
                ra = (resp.headers.get("Retry-After") or "").strip()
                if ra:
                    retry_after = float(ra)
            except Exception:  # noqa: BLE001
                retry_after = None
            raise QcApiError(f"质检接口 HTTP {resp.status_code}：{body}", retryable=True,
                             status=resp.status_code, retry_after=retry_after)
        raise QcApiError(f"质检接口 HTTP {resp.status_code}：{body}",
                         retryable=resp.status_code not in FATAL_HTTP_STATUS,
                         status=resp.status_code)
    try:
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        raise QcApiError(f"质检接口返回非 JSON：{(resp.text or '')[:200]}",
                         retryable=True) from e
    choices = data.get("choices") or []
    if not choices:
        raise QcApiError(f"质检接口返回缺少 choices：{json.dumps(data, ensure_ascii=False)[:200]}",
                         retryable=True)
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content:
        content = choices[0].get("text") or ""
    if not content:
        raise QcApiError("质检接口返回内容为空", retryable=True)
    return {"content": str(content), "latency_ms": latency, "url": url}


def _post_chat(ep: dict, payload: dict, timeout: int, retries: int = None,
               backoff: float = None) -> dict:
    """带重试与指数退避的请求：仅对瞬时故障重试，不可重试错误立即抛出。

    返回体额外带 attempts（实际尝试次数）与 retry_errors（此前失败原因），供留痕。
    """
    total = 1 + max(0, int(API_RETRY_ATTEMPTS if retries is None else retries))
    base = API_RETRY_BACKOFF if backoff is None else max(0.0, float(backoff))
    errors: list = []
    for i in range(total):
        try:
            r = _post_chat_once(ep, payload, timeout)
            r["attempts"] = i + 1
            r["retry_errors"] = errors
            if errors:
                logger.warning(f"质检接口重试成功：共尝试 {i + 1} 次，此前失败：{errors[:2]}")
            return r
        except QcApiError as e:
            last = (i >= total - 1)
            if not e.retryable or last:
                raise e.with_attempts(i + 1, errors)
            wait = e.retry_after if (e.retry_after and e.retry_after > 0) else base * (2 ** i)
            wait = min(float(wait), API_RETRY_MAX_SLEEP)
            errors.append(str(e)[:200])
            logger.warning(f"质检接口瞬时故障（第 {i + 1}/{total} 次尝试失败）：{e}；"
                           f"{wait:.1f}s 后重试")
            if wait > 0:
                time.sleep(wait)
    raise RuntimeError("质检接口重试流程异常结束")  # 理论不可达


# 1x1 PNG（视觉连通性探测用，避免依赖本地文件）
_PROBE_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AGtDQAAAABJRU5ErkJggg==")


def test_vision(ep: dict, timeout: int = 60) -> dict:
    """视觉连通性探测：用一张极小图片请求一次，确认该接口 / 模型支持图像输入。

    永不抛异常：失败时返回 success=False + error。
    """
    if not (ep.get("base_url") and ep.get("api_key") and ep.get("model")):
        return {"success": False, "vision": False,
                "error": "接口未配置（base_url / api_key / model 均为必填）"}
    content = [
        {"type": "text", "text": "这是一张测试图片，请只回复：OK"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _PROBE_PNG_B64}},
    ]
    payload = {"model": ep["model"],
               "messages": [{"role": "user", "content": content}],
               "temperature": 0, "max_tokens": 16, "stream": False}
    try:
        r = _post_chat(ep, payload, timeout, retries=1, backoff=1.0)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "vision": False, "error": str(e),
                "attempts": getattr(e, "attempts", 1)}
    return {"success": True, "vision": True, "latency_ms": r["latency_ms"],
            "url": r["url"], "reply": (r.get("content") or "")[:100],
            "attempts": int(r.get("attempts", 1)),
            "retries_used": max(0, int(r.get("attempts", 1)) - 1)}


def parse_verdict(content: str, pass_score: int) -> dict:
    text = (content or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if m:
            text = m.group(1).strip()
    obj = None
    try:
        obj = json.loads(text)
    except Exception:  # noqa: BLE001
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            try:
                obj = json.loads(re.sub(r",\s*([}\]])", r"\1", text[s:e + 1]))
            except Exception:  # noqa: BLE001
                obj = None
    if not isinstance(obj, dict):
        raise RuntimeError(f"质检结论无法解析为 JSON：{text[:200]}")
    score = obj.get("score")
    try:
        score = int(round(float(score)))
    except Exception:  # noqa: BLE001
        score = None
    passed = obj.get("pass")
    if passed is None:
        passed = obj.get("passed")
    if isinstance(passed, str):
        passed = passed.strip().lower() in ("true", "yes", "1", "pass", "达标", "合格")
    if passed is None:
        passed = (score is not None and score >= pass_score)
    issues = obj.get("issues") or []
    if isinstance(issues, str):
        issues = [issues]
    # P0：代码侧硬闸（关键缺陷阻断 + 分数不达标强制不通过），只收紧不放宽
    return _finalize_verdict({
        "passed": bool(passed) and (score is None or score >= pass_score),
        "score": score,
        "reason": str(obj.get("reason") or obj.get("comment") or "")[:500],
        "issues": [str(x)[:200] for x in issues][:6],
        "raw": text[:1000],
    }, pass_score)


def _run_vision(ep: dict, prompt: str, image_paths: list, cfg: dict) -> dict:
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url",
                        "image_url": {"url": encode_image_data_url(p, cfg.get("image_max_side", 1024))}})
    payload = {
        "model": ep["model"],
        "messages": [
            {"role": "system", "content": "你是严格、客观的漫剧内容质检员，只输出 JSON。"},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
    }
    t0 = time.time()
    resp = _post_chat(ep, payload, cfg.get("timeout", 180),
                      retries=cfg.get("api_retries", API_RETRY_ATTEMPTS),
                      backoff=cfg.get("api_backoff", API_RETRY_BACKOFF))
    verdict = parse_verdict(resp["content"], cfg.get("pass_score", 70))
    verdict.update({"ok": True, "skipped": False, "latency_ms": resp["latency_ms"],
                    "api_total_ms": int((time.time() - t0) * 1000),
                    "api_attempts": int(resp.get("attempts", 1)),
                    "retries_used": max(0, int(resp.get("attempts", 1)) - 1),
                    "call_url": resp["url"], "model": ep["model"]})
    return verdict


# ===================== 图片质检 =====================

def check_image(image_path: str, shot_desc: str = "", cfg: dict = None,
                override: dict = None) -> dict:
    """单张图片质检。永不抛异常：失败时返回 ok=False 并带 error。
    override 仅用于「测试连通性」临时传参，不落盘。"""
    cfg = cfg or _empty_config()
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "质检总开关未开启"}
    if not cfg.get("image_enabled"):
        return {"ok": False, "skipped": True, "reason": "图片质检开关未开启"}
    ep = resolve_endpoint(cfg, override)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {"ok": False, "skipped": True, "reason": "质检接口未配置（base_url/api_key/model）"}
    if not image_path or not os.path.isfile(image_path):
        return {"ok": False, "skipped": False, "error": f"图片不存在：{image_path}"}
    prompt = (cfg.get("image_prompt") or DEFAULT_IMAGE_PROMPT).replace(
        "{shot_desc}", shot_desc or "（无）").replace("{pass_score}", str(cfg.get("pass_score", 70)))
    prompt = prompt + WATERMARK_EXEMPT_NOTE + CRITICAL_RULE_NOTE   # P0：收紧放行（关键缺陷必须不通过）
    try:
        return _run_vision(ep, prompt, [image_path], cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片质检调用失败：{e}")
        return {"ok": False, "skipped": False, "error": str(e),
                "api_attempts": getattr(e, "attempts", 1),
                "retryable": getattr(e, "retryable", None)}


# ===================== 视频质检（ffmpeg 抽帧） =====================

def _decode_io(raw) -> str:
    """subprocess 原始字节 → 文本。

    Windows 下 text=True 会按 locale(cp936) 解码 ffmpeg/ffprobe 的 UTF-8 输出，
    遇到非 GBK 字节会抛 UnicodeDecodeError 被外层 except 吞掉，导致帧率/时长静默解析为 0。
    因此统一走「UTF-8 + replace」解码。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


def ffmpeg_exe() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_exe() -> str:
    return shutil.which("ffprobe") or ""


def _ffmpeg_stream_info(video_path: str) -> dict:
    """解析 ffmpeg -i 输出里的 时长 / 帧率（不依赖 ffprobe）"""
    info = {"fps": 0.0, "duration": 0.0}
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", video_path],
                           capture_output=True, timeout=60)
        txt = _decode_io(p.stderr) + _decode_io(p.stdout)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if m:
            info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*fps", txt) or re.search(r"(\d+(?:\.\d+)?)\s*tbr", txt)
        if m2:
            info["fps"] = float(m2.group(1))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"解析视频流信息失败：{e}")
    return info


def _count_video_frames(video_path: str) -> int:
    """统计视频总帧数：优先 ffprobe -count_frames，其次 ffmpeg 解码计数"""
    fp = _ffprobe_exe()
    if fp:
        try:
            p = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=nb_read_frames",
                                "-of", "default=nw=1:nk=1", video_path],
                               capture_output=True, timeout=300)
            first = _decode_io(p.stdout).strip().splitlines()
            if first and first[0].strip().isdigit():
                return int(first[0].strip())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ffprobe 统计帧数失败：{e}")
    try:
        p = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", video_path,
                            "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
                           capture_output=True, timeout=600)
        txt = _decode_io(p.stderr) + _decode_io(p.stdout)
        hits = re.findall(r"frame=\s*(\d+)", txt)
        if hits:
            return int(hits[-1])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ffmpeg 统计帧数失败：{e}")
    return 0


def _fill_video_meta(meta: dict, video_path: str) -> dict:
    """补全 fps / frame_count（已有时不重复探测）"""
    if not meta.get("fps"):
        meta["fps"] = _ffmpeg_stream_info(video_path).get("fps") or 0.0
    if not meta.get("frame_count") and meta.get("fps") and meta.get("duration"):
        meta["frame_count"] = int(round(float(meta["duration"]) * float(meta["fps"])))
    return meta


def video_meta(video_path: str, fallback: dict = None) -> dict:
    """视频元信息 {duration, fps, frame_count, source, degraded}（B 项⑤）

    时长三级回退：
      ① ffprobe format=duration（最准）
      ② ffmpeg -i 的 Duration 行
      ③ 帧数 ÷ 帧率 推算（帧数 = ffprobe -count_frames / ffmpeg 解码计数 / 调用方传入；
         帧率 = ffmpeg -i 解析值 / 调用方传入，如 ComfyUI 返回值）
    """
    fb = dict(fallback or {})
    meta = {"duration": 0.0,
            "fps": float(fb.get("fps") or 0.0),
            "frame_count": int(fb.get("frame_count") or 0),
            "source": "unknown", "degraded": False}
    if not video_path or not os.path.isfile(video_path):
        meta["degraded"] = True
        return meta

    fp = _ffprobe_exe()
    if fp:
        try:
            p = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", video_path],
                               capture_output=True, text=True, timeout=60)
            s = (p.stdout or "").strip()
            if s and s.upper() != "N/A":
                d = float(s)
                if d > 0:
                    meta["duration"] = d
                    meta["source"] = "ffprobe-format"
                    return _fill_video_meta(meta, video_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ffprobe 读取时长失败：{e}")

    info = _ffmpeg_stream_info(video_path)
    if info.get("fps") and not meta["fps"]:
        meta["fps"] = info["fps"]
    if info.get("duration", 0) > 0:
        meta["duration"] = info["duration"]
        meta["source"] = "ffmpeg-duration"
        return _fill_video_meta(meta, video_path)

    if not meta["frame_count"]:
        meta["frame_count"] = _count_video_frames(video_path)
    if meta["frame_count"] and meta["fps"]:
        meta["duration"] = float(meta["frame_count"]) / float(meta["fps"])
        meta["source"] = "frame_count/fps"
    else:
        meta["degraded"] = True          # 时长仍未知：抽帧将退化为固定时间点
    return meta


def video_duration(video_path: str, fallback: dict = None) -> float:
    """视频时长（秒）。读不到时按「帧数 ÷ 帧率」或调用方（ComfyUI）返回值推算"""
    return float(video_meta(video_path, fallback).get("duration") or 0.0)


def extract_frames(video_path: str, out_dir: str, count: int = 3,
                   prefix: str = "frame", fallback_meta: dict = None) -> dict:
    """全片均匀抽帧（片头/中段/片尾），并校验每帧实际时间戳。

    返回 {ok, frames, frame_meta, timestamps, duration, meta, error}
      frames      : 抽帧图片路径列表（喂给多模态模型）
      frame_meta  : [{path, requested_ts, actual_ts, verified}]，actual_ts 由 ffmpeg showinfo 解析
    """
    result = {"ok": False, "frames": [], "frame_meta": [], "timestamps": [],
              "duration": 0.0, "meta": {}, "error": ""}
    if not video_path or not os.path.isfile(video_path):
        result["error"] = f"视频不存在：{video_path}"
        return result
    if not shutil.which("ffmpeg"):
        result["error"] = "未找到 ffmpeg（请安装并加入 PATH）"
        return result
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"抽帧目录创建失败：{e}"
        return result

    meta = video_meta(video_path, fallback_meta)
    dur = float(meta.get("duration") or 0.0)
    result["duration"] = round(dur, 3)
    result["meta"] = meta

    count = max(1, min(6, int(count or 3)))
    if dur > 0:
        # 全片均匀覆盖：5% ~ 95%（避免首尾黑场/异常帧），count=1 时取正中间
        if count == 1:
            times = [round(dur * 0.5, 3)]
        else:
            lo, hi = dur * 0.05, dur * 0.95
            times = [round(lo + (hi - lo) * i / (count - 1), 3) for i in range(count)]
    else:
        # 时长确实无法推算：退化按固定时间点抽帧（0s/1s/2s…），并标记 degraded
        times = [float(i) for i in range(count)]
        result["meta"]["degraded"] = True

    frames, frame_meta = [], []
    exe = ffmpeg_exe()
    for i, ts in enumerate(times):
        out = os.path.join(out_dir, f"{prefix}_{i + 1}_{int(ts * 1000)}ms.jpg")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "info", "-copyts",
               "-ss", f"{ts}", "-i", video_path, "-frames:v", "1",
               "-vf", "showinfo", "-q:v", "2", out]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            actual = None
            m = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", p.stderr or "")
            if m:
                actual = round(float(m.group(1)), 3)
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                frames.append(out)
                if actual is None:
                    actual = round(ts, 3)      # ffmpeg 未回吐 pts 时，以请求时间戳兜底
                frame_meta.append({
                    "path": out,
                    "requested_ts": round(ts, 3),
                    "actual_ts": actual,
                    "verified": abs(actual - ts) <= max(1.0, dur * 0.05) if dur > 0 else False,
                })
            else:
                logger.warning(f"抽帧失败 ts={ts}s: {(p.stderr or '')[:200]}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"抽帧异常 ts={ts}s: {e}")
    result["ok"] = bool(frames)
    result["frames"] = frames
    result["frame_meta"] = frame_meta
    result["timestamps"] = [fm["actual_ts"] for fm in frame_meta]
    if not frames:
        result["error"] = "ffmpeg 抽帧未产出任何图片"
    return result


def check_video(video_path: str, shot_desc: str = "", cfg: dict = None,
                override: dict = None, frames_dir: str = None,
                fallback_meta: dict = None) -> dict:
    """视频质检：ffmpeg 抽帧 → 多模态判定。永不抛异常。
    override 仅用于「测试连通性」临时传参，不落盘。"""
    cfg = cfg or _empty_config()
    if not cfg.get("enabled"):
        return {"ok": False, "skipped": True, "reason": "质检总开关未开启"}
    if not cfg.get("video_enabled"):
        return {"ok": False, "skipped": True, "reason": "视频质检开关未开启"}
    ep = resolve_endpoint(cfg, override)
    if not (ep["base_url"] and ep["api_key"] and ep["model"]):
        return {"ok": False, "skipped": True, "reason": "质检接口未配置（base_url/api_key/model）"}

    if not frames_dir:
        frames_dir = os.path.join(os.path.dirname(os.path.abspath(video_path)),
                                  "_qc_frames", os.path.splitext(os.path.basename(video_path))[0])
    fr = extract_frames(video_path, frames_dir, cfg.get("video_frame_count", 3),
                        fallback_meta=fallback_meta)
    if not fr["ok"]:
        return {"ok": False, "skipped": False, "error": fr["error"], "duration": fr["duration"],
                "frame_meta": fr.get("frame_meta") or [], "video_meta": fr.get("meta") or {}}

    prompt = (cfg.get("video_prompt") or DEFAULT_VIDEO_PROMPT).replace(
        "{shot_desc}", shot_desc or "（无）").replace("{frame_count}", str(len(fr["frames"])))
    ts_brief = "、".join(f"第{i + 1}帧 {fm['actual_ts']}s"
                        for i, fm in enumerate(fr.get("frame_meta") or []))
    prompt = f"共 {len(fr['frames'])} 张抽帧图片（按时间顺序；实际时间戳：{ts_brief}）。\n" + prompt
    prompt = prompt + WATERMARK_EXEMPT_NOTE + CRITICAL_RULE_NOTE   # P0：收紧放行（关键缺陷必须不通过）
    try:
        verdict = _run_vision(ep, prompt, fr["frames"], cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"视频质检调用失败：{e}")
        return {"ok": False, "skipped": False, "error": str(e),
                "api_attempts": getattr(e, "attempts", 1),
                "retryable": getattr(e, "retryable", None),
                "frames": fr["frames"], "duration": fr["duration"],
                "frame_meta": fr.get("frame_meta") or [],
                "timestamps": fr.get("timestamps") or [],
                "video_meta": fr.get("meta") or {}}
    verdict.update({"frames": fr["frames"], "duration": fr["duration"],
                    "frame_count": len(fr["frames"]),
                    "frame_meta": fr.get("frame_meta") or [],
                    "timestamps": fr.get("timestamps") or [],
                    "duration_source": (fr.get("meta") or {}).get("source"),
                    "video_meta": fr.get("meta") or {}})
    return verdict


# ===================== 质检历史持久化 =====================

# 只替换文件系统非法字符（\ / : * ? " < > | 与控制字符），保留中文等 Unicode 字符。
# 旧规则「非 ASCII 一律折叠成下划线」会让「青玉短笛_top」与「雨后石桥_top」都变成
# 「______top」而命中同一历史文件、互相覆盖 shot_id（物品 top 视角记录被写成了场景名），
# 这里改为保真命名以根治该命名复用问题。
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_token(raw, fallback: str = "0") -> str:
    s = str(raw if raw not in (None, "") else fallback).strip()
    safe = _ILLEGAL_NAME_CHARS.sub("_", s).strip(" .")
    return safe or fallback


def history_path(qc_root: str, project: str, kind: str, shot_key) -> str:
    safe_kind = safe_token(kind, "asset")
    safe_shot = safe_token(shot_key, "0")
    return os.path.join(qc_root, project, f"{safe_kind}_{safe_shot}.json")


def append_history(qc_root: str, project: str, kind: str, shot_key,
                   entry: dict) -> str:
    """追加一条质检/重试记录，返回历史文件绝对路径"""
    path = history_path(qc_root, project, kind, shot_key)
    data = {"project": project, "kind": kind, "shot_id": shot_key,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "records": []}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f) or {}
            data["records"] = old.get("records") or []
            data["created_at"] = old.get("created_at")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"质检历史读取失败（将重建）：{e}")
    data.setdefault("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry = dict(entry or {})
    entry.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    data["records"].append(entry)
    data["total_attempts"] = len(data["records"])
    data["last_passed"] = bool(entry.get("passed"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


def read_history(qc_root: str, project: str, kind: str, shot_key) -> dict:
    path = history_path(qc_root, project, kind, shot_key)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    return {}
