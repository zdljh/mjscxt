# -*- coding: utf-8 -*-
"""画面内文字/水印后处理（ffmpeg delogo 区域修复）

背景：分镜生成 / H3 视频工作流本身不含任何水印叠加节点，违规标识来自模型权重
（画面右下角常出现「AI生成」合规标识），提示词侧无法可靠压制；台词文本也可能
被模型当作画面字幕画出来。因此本模块在产物落盘前，对固定画面区域做插值修复。

特性：
- 区域按画面比例定义（默认右下角），自动适配不同分辨率；
- 原文件先备份到指定目录，便于回滚与前后对比；
- 仅改动区域像素，其余画面零改动；
- ffmpeg 不可用时安全跳过（只记录 error，不抛异常打断主流程）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

FFMPEG_BIN = os.getenv("FFMPEG_BIN") or "ffmpeg"
FFPROBE_BIN = os.getenv("FFPROBE_BIN") or "ffprobe"

# 右下角水印区（比例坐标：x0, y0, x1, y1）
DEFAULT_REGION: Tuple[float, float, float, float] = (0.895, 0.923, 0.993, 0.978)


def available() -> bool:
    return bool(shutil.which(FFMPEG_BIN)) and bool(shutil.which(FFPROBE_BIN))


def probe_size(path: str) -> Tuple[int, int]:
    """返回视频/图片的画面宽高；失败抛 RuntimeError。"""
    cmd = [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"ffprobe 读取尺寸失败: {out.stderr.strip()[:200]}")
    wh = out.stdout.strip().splitlines()[0].strip().split("x")
    return int(wh[0]), int(wh[1])


def probe_has_audio(path: str) -> Optional[bool]:
    """该视频是否含音频流（用于决定去水印时保留还是丢弃音轨）。

    2026-09-17：此前 clean_video 一律带 `-an`，把 H3 联合生成的环境音/打斗音效
    在落盘前就丢掉了（成片因此完全没有音效）。现在改成「有音轨就原样保留」。

    G6（P1，2026-09-21）：改三态 `True / False / None`。
    探测**异常**（ffprobe 超时/损坏/环境缺二进制但 available() 误判可用等）
    返回 `None` —— 调用方**不得**把 None 当 False（旧写法 `except: return False`
    会让「探测失败」等价「没有音轨」，随后 clean_video 加 `-an` 把原音轨丢掉）。
    None 时调用方必须跳过改写并记录 error。
    """
    try:
        cmd = [FFPROBE_BIN, "-v", "error", "-select_streams", "a:0",
               "-show_entries", "stream=codec_name", "-of", "csv=p=0", path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception as e:  # pragma: no cover - 环境相关
        logger.warning(f"probe_has_audio 探测异常（按未知处理，不丢音轨）: {e}")
        return None


def region_pixels(width: int, height: int,
                  region: Sequence[float] = DEFAULT_REGION) -> Tuple[int, int, int, int]:
    """比例区域 → 像素矩形 (x, y, w, h)，保证不贴边（delogo 要求区域在画面内）。"""
    x0, y0, x1, y1 = region
    x = max(1, int(round(width * x0)))
    y = max(1, int(round(height * y0)))
    w = int(round(width * (x1 - x0))) + 1
    h = int(round(height * (y1 - y0))) + 1
    w = max(2, min(w, width - x - 1))
    h = max(2, min(h, height - y - 1))
    return x, y, w, h


def _filter(width: int, height: int, region: Sequence[float]) -> Tuple[str, list]:
    x, y, w, h = region_pixels(width, height, region)
    return f"delogo=x={x}:y={y}:w={w}:h={h}", [x, y, w, h]


def _backup(path: str, backup_dir: Optional[str]) -> Optional[str]:
    if not backup_dir:
        return None
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, os.path.basename(path))
    if os.path.exists(dst):
        stem, ext = os.path.splitext(os.path.basename(path))
        dst = os.path.join(backup_dir, f"{stem}_orig{ext}")
    shutil.copy2(path, dst)
    return dst


def clean_image(path: str, region: Sequence[float] = DEFAULT_REGION,
                backup_dir: Optional[str] = None) -> dict:
    """去除图片指定区域内的文字/水印（原地替换，先备份）。"""
    rec = {"ok": False, "applied": False, "target": path, "kind": "image"}
    if not available():
        rec["error"] = "ffmpeg/ffprobe 不可用，跳过去水印"
        return rec
    tmp = None
    try:
        w, h = probe_size(path)
        filt, box = _filter(w, h, region)
        rec["size"] = [w, h]
        rec["region"] = box
        rec["backup"] = _backup(path, backup_dir)
        # G8：临时文件点前缀命名，且 finally 统一清理（成功则置 None 跳过）
        tmp = path + ".clean.png"
        cmd = [FFMPEG_BIN, "-y", "-v", "error", "-i", path, "-vf", filt,
               "-frames:v", "1", tmp]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if out.returncode != 0 or not os.path.exists(tmp):
            rec["error"] = (out.stderr or "ffmpeg 处理失败").strip()[:200]
            return rec
        os.replace(tmp, path)
        tmp = None
        rec.update({"ok": True, "applied": True})
    except Exception as e:  # pragma: no cover - 兜底
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as e:
                logger.debug("清理临时文件失败（忽略）：%s", e)
    return rec


def clean_video(path: str, region: Sequence[float] = DEFAULT_REGION,
                backup_dir: Optional[str] = None, crf: int = 18) -> dict:
    """去除视频指定区域内的文字/水印（逐帧区域插值，原地替换，先备份）。

    **音轨策略：原样保留**。有音轨则 `-c:a copy`（H3 联合生成的音效不能被这里吃掉）；
    本来就没有音轨才加 `-an`。2026-09-17 之前是一律 `-an`，导致成片彻底没有音效。
    """
    rec = {"ok": False, "applied": False, "target": path, "kind": "video"}
    if not available():
        rec["error"] = "ffmpeg/ffprobe 不可用，跳过去水印"
        return rec
    tmp = None
    try:
        w, h = probe_size(path)
        filt, box = _filter(w, h, region)
        rec["size"] = [w, h]
        rec["region"] = box
        rec["backup"] = _backup(path, backup_dir)
        # G8：临时文件用 .clean.tmp（非 .mp4 后缀），避免被 *.mp4 glob 命中当"分片"；
        # 成功 replace 后置 None，失败/异常由 finally 清理。
        # ⚠️ 扩展名不再是 .mp4，必须显式 -f mp4 指定容器（ffmpeg 否则靠扩展名推断会失败）
        tmp = os.path.splitext(path)[0] + ".clean.tmp"
        # G6：probe_has_audio 改三态。None（探测失败）时**绝不改写原文件**——
        # 旧写法把 None 当 False 会加 -an 把原音轨丢掉，再补一条静音轨，
        # 成片"看起来有音轨"实为全静音（历史事故同型）。
        has_audio = probe_has_audio(path)
        rec["has_audio"] = has_audio
        if has_audio is None:
            rec["error"] = "音轨探测失败（ffprobe 异常），为保护原音轨已跳过去水印改写"
            logger.warning(f"clean_video 跳过：{path} 音轨探测失败，不动原文件")
            return rec
        audio_args = ["-c:a", "copy"] if has_audio else ["-an"]
        cmd = [FFMPEG_BIN, "-y", "-v", "error", "-i", path, "-vf", filt,
               "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
               "-pix_fmt", "yuv420p", "-f", "mp4"] + audio_args + ["-movflags", "+faststart", tmp]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if out.returncode != 0 or not os.path.exists(tmp):
            rec["error"] = (out.stderr or "ffmpeg 处理失败").strip()[:200]
            return rec
        os.replace(tmp, path)
        tmp = None
        rec.update({"ok": True, "applied": True})
    except Exception as e:  # pragma: no cover - 兜底
        rec["error"] = f"{type(e).__name__}: {e}"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as e:
                logger.debug("清理临时文件失败（忽略）：%s", e)
    return rec
