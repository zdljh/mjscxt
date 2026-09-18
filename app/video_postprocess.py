"""
视频后期处理 - FlashVSR 超分辨率 + FFmpeg 合并
"""
import os
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
           "stream=index,codec_type,codec_name,width,height:format=duration,format_name",
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
    tmp_out = target + ".noaudio.tmp.mp4" if in_place else target
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

    if backup and in_place:
        bak = os.path.splitext(video_path)[0] + ".withaudio.bak.mp4"
        try:
            shutil.copy2(video_path, bak)
            report["backup"] = bak
        except OSError as e:
            report["error"] = f"备份失败，已中止（不做无备份剥离）：{e}"
            return report

    attempts = [
        ("copy", ["ffmpeg", "-y", "-i", video_path, "-map", "0:v", "-c", "copy", "-an", tmp_out]),
        ("reencode_h264", ["ffmpeg", "-y", "-i", video_path, "-map", "0:v", "-an",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", tmp_out]),
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
                os.replace(tmp_out, target)
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
    tmp_out = os.path.splitext(video_path)[0] + ".audioadded.tmp.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"anullsrc=r={sample_rate}:cl=stereo",
           "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "128k", "-shortest",
           "-movflags", "+faststart", tmp_out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception as e:  # pragma: no cover - 环境相关
        report["error"] = f"{type(e).__name__}: {e}"
        return report
    if r.returncode != 0 or not os.path.exists(tmp_out):
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
        """使用 FFmpeg 合并视频"""
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # 创建文件列表
        list_file = output_path + ".list"
        with open(list_file, 'w', encoding='utf-8') as f:
            for v in video_paths:
                f.write(f"file '{v}'\n")

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
                logger.info(f"视频合并成功: {output_path}")
                os.remove(list_file)
                return output_path
            else:
                logger.error(f"FFmpeg 错误: {result.stderr}")
                return ""
        except Exception as e:
            logger.error(f"合并视频失败: {e}")
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
                try:
                    os.remove(srt_file)
                except OSError:
                    pass
                return output_path
            logger.error(f"添加字幕失败（返回码 {result.returncode}）："
                         f"{(result.stderr or '')[-800:]}")
            return ""
        except Exception as e:  # noqa: BLE001
            logger.error(f"添加字幕失败: {e}")
            return ""

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

    def generate_final_video(self, script_path: str, project_name: str) -> str:
        """生成最终视频"""
        with open(script_path, 'r', encoding='utf-8') as f:
            script = json.load(f)

        videos_dir = os.path.join(PROJECT_OUTPUT_DIR, "videos", project_name)
        final_dir = os.path.join(PROJECT_OUTPUT_DIR, "final", project_name)
        os.makedirs(videos_dir, exist_ok=True)
        os.makedirs(final_dir, exist_ok=True)

        # 获取所有视频片段
        video_files = sorted([
            os.path.join(videos_dir, f)
            for f in os.listdir(videos_dir)
            if f.endswith('.mp4')
        ])

        if not video_files:
            logger.warning("没有找到视频片段")
            return ""

        # 合并视频
        output_path = os.path.join(final_dir, f"{project_name}.mp4")
        self.concat_videos(video_files, output_path)

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
                             os.path.join(final_dir, f"{project_name}_subtitles.mp4"))

        logger.info(f"最终视频: {output_path}")
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
