"""
视频后期处理 - FlashVSR 超分辨率 + FFmpeg 合并
"""
import os
import re
import subprocess
import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional, Dict
import json

from config import COMFYUI_URL, PROJECT_OUTPUT_DIR, VIDEOS_DIR, FINAL_DIR

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== 集号 / 片段目录（审计 S4：与 app._ep_dir 同口径） =====================
# 写入侧（app.py 的 _ep_dir）遵守「第 1 集平铺、第 2 集起 epNN/」；本模块原先完全不知道集号，
# 于是 `generate_final_video` 只 listdir 平铺目录 → 多集项目合成第 2 集时会拿到第 1 集的片段。
_SHOT_RE = re.compile(r"^shot_(\d+)")


def _episode_no_of(script: dict, fallback=None) -> int:
    """从剧本里取集号（metadata.episode_no > 顶层 episode_no > 兜底），最小 1"""
    if isinstance(script, dict):
        meta = script.get("metadata") or {}
        for v in (meta.get("episode_no"), script.get("episode_no"), fallback):
            try:
                if v is None or v == "":
                    continue
                n = int(v)
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue
    try:
        return max(1, int(fallback or 1))
    except (TypeError, ValueError):
        return 1


def _ep_videos_dir(project: str, ep: int) -> str:
    """该集视频片段目录（第 1 集 = 平铺目录，第 2 集起 epNN/）"""
    base = os.path.join(PROJECT_OUTPUT_DIR, "videos", project)
    return os.path.join(base, f"ep{ep:02d}") if int(ep) > 1 else base


def _ordered_shot_files(videos_dir: str, ep: int) -> List[str]:
    """按**镜头号数字**顺序取逐镜片段（而非文件名字典序）。

    显式排除整集成片 `*_full.mp4`（否则会把整集和它的各镜一起拼，产出内容重复的成片，
    体积够大能骗过 100KB/2s 硬闸）；一个逐镜片段都没有时，才回退采用整集成片。
    """
    try:
        names = os.listdir(videos_dir)
    except OSError:
        return []
    media = [f for f in names if f.lower().endswith((".mp4", ".mov", ".mkv"))]
    shots = sorted((f for f in media if _SHOT_RE.match(f) and not f.endswith("_full.mp4")),
                   key=lambda f: int(_SHOT_RE.match(f).group(1)))
    if shots:
        return [os.path.join(videos_dir, f) for f in shots]
    # 整集模式（video_mode=episode）磁盘上只有一支 *full*.mp4，直接采用
    for cand in (f"ep{int(ep):02d}_full.mp4", "episode_full.mp4"):
        if cand in media:
            return [os.path.join(videos_dir, cand)]
    fulls = [f for f in media if f.endswith("_full.mp4")]
    return [os.path.join(videos_dir, fulls[0])] if fulls else []


# ===================== 音轨工具（H3 出片音轨策略见 config：H3_EMIT_AUDIO / H3_STRIP_AUDIO） =====================

def probe_media(path: str) -> Dict:
    """ffprobe 读取媒体信息（含视频/音频流清单），任何异常都落到 info.error，不抛出"""
    info = {"path": os.path.abspath(path) if path else "", "ok": False,
            "has_video": False, "has_audio": False,
            "video_streams": 0, "audio_streams": 0}
    if not path or not os.path.exists(path):
        info["error"] = "文件不存在"
        return info
    info["size_bytes"] = os.path.getsize(path)
    info["size_mb"] = round(info["size_bytes"] / 1048576, 3)
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=index,codec_type,codec_name,width,height,r_frame_rate,"
           "sample_rate,channels:format=duration,format_name",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            info["error"] = (r.stderr or "ffprobe 失败").strip()[:200]
            return info
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams") or []
        info["streams"] = [{"index": s.get("index"), "type": s.get("codec_type"),
                            "codec": s.get("codec_name"), "width": s.get("width"),
                            "height": s.get("height")} for s in streams]
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        info["video_streams"] = len(videos)
        info["audio_streams"] = len(audios)
        info["has_video"] = bool(videos)
        info["has_audio"] = bool(audios)
        info["video_codec"] = videos[0].get("codec_name") if videos else None
        info["audio_codec"] = audios[0].get("codec_name") if audios else None
        if videos:
            info["width"] = videos[0].get("width")
            info["height"] = videos[0].get("height")
            # B-05 P1-4：实测帧率（ffprobe r_frame_rate 形如 "30000/1001"），供降级重编码参考
            _fr = str(videos[0].get("r_frame_rate") or "")
            try:
                num, _, den = _fr.partition("/")
                if den:
                    info["fps"] = round(int(num) / int(den), 3)
                elif num:
                    info["fps"] = float(num)
            except (ValueError, ZeroDivisionError):
                pass
        if audios:
            # B-04 P1-3（已修复 S-01）：补 audio 采样率/声道探测，供音轨一致性判定
            info["sample_rate"] = audios[0].get("sample_rate")
            info["channels"] = audios[0].get("channels")
        info["duration"] = round(float((d.get("format") or {}).get("duration") or 0), 3)
        info["format_name"] = (d.get("format") or {}).get("format_name")
        info["ok"] = True
    except Exception as e:  # pragma: no cover - 环境相关
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def has_audio_stream(path: str) -> bool:
    """视频是否含音频流（ffprobe 实测，不猜测）"""
    return bool(probe_media(path).get("has_audio"))


def strip_audio(video_path: str, output_path: Optional[str] = None,
                backup: bool = True) -> Dict:
    """剥离视频音轨（-an，视频流直接复制不重编码；失败时降级 libx264 重编码）

    - 不传 output_path 时原地替换：先写临时文件，校验无音轨后再替换，原文件可按 backup 留存
    - backup=True 且原文件确有音轨时，备份为 <同名>.withaudio.bak.mp4（同目录）
    """
    t0 = time.time()
    report = {"ok": False, "input": os.path.abspath(video_path) if video_path else "",
              "changed": False, "backup": None, "method": None,
              "has_audio_before": None, "has_audio_after": None}
    if not video_path or not os.path.exists(video_path):
        report["error"] = "输入视频不存在"
        return report
    before = probe_media(video_path)
    report["has_audio_before"] = before.get("has_audio")
    report["before"] = {k: before.get(k) for k in ("width", "height", "duration", "size_mb", "video_codec")}
    if not before.get("has_audio"):
        report.update({"ok": True, "changed": False, "has_audio_after": False,
                       "output": os.path.abspath(video_path), "message": "输入本身无音轨，无需处理"})
        report["elapsed_sec"] = round(time.time() - t0, 2)
        return report

    in_place = not output_path
    target = os.path.abspath(output_path or video_path)
    # G8：临时文件用 .clean（非 .mp4 后缀），避免被 *.mp4 glob（_ordered_shot_files /
    # get_output_status）命中当成"分片/成片"；容器格式改由 -f mp4 显式指定。
    tmp_out = target + ".noaudio.clean" if in_place else target
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

    if backup and in_place:
        bak = os.path.splitext(video_path)[0] + ".withaudio.bak.mp4"
        try:
            shutil.copy2(video_path, bak)
            report["backup"] = bak
        except OSError as e:
            report["error"] = f"备份失败，已中止（不做无备份剥离）：{e}"
            return report

    # in_place 时输出到 .noaudio.clean（非 .mp4），必须显式 -f mp4 指定容器；
    # 非 in_place 时输出到调用方给的目标（容器按其自身扩展名推断），不强加 -f mp4
    fmt = ["-f", "mp4"] if in_place else []
    attempts = [
        ("copy", ["ffmpeg", "-y", "-i", video_path, "-map", "0:v", "-c", "copy", "-an"]
         + fmt + [tmp_out]),
        ("reencode_h264", ["ffmpeg", "-y", "-i", video_path, "-map", "0:v", "-an",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
         + fmt + [tmp_out]),
    ]
    last_err = ""
    for method, cmd in attempts:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0 and os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
            after = probe_media(tmp_out)
            if after.get("has_audio"):
                last_err = f"{method} 后仍检测到音轨"
                continue
            if in_place:
                try:
                    os.replace(tmp_out, target)
                except OSError as e:
                    # S-02：Windows 文件锁（多任务场景：autopilot 跑图 + 前端查看同一视频）
                    # os.replace 目标被占用会抛 OSError。保留 tmp_out 供下次清理，
                    # 原文件音轨未剥离（保留原状），但标记 error 让调用方知晓。
                    report["error"] = (f"剥离成功但 os.replace 失败（原文件保留音轨未变，"
                                       f"tmp 残留 {os.path.basename(tmp_out)}）：{e}")
                    report["ok"] = False
                    report["changed"] = False
                    report["method"] = method
                    report["elapsed_sec"] = round(time.time() - t0, 2)
                    logger.error(report["error"])
                    return report
            report.update({"ok": True, "changed": True, "method": method,
                           "output": os.path.abspath(target),
                           "has_audio_after": False,
                           "after": {k: after.get(k) for k in ("width", "height", "duration", "size_mb", "video_codec")}})
            report["elapsed_sec"] = round(time.time() - t0, 2)
            return report
        last_err = (r.stderr or "").strip()[-300:]
        logger.warning(f"剥离音轨失败（{method}）：{last_err[:160]}")

    try:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    except OSError:
        pass
    report["error"] = f"剥离音轨失败：{last_err}"
    report["elapsed_sec"] = round(time.time() - t0, 2)
    return report


def ensure_no_audio(video_path: str, backup: bool = True) -> Dict:
    """确保视频无音轨：ffprobe 校验 → 若无音轨直接返回；若有则剥离并复核"""
    report = strip_audio(video_path, output_path=None, backup=backup)
    if report.get("ok") and not report.get("changed"):
        report["message"] = "已确认无音轨（未改动文件）"
    return report


def ensure_audio_track(video_path: str, sample_rate: int = 48000) -> Dict:
    """确保视频**含有**音轨：已有音轨直接返回；没有则补一条等长静音 AAC 轨。

    为什么要补：成片拼接用的是 concat demuxer + `-c copy`。如果部分镜头有音轨、
    部分没有（H3 未必每镜都出声），拼出来的音轨会错位甚至拼接失败。
    统一补齐后拼接行为可预测。

    与本项目其它后处理一致：失败不抛异常，只把原因写进返回的 error。
    """
    t0 = time.time()
    report: Dict = {"ok": False, "target": os.path.abspath(video_path) if video_path else "",
                    "kind": "audio_pad", "changed": False, "method": None}
    if not video_path or not os.path.exists(video_path):
        report["error"] = "输入视频不存在"
        return report
    before = probe_media(video_path)
    report["has_audio_before"] = bool(before.get("has_audio"))
    report["before"] = {k: before.get(k) for k in ("width", "height", "duration", "size_mb")}
    if before.get("has_audio"):
        report.update({"ok": True, "changed": False, "has_audio_after": True,
                       "message": "已有音轨（未改动文件）"})
        report["elapsed_sec"] = round(time.time() - t0, 2)
        return report
    dur = float(before.get("duration") or 0)
    if dur <= 0:
        report["error"] = "无法读取视频时长，跳过补音轨"
        return report
    # G8：临时文件用 .clean（非 .mp4 后缀），避免被 *.mp4 glob 命中；容器由 -f mp4 显式指定
    tmp_out = os.path.splitext(video_path)[0] + ".audioadded.clean"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "128k", "-shortest",
           "-f", "mp4", "-movflags", "+faststart", tmp_out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as e:  # pragma: no cover - 环境相关
        report["error"] = f"{type(e).__name__}: {e}"
        return report
    if r.returncode != 0 or not os.path.exists(tmp_out):
        # B-16 P2-11：补音轨失败 → 清理中间产物（.clean 半成品）
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except OSError:
            pass
        report["error"] = (r.stderr or "ffmpeg 补音轨失败").strip()[-300:]
        return report
    after = probe_media(tmp_out)
    if not after.get("has_audio"):
        report["error"] = "补音轨后仍未检测到音频流"
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        return report
    os.replace(tmp_out, video_path)
    report.update({"ok": True, "changed": True, "has_audio_after": True,
                   "method": "silent_aac_pad",
                   "after": {k: after.get(k) for k in ("width", "height", "duration",
                                                       "size_mb", "audio_codec")}})
    report["elapsed_sec"] = round(time.time() - t0, 2)
    return report



class VideoPostProcessor:
    """视频后期处理器"""

    def __init__(self, comfyui_url: str = COMFYUI_URL):
        self.comfyui_url = comfyui_url

    def concat_videos(self, video_paths: List[str], output_path: str,
                     audio_paths: Optional[List[str]] = None) -> str:
        """使用 FFmpeg 合并视频

        S-01 修复：拼接前探测每个片段的音轨参数（codec/sample_rate/channels）。
        - 全部一致 → 走 concat demuxer + -c copy（快，不重编码）
        - 不一致（或部分有音轨/部分无）→ 走 concat filter 重编码（统一采样率/声道/编码器）
        """
        if not video_paths:
            logger.warning("合并视频失败：输入片段为空")
            return ""

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # 探测音轨参数一致性
        probes = [probe_media(p) for p in video_paths]
        audio_params = set()
        has_any_audio = False
        for p in probes:
            if p.get("has_audio"):
                has_any_audio = True
                audio_params.add((
                    p.get("audio_codec"), p.get("sample_rate"),
                    p.get("channels"),
                ))
        need_reencode = has_any_audio and len(audio_params) > 1

        if need_reencode:
            logger.info(f"音轨参数不一致（{len(audio_params)} 种），走 concat filter 重编码路径")
            return self._concat_reencode(video_paths, output_path, probes)

        # 参数一致 → 走 concat demuxer（快，不重编码）
        list_file = output_path + ".list"
        with open(list_file, 'w', encoding='utf-8') as f:
            for v in video_paths:
                f.write(f"file '{os.path.abspath(v)}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            output_path
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info(f"视频合并成功（demuxer -c copy）: {output_path}")
                os.remove(list_file)
                return output_path
            else:
                # -c copy 失败（常见原因：音视频参数实际不一致但探测未捕获，
                # 如 H265 vs H264 混拼、关键帧对齐失败）→ 降级重编码兜底
                logger.warning(f"demuxer 拼接失败，降级 concat filter 重编码：{result.stderr[-300:]}")
                os.remove(list_file) if os.path.exists(list_file) else None
                return self._concat_reencode(video_paths, output_path, probes)
        except Exception as e:
            logger.error(f"合并视频失败: {e}")
            # G8：异常路径也要清掉 concat list 临时文件，不留 .list 残片
            try:
                if os.path.exists(list_file):
                    os.remove(list_file)
            except OSError:
                pass
            return ""

    def _concat_reencode(self, video_paths: List[str], output_path: str,
                         probes: List[Dict]) -> str:
        """concat filter 重编码拼接（分辨率/帧率由源片实测推导，不硬编码）

        B-05 P1-4：原实现硬编码 scale=1280:720 + fps=30 → 竖屏 9:16 项目被悄悄
        改成横屏加黑边且不可逆。改为以「分辨率/帧率出现最多的片段」为基准，
        竖屏保持竖屏、帧率与源片一致。

        filter_complex 结构：
          [i:v:0]scale/fps/format → [vi]
          [i:a?]aresample/aformat → [ai]
          [v0][v1]...concat=n:1:0 → [outv]
          [a0][a1]...concat=n:0:1 → [outa]
        """
        if not video_paths:
            return ""
        n = len(video_paths)

        # B-05：从源片实测推导目标分辨率/帧率（以出现最多的宽/高/帧率为准）
        target_w, target_h = 1280, 720  # 兜底值（probe 全部失败时才用）
        target_fps = 30
        w_votes: Dict[int, int] = {}
        h_votes: Dict[int, int] = {}
        fps_votes: Dict[int, int] = {}
        for i, p in enumerate(video_paths):
            pro = probes[i] if i < len(probes) else {}
            w = int(pro.get("width") or 0)
            h = int(pro.get("height") or 0)
            fps = int(round(float(pro.get("fps") or 0)))
            if w and h:
                w_votes[w] = w_votes.get(w, 0) + 1
                h_votes[h] = h_votes.get(h, 0) + 1
            if fps:
                fps_votes[fps] = fps_votes.get(fps, 0) + 1
        if w_votes:
            target_w = max(w_votes, key=w_votes.get)
        if h_votes:
            target_h = max(h_votes, key=h_votes.get)
        if fps_votes:
            target_fps = max(fps_votes, key=fps_votes.get)
        # 宽高对齐到偶数（libx264 要求），防止 721 之类奇数
        target_w = target_w // 2 * 2 or 1280
        target_h = target_h // 2 * 2 or 720
        logger.info(f"降级重编码基准：{target_w}x{target_h}@{target_fps}fps（源片实测）")

        inputs: List[str] = []
        for p in video_paths:
            inputs += ["-i", os.path.abspath(p)]

        # 逐片段：视频归一化（scale 到源片基准 + fps + pix_fmt + sar）→ [vi]；音频归一化 → [ai]
        per_stream: List[str] = []
        for i in range(n):
            per_stream.append(
                f"[{i}:v:0]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={target_fps},format=yuv420p,setsar=1[v{i}]"
            )
            per_stream.append(
                f"[{i}:a?]aresample=24000,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]"
            )
        # 拼接
        v_concat_in = "".join(f"[v{i}]" for i in range(n))
        a_concat_in = "".join(f"[a{i}]" for i in range(n))
        per_stream.append(f"{v_concat_in}concat=n={n}:v=1:a=0[outv]")
        per_stream.append(f"{a_concat_in}concat=n={n}:v=0:a=1[outa]")
        filter_complex = ";".join(per_stream)

        cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + [
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]
        logger.info(f"concat filter 重编码拼接 {n} 个片段 → {output_path}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode == 0 and os.path.exists(output_path):
                logger.info(f"视频合并成功（filter 重编码）: {output_path}")
                return output_path
            logger.error(f"视频合并失败（filter 重编码）: {result.stderr[-500:]}")
            return ""
        except Exception as e:
            logger.error(f"视频合并异常（filter 重编码）: {e}")
            return ""

    def add_subtitles(self, video_path: str, subtitles: List[dict],
                     output_path: str) -> str:
        """添加字幕到视频（烧录）

        Windows 路径坑（实测踩过）：ffmpeg 的 `subtitles=` 是**滤镜参数**，其解析器
        会把反斜杠当转义符吃掉，`C:\\a\\b.srt` 会变成 `Cab.srt`，报
        "Unable to parse original_size option value ..."。
        因此这里改为：把 ffmpeg 的工作目录切到 SRT 所在目录，滤镜只传**纯文件名**。
        这样既不出现反斜杠也不用转义盘符冒号，且路径里的中文/空格也一并规避。
        """
        # G8：SRT 临时文件统一 finally 清理（成功则置 None 跳过；失败/异常路径不留残片）
        srt_file = None
        try:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

            # 生成 SRT 文件
            srt_file = output_path + ".srt"
            with open(srt_file, 'w', encoding='utf-8') as f:
                for i, sub in enumerate(subtitles, 1):
                    start = sub.get("start", 0)
                    end = sub.get("end", start + 3)
                    text = sub.get("text", "")
                    f.write(f"{i}\n")
                    f.write(f"{self._format_time(start)} --> {self._format_time(end)}\n")
                    f.write(f"{text}\n\n")

            srt_dir = os.path.dirname(os.path.abspath(srt_file))
            srt_name = os.path.basename(srt_file)
            # 文件名内可能含单引号（项目名极端情况），按 ffmpeg 滤镜语法转义
            filter_arg = "subtitles=filename='" + srt_name.replace("'", r"\'") + "'"
            # -map 0:a? 保证输入有音轨时不丢（?=可选，无音轨不报错）。
            # 不显式 map 时 ffmpeg 的 -vf 可能只选视频流，导致 H3 原生音效丢失。
            cmd = [
                "ffmpeg", "-y",
                "-i", os.path.abspath(video_path),
                "-vf", filter_arg,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-map", "0:v:0", "-map", "0:a?",
                "-c:a", "copy",
                os.path.abspath(output_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=1800, cwd=srt_dir)
            if result.returncode == 0 and os.path.isfile(output_path) \
                    and os.path.getsize(output_path) > 0:
                logger.info(f"字幕添加成功: {output_path}")
                srt_file = None
                return output_path
            logger.error(f"添加字幕失败（返回码 {result.returncode}）："
                         f"{(result.stderr or '')[-800:]}")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.error(f"添加字幕失败: {e}")
            return ""
        finally:
            if srt_file and os.path.exists(srt_file):
                try:
                    os.remove(srt_file)
                except OSError:
                    pass

    def _format_time(self, seconds: float) -> str:
        """格式化时间为 SRT 格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    def upscale_with_flashvsr(self, video_path: str, output_path: str,
                               scale: int = 4, project_name: Optional[str] = None,
                               mode: Optional[str] = None, **kwargs) -> Dict:
        """使用 FlashVSR 进行**真实**超分辨率（委托 upscale_client.VideoUpscaler）

        原实现为占位（只打日志并直接 return True，静默成功）；
        现改为调用已实跑验证的 FlashVSR Ultra-Fast 链路：
        VHS_LoadVideoPath → FlashVSRInitPipe → FlashVSRNodeAdv → VHS_VideoCombine。

        返回 VideoUpscaler 的结构化结果（before / after / elapsed_sec / output_path 等）；
        失败时抛出 upscale_client.UpscaleError，绝不静默返回成功。
        """
        import shutil
        from upscale_client import VideoUpscaler   # 延迟导入，避免与应用初始化互相依赖

        project = (project_name
                   or os.path.basename(os.path.dirname(os.path.abspath(output_path)))
                   or "project")
        logger.info(f"FlashVSR 超分: {video_path} -> {output_path}（{scale}x, mode={mode or 'default'}）")
        res = VideoUpscaler(self.comfyui_url).upscale(
            video_path, project_name=project, scale=int(scale), mode=mode, **kwargs)

        # 兼容旧签名：调用方指定了 output_path 时，再拷贝一份到该路径
        if output_path and os.path.abspath(output_path) != os.path.abspath(res["output_path"]):
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            shutil.copy2(res["output_path"], output_path)
            res["output_path"] = os.path.abspath(output_path)
        return res

    def generate_final_video(self, script_path: str, project_name: str,
                             episode_no=None) -> str:
        """生成最终视频（**按集**合成：第 1 集平铺、第 2 集起 epNN/）

        审计 S4 修复：
        1. 旧签名没有 `episode_no`，只 `listdir` 平铺目录 —— 多集项目点「生成成片」时，
           第 2 集及以后**完全漏掉**（拿到的是第 1 集的片段），而接口照样返回 success:true
           并把它登记进「成品验收」队列。现在集号取「入参 > 剧本 episode_no > 1」，
           目录口径与写入侧 `app._ep_dir` 一致；
        2. 文件名带上集号（`epNN_final.mp4`），与 `pipeline.final_path` / 集进度推导
           （`app.py` 的 `ep{ep:02d}_final.mp4`）统一 —— 此前手合成的成片叫 `<项目>.mp4`，
           托管侧的 `probe_final` 永远看不见；
        3. 只取逐镜 `shot_NN.*` 且按镜头号数字排序（排除 `*_full.mp4`，避免整集与各镜
           一起拼出内容重复的成片）；
        4. `concat_videos` 失败会返回 `""`，旧代码**忽略返回值**照样返回一个不存在的路径
           → 这里把失败上抛为 `""`。
        """
        with open(script_path, 'r', encoding='utf-8') as f:
            script = json.load(f)

        ep = _episode_no_of(script, episode_no)
        videos_dir = _ep_videos_dir(project_name, ep)
        final_dir = os.path.join(PROJECT_OUTPUT_DIR, "final", project_name)
        os.makedirs(videos_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)

        # 获取该集的视频片段（按镜头号排序；无逐镜片段时回退整集成片）
        video_files = _ordered_shot_files(videos_dir, ep)
        if not video_files:
            logger.warning(f"没有找到第 {ep} 集的视频片段：{videos_dir}")
            return ""

        # 合并视频
        output_path = os.path.join(final_dir, f"ep{ep:02d}_final.mp4")
        if not self.concat_videos(video_files, output_path):
            # ⚠️ 必须判返回值：否则拼失败时对外抛出一个**不存在**的 URL，还被登记成交付物
            logger.error(f"第 {ep} 集成片拼接失败（未产出有效文件）：{output_path}")
            return ""

        # 添加字幕（dialogue 兼容结构化 [{speaker,text}] 与旧字符串）
        from dialogue_utils import dialogue_text
        subtitles = []
        current_time = 0
        for shot in script.get("shots", []):
            duration = shot.get("duration", 5)
            text = dialogue_text(shot.get("dialogue"))
            if text:
                subtitles.append({
                    "start": current_time,
                    "end": current_time + duration,
                    "text": text
                })
            current_time += duration

        if subtitles:
            self.add_subtitles(output_path, subtitles,
                             os.path.join(final_dir, f"ep{ep:02d}_final_subtitles.mp4"))

        logger.info(f"第 {ep} 集最终视频: {output_path}")
        return output_path

    def get_output_status(self, project_name: str) -> Dict:
        """获取项目输出状态"""
        project_dir = os.path.join(PROJECT_OUTPUT_DIR, project_name)
        result = {
            "project": project_name,
            "exists": os.path.exists(project_dir),
            "videos": [],
            "images": [],
            "final": None
        }

        videos_dir = os.path.join(PROJECT_OUTPUT_DIR, "videos", project_name)
        final_dir = os.path.join(PROJECT_OUTPUT_DIR, "final", project_name)

        if os.path.exists(videos_dir):
            result["videos"] = [f for f in os.listdir(videos_dir) if f.endswith('.mp4')]

        if os.path.exists(final_dir):
            final_files = [f for f in os.listdir(final_dir) if f.endswith('.mp4')]
            result["final"] = final_files[0] if final_files else None

        return result
