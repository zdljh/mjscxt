# -*- coding: utf-8 -*-
"""
QwenTTS 配音客户端（真实链路）
==============================

基于本机 ComfyUI（127.0.0.1:8188）已注册的 ComfyUI-Qwen-TTS 节点（FB_Qwen3TTS*）实现真实配音：

    FB_Qwen3TTSCustomVoice / FB_Qwen3TTSVoiceDesign  ->  SaveAudio  ->  ComfyUI output  ->  项目 output/dub/

设计要点
--------
* 角色音色一致性：每个角色固定一套「音色定义 + seed」，全剧逐句复用，保证同一角色音色不变；
* 两种音色模式：preset（9 个预置 speaker）/ design（中文音色描述驱动 VoiceDesign，可自定义音色）；
* 批量合成：把多句台词编进同一个 ComfyUI prompt（模型只加载一次），逐句回填产物路径与时长；
* 落盘隔离：项目侧产物统一写 output/dub/<项目>/（lines/ 单句 + 整集合并音频），
  ComfyUI 侧原始文件保留在 ComfyUI/output/dub/<项目>/，互不覆盖；
* 不静默失败：节点 / 模型缺失、ComfyUI 执行报错、未取到音频文件都会抛出 TTSError 并给出明确原因。
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from config import COMFYUI_URL, MODELS_DIR, TTS_DEFAULT_PARAMS
from dialogue_utils import (
    normalize_lines as _norm_dlg_lines, dialogue_text as _dlg_text,
    dialogue_speaker as _dlg_speaker,
    format_line as _dlg_line, has_dialogue as _has_dlg,
)

logger = logging.getLogger(__name__)

# 语音合成必需节点（Add-new：缺任一即视为环境不可用）
REQUIRED_NODES = ("FB_Qwen3TTSCustomVoice", "FB_Qwen3TTSVoiceDesign", "SaveAudio")
OPTIONAL_NODES = ("FB_Qwen3TTSVoiceClone", "FB_Qwen3TTSDialogueInference", "FB_Qwen3TTSRoleBank")

# 模型权重约定目录（ComfyUI-Qwen-TTS 从 models/qwen-tts 或 HF 缓存读取）
TTS_MODEL_ROOT = os.path.join(MODELS_DIR, "qwen-tts")
TTS_MODEL_EXPECTED = [
    ("Qwen3-TTS-12Hz-1.7B-CustomVoice", "1.7B 预置音色（主用）"),
    ("Qwen3-TTS-12Hz-1.7B-VoiceDesign", "1.7B 音色设计"),
    ("Qwen3-TTS-12Hz-0.6B-CustomVoice", "0.6B 预置音色（低显存备选）"),
    ("Qwen3-TTS-Tokenizer-12Hz", "音频 Tokenizer / 编解码"),
]

# 9 个预置音色（来自节点 schema，实测可用）
VOICE_PRESETS = [
    {"speaker": "Ryan", "label": "Ryan · 少年清亮", "gender": "male"},
    {"speaker": "Aiden", "label": "Aiden · 青年清朗", "gender": "male"},
    {"speaker": "Dylan", "label": "Dylan · 青年阳光", "gender": "male"},
    {"speaker": "Eric", "label": "Eric · 沉稳磁性", "gender": "male"},
    {"speaker": "Uncle_fu", "label": "Uncle_fu · 中年浑厚", "gender": "male"},
    {"speaker": "Serena", "label": "Serena · 温柔知性", "gender": "female"},
    {"speaker": "Vivian", "label": "Vivian · 干练明亮", "gender": "female"},
    {"speaker": "Ono_anna", "label": "Ono_anna · 活泼灵动", "gender": "female"},
    {"speaker": "Sohee", "label": "Sohee · 甜美少女", "gender": "female"},
]
SPEAKER_KEYS = tuple(v["speaker"] for v in VOICE_PRESETS)
MALE_POOL = ["Ryan", "Aiden", "Dylan", "Eric", "Uncle_fu"]
FEMALE_POOL = ["Serena", "Vivian", "Ono_anna", "Sohee"]
# 说话人兜底名（非角色表中的真实角色）：当某句台词无法定位说话人时用它的默认音色。
# ⚠️ 2026-09-19 起它**不再是**「旁白补声」入口 —— 旁白通道已关闭（见 build_dub_plan 注释），
# 这里只作为「说话人识别不出来」时的音色兜底保留。
NARRATION_SPEAKER = "旁白"

LANGUAGES = ["Auto", "Chinese", "English", "Japanese", "Korean", "French",
             "German", "Spanish", "Portuguese", "Russian", "Italian"]

_FEMALE_HINTS = ("女", "少女", "姑娘", "女子", "母", "姐", "妹", "娘", "婆婆", "妃", "后",
                 "妈", "妮", "姬", "丫鬟", "圣女", "仙姑", "丫头")
_ELDER_HINTS = ("老", "苍老", "沧桑", "古稀", "岁月", "长辈", "长老", "爷爷", "老翁", "师尊")


class TTSError(Exception):
    """配音链路业务异常（环境不可用 / 执行失败 / 产物缺失）"""


# ===================== 基础工具 =====================

def _http_json(url: str, payload: Optional[dict] = None, timeout: int = 60):
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_bytes(url: str, timeout: int = 300) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read()


def probe_audio(path: str) -> Dict:
    """ffprobe 读取音频信息（时长/编码/采样率/声道/大小）"""
    info = {"path": os.path.abspath(path), "ok": False}
    if not path or not os.path.exists(path):
        info["error"] = "文件不存在"
        return info
    info["size_bytes"] = os.path.getsize(path)
    info["size_mb"] = round(info["size_bytes"] / 1048576, 3)
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_name,sample_rate,channels",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            info["error"] = (r.stderr or "ffprobe 失败").strip()[:200]
            return info
        d = json.loads(r.stdout or "{}")
        fmt = d.get("format") or {}
        st = (d.get("streams") or [{}])[0]
        info.update({
            "ok": True,
            "duration": round(float(fmt.get("duration") or 0), 3),
            "codec": st.get("codec_name"),
            "sample_rate": st.get("sample_rate"),
            "channels": st.get("channels"),
        })
    except Exception as e:  # pragma: no cover - 环境相关
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def concat_audio(paths: List[str], output_path: str, fmt: str = "wav") -> str:
    """把多个音频片段按顺序合并为一个音轨（ffmpeg concat filter，重编码保证兼容）"""
    paths = [p for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        raise TTSError("没有可合并的音频片段")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-i", p]
    filt = "".join(f"[{i}:a]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=0:a=1[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]"]
    cmd += ["-c:a", "libmp3lame", "-q:a", "2", output_path] if fmt == "mp3" \
        else ["-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1", output_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise TTSError(f"音频合并失败: {(r.stderr or '').strip()[:300]}")
    return os.path.abspath(output_path)


def safe_name(name: str, limit: int = 40) -> str:
    keep = [c for c in str(name or "") if c.isalnum() or c in "_-" or "\u4e00" <= c <= "\u9fff"]
    return "".join(keep)[:limit] or "line"


def stable_seed(key: str) -> int:
    """由角色名派生的稳定种子：同一角色在任何一次运行中都得到同一音色参数"""
    h = hashlib.md5(str(key).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % (2 ** 31 - 1) or 12345


def guess_gender(text: str) -> str:
    s = str(text or "")
    if any(k in s for k in _FEMALE_HINTS):
        return "female"
    return "male"


def is_elder(text: str) -> bool:
    return any(k in str(text or "") for k in _ELDER_HINTS)


# ===================== 环境自检 =====================

def check_environment(comfyui_url: str = COMFYUI_URL) -> Dict:
    """核查 ComfyUI 侧 Qwen-TTS 节点与模型权重，给出可用/缺失结论（不臆测）"""
    result = {
        "available": False, "reasons": [], "nodes": {}, "speakers": list(SPEAKER_KEYS),
        "comfyui_online": False, "model_root": TTS_MODEL_ROOT,
        "model_dirs": [], "expected_models": [], "default_params": dict(TTS_DEFAULT_PARAMS),
    }
    try:
        info = _http_json(f"{comfyui_url.rstrip('/')}/object_info", timeout=60)
        result["comfyui_online"] = True
    except Exception as e:
        result["reasons"].append(f"ComfyUI 不可访问（{comfyui_url}）：{type(e).__name__} {e}")
        return result

    for n in REQUIRED_NODES:
        result["nodes"][n] = n in info
    for n in OPTIONAL_NODES:
        result["nodes"][n] = n in info
    missing_nodes = [n for n in REQUIRED_NODES if not result["nodes"].get(n)]
    if missing_nodes:
        result["reasons"].append("缺少 Qwen-TTS 节点：" + "、".join(missing_nodes))

    node = info.get("FB_Qwen3TTSCustomVoice") or {}
    try:
        speakers = node["input"]["required"]["speaker"][0]
        if isinstance(speakers, list) and speakers:
            result["speakers"] = speakers
    except Exception:
        pass

    # 模型权重：models/qwen-tts 下逐项核对
    if os.path.isdir(TTS_MODEL_ROOT):
        try:
            result["model_dirs"] = sorted(
                d for d in os.listdir(TTS_MODEL_ROOT)
                if os.path.isdir(os.path.join(TTS_MODEL_ROOT, d)))
        except OSError as e:
            result["reasons"].append(f"模型目录不可读：{e}")
    else:
        result["reasons"].append(f"模型根目录不存在：{TTS_MODEL_ROOT}")

    found_names = " ".join(result["model_dirs"]).lower()
    for expect, desc in TTS_MODEL_EXPECTED:
        hit = [d for d in result["model_dirs"] if expect.split("Qwen3-TTS-")[-1].lower() in d.lower()] \
            or ([d for d in result["model_dirs"] if expect.lower() in d.lower()] if expect.lower() in found_names else [])
        result["expected_models"].append({
            "name": expect, "desc": desc, "found": bool(hit), "matched": hit,
        })
    custom_ok = any(m["found"] for m in result["expected_models"] if "CustomVoice" in m["name"])
    if not custom_ok:
        result["reasons"].append("未找到 CustomVoice 预置音色权重（models/qwen-tts 下）")

    result["available"] = not result["reasons"]
    return result


def list_voices() -> Dict:
    return {
        "speakers": VOICE_PRESETS,
        "languages": LANGUAGES,
        "model_choices": ["1.7B", "0.6B"],
        "modes": [
            {"key": "preset", "label": "预置音色（9 个内置音色，稳定）"},
            {"key": "design", "label": "音色设计（按中文描述生成音色，可自定义）"},
        ],
    }


# ===================== 配音计划（角色 → 音色） =====================

# 中性情绪：这类词出现时没必要切 design 模式（切了反而可能让音色漂移，白花时间）
_NEUTRAL_EMOTIONS = ("平静", "平靜", "平常", "正常", "无", "無", "普通", "一般", "",
                     "中性", "陈述", "陳述", "陈述句")

# 情绪 → 语气描述（给 VoiceDesign 节点的中文指令）
_EMOTION_HINTS = {
    "愤怒": "愤怒地咬牙说出，语速偏快、声音发紧",
    "生气": "带怒气地说，语气冲",
    "低沉": "压低声音，语速缓慢、语气沉重",
    "悲伤": "带着哭腔，声音颤抖、语速慢",
    "难过": "声音低哑，带着失落",
    "惊喜": "语气上扬，带着意外的兴奋",
    "兴奋": "语速快、音调高，情绪饱满",
    "紧张": "语速急促、声音发紧，带着不安",
    "恐惧": "声音发抖、气息不稳，带着害怕",
    "害怕": "声音发抖、怯懦",
    "冷静": "语气平稳克制，不带起伏",
    "坚定": "语气斩钉截铁，字字有力",
    "温柔": "语气温柔舒缓，带着笑意",
    "疑惑": "语气上扬，带着疑问",
    "嘲讽": "带着讽刺与轻蔑，语调拖长",
    "疲惫": "气息虚弱，语速缓慢",
    "焦急": "语速很快，透着急切",
}


def _emotion_aware() -> bool:
    """是否启用情绪化配音（TTS_DEFAULT_PARAMS.emotion_aware，默认开）"""
    try:
        return bool(TTS_DEFAULT_PARAMS.get("emotion_aware", True))
    except Exception:  # noqa: BLE001
        return True


def _is_neutral_emotion(emotion: str) -> bool:
    e = str(emotion or "").strip()
    if not e:
        return True
    return any(k and k in e for k in _NEUTRAL_EMOTIONS)


def _emotion_instruct(emotion: str, char_desc: str = "") -> str:
    """把「角色音色底稿 + 该镜情绪」拼成 VoiceDesign 的 instruct。

    角色描述放前面用于稳住音色，情绪描述放后面控制语气 —— 顺序反了会
    让模型把情绪当成音色特征，导致同一角色每句听起来都不像同一个人。
    """
    e = str(emotion or "").strip()
    hint = ""
    for k, v in _EMOTION_HINTS.items():
        if k in e:
            hint = v
            break
    if not hint:
        hint = f"语气：{e}"
    desc = str(char_desc or "").strip()
    # 角色描述可能自带「，」结尾，这里统一裁剪避免重复标点
    return f"{desc}；{hint}".strip("；， ").strip("；") if desc else hint


def default_voice_map(characters: List[dict], project: str = "", episode: int = 1) -> Dict:
    """按剧本角色生成默认音色映射：同角色固定 speaker + seed（全剧一致）

    优先采用角色自带的 tts_voice / speaker 字段；否则按性别线索从音色池顺序分配。
    """
    used_male, used_female = 0, 0
    chars: Dict[str, Dict] = {}
    for idx, ch in enumerate(characters or []):
        name = str(ch.get("name") or f"角色{idx + 1}")
        if name in chars:
            continue
        explicit = str(ch.get("tts_voice") or ch.get("speaker") or "").strip()
        if explicit in SPEAKER_KEYS:
            speaker = explicit
        else:
            hint = " ".join(str(ch.get(k) or "") for k in
                            ("voice_style", "appearance", "personality", "name", "description"))
            pool = FEMALE_POOL if guess_gender(hint) == "female" else MALE_POOL
            i = used_female if pool is FEMALE_POOL else used_male
            speaker = pool[i % len(pool)]
            if pool is FEMALE_POOL:
                used_female += 1
            else:
                used_male += 1
        voice_style = str(ch.get("voice_style") or "").strip()
        chars[name] = {
            "mode": "preset",
            "speaker": speaker,
            "instruct": voice_style,          # 仅 design 模式生效；preset 下作为参考描述保存
            "seed": stable_seed(f"{project}|{name}"),
            "model_choice": TTS_DEFAULT_PARAMS["model_choice"],
            "language": TTS_DEFAULT_PARAMS["language"],
        }
    return {
        "project": project,
        "episode": episode,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "characters": chars,
        "lines": {},
    }


def normalize_voice(raw: Optional[dict], fallback: Optional[dict] = None) -> Dict:
    """校验并归一化单个音色配置（非法 speaker 直接报错，不静默回退）"""
    base = dict(fallback or {})
    raw = raw or {}
    voice = {
        "mode": raw.get("mode") or base.get("mode") or "preset",
        "speaker": raw.get("speaker") or base.get("speaker") or SPEAKER_KEYS[0],
        "instruct": raw.get("instruct", base.get("instruct", "")) or "",
        "seed": int(raw.get("seed") if raw.get("seed") is not None else (base.get("seed") or 0)),
        "model_choice": raw.get("model_choice") or base.get("model_choice") or TTS_DEFAULT_PARAMS["model_choice"],
        "language": raw.get("language") or base.get("language") or TTS_DEFAULT_PARAMS["language"],
        "temperature": float(raw.get("temperature") or base.get("temperature") or TTS_DEFAULT_PARAMS["temperature"]),
    }
    if voice["mode"] not in ("preset", "design"):
        raise TTSError(f"不支持的音色模式：{voice['mode']}")
    if voice["mode"] == "preset" and voice["speaker"] not in SPEAKER_KEYS:
        raise TTSError(f"不支持的预置音色：{voice['speaker']}（可选：{'、'.join(SPEAKER_KEYS)}）")
    if voice["mode"] == "design" and not str(voice["instruct"]).strip():
        raise TTSError("音色设计模式需要提供音色描述（instruct）")
    if voice["model_choice"] not in ("1.7B", "0.6B"):
        raise TTSError(f"不支持的模型规格：{voice['model_choice']}")
    return voice


def clean_line_text(text: str, character: str = "") -> str:
    """清洗台词：去除引号/说话人前缀/舞台提示括号，保留可朗读文本"""
    s = str(text or "").strip()
    for q in ("“", "”", "\"", "「", "」", "『", "』"):
        s = s.replace(q, "")
    if character:
        for pat in (f"{character}：", f"{character}:", f"{character} :"):
            if s.startswith(pat):
                s = s[len(pat):].strip()
    s = s.replace("（", "(").replace("）", ")")
    while "(" in s and ")" in s:
        a, b = s.find("("), s.find(")")
        if a == -1 or b < a:
            break
        s = (s[:a] + s[b + 1:]).strip()
    return s.strip()


SELF_REF_MARKERS = ("吾乃", "吾是", "老夫", "在下", "本座", "余乃", "我叫", "我是", "某乃")


def _first_present(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_dialogue(raw) -> Tuple[str, str]:
    """兼容台词多种写法，返回 (说话人, 台词文本)

    内部委托 dialogue_utils，保证与剧本/分镜生成阶段的结构定义完全一致：
    - 结构化 [{"speaker","text"}] / {"speaker","text"}：原样读取；
    - dict 兼容 character/role/line/content 等别名；
    - 旧纯字符串：说话人为空（由上层按角色表推断）。
    """
    return _dlg_speaker(raw), _dlg_text(raw)


def infer_character(text: str, characters: List[dict]) -> str:
    """台词未标注说话人时按文本推断（角色全名 / 名字片段 / 自称句式）"""
    if not text:
        return ""
    best, best_score, best_len = "", 0, 0
    for ch in characters or []:
        name = _first_present(ch.get("name") if isinstance(ch, dict) else str(ch))
        if not name:
            continue
        score = 0
        if name in text:
            score = 3
        else:
            tokens = [t for t in name.replace("·", " ").replace("・", " ").split() if t]
            for t in tokens:
                if len(t) >= 2 and t in text:
                    score = max(score, 2)
                elif len(t) == 1 and t in text and any(m in text for m in SELF_REF_MARKERS):
                    score = max(score, 2)
        if score > best_score or (score == best_score and score and len(name) > best_len):
            best, best_score, best_len = name, score, len(name)
    return best


def build_dub_plan(script: Dict, voice_map: Optional[Dict] = None,
                   project: str = "", episode: int = 1,
                   shot_ids: Optional[List] = None,
                   only_missing: bool = False,
                   out_dir_wav: str = "") -> Dict:
    """由剧本 + 音色映射生成逐句配音计划

    默认取该镜头出场角色中的第一位作为说话人（shots[].characters_in_shot），
    可在 voice_map["lines"][<shot_id>] 中按句覆盖角色 / 音色 / seed。
    """
    characters = script.get("characters") or []
    vmap = json.loads(json.dumps(voice_map or default_voice_map(characters, project, episode),
                                 ensure_ascii=False))
    vmap.setdefault("characters", {})
    vmap.setdefault("lines", {})

    # 角色音色底稿：情绪化配音时固定这部分，只让「情绪」变，避免每句音色漂移
    _char_desc = {}
    for _ch in characters:
        _nm = str(_ch.get("name") or "").strip()
        if not _nm:
            continue
        _char_desc[_nm] = "，".join(
            str(_ch.get(k) or "").strip() for k in
            ("voice_style", "personality", "appearance", "description")
            if str(_ch.get(k) or "").strip())[:120]

    # 逐句角色覆盖：voice_map["lines"][str(shot_id)] = {"character": "...", ...}
    lines: List[Dict] = []
    legacy_narration: List[str] = []   # 旧剧本里「只剩旁白、没有台词」的镜头（旁白通道已关闭，会被记警告）
    for shot in script.get("shots") or []:
        shot_id = shot.get("shot_id")
        if shot_ids and str(shot_id) not in [str(s) for s in shot_ids]:
            continue
        cast = shot.get("characters_in_shot") or shot.get("characters") or []
        # 台词来源：结构化 dialogue（[{speaker,text}]）优先，旧剧本回退 dialogue_text / 字符串推断
        dlg_rows = _norm_dlg_lines(shot.get("dialogue"), characters, cast)
        if not dlg_rows and shot.get("dialogue_text"):
            dlg_rows = _norm_dlg_lines(shot.get("dialogue_text"), characters, cast)
        # ---- 旁白通道已关闭（2026-09-19）----
        # 原实现会在这里把 narration（心理活动/背景补叙/环境描写）用默认「旁白」音色补声，
        # 目的是让「无台词但有旁白」的镜头不成片无声。但旁白被当成原文叙述的公共出口后，
        # 一句句画外音解说把成片彻底淹没（实测 ep04 旁白 2231 字 ≈ 496 秒铺在 100 秒画面上，
        # 尾部被 `-shortest` 静默截掉）。现在产品侧已决定「成片不产出旁白」：
        # 剧本阶段不再写 narration（novel_to_script.REWRITE_RULES 第 8 条），配音链路也不再念它。
        # 旧剧本里残留的 narration 会被显式记账（见下方 legacy_narration），不静默丢弃。
        if not dlg_rows and clean_line_text(shot.get("narration")):
            legacy_narration.append(str(shot_id))
        override = (vmap.get("lines") or {}).get(str(shot_id)) or {}
        multi = len(dlg_rows) > 1
        for li, row in enumerate(dlg_rows):
            text = clean_line_text(row.get("text"))
            if not text:
                continue
            dlg_speaker = row.get("speaker") or ""
            char_name = _first_present(
                override.get("character"), dlg_speaker,
                shot.get("dialogue_speaker"), shot.get("speaker"), shot.get("character"),
                cast[0] if cast else "", NARRATION_SPEAKER)
            base_voice = vmap["characters"].get(char_name) or default_voice_map(
                [{"name": char_name}], project, episode)["characters"].get(char_name)
            voice = normalize_voice(override, base_voice)
            # ---- 情绪化配音 ----
            # 剧本每镜都带 emotion 字段（如「愤怒 / 低沉 / 惊喜」），但此前**完全没被
            # 送进 TTS**：每句都用角色级固定 preset 音色，于是所有台词听起来一个调。
            # 这里把该镜情绪拼进 instruct，并在确实有情绪时切到 design 模式
            # （preset 模式下 CustomVoice 节点会把 instruct 当备注忽略，只有
            #  VoiceDesign 节点才真正按 instruct 控制语气）。
            emotion = str(shot.get("emotion") or "").strip()
            if emotion and _emotion_aware() and not _is_neutral_emotion(emotion):
                desc = _char_desc.get(char_name) or ""
                voice = dict(voice, mode="design",
                             instruct=_emotion_instruct(emotion, desc))
            suffix = f"_{li + 1}" if multi else ""
            out_name = (f"ep{int(episode):02d}_shot{int(shot_id):02d}{suffix}"
                        f"_{safe_name(char_name, 12)}.wav")
            out_path = os.path.join(out_dir_wav, out_name) if out_dir_wav else out_name
            if only_missing and out_dir_wav and os.path.exists(out_path) \
                    and os.path.getsize(out_path) > 0:
                continue
            lines.append({
                "line_id": f"ep{int(episode):02d}_shot{int(shot_id):02d}{suffix}",
                "shot_id": shot_id,
                "character": char_name,
                "text": text,
                # 配音行一律来自剧本 dialogue（旁白通道已于 2026-09-19 关闭）
                "source": "dialogue",
                "duration_hint": shot.get("duration"),
                "emotion": emotion,
                "voice": voice,
                "out_name": out_name,
                "out_path": os.path.abspath(out_path) if out_dir_wav else "",
            })

    chars_out = []
    for ch in characters:
        name = str(ch.get("name") or "")
        voice = normalize_voice(vmap["characters"].get(name), None) if name in vmap["characters"] else None
        used = sum(1 for l in lines if l["character"] == name)
        chars_out.append({
            "name": name,
            "voice_style": ch.get("voice_style") or "",
            "voice": voice,
            "line_count": used,
        })
    warns: List[str] = []
    if legacy_narration:
        warns.append(
            f"有 {len(legacy_narration)} 个镜头只有旁白、没有台词（镜头 {', '.join(legacy_narration[:8])}"
            f"{' 等' if len(legacy_narration) > 8 else ''}）：旁白通道已关闭，这些镜头不会产出配音，"
            f"成片会留白。这批剧本是旁白改造前生成的，建议重新生成剧本"
            f"（原文的心理活动应当改写成该角色的自语台词）。")
    return {
        "project": project, "episode": episode,
        "characters": chars_out,
        "lines": lines,
        "line_count": len(lines),
        "warnings": warns,
        "voice_map": vmap,
    }


def save_voice_map(voice_map: Dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = dict(voice_map or {})
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


def load_voice_map(path: str) -> Optional[Dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"音色映射读取失败 {path}: {e}")
        return None


# ===================== 合成客户端 =====================

class QwenTTSClient:
    """通过 ComfyUI API 调用 Qwen-TTS 节点做真实语音合成"""

    def __init__(self, comfyui_url: str = COMFYUI_URL,
                 out_root: str = None, params: Dict = None):
        self.comfyui_url = comfyui_url.rstrip("/")
        self.params = dict(TTS_DEFAULT_PARAMS)
        if params:
            self.params.update(params)
        self.out_root = out_root

    # ---------- 低层 ----------

    def _submit(self, prompt: Dict) -> str:
        r = _http_json(f"{self.comfyui_url}/prompt",
                       {"prompt": prompt, "client_id": f"dub-{int(time.time())}"})
        if not r.get("prompt_id"):
            raise TTSError(f"ComfyUI 未返回 prompt_id：{json.dumps(r, ensure_ascii=False)[:300]}")
        if r.get("node_errors"):
            raise TTSError(f"工作流校验失败：{json.dumps(r['node_errors'], ensure_ascii=False)[:400]}")
        return r["prompt_id"]

    def _wait(self, prompt_id: str, timeout: int,
              progress_cb: Optional[Callable[[str], None]] = None) -> Dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                h = _http_json(f"{self.comfyui_url}/history/{prompt_id}", timeout=60)
            except Exception:
                time.sleep(3)
                continue
            if prompt_id in h:
                entry = h[prompt_id]
                status = entry.get("status") or {}
                if status.get("status_str") == "error" or not status.get("completed", True):
                    msgs = status.get("messages") or []
                    detail = ""
                    for m in msgs:
                        if isinstance(m, list) and m and m[0] in ("execution_error", "execution_interrupted"):
                            detail = json.dumps(m[1] if len(m) > 1 else m, ensure_ascii=False)[:400]
                    raise TTSError(f"ComfyUI 执行失败：{detail or status.get('status_str')}")
                return entry
            if progress_cb:
                progress_cb(f"ComfyUI 合成中（{int(time.time() - t0)}s）")
            time.sleep(3)
        raise TTSError(f"配音超时（>{timeout}s），prompt_id={prompt_id}")

    def _download_and_convert(self, item: dict, raw_info: dict,
                              out_path: str) -> Dict:
        q = urllib.parse.urlencode({
            "filename": raw_info.get("filename"),
            "subfolder": raw_info.get("subfolder") or "",
            "type": raw_info.get("type") or "output",
        })
        blob = _http_bytes(f"{self.comfyui_url}/view?{q}", timeout=600)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        suffix = os.path.splitext(raw_info.get("filename") or ".flac")[1] or ".flac"
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="tts_raw_")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(blob)
            cmd = ["ffmpeg", "-y", "-i", tmp, "-ar", "24000", "-ac", "1",
                   "-c:a", "pcm_s16le", out_path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                raise TTSError(f"音频转码失败: {(r.stderr or '').strip()[:300]}")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        info = probe_audio(out_path)
        if not info.get("ok") or (info.get("duration") or 0) <= 0:
            raise TTSError(f"产物音频无效：{out_path}（{info.get('error') or '时长为 0'}）")
        return info

    # ---------- 高层 ----------

    def _node_inputs(self, text: str, voice: dict, is_last: bool) -> Dict:
        unload = not self.params.get("keep_model_loaded", False) and is_last
        common = {
            "model_choice": voice.get("model_choice") or self.params["model_choice"],
            "device": self.params.get("device", "auto"),
            "precision": self.params.get("precision", "bf16"),
            "language": voice.get("language") or self.params["language"],
            "seed": int(voice.get("seed") or 0),
            "max_new_tokens": int(self.params.get("max_new_tokens", 2048)),
            "top_p": float(self.params.get("top_p", 0.8)),
            "top_k": int(self.params.get("top_k", 20)),
            "temperature": float(voice.get("temperature") or self.params.get("temperature", 1.0)),
            "repetition_penalty": float(self.params.get("repetition_penalty", 1.05)),
            "attention": self.params.get("attention", "auto"),
            "unload_model_after_generate": bool(unload),
        }
        if voice.get("mode") == "design":
            return "FB_Qwen3TTSVoiceDesign", dict(common, text=text,
                                                  instruct=str(voice.get("instruct") or ""))
        return "FB_Qwen3TTSCustomVoice", dict(common, text=text,
                                              speaker=voice.get("speaker") or SPEAKER_KEYS[0],
                                              instruct=str(voice.get("instruct") or ""))

    def synthesize_batch(self, items: List[Dict],
                         progress_cb: Optional[Callable[[int, int, dict, str], None]] = None,
                         timeout: int = None) -> List[Dict]:
        """批量合成：items = [{text, voice, out_path, store_prefix}]，逐句回填产物信息"""
        timeout = int(timeout or self.params.get("timeout", 1200))
        prompt: Dict = {}
        bindings: List[Dict] = []
        for i, it in enumerate(items):
            ctype, inputs = self._node_inputs(it["text"], it["voice"], i == len(items) - 1)
            cv, sv = f"cv{i}", f"sv{i}"
            prompt[cv] = {"class_type": ctype, "inputs": inputs}
            prompt[sv] = {"class_type": "SaveAudio",
                          "inputs": {"audio": [cv, 0], "filename_prefix": it["store_prefix"]}}
            bindings.append({"cv": cv, "sv": sv, "item": it})

        logger.info(f"QwenTTS 批量合成 {len(items)} 句 → prompt 节点 {len(prompt)} 个")
        prompt_id = self._submit(prompt)
        history = self._wait(prompt_id, timeout, progress_cb=lambda m: None)
        outputs = history.get("outputs") or {}

        results: List[Dict] = []
        for b in bindings:
            it, sv = b["item"], b["sv"]
            raw = ((outputs.get(sv) or {}).get("audio") or [None])[0]
            rec = {"line_id": it.get("line_id"), "shot_id": it.get("shot_id"),
                   "character": it.get("character"), "text": it["text"],
                   "speaker": it["voice"].get("speaker"), "mode": it["voice"].get("mode"),
                   "seed": it["voice"].get("seed"), "out_path": os.path.abspath(it["out_path"]),
                   "prompt_id": prompt_id}
            if not raw:
                rec.update({"ok": False, "error": "ComfyUI 未返回音频文件"})
                results.append(rec)
                continue
            try:
                info = self._download_and_convert(it, raw, it["out_path"])
                rec.update({"ok": True, "comfy_raw": raw.get("filename"),
                            "comfy_subfolder": raw.get("subfolder"),
                            "duration": info.get("duration"), "size_bytes": info.get("size_bytes"),
                            "codec": info.get("codec"), "sample_rate": info.get("sample_rate"),
                            "channels": info.get("channels")})
            except TTSError as e:
                rec.update({"ok": False, "error": str(e)})
            results.append(rec)
        return results

    def synthesize_lines(self, lines: List[Dict], out_dir: str,
                         progress_cb: Optional[Callable[[int, int, dict, str], None]] = None,
                         timeout: int = None) -> List[Dict]:
        """按行合成（自动分批；批失败时降级为逐句重试，单句失败不影响其它句）"""
        batch_size = max(1, int(self.params.get("batch_lines", 4)))
        os.makedirs(out_dir, exist_ok=True)
        prepared: List[Dict] = []
        for i, ln in enumerate(lines):
            out_path = ln.get("out_path") or os.path.join(out_dir, ln.get("out_name") or f"line_{i:02d}.wav")
            prepared.append({
                "text": ln["text"], "voice": ln["voice"], "out_path": out_path,
                "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
                "character": ln.get("character"),
                "store_prefix": f"dub/{ln.get('project_tag') or 'project'}/{os.path.splitext(os.path.basename(out_path))[0]}",
            })

        all_results: List[Dict] = []
        done = 0
        for start in range(0, len(prepared), batch_size):
            batch = prepared[start:start + batch_size]
            try:
                res = self.synthesize_batch(batch, timeout=timeout)
            except TTSError as e:
                logger.warning(f"批量合成失败（{len(batch)} 句），降级逐句重试：{e}")
                res = []
                for one in batch:
                    try:
                        res.extend(self.synthesize_batch([one], timeout=timeout))
                    except TTSError as e2:
                        res.append({"ok": False, "line_id": one.get("line_id"),
                                    "shot_id": one.get("shot_id"), "character": one.get("character"),
                                    "out_path": os.path.abspath(one["out_path"]),
                                    "error": str(e2), "text": one["text"]})
            all_results.extend(res)
            done += len(batch)
            if progress_cb:
                progress_cb(done, len(prepared), res[-1] if res else batch[-1],
                            "" if all(r.get("ok") for r in res) else "部分句失败")
        return all_results

    def synthesize_one(self, text: str, voice: dict, out_path: str,
                       store_prefix: str = None, timeout: int = None) -> Dict:
        """单句合成（试听用）"""
        tag = os.path.splitext(os.path.basename(out_path))[0]
        res = self.synthesize_batch([{
            "text": text, "voice": voice, "out_path": out_path,
            "store_prefix": store_prefix or f"dub/preview/{tag}",
            "line_id": tag, "shot_id": None, "character": None,
        }], timeout=timeout)
        return res[0] if res else {"ok": False, "error": "合成未返回结果"}
