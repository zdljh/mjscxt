"""
剧本提示词分析器 —— 用用户配置的自定义 API 为剧本生成/优化提示词

产出两类提示词：
1. 分镜视频提示词 prompt_h3（H3 Ref2VA 结构：subject_definitions + <Picture N> + detailed_description）
2. 参考图提示词：角色的 reference_prompt_zh / reference_prompt_en、物品、场景，以及每个镜头的分镜图中文提示词 storyboard_prompt_zh

支持：单镜头重写、批量生成（全部镜头 / 全部参考图提示词）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

from llm_client import LLMError
from dialogue_utils import format_line as _dlg_line
from fs_atomic import atomic_write_json
import h3_prompt_kit
import style_kit

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是 MiniMax H3 视频生成模型的提示词工程师，精通 Ref2VA（参考图转视频）结构化提示词撰写，"
    "并严格输出合法 JSON。"
)

#: 六段式（Ref2VA）—— 任一缺失即为不合格输出
REQUIRED_MARKERS = h3_prompt_kit.REF_SECTIONS + ("<Picture 1>",)


def detect_ref_mode(storyboards_dir: str, project_name: str) -> str:
    """有已生成分镜图时使用分镜模式（<Picture 1> = 分镜图），否则使用角色+场景模式"""
    if not project_name:
        return "character_scene"
    manifest = os.path.join(storyboards_dir, project_name, "storyboard_manifest.json")
    if os.path.isfile(manifest):
        return "storyboard"
    return "character_scene"


def _char_brief(script: dict, names=None, limit: int = 4) -> list:
    out = []
    for c in (script.get("characters") or []):
        if not isinstance(c, dict):
            continue
        if names and c.get("name") not in names:
            continue
        out.append({
            "name": c.get("name"),
            "appearance": (c.get("appearance") or c.get("description") or "")[:60],
            "reference_prompt_zh": (c.get("reference_prompt_zh") or "")[:60],
        })
        if len(out) >= limit:
            break
    return out


def _scene_brief(script: dict, name=None) -> dict:
    for s in (script.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        if name and s.get("name") == name:
            return {"name": s.get("name"),
                    "appearance": (s.get("appearance") or s.get("location") or "")[:80],
                    "reference_prompt_zh": (s.get("reference_prompt_zh") or "")[:60]}
    scenes = [s for s in (script.get("scenes") or []) if isinstance(s, dict)]
    if scenes:
        s = scenes[0]
        return {"name": s.get("name"),
                "appearance": (s.get("appearance") or s.get("location") or "")[:80],
                "reference_prompt_zh": (s.get("reference_prompt_zh") or "")[:60]}
    return {"name": "未指定场景", "appearance": "", "reference_prompt_zh": ""}


def _item_brief(script: dict, names=None, limit: int = 3) -> list:
    out = []
    for i in (script.get("items") or []):
        if not isinstance(i, dict):
            continue
        if names and i.get("name") not in names:
            continue
        out.append({"name": i.get("name"), "appearance": (i.get("appearance") or "")[:50]})
        if len(out) >= limit:
            break
    return out


def build_shot_prompt(script: dict, shot: dict, ref_mode: str = "character_scene") -> str:
    """构造「单镜头 prompt_h3 + 分镜图提示词」的模型输入"""
    style = script.get("style") or "3D动漫渲染"
    chars = _char_brief(script, shot.get("characters_in_shot"))
    scene = _scene_brief(script, shot.get("location"))
    items = _item_brief(script, shot.get("items_in_shot"))
    duration = shot.get("duration") or 5

    if ref_mode == "storyboard":
        pic_defs = ("【参考图语义】\n"
                    "<Picture 1> = 该镜头的分镜图（定义构图、景别、机位、环境与人物姿态）\n"
                    "<Picture 2> = 主角外观锚点（定义五官、发型、服装与画风）\n")
    else:
        pic_defs = ("【参考图语义】\n"
                    "<Picture 1> = 主角外观锚点（定义五官、发型、服装与画风）\n"
                    "<Picture 2> = 场景环境参考（定义环境与氛围）\n"
                    "若镜头出现第二位主要角色，可继续写 <Picture 3>；最多 3 张参考图。\n")

    # 画面细节：description 限长后的扩展位（时间/天气/光源方向/动作过程）。分析器是提示词的
    # **最终作者**，看不到这些细节就只能靠猜 —— 实测「黄昏逆光」这类光要素若只存在于
    # visual_detail，则写出的 prompt_h3 / storyboard_prompt_zh 完全没有光影描述，
    # 生成端 _extract_light_hint 又会被 storyboard_prompt_zh 顶掉优先级，最终分镜图光影丢失。
    desc_text = str(shot.get("description") or "").strip()
    detail_text = str(shot.get("visual_detail") or "").strip()
    visual_text = (f"{desc_text}。{detail_text}" if desc_text else detail_text) \
        if (detail_text and detail_text != desc_text) else desc_text

    return f"""【任务】为漫剧《{script.get('title') or ''}》的镜头 {shot.get('shot_id')} 生成/优化 H3 Ref2VA 提示词。
【画面风格】{style}
{pic_defs}
【本镜头信息】
镜号：{shot.get('shot_id')}　时长：{duration} 秒
景别与运镜：{shot.get('camera') or '中景'}
场景：{scene.get('name')}（{scene.get('appearance')}）
画面内容：{visual_text}
对白：{_dlg_line(shot.get('dialogue')) or '（无）'}
情绪：{shot.get('emotion') or ''}　音效：{shot.get('audio_cues') or ''}
出场角色：{json.dumps(chars, ensure_ascii=False)}
出场物品：{json.dumps(items, ensure_ascii=False)}
【prompt_h3 结构要求】必须严格按下面**六段**排版，段名逐字一致、顺序不可调换，缺一不可：
subject_definitions:
<Picture 1> - 说明该参考图在本镜中的作用（构图/场景/人物姿态基准）
<Picture 2> - 说明该参考图在本镜中的作用（人物外观锚点）
<Subject 1> - 角色名：该角色在本镜头中的外观与服装（与参考图一致，不得改服装）

summary:
2~4 句话概述这段约 {duration} 秒的视频内容、主体与情绪基调。

retention_analysis:
- 必须保留 <Picture 1> 中的哪些元素（构图、景别、机位、环境）
- 必须保留 <Picture 2> 中角色的哪些元素（五官、发型、服装、配饰）
- 哪些改造**不允许**发生（不得换人、不得改服装、不得加文字水印）

detailed_description:
[Shot 1] 00:00.000 {shot.get('camera') or '中景'}：写清主体动作与表情、环境与光线、镜头运动方式与节奏。台词必须带语言标记写成「(S1) 说：[Chinese] 台词原文」（语言按原文，中文写 Chinese）。时间码格式固定为 MM:SS.mmm；时长超过 6 秒时拆成 2~3 个时间码节拍，总时长必须等于 {duration} 秒。

overall_soundscape:
一段连贯散文，描述全程环境音、动作音与非语言人声（角色听得到的声音）。

non_diegetic_music:
一段连贯散文，描述画面外配乐（配器、速度、情绪走向），始终保持在画面之外、无人声演唱。

【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字：
{{"prompt_h3": "完整六段式 H3 Ref2VA 提示词（六段段名齐全、按上述顺序，用 \\n 换行）", "storyboard_prompt_zh": "该镜头分镜图的中文提示词（80 字以内，写构图、景别、人物姿态、环境与光线，可直接用于分镜图生成）"}}
【硬性约束】六段段名必须原样出现在输出中；画面里严禁出现任何文字、字幕、水印、logo；不要写与画面无关的抽象词（cinematic / beautiful 之类），改成具体视觉与听觉细节。"""


def _missing_sections(text: str) -> list:
    """按 H3 规范列出缺失的段（含 <Picture 1> 标签）"""
    missing = list(h3_prompt_kit.validate(text).get("missing") or [])
    if text and "<picture 1>" not in text.lower():
        missing.append("<Picture 1>")
    return missing


def generate_shot_prompt(client, script: dict, shot: dict, ref_mode: str = "character_scene",
                         extra_instruction: str = "") -> dict:
    prompt = build_shot_prompt(script, shot, ref_mode)
    if extra_instruction:
        prompt += f"\n【额外要求】{extra_instruction}"
    data = client.chat_json(prompt, system=SYSTEM_PROMPT, temperature=0.5, max_tokens=3200)
    text = str(data.get("prompt_h3") or "").strip()
    missing = _missing_sections(text)
    if missing:
        # 结构不合规：按规范强修一次，并把**具体缺了哪几段**回灌给模型（比笼统说「不合规」有效）
        fix = (prompt + "\n【上一次输出结构不合规】上一次输出的 prompt_h3 缺少这些段："
                       + "、".join(missing)
                       + "。请严格按上面「六段式」结构补齐全部段落后重新输出 JSON，"
                         "段名必须逐字出现且顺序不可调换。")
        data = client.chat_json(fix, system=SYSTEM_PROMPT, temperature=0.3, max_tokens=3200)
        text = str(data.get("prompt_h3") or "").strip()
        missing = _missing_sections(text)
    return {
        "prompt_h3": text,
        "storyboard_prompt_zh": str(data.get("storyboard_prompt_zh") or "").strip(),
        "valid": bool(text) and not missing,
        "missing": missing,
    }


def analyze_shots(client, script: dict, ref_mode: str = "character_scene", shot_ids=None,
                  extra_instruction: str = "", progress_cb=None) -> dict:
    shots = [s for s in (script.get("shots") or []) if isinstance(s, dict)]
    targets = [s for s in shots if not shot_ids or str(s.get("shot_id")) in {str(i) for i in shot_ids}]
    updated, errors, invalid = 0, [], 0
    for i, shot in enumerate(targets):
        if progress_cb:
            progress_cb("shots", i, len(targets), f"生成镜头 {shot.get('shot_id')} 提示词…",
                        int(70 + (i + 1) / max(1, len(targets)) * 28))
        try:
            res = generate_shot_prompt(client, script, shot, ref_mode=ref_mode,
                                       extra_instruction=extra_instruction)
            if res["prompt_h3"]:
                shot["prompt_h3"] = res["prompt_h3"]
                updated += 1
            if res.get("storyboard_prompt_zh"):
                shot["storyboard_prompt_zh"] = res["storyboard_prompt_zh"]
            if not res["valid"]:
                invalid += 1
                miss = "、".join(res.get("missing") or []) or "未知"
                errors.append(f"镜头 {shot.get('shot_id')}：结构校验未通过，缺 {miss}")
        except LLMError as e:
            errors.append(f"镜头 {shot.get('shot_id')} 失败：{e}")
            logger.warning(f"镜头 {shot.get('shot_id')} 提示词生成失败：{e}")
    return {"updated": updated, "total": len(targets), "errors": errors, "invalid": invalid}


ASSET_SPECS = {
    "characters": {
        "label": "角色",
        "brief_keys": ["name", "appearance", "personality"],
        "hint": "角色参考图（三视图/设定图）：白底或简洁背景、正面全身、清晰五官与服装细节，用于后续多视图生成",
    },
    "items": {
        "label": "物品",
        "brief_keys": ["name", "category", "appearance"],
        "hint": "物品参考图（道具设定图）：单一主体居中、简洁背景、材质与配色清晰",
    },
    "scenes": {
        "label": "场景",
        "brief_keys": ["name", "location", "appearance"],
        "hint": "场景参考图（环境概念图）：广角视野、明确的时间与天气、光影氛围强烈、无人物",
    },
}


def analyze_asset_prompts(client, script: dict, kinds=("characters", "items", "scenes"),
                          progress_cb=None) -> dict:
    style = script.get("style") or "3D动漫渲染"
    updated, errors = 0, []
    for k, kind in enumerate(kinds):
        spec = ASSET_SPECS.get(kind)
        rows = [r for r in (script.get(kind) or []) if isinstance(r, dict)]
        if not spec or not rows:
            continue
        brief = [{key: (r.get(key) or "") for key in spec["brief_keys"]} for r in rows]
        prompt = f"""【任务】为漫剧《{script.get('title') or ''}》的{spec['label']}生成/优化参考图提示词（中英双语）。
【画面风格】{style}
【{spec['label']}清单】
{json.dumps(brief, ensure_ascii=False)}
【参考图要求】{spec['hint']}
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字：
{{"items": [{{"name": "名称（必须与清单完全一致）", "reference_prompt_zh": "中文参考图提示词（60 字以内，只写画面可见的具体特征：外观、材质、配色、构图、光线）", "reference_prompt_en": "English prompt, under 45 words, comma-separated concrete visual keywords; an accurate translation of reference_prompt_zh"}}]}}
【风格红线·重要】风格词由**程序统一在末尾追加**（幂等，不会重复），你不要写。因此每一条 reference_prompt_zh / reference_prompt_en 都**不得包含风格词、画风词或质量词**（如「国漫」「3D渲染」「电影级」「精致」「masterpiece」「best quality」「8k」）；自己写了会导致风格重复两遍，视为不合格输出。
【英文红线】reference_prompt_en 必须是 reference_prompt_zh 的**准确英文翻译**，严禁把中文概念硬音译成自造罗马字（例如「国漫」必须写成 'Chinese animated style'，绝不能写成 'xuanxuan'）。
【硬性约束】items 数组必须覆盖清单中的每一个名称，顺序一致，不要新增名称。"""
        if progress_cb:
            progress_cb("assets", k, len(kinds), f"生成{spec['label']}参考图提示词…",
                        int(30 + (k + 1) / max(1, len(kinds)) * 38))
        try:
            data = client.chat_json(prompt, system=SYSTEM_PROMPT, temperature=0.45, max_tokens=3000)
            got = {str(x.get("name")): x for x in (data.get("items") or []) if isinstance(x, dict)}
            touched = []
            for row in rows:
                hit = got.get(str(row.get("name")))
                if not hit:
                    continue
                zh = str(hit.get("reference_prompt_zh") or "").strip()
                en = str(hit.get("reference_prompt_en") or "").strip()
                if zh:
                    row["reference_prompt_zh"] = zh
                if en:
                    row["reference_prompt_en"] = en
                if zh or en:
                    updated += 1
                    touched.append(row)
            # 确定性补风格（中英双语）：模型被要求不写风格，这里统一收尾，不再有重复
            style_kit.apply_asset_style_all(touched, style)
        except LLMError as e:
            errors.append(f"{spec['label']}参考图提示词失败：{e}")
            logger.warning(f"{spec['label']}参考图提示词失败：{e}")
    return {"updated": updated, "errors": errors}


def analyze_script(client, script: dict, mode: str = "all", shot_ids=None,
                   storyboards_dir: str = "", project_name: str = "",
                   extra_instruction: str = "", progress_cb=None) -> dict:
    """mode: shots（仅镜头 prompt_h3）/ assets（仅参考图提示词）/ all"""
    ref_mode = detect_ref_mode(storyboards_dir, project_name)
    result = {"mode": mode, "ref_mode": ref_mode,
              "shots": {"updated": 0, "total": 0, "errors": [], "invalid": 0},
              "assets": {"updated": 0, "errors": []}}
    if mode in ("shots", "all"):
        if progress_cb:
            progress_cb("shots", 0, 1, "开始生成镜头提示词…", 5)
        result["shots"] = analyze_shots(client, script, ref_mode=ref_mode, shot_ids=shot_ids,
                                        extra_instruction=extra_instruction, progress_cb=progress_cb)
    if mode in ("assets", "all"):
        result["assets"] = analyze_asset_prompts(client, script, progress_cb=progress_cb)

    meta = script.setdefault("metadata", {})
    meta["prompt_analysis"] = {
        "mode": mode,
        "ref_mode": ref_mode,
        "shots_updated": result["shots"].get("updated", 0),
        "assets_updated": result["assets"].get("updated", 0),
        "model": client.model,
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if progress_cb:
        progress_cb("done", 1, 1, "提示词生成完成", 100)
    return result


def save_script_inplace(script: dict, script_path: str) -> str:
    """把更新后的剧本写回原文件（不存在时另存）。

    A4（2026-09-23 收口）：原为裸 ``open(...,"w") + json.dump``（**非原子写**）。
    这是 `/api/scripts/analyze-prompts` 的写盘面，覆盖式重写 caller 传入的 `script_path`：
    崩溃 / 断电 / 并发写会在原地留半截 JSON（剧本直接不可用，且原内容已被截断）。
    改走 :func:`fs_atomic.atomic_write_json`（唯一临时名 + fsync + `.bak` 快照 + replace 退避），
    与全库其它落盘口径一致。
    """
    if not script_path or not os.path.isfile(script_path):
        raise LLMError(f"剧本文件不存在：{script_path}")
    atomic_write_json(script_path, script)
    logger.info("剧本提示词已写回：%s", script_path)
    return script_path
