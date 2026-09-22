# -*- coding: utf-8 -*-
"""D-05（P1）整集视频质检「覆盖率」纯逻辑（零第三方依赖，便于离线单测）

背景
----
整集模式（``video_mode=episode``，也是默认模式）一次产出 20~44 段拼接的整片。
修复前质检有**双重降采样**：

1. **文本侧**：``_episode_qc_desc`` 的 ``limit=12`` —— 40 镜的整集只把前 12 镜给模型，
   其余写「…（其余 N 镜略）」，模型以为整集只有 12 个镜头。
2. **图像侧**：抽帧数取 qc 配置 ``video_frame_count``（默认 3、**上限 6**），
   且策略是「全片 5%~95% 均布」——3 帧 = 5%/50%/95% 三个时间点。

于是整集中段几十段**完全没有采样**，模型拿 3 张无关帧去判「这 40 个镜头里的角色有没有崩坏」，
整集质检形同虚设：中段脸崩/穿模/纯色块一律漏检，崩坏片直接进正式目录被动/超分/交付。

本模块提供两个纯函数（不 import Flask / requests / PIL，可独立离线运行）：
- :func:`episode_frame_ratios` —— 把各段时长折算成「每段中点」在全片中的占比，供
  ``qc_client.extract_frames(frame_ratio=...)`` 按段抽帧，保证每段至少被采到一次。
- :func:`episode_qc_desc` —— 镜头摘要：不再静默截断，截断必须显式声明「其余未提供」。

对应缺陷报告：全项目缺陷审计报告 D-05（开发 B · 质检覆盖率）。
"""
from __future__ import annotations

#: 整集按段抽帧的**安全上限**。正常一集 ≤44 段不会触发；
#: 仅防极端段数导致 ffmpeg 抽帧耗时失控。注意与 qc 配置 ``video_frame_count`` 的
#: 1~6 上限是**两回事**：后者只约束单镜/默认抽帧路径，整集路径不受它约束。
EPISODE_MAX_FRAMES = 48

#: 整集镜头摘要默认条数上限（原为 12，导致 40 镜整集中后段对模型完全不可见）。
DEFAULT_DESC_LIMIT = 60

#: 摘要被截断时必须出现的声明（验收标准 ③ 依赖此固定字样）。
TRUNCATED_NOTE = "其余未提供"


def episode_frame_ratios(segs: list, max_frames: int = None) -> list:
    """把整集各段时长折算成「每段中点」在全片中的占比列表（0~1，升序）。

    例：40 段各 5s（全片 200s）→ 占比 ≈ 0.0125 / 0.0375 / … / 0.9875，
    相邻间距恒为 ``单段时长 / 全片时长``，远小于「单段时长×1.5」的验收阈值。

    返回空列表表示段时长不可用（无段 / 全 0），由调用方退回配置抽帧数。
    """
    durs = [max(0.0, float((sg or {}).get("duration") or 0.0)) for sg in (segs or [])]
    total = sum(durs)
    if total <= 0:
        return []
    ratios, acc = [], 0.0
    for d in durs:
        ratios.append(min(1.0, (acc + d / 2.0) / total))
        acc += d
    try:
        cap = int(max_frames) if max_frames else EPISODE_MAX_FRAMES
    except (TypeError, ValueError):
        cap = EPISODE_MAX_FRAMES
    if cap > 0 and len(ratios) > cap:
        # 等步长降采样（保序）：极端段数时仍保持全片覆盖
        step = len(ratios) / float(cap)
        ratios = [ratios[min(len(ratios) - 1, int(i * step))] for i in range(cap)]
    return ratios


def episode_qc_desc(shots: list, limit: int = DEFAULT_DESC_LIMIT, warn=None) -> str:
    """构造整集质检用的「镜头信息」摘要（逐镜一行，控制长度）。

    ``limit`` 默认 60（原为 12）：40 镜的整集不再被截到只剩前 12 镜。
    一旦真的截断，返回串**必须**含 :data:`TRUNCATED_NOTE`（「其余未提供」），
    让模型知道信息不完整，而不是误以为整集只有 ``limit`` 个镜头。

    ``warn``：可选告警回调（如 ``app.logger.warning``）。截断时调用，保证可观测。
    """
    rows = []
    shot_list = [s for s in (shots or []) if isinstance(s, dict)]
    total = len(shot_list)
    truncated = 0
    for i, s in enumerate(shot_list):
        if i >= limit:
            truncated = total - limit
            rows.append(f"…（{TRUNCATED_NOTE}：本集共 {total} 镜，此处仅列出前 {limit} 镜）")
            break
        cam = str(s.get("camera") or "").strip()
        desc = str(s.get("description") or "").strip()[:60]
        rows.append(f"镜{s.get('shot_id', i + 1)}[{cam}] {desc}")
    if not rows:
        return "（无镜头信息）"
    if truncated and callable(warn):
        warn("整集质检摘要被截断：共 %d 镜，仅提供前 %d 镜（已显式声明未提供）",
             total, limit)
    return "本集共 %d 镜：" % total + "；".join(rows)
