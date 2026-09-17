# -*- coding: utf-8 -*-
"""情绪化配音自检：剧本 shots[].emotion 是否真的被送进 TTS。

运行： python _verify_tts_emotion.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  <- {detail}" if detail and not cond else ""))


import tts_client as T  # noqa: E402
from config import TTS_DEFAULT_PARAMS  # noqa: E402

print("\n[1] 开关与默认")
check("emotion_aware 进默认参数", "emotion_aware" in TTS_DEFAULT_PARAMS)
check("默认开启", bool(TTS_DEFAULT_PARAMS.get("emotion_aware")))
check("_emotion_aware 读得到", T._emotion_aware() is True)

print("\n[2] 中性情绪判定（不该切 design，避免音色白漂移）")
for e in ("平静", "", "无", "一般"):
    check(f"{e!r} 判为中性", T._is_neutral_emotion(e))
for e in ("愤怒", "低沉", "惊喜", "紧张"):
    check(f"{e!r} 判为有情绪", not T._is_neutral_emotion(e))

print("\n[3] instruct 组装")
s = T._emotion_instruct("愤怒", "沉稳青年男声，冷峻")
check("角色描述在前（稳音色）", s.startswith("沉稳青年男声"), s)
check("情绪提示在后（控语气）", "愤怒" in s, s)
check("无角色描述时不残留分隔符", not T._emotion_instruct("低沉", "").startswith("；"))
check("未收录情绪也能降级描述", "狂喜" in T._emotion_instruct("狂喜", "少女音"))

print("\n[4] 逐句计划真的带上情绪")
script = {
    "characters": [{"name": "剑心", "voice_style": "沉稳青年男声", "personality": "冷峻寡言"}],
    "shots": [
        {"shot_id": 1, "emotion": "愤怒", "duration": 4,
         "dialogue": [{"speaker": "剑心", "text": "把剑还来"}]},
        {"shot_id": 2, "emotion": "平静", "duration": 4,
         "dialogue": [{"speaker": "剑心", "text": "我知道了"}]},
        {"shot_id": 3, "emotion": "低沉", "duration": 4,
         "dialogue": [{"speaker": "剑心", "text": "这一切该结束了"}]},
    ],
}
plan = T.build_dub_plan(script, None, "P", 1)
by_shot = {l["shot_id"]: l for l in plan["lines"]}
check("生成 3 句", len(plan["lines"]) == 3, str(len(plan["lines"])))
check("line 带上 emotion 字段", by_shot[1].get("emotion") == "愤怒")

v1 = by_shot[1]["voice"]
check("愤怒 → design 模式（instruct 才生效）", v1["mode"] == "design", v1["mode"])
check("愤怒 → instruct 含情绪提示", "愤怒" in v1["instruct"], v1["instruct"])
check("愤怒 → instruct 含角色底稿", "沉稳青年男声" in v1["instruct"], v1["instruct"])

v2 = by_shot[2]["voice"]
check("平静 → 保持 preset（不浪费、不漂移）", v2["mode"] == "preset", v2["mode"])
check("平静 → speaker 仍在", bool(v2.get("speaker")), str(v2))

v3 = by_shot[3]["voice"]
check("低沉 → design 模式", v3["mode"] == "design", v3["mode"])
check("低沉 → instruct 含低沉提示", "低沉" in v3["instruct"] or "压低声音" in v3["instruct"],
      v3["instruct"])

print("\n[5] 关闭开关后退回老行为")
T.TTS_DEFAULT_PARAMS["emotion_aware"] = False
plan2 = T.build_dub_plan(script, None, "P", 1)
check("关闭后全为 preset", all(l["voice"]["mode"] == "preset" for l in plan2["lines"]),
      str([l["voice"]["mode"] for l in plan2["lines"]]))
T.TTS_DEFAULT_PARAMS["emotion_aware"] = True

print("\n" + "=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("情绪化配音自检全部通过")
