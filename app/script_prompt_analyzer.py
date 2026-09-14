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

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是 MiniMax H3 视频生成模型的提示词工程师，精通 Ref2VA（参考图转视频）结构化提示词撰写，"
    "并严格输出合法 JSON。"
)

REQUIRED_MARKERS = ("subject_definitions", "detailed_description", "<Picture 1>")


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

    return f"""【任务】为漫剧《{script.get('title') or ''}》的镜头 {shot.get('shot_id')} 生成/优化 H3 Ref2VA 提示词。
【画面风格】{style}
{pic_defs}
【本镜头信息】
镜号：{shot.get('shot_id')}　时长：{duration} 秒
景别与运镜：{shot.get('camera') or '中景'}
场景：{scene.get('name')}（{scene.get('appearance')}）
画面内容：{shot.get('description') or ''}
对白：{_dlg_line(shot.get('dialogue')) or '（无）'}
情绪：{shot.get('emotion') or ''}　音效：{shot.get('audio_cues') or ''}
出场角色：{json.dumps(chars, ensure_ascii=False)}
出场物品：{json.dumps(items, ensure_ascii=False)}
【prompt_h3 结构要求】必须严格按下面三段式排版，缺一不可：
subject_definitions:
<Picture 1> is the reference image defining ... （英文一行，说明该图作用）
<Picture 2> is the reference image defining ... （英文一行，说明该图作用）
<Subject 1> is 角色名 — 该角色在本镜头中的外观与服装（英文短语）
（空行）
detailed_description:
[镜头{shot.get('shot_id')}, 0-{duration}秒] 景别与运镜。用中文写清：主体动作与表情、环境与光线、镜头运动方式、情绪节奏、对白（如有）、音效氛围；并明确要求「保持与参考图一致、画面连贯稳定、无畸形、无文字水印」。
风格：{style}，电影级打光，画面流畅稳定。
【输出要求】严格只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字：
{{"prompt_h3": "完整的 H3 Ref2VA 提示词（包含 subject_definitions / <Picture 1> / <Picture 2> / <Subject 1> / detailed_description 各部分，用 \\n 换行）", "storyboard_prompt_zh": "该镜头分镜图的中文提示词（60 字以内，描述构图、景别、人物姿态、环境与光线，可直接用于分镜图生成）"}}"""


def generate_shot_prompt(client, script: dict, shot: dict, ref_mode: str = "character_scene",
                         extra_instruction: str = "") -> dict:
    prompt = build_shot_prompt(script, shot, ref_mode)
    if extra_instruction:
        prompt += f"\n【额外要求】{extra_instruction}"
    data = client.chat_json(prompt, system=SYSTEM_PROMPT, temperature=0.5, max_tokens=2200)
    text = str(data.get("prompt_h3") or "").strip()
    if not text or any(m.lower() not in text.lower() for m in REQUIRED_MARKERS):
        # 结构不合规，按规范强修一次
        fix = (prompt + "\n【上一次输出结构不合规】上一次输出缺少 subject_definitions / <Picture 1> / "
                        "detailed_description 中的部分内容。请严格按规定结构重新输出 JSON。")
        data = client.chat_json(fix, system=SYSTEM_PROMPT, temperature=0.3, max_tokens=2200)
        text = str(data.get("prompt_h3") or "").strip()
    return {
        "prompt_h3": text,
        "storyboard_prompt_zh": str(data.get("storyboard_prompt_zh") or "").strip(),
        "valid": bool(text) and all(m.lower() in text.lower() for m in REQUIRED_MARKERS),
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
                errors.append(f"镜头 {shot.get('shot_id')}：结构校验未完全通过")
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
{{"items": [{{"name": "名称（必须与清单完全一致）", "reference_prompt_zh": "中文参考图提示词（含主体外观、材质、构图、光线、风格关键词，60 字以内）", "reference_prompt_en": "English prompt for image generation, under 40 words, comma separated keywords"}}]}}
【硬性约束】items 数组必须覆盖清单中的每一个名称，顺序一致，不要新增名称。"""
        if progress_cb:
            progress_cb("assets", k, len(kinds), f"生成{spec['label']}参考图提示词…",
                        int(30 + (k + 1) / max(1, len(kinds)) * 38))
        try:
            data = client.chat_json(prompt, system=SYSTEM_PROMPT, temperature=0.45, max_tokens=3000)
            got = {str(x.get("name")): x for x in (data.get("items") or []) if isinstance(x, dict)}
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
        except LLMError as e:
            errors.append(f"{spec['label']}参考图提示词失败：{e}")
            logger.warning(f"{spec['label']}参考图提示词失败：{e}")
    return {"updated": updated, "errors": errors}


def analyze_script(client, script: dict, mode: str = "all", shot_ids=None,
                   storyboards_dir: str = "", project_name: str = "",
                   extra_instruction: str = "", progress_cb=None) -> dict:
    """mode: shots（仅镜头 prompt_h3）/ assets（仅参考图提示词）/ all"""
    t0 = time.time
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
    """把更新后的剧本写回原文件（不存在时另存）"""
    if not script_path or not os.path.isfile(script_path):
        raise LLMError(f"剧本文件不存在：{script_path}")
    with open(script_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)
    logger.info(f"剧本提示词已写回：{script_path}")
    return script_path
