# -*- coding: utf-8 -*-
"""视频水印模块（C 项⑨⑩）

能力：
- 可配置水印：文案（或图片水印）、位置、字号、透明度、边距、总开关
- 「全视频移动」模式：位置随时间漂移（drift）/ 四角轮换（rotate），覆盖全片
- **默认关闭**（enabled=False）：不产出任何带水印文件

说明：
- 仅对**视频**生效；图片生成链路不添加任何水印（C 项⑧），本模块不参与图片生成
- 基于 ffmpeg drawtext / overlay 后处理，不改动 ComfyUI 原始工作流文件
- 调用方（app.py）在拿到水印配置且 enabled=True 时才调用 apply_watermark
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "enabled": False,             # 总开关：默认关闭（不加水印）
    "type": "text",               # text=文字水印 | image=图片水印
    "text": "漫剧生成系统",
    "image_path": "",             # 图片水印文件（type=image 时使用）
    "font_file": "",              # 留空自动挑选系统中文字体
    "font_size": 36,              # 字号（px，按视频原始分辨率）
    "font_color": "white",        # 文字颜色
    "opacity": 0.6,               # 透明度 0~1
    "margin": 24,                 # 边距（px）
    "position": "bottom-right",   # top-left / top-right / bottom-left / bottom-right / center
    "mode": "static",             # static=固定 | drift=漂移 | rotate=四角轮换（后两者为「全视频移动」）
    "move_period": 6.0,           # 移动/轮换周期（秒）
    "image_scale": 0.18,          # 图片水印宽度占视频宽度比例
    "updated_at": "",
}

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")
MODES = ("static", "drift", "rotate")
MODES_LABEL = {"static": "固定位置", "drift": "全视频移动·漂移", "rotate": "全视频移动·四角轮换"}

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def ffmpeg_exe() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _to_bool(v, default=False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "开", "是")


def _clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def resolve_font(font_file: str = "") -> str:
    """解析可用字体文件：优先用户指定，其次常见系统中文字体"""
    if font_file and os.path.isfile(font_file):
        return font_file
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def normalize(raw: dict = None, base: dict = None) -> dict:
    """归一化配置（非法值回落到默认值）"""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in (base or {}).items() if k in DEFAULT_CONFIG})
    cfg.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_CONFIG})
    cfg["enabled"] = _to_bool(cfg.get("enabled"), False)
    cfg["type"] = str(cfg.get("type") or "text").lower()
    if cfg["type"] not in ("text", "image"):
        cfg["type"] = "text"
    cfg["position"] = str(cfg.get("position") or "bottom-right")
    if cfg["position"] not in POSITIONS:
        cfg["position"] = "bottom-right"
    cfg["mode"] = str(cfg.get("mode") or "static")
    if cfg["mode"] not in MODES:
        cfg["mode"] = "static"
    cfg["text"] = str(cfg.get("text") or "")
    cfg["font_size"] = int(_clamp(cfg.get("font_size"), 8, 400, 36))
    cfg["opacity"] = round(_clamp(cfg.get("opacity"), 0.05, 1.0, 0.6), 3)
    cfg["margin"] = int(_clamp(cfg.get("margin"), 0, 500, 24))
    cfg["move_period"] = round(_clamp(cfg.get("move_period"), 1.0, 120.0, 6.0), 2)
    cfg["image_scale"] = round(_clamp(cfg.get("image_scale"), 0.02, 1.0, 0.18), 3)
    cfg["font_color"] = str(cfg.get("font_color") or "white")
    cfg["image_path"] = str(cfg.get("image_path") or "")
    cfg["font_file"] = str(cfg.get("font_file") or "")
    return cfg


def load_config(config_path: str) -> dict:
    raw = {}
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"水印配置读取失败（用默认值）：{e}")
    return normalize(raw)


def save_config(config_path: str, patch: dict) -> dict:
    cfg = normalize(patch, base=load_config(config_path))
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def public_view(cfg: dict) -> dict:
    cfg = normalize(cfg)
    font = resolve_font(cfg.get("font_file"))
    img_ok = bool(cfg.get("image_path") and os.path.isfile(cfg["image_path"]))
    ready = bool(ffmpeg_available()) and (
        bool(font) if cfg.get("type") == "text" else img_ok)
    if not cfg.get("enabled"):
        ready_state = "disabled"
    elif not ffmpeg_available():
        ready_state = "no_ffmpeg"
    elif cfg.get("type") == "text" and not font:
        ready_state = "no_font"
    elif cfg.get("type") == "image" and not img_ok:
        ready_state = "no_image"
    else:
        ready_state = "ready"
    return {
        "config": cfg,
        "enabled": cfg["enabled"],
        "type": cfg["type"],
        "mode": cfg["mode"],
        "mode_label": MODES_LABEL.get(cfg["mode"], cfg["mode"]),
        "position": cfg["position"],
        "font_resolved": font,
        "image_ok": img_ok,
        "ffmpeg": ffmpeg_available(),
        "ready": ready,
        "ready_state": ready_state,
        "positions": list(POSITIONS),
        "modes": [{"value": m, "label": MODES_LABEL[m]} for m in MODES],
    }


# --------------------------------------------------------------------------
# ffmpeg 滤镜构造
# --------------------------------------------------------------------------

def _esc_text(s: str) -> str:
    """转义 drawtext 文本中的特殊字符（:,;,\\,',%,[,] 与 filtergraph 分隔符）"""
    out = []
    for ch in s:
        if ch in ("\\", "'", ":", ";", "%", "[", "]", ",", "\n"):
            out.append("\\" + ("\n" if ch == "\n" else ch))
        else:
            out.append(ch)
    return "".join(out)


def _esc_path(p: str) -> str:
    """转义 filter 中的文件路径（Windows 盘符冒号 + 反斜杠）"""
    return p.replace("\\", "/").replace(":", "\\:")


def _pos_expr(position: str, margin: int):
    m = int(margin)
    table = {
        "top-left": (f"{m}", f"{m}"),
        "top-right": (f"W-w-{m}", f"{m}"),
        "bottom-left": (f"{m}", f"H-h-{m}"),
        "bottom-right": (f"W-w-{m}", f"H-h-{m}"),
        "center": ("(W-w)/2", "(H-h)/2"),
    }
    return table.get(position, table["bottom-right"])


def _xy_expr(cfg: dict):
    """按 mode 生成 x / y 表达式（drawtext 与 overlay 通用变量：W/H 画布、w/h 水印尺寸、t 秒）"""
    m = int(cfg["margin"])
    if cfg["mode"] == "drift":
        # 全片平滑漂移：横向 sin / 纵向 cos，始终全画面移动
        return ("(W-w)*abs(sin(t*0.35))", "(H-h)*abs(cos(t*0.28))")
    if cfg["mode"] == "rotate":
        p = float(cfg["move_period"])
        idx = f"mod(floor(t/{p})\\,4)"
        x = (f"if(or(eq({idx}\\,0)\\,eq({idx}\\,3))\\,{m}\\,W-w-{m})")
        y = (f"if(or(eq({idx}\\,0)\\,eq({idx}\\,1))\\,{m}\\,H-h-{m})")
        return (x, y)
    return _pos_expr(cfg["position"], m)


def build_filter(cfg: dict) -> dict:
    """生成 ffmpeg 滤镜参数。返回 {kind, vf/filter_complex, vmap, extra_inputs, error}"""
    cfg = normalize(cfg)
    opacity = cfg["opacity"]
    x, y = _xy_expr(cfg)
    if cfg["type"] == "image":
        img = cfg.get("image_path") or ""
        if not img or not os.path.isfile(img):
            return {"error": f"图片水印文件不存在：{img or '（未设置）'}"}
        sc = cfg["image_scale"]
        fc = (f"[1:v]format=rgba,scale=trunc(iw*{sc}/2)*2:-2,"
              f"colorchannelmixer=aa={opacity}[wm];"
              f"[0:v][wm]overlay=x={x}:y={y}:format=auto[v]")
        return {"kind": "image", "filter_complex": fc, "vmap": "[v]",
                "extra_inputs": [img], "error": ""}
    text = cfg.get("text") or ""
    if not text.strip():
        return {"error": "文字水印内容为空（请在设置里填写水印文案）"}
    font = resolve_font(cfg.get("font_file"))
    if not font:
        return {"error": "未找到可用字体（请在设置里指定 font_file，或安装中文字体）"}
    vf = ("drawtext=fontfile='{font}':text='{text}':fontsize={size}:"
          "fontcolor={color}@{alpha}:x={x}:y={y}:shadowcolor=black@0.35:shadowx=1:shadowy=1").format(
        font=_esc_path(font), text=_esc_text(text), size=cfg["font_size"],
        color=cfg["font_color"], alpha=opacity, x=x, y=y)
    return {"kind": "text", "vf": vf, "vmap": "", "extra_inputs": [], "error": ""}


def default_output_path(src: str, out_dir: str = "") -> str:
    base = os.path.splitext(os.path.basename(src))[0]
    if not out_dir:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(src)), "watermarked")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{base}_watermark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4")


def apply_watermark(src: str, dst: str = None, cfg: dict = None, out_dir: str = "",
                    timeout: int = 900) -> dict:
    """给视频烧写水印（后处理）。

    返回 {ok, out_path, cmd, elapsed, error, mode, type, enabled, skipped}
    - cfg.enabled=False 时不做任何事，直接返回 skipped=True（默认关闭）
    """
    cfg = normalize(cfg)
    result = {"ok": False, "out_path": "", "cmd": "", "elapsed": 0.0, "error": "",
              "mode": cfg["mode"], "type": cfg["type"], "enabled": cfg["enabled"],
              "skipped": False}
    if not cfg["enabled"]:
        result.update({"ok": True, "skipped": True, "error": ""})
        return result
    if not src or not os.path.isfile(src):
        result["error"] = f"源视频不存在：{src}"
        return result
    if not ffmpeg_available():
        result["error"] = "未找到 ffmpeg（请安装并加入 PATH）"
        return result

    flt = build_filter(cfg)
    if flt.get("error"):
        result["error"] = flt["error"]
        return result

    out_path = dst or default_output_path(src, out_dir)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    for extra in flt.get("extra_inputs") or []:
        cmd += ["-i", extra]
    if flt["kind"] == "image":
        cmd += ["-filter_complex", flt["filter_complex"], "-map", flt["vmap"]]
    else:
        cmd += ["-vf", flt["vf"], "-map", "0:v:0"]
    cmd += ["-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out_path]
    result["cmd"] = " ".join(cmd)

    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        result["elapsed"] = round(time.time() - t0, 2)
        if p.returncode != 0 or not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
            result["error"] = (p.stderr or "").strip()[:800] or f"ffmpeg 返回码 {p.returncode}"
            return result
    except Exception as e:  # noqa: BLE001
        result["elapsed"] = round(time.time() - t0, 2)
        result["error"] = f"水印烧写异常：{e}"
        return result

    result.update({"ok": True, "out_path": out_path})
    return result
