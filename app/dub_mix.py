"""
音画对齐与混音合成（配音轨 × 成片视频 → 带配音成片）
====================================================

把 QwenTTS 产出的逐句配音音频，按「镜头时间轴」对齐叠合到 H3 无声成片上，
产出一个带配音、仍不含原始音轨的最终成片。

对齐策略
--------
* timeline（默认）：按剧本镜头顺序累计时间轴，逐句音频用 ``adelay`` 落在对应镜头起点；
  同一镜头多句时按顺序顺排，绝不倒挂（``start = max(镜头起点, 上一句结束)``）；
* concat：把整集合并音轨从 0 秒起整体铺上（不做逐镜头对齐，用于快速试听）；
* 镜头时长优先取**真实分段视频时长**（ffprobe 实测），缺失时回退剧本 duration；
* 逐句可微调：``line_offsets``（按 line_id 提前/延后秒数）、``lead_in_sec``、``gap_sec``；
* 超长句处理：``max_line_sec > 0`` 时对该句做 ``atempo`` 变速压缩（0.5x~2x 串联），
  不裁切、不丢弃内容。

技术要点
--------
* 合成时用 ``anullsrc`` 生成与视频等长的静音底轨，逐句 ``adelay`` + ``amix(normalize=0)``，
  避免任何句子被平均值衰减；
* 画面默认 ``-c:v copy`` 不重编码（快且无画质损失），可由 ``video_codec=reencode`` 切换；
* 默认丢弃成片原音轨（``keep_original_audio=False``），符合「H3 出片无声、配音统一由 TTS 负责」；
* 不静默失败：缺视频 / 缺音频 / ffmpeg 报错都会抛出 ``DubMixError`` 并带 stderr 摘要。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Dict, List, Optional

from config import MIX_DEFAULT_PARAMS, DUB_MIX_DIR
from video_postprocess import probe_media

logger = logging.getLogger(__name__)

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class DubMixError(Exception):
    """音画合成失败（带明确原因）"""


# =====================================================================
# 环境
# =====================================================================

def ffmpeg_available() -> Dict:
    """ffmpeg / ffprobe 可用性（实测，不猜测）"""
    out = {"available": False, "ffmpeg": "", "ffprobe": "", "reasons": []}
    for name, key in ((FFMPEG, "ffmpeg"), (FFPROBE, "ffprobe")):
        try:
            r = subprocess.run([name, "-version"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                out[key] = (r.stdout or "").splitlines()[0][:120]
            else:
                out["reasons"].append(f"{name} 返回码 {r.returncode}")
        except Exception as e:  # pragma: no cover - 环境相关
            out["reasons"].append(f"{name} 不可用：{type(e).__name__}")
    out["available"] = bool(out["ffmpeg"] and out["ffprobe"])
    return out


def probe_audio_info(path: str) -> Dict:
    """音频信息（ffprobe）"""
    return probe_media(path)


# =====================================================================
# 时间轴
# =====================================================================

def shot_timeline(script: Dict, videos_dir: str = "", segments: Optional[List[str]] = None) -> Dict:
    """按镜头顺序计算时间轴。

    返回 ``{"shots": [...], "total_sec": float, "duration_source": "video"/"script"/"mixed"}``
    每个镜头：``{shot_id, index, start, duration, end, duration_source, video}``
    """
    shots = (script or {}).get("shots") or []
    files: List[str] = []
    if segments:
        files = [p for p in segments if p and os.path.exists(p)]
    elif videos_dir and os.path.isdir(videos_dir):
        files = sorted(os.path.join(videos_dir, f) for f in os.listdir(videos_dir)
                       if f.lower().endswith((".mp4", ".mov", ".mkv")))
    real_durations: List[float] = []
    for p in files:
        info = probe_media(p)
        real_durations.append(round(float(info.get("duration") or 0), 3))

    rows, t, srcs = [], 0.0, set()
    for i, shot in enumerate(shots):
        real = real_durations[i] if i < len(real_durations) and real_durations[i] > 0 else None
        try:
            script_dur = float(shot.get("duration") or 0)
        except (TypeError, ValueError):
            script_dur = 0.0
        dur = real if real else (script_dur or 5.0)
        src = "video" if real else "script"
        srcs.add(src)
        rows.append({
            "shot_id": shot.get("shot_id", i + 1), "index": i,
            "start": round(t, 3), "duration": round(dur, 3), "end": round(t + dur, 3),
            "duration_source": src,
            "video": files[i] if i < len(files) else "",
        })
        t += dur
    source = srcs.pop() if len(srcs) == 1 else ("mixed" if srcs else "script")
    return {"shots": rows, "total_sec": round(t, 3), "duration_source": source,
            "segment_count": len(files)}


def build_entries(lines: List[Dict], timeline: Dict, params: Optional[Dict] = None,
                  mode: str = "") -> Dict:
    """逐句配音 → 时间轴条目（纯计算，不落盘）

    ``lines``：配音清单（含 ``line_id / shot_id / out_path / text / character``）
    返回 ``{"entries": [...], "warnings": [...], "coverage_sec": float}``
    """
    p = dict(MIX_DEFAULT_PARAMS)
    p.update(params or {})
    mode = mode or p.get("mode") or "timeline"

    if mode == "concat":
        return {"entries": [], "warnings": ["concat 模式由调用方提供整轨音频"], "coverage_sec": 0.0}

    slots = {str(s["shot_id"]): s for s in timeline.get("shots") or []}
    offsets = p.get("line_offsets") or {}
    warnings: List[str] = []
    entries: List[Dict] = []
    cursor = 0.0
    for ln in lines or []:
        path = ln.get("out_path") or ""
        if not path or not os.path.exists(path):
            continue
        slot = slots.get(str(ln.get("shot_id")))
        start = slot["start"] if slot else cursor
        try:
            start = float(start) + float(p.get("lead_in_sec") or 0)
        except (TypeError, ValueError):
            pass
        try:
            start += float(offsets.get(str(ln.get("line_id")), 0) or 0)
        except (TypeError, ValueError):
            pass
        try:
            start += float(p.get("gap_sec") or 0)
        except (TypeError, ValueError):
            pass
        if start < cursor:  # 同一镜头多句顺排，不倒挂
            start = cursor
        info = probe_media(path)
        dur = round(float(info.get("duration") or 0), 3)
        if dur <= 0:
            warnings.append(f"配音文件无有效时长，已跳过：{os.path.basename(path)}")
            continue
        fit_ratio = 1.0
        max_line = float(p.get("max_line_sec") or 0)
        if max_line > 0 and dur > max_line:
            fit_ratio = round(dur / max_line, 4)
            warnings.append(
                f"{os.path.basename(path)} 时长 {dur}s 超过上限 {max_line}s，"
                f"按 {fit_ratio}x 变速压缩（不裁切内容）")
        entries.append({
            "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
            "character": ln.get("character"), "text": ln.get("text"),
            "audio_path": os.path.abspath(path), "audio_dur": dur,
            "start": round(start, 3), "end": round(start + dur, 3),
            "shot_start": (slot or {}).get("start"), "shot_end": (slot or {}).get("end"),
            "fit_ratio": fit_ratio,
        })
        cursor = start + dur
    return {"entries": entries, "warnings": warnings,
            "coverage_sec": round(sum(e["audio_dur"] for e in entries), 3)}


def apply_line_offsets(entries: List[Dict], total_sec: float = 0.0) -> List[Dict]:
    """条目越界提示（超出视频总时长时仅告警，不静默裁掉）"""
    warns = []
    for e in entries or []:
        if total_sec and e.get("start", 0) >= total_sec:
            warns.append(f"{e.get('line_id')} 起始 {e.get('start')}s 已超出视频时长 {total_sec}s")
    return warns


# =====================================================================
# 合成
# =====================================================================

def _atempo_chain(ratio: float) -> List[str]:
    """把任意变速比拆成 atempo 可接受区间（0.5~2.0）的串联"""
    chain, r = [], float(ratio)
    while r > 2.0:
        chain.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        chain.append("atempo=0.5")
        r /= 0.5
    if abs(r - 1.0) > 1e-3:
        chain.append(f"atempo={r:.4f}")
    return chain


def mix_video_with_entries(video_path: str, entries: List[Dict], out_path: str,
                           params: Optional[Dict] = None) -> Dict:
    """把逐句配音按时间轴叠合到视频上（画面默认不重编码）"""
    p = dict(MIX_DEFAULT_PARAMS)
    p.update(params or {})
    if not video_path or not os.path.exists(video_path):
        raise DubMixError(f"成片视频不存在：{video_path}")
    vinfo = probe_media(video_path)
    if not vinfo.get("has_video"):
        raise DubMixError(f"目标文件不含视频流：{video_path}")
    vdur = float(vinfo.get("duration") or 0)

    usable = [e for e in (entries or [])
              if e.get("audio_path") and os.path.exists(e["audio_path"])]
    if not usable:
        raise DubMixError("没有可用的配音音频（请先完成配音合成，或检查配音文件是否存在）")

    if os.path.dirname(out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    sr = int(p.get("sample_rate") or 48000)

    # ⚠️ 静音底轨是 lavfi **无限源**：时长绝不允许「未定」。
    #    旧写法把时长挂在输入选项 `-t` 上、且 `if vdur > 0` 才加 —— 一旦 ffprobe 取不到
    #    视频时长（vdur == 0），`-t` 与结尾的 `-shortest` 会**双双缺席**，而 amix 用的是
    #    `duration=first`（first 恰好就是这条无限底轨）→ ffmpeg 会一直往磁盘写，
    #    实测曾一条命令把 C 盘写满 80GB（2026-09-19 事故）。
    #    现在把时长写进 lavfi 滤镜参数 `d=`（权威、不依赖选项位置），并按
    #    「视频实测时长 / 条目时间轴末端 + 1s 尾韵」取大值兜底，任何分支都有限。
    tail_sec = 1.0
    derived_sec = 0.0
    for e in usable:
        try:
            derived_sec = max(derived_sec,
                              float(e.get("start") or 0)
                              + max(float(e.get("audio_dur") or 0), 0.5))
        except (TypeError, ValueError):
            continue
    base_dur = max(vdur, derived_sec + tail_sec) if vdur > 0 else (derived_sec + tail_sec)
    base_dur = max(base_dur, 1.0)

    cmd = [FFMPEG, "-y", "-v", "error", "-i", video_path]
    cmd += ["-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono:d={base_dur:.3f}"]
    for e in usable:
        cmd += ["-i", e["audio_path"]]

    filters = [f"[1:a]aformat=sample_rates={sr}:channel_layouts=mono[base]"]
    mix_inputs = ["[base]"]
    # 原音轨（H3 生成的环境音/打斗音效）作为「垫底」混入，音量默认 0.3：
    # 1.0 会盖住台词，0.3 是"听得到但不抢戏"。2026-09-17 之前这里默认直接被丢弃。
    orig_vol = p.get("original_audio_volume")
    orig_vol = 1.0 if orig_vol is None else float(orig_vol)
    orig_kept = bool(p.get("keep_original_audio") and vinfo.get("has_audio"))
    if orig_kept:
        chain = f"[0:a]aformat=sample_rates={sr}:channel_layouts=stereo"
        if abs(orig_vol - 1.0) > 1e-3:
            chain += f",volume={orig_vol:.4f}"
        filters.append(chain + "[orig]")
        mix_inputs.append("[orig]")
    for i, e in enumerate(usable, start=2):
        ms = max(0, int(round(float(e.get("start") or 0) * 1000)))
        chain = [f"[{i}:a]aformat=sample_rates={sr}:channel_layouts=mono"]
        ratio = float(e.get("fit_ratio") or 1.0)
        if abs(ratio - 1.0) > 1e-3:
            chain += _atempo_chain(ratio)
        # 每条也可自带音量（音效轨走这条路径时给 0.3，人声台词默认 1.0）
        e_vol = e.get("volume")
        if e_vol is not None and abs(float(e_vol) - 1.0) > 1e-3:
            chain.append(f"volume={float(e_vol):.4f}")
        chain.append(f"adelay={ms}:all=1[lt{i}]")
        filters.append(",".join(chain))
        mix_inputs.append(f"[lt{i}]")
    filters.append("".join(mix_inputs) +
                   f"amix=inputs={len(mix_inputs)}:normalize=0:duration=first[aout]")

    vcodec = p.get("video_codec") or "copy"
    cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]"]
    if vcodec == "reencode":
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", str(p.get("audio_bitrate") or "192k"),
            "-movflags", "+faststart"]
    # 底轨已在滤镜参数里定长，`-shortest` 此时**恒安全**：成片长度 = min(底轨, 视频)。
    # 旧代码 `if vdur > 0` 才加，等于把「写盘是否无限」交给了 ffprobe 的运气。
    cmd += ["-shortest"]
    cmd += [out_path]

    started = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=int(p.get("timeout") or 900))
    except subprocess.TimeoutExpired:
        raise DubMixError(f"ffmpeg 合成超时（>{p.get('timeout')}s）")
    except Exception as e:  # pragma: no cover - 环境相关
        raise DubMixError(f"ffmpeg 调用失败：{type(e).__name__}: {e}")
    elapsed = round(time.time() - started, 3)
    if r.returncode != 0:
        raise DubMixError(f"ffmpeg 合成失败（code={r.returncode}）：{(r.stderr or '').strip()[:400]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        raise DubMixError("ffmpeg 返回成功但未生成有效文件")

    after = probe_media(out_path)
    if not after.get("has_audio"):
        raise DubMixError("合成结果不含音频流，判定失败")
    warn = ""
    if p.get("keep_original_audio") and not vinfo.get("has_audio"):
        warn = ("成片源视频不含音轨，音效无法垫底（请检查 H3_EMIT_AUDIO / H3_STRIP_AUDIO，"
                "或该镜本就无声）")
    return {
        "ok": True, "output_path": os.path.abspath(out_path),
        "elapsed_sec": elapsed, "entry_count": len(usable),
        "coverage_sec": round(sum(e.get("audio_dur") or 0 for e in usable), 3),
        "original_audio_kept": orig_kept,
        "original_audio_volume": orig_vol if orig_kept else 0.0,
        "warning": warn,
        "video_before": vinfo, "video_after": after,
        "cmd": " ".join(cmd), "params": p,
    }


def write_mix_report(report: Dict, path: str) -> str:
    """落盘合成报告（JSON）"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


def mix_out_dir(project_name: str) -> str:
    d = os.path.join(DUB_MIX_DIR, project_name)
    os.makedirs(d, exist_ok=True)
    return d
