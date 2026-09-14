# -*- coding: utf-8 -*-
"""Provider 注册表与选择（P1-4）

对外只暴露两个函数：
- `get(kind)`   → 取当前生效的 Provider 实例
- `catalog()`   → 列出全部可选 Provider 及其可用状态（供前端展示 / 切换）

当前生效的 Provider 由环境变量决定，默认全部本地：
  MJSCXT_PROVIDER_IMAGE / MJSCXT_PROVIDER_VIDEO / MJSCXT_PROVIDER_TTS
"""
from __future__ import annotations

import logging
import os
import threading
import time

from env_loader import env
from providers.base import BaseProvider, ImageProvider, ProviderError, TTSProvider, VideoProvider
from providers.cloud import (
    CosyVoiceCloudTTSProvider,
    DoubaoCloudImageProvider,
    KlingCloudVideoProvider,
    VeoCloudVideoProvider,
    ViduCloudVideoProvider,
    WanCloudVideoProvider,
)
from providers.local import ComfyUIImageProvider, ComfyUIVideoProvider, QwenTTSLocalProvider

logger = logging.getLogger(__name__)

# ===================== 可用性探测缓存 =====================
# 可用性探测会真连网络（ComfyUI /system_stats、TTS 环境检查），
# 若每次列表接口都探一遍，前端刷新会明显卡顿（实测 /api/providers 6.7s）。
# 这里做 TTL 缓存：默认 20 秒内复用结果；切引擎 / 手动刷新时立即失效。
_PROBE_TTL = float(os.getenv("MJSCXT_PROVIDER_PROBE_TTL", "20") or 20)
_PROBE_CACHE: dict = {}          # (kind, name) -> (ts, available, reason)
_PROBE_LOCK = threading.RLock()


def invalidate_probe_cache() -> None:
    """清空可用性探测缓存（切换引擎、用户点「刷新」时调用）"""
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()


def _probe(provider: BaseProvider, force: bool = False) -> tuple:
    """探测 Provider 可用性，返回 (available, reason)；带 TTL 缓存"""
    key = (provider.kind, provider.name)
    now = time.time()
    if not force:
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(key)
            if hit and (now - hit[0]) < _PROBE_TTL:
                return hit[1], hit[2]
    try:
        ok = bool(provider.available())
    except Exception as e:  # noqa: BLE001
        ok, reason = False, f"探测异常：{e}"
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = (now, ok, reason)
        return ok, reason
    reason = ""
    if not ok:
        try:
            reason = provider.unavailable_reason() or ""
        except Exception:  # noqa: BLE001
            reason = ""
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = (now, ok, reason)
    return ok, reason


def _probe_all(providers: list) -> None:
    """并发预热全部 Provider 的可用性缓存

    可用性探测本质是网络等待（ComfyUI /system_stats、TTS 环境检查），
    串行会把各 Provider 的等待时间直接叠加（实测 6.7s）；并发后总耗时
    约等于其中最慢的一个。
    """
    pending = []
    now = time.time()
    with _PROBE_LOCK:
        for p in providers:
            hit = _PROBE_CACHE.get((p.kind, p.name))
            if not (hit and (now - hit[0]) < _PROBE_TTL):
                pending.append(p)
    if not pending:
        return
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(pending))) as ex:
            futures = [ex.submit(_probe, p) for p in pending]
            for fu in futures:
                try:
                    fu.result(timeout=20)
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        logger.debug(f"并发探测不可用，退回串行：{e}")
        for p in pending:
            _probe(p)

# kind → 可选 Provider（顺序即前端展示顺序，本地在前）
_REGISTRY: dict = {
    "image": [ComfyUIImageProvider(), DoubaoCloudImageProvider()],
    "video": [ComfyUIVideoProvider(), WanCloudVideoProvider(), KlingCloudVideoProvider(),
              ViduCloudVideoProvider(), VeoCloudVideoProvider()],
    "tts": [QwenTTSLocalProvider(), CosyVoiceCloudTTSProvider()],
}

_ENV_KEYS = {
    "image": "MJSCXT_PROVIDER_IMAGE",
    "video": "MJSCXT_PROVIDER_VIDEO",
    "tts": "MJSCXT_PROVIDER_TTS",
}


def _resolve(kind: str) -> BaseProvider:
    """按环境变量解析当前生效的 Provider（找不到则回退到第一个可用的）"""
    providers = _REGISTRY.get(kind) or []
    if not providers:
        raise ProviderError(f"未知的 Provider 类别：{kind}")
    want = env(_ENV_KEYS.get(kind, ""), "").strip()
    if want:
        for p in providers:
            if p.name == want:
                ok, reason = _probe(p)
                if not ok:
                    logger.warning(f"{kind} Provider「{want}」不可用：{reason}，回退到本地实现")
                    break
                return p
        else:
            logger.warning(f"未找到 {kind} Provider「{want}」，回退到默认实现")
    # 回退：优先第一个可用的本地实现，否则第一个
    for p in providers:
        if p.local and _probe(p)[0]:
            return p
    return providers[0]


def get(kind: str) -> BaseProvider:
    """取当前生效的 Provider 实例（kind: image / video / tts）"""
    return _resolve(kind)


def get_image() -> ImageProvider:
    return _resolve("image")


def get_video() -> VideoProvider:
    return _resolve("video")


def get_tts() -> TTSProvider:
    return _resolve("tts")


def catalog(force: bool = False) -> dict:
    """列出全部 Provider 与可用状态（供前端展示/切换）

    force=True 时绕过可用性缓存重新探测（前端「刷新引擎状态」用）。
    """
    if force:
        invalidate_probe_cache()
    # 先并发把所有 Provider 的可用性探完（缓存就绪），后续 _resolve / info 全部命中缓存
    _probe_all([p for group in _REGISTRY.values() for p in group])
    out = {}
    for kind, providers in _REGISTRY.items():
        current = _resolve(kind)
        items = []
        for p in providers:
            # 注意：这里不再传 force —— 开头已整体失效，force 会让同一 Provider
            # 被 _resolve 与本次循环各探一遍（实测强制刷新因此翻倍到 7.6s）。
            ok, reason = _probe(p)
            item = dict(p.info(available=ok))
            if reason:
                item["unavailable_reason"] = reason
            item["current"] = (p.name == current.name)
            items.append(item)
        out[kind] = {
            "env_key": _ENV_KEYS.get(kind),
            "current": current.name,
            "items": items,
        }
    return out


def set_active(kind: str, name: str) -> dict:
    """切换生效 Provider（写环境变量，仅当前进程有效；持久化请改 .env）"""
    providers = _REGISTRY.get(kind) or []
    target = next((p for p in providers if p.name == name), None)
    if not target:
        raise ProviderError(f"{kind} 下不存在 Provider「{name}」")
    os.environ[_ENV_KEYS[kind]] = name
    invalidate_probe_cache()
    return {"ok": True, "kind": kind, "current": name,
            "note": "仅当前进程生效；长期切换请写入 .env 的 " + _ENV_KEYS[kind]}


def register(provider: BaseProvider) -> None:
    """注册自定义 Provider（供插件扩展）"""
    if not isinstance(provider, BaseProvider):
        raise ProviderError("Provider 必须继承 providers.base.BaseProvider")
    _REGISTRY.setdefault(provider.kind, []).append(provider)
    logger.info(f"已注册自定义 Provider：{provider.kind}/{provider.name}")
