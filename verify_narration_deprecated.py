# -*- coding: utf-8 -*-
"""D-11b（P2）「narration 遗留字段废弃告警」· 离线回归脚本

对应缺陷：全项目缺陷审计报告 D-11 第 2 部分 —— ``narration`` 遗留字段的废弃告警。
验收标准（报告原文）：「仍产出 ``narration`` 的项目，日志出现 warning。」

产品铁律（不可违背）：旁白通道已关闭（2026-09-19）。剧本阶段不写 narration、
配音链路不念旁白、成片不产出旁白；旧剧本残留的 narration 只做 legacy 记账。
本脚本只验证「遗留字段变得可见」，不涉及任何业务行为改动。

覆盖 4 条：
  ① 旧剧本（shots 带 narration）→ ``audit_script`` 返回的 narration_line_count 正确、
     warnings 含中文提示、**且日志出现 WARNING 级别记录**（一次一条，不是每镜一条）
  ② 新剧本（无 narration）→ **不产生**任何 narration 相关 warning（避免日志噪声）
  ③ 行为契约不变：固定输入下各计数键、warnings 条数与文案逐项一致（改造前后一致）
  ④ 静态断言：``app/novel_to_script.py`` 里 ``narration`` 已带 deprecated 标记

运行（仅需标准库；``dialogue_utils`` 为纯逻辑模块，可直接 import）：
    MJSCXT_AUTOPILOT=0 python verify_narration_deprecated.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import ast
import io
import logging
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)

import dialogue_utils  # noqa: E402  （纯逻辑模块，无项目内依赖）

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


class Capture(logging.Handler):
    """捕获 logger 记录（默认 WARNING 即可通过，但为口径一致放开到 DEBUG 再按级别取）。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.recs = []

    def emit(self, r):
        self.recs.append(r)

    def messages(self, level=logging.WARNING):
        return [r.getMessage() for r in self.recs if r.levelno >= level]


def _audit_with_log(script):
    """跑 audit_script 并回收 dialogue_utils 日志（跑完还原 logger 级别/处理器）。

    注意：默认 root 级别是 WARNING，``logger.warning`` 本可直达；此处仍显式降到 DEBUG，
    与 verify_silent_except.py 口径一致，避免运行环境把 logger 级别调高后误判。
    """
    cap = Capture()
    _lv = dialogue_utils.logger.level
    dialogue_utils.logger.setLevel(logging.DEBUG)
    dialogue_utils.logger.addHandler(cap)
    try:
        return dialogue_utils.audit_script(script), cap
    finally:
        dialogue_utils.logger.removeHandler(cap)
        dialogue_utils.logger.setLevel(_lv)


# ============================================================
print("=" * 72)
print("D-11b §1　旧剧本（shots 带 narration）→ 计数正确 + 中文提示 + WARNING 日志")
print("=" * 72)

OLD_SCRIPT = {
    "characters": [{"name": "林风"}, {"name": "苏晴"}],
    "shots": [
        {"shot_id": 1, "seq": 1, "dialogue": {"speaker": "林风", "text": "你来了。"},
         "narration": "夜色如墨，山风呜咽。", "description": "山巅", "prompt_h3": "p1"},
        {"shot_id": 2, "seq": 2, "dialogue": "", "narration": "他握紧了拳头。",
         "description": "特写", "prompt_h3": "p2"},
        {"shot_id": 3, "seq": 3, "dialogue": {"speaker": "苏晴", "text": "住手！"},
         "audio_cues": "剑鸣", "description": "远景", "prompt_h3": "p3"},
        {"shot_id": 4, "seq": 4, "dialogue": [], "narration": "风停了。",
         "description": "空镜", "prompt_h3": "p4"},
    ],
}

old_res, old_cap = _audit_with_log(OLD_SCRIPT)
old_stats = old_res["stats"]

check("1.1 narration_line_count 正确（2 个无台词但残留旁白的镜头）",
      old_stats["narration_line_count"] == 2, f"实际 {old_stats['narration_line_count']}")
check("1.2 speakable_line_count_total 含旁白（2 台词 + 2 旁白 = 4）",
      old_stats["speakable_line_count_total"] == 4,
      f"实际 {old_stats['speakable_line_count_total']}")

_nar_warn = [w for w in old_res["warnings"] if "旁白" in w]
check("1.3 warnings 含中文旁白废弃提示", len(_nar_warn) == 1, f"warnings={old_res['warnings']}")
check("1.4 提示文案点名「本系统已不再产出旁白」并建议重新生成",
      bool(_nar_warn) and "不再产出旁白" in _nar_warn[0] and "重新生成" in _nar_warn[0],
      _nar_warn[0][:80] if _nar_warn else "")

_log_warn = [m for m in old_cap.messages(logging.WARNING) if "旁白" in m]
check("1.5 日志出现 WARNING 级别记录（修复前的核心缺口：日志里毫无痕迹）",
      len(_log_warn) == 1, f"WARNING 记录数={len(_log_warn)}")
check("1.6 日志是「一次性」一条而非每镜一条（4 镜/2 旁白只打 1 条）",
      len(old_cap.messages(logging.WARNING)) == 1,
      f"WARNING 记录数={len(old_cap.messages(logging.WARNING))}")
check("1.7 日志文案说明「2026-09-19 关闭旁白通道之前的遗留产物」并点名镜头标签",
      bool(_log_warn) and "遗留" in _log_warn[0] and "#2" in _log_warn[0],
      _log_warn[0][:100] if _log_warn else "")

# ============================================================
print()
print("=" * 72)
print("D-11b §2　新剧本（无 narration）→ 不产生任何 narration 相关 warning")
print("=" * 72)

NEW_SCRIPT = {
    "characters": [{"name": "林风"}],
    "shots": [
        {"shot_id": 1, "dialogue": {"speaker": "林风", "text": "出发。"},
         "description": "清晨山道", "prompt_h3": "n1"},
    ],
}

new_res, new_cap = _audit_with_log(NEW_SCRIPT)
check("2.1 narration_line_count == 0", new_res["stats"]["narration_line_count"] == 0,
      f"实际 {new_res['stats']['narration_line_count']}")
check("2.2 返回值里无 narration 相关 warning",
      not [w for w in new_res["warnings"] if "旁白" in w],
      f"warnings={new_res['warnings']}")
check("2.3 日志里无 narration / 旁白相关 WARNING（不制造噪声）",
      not [m for m in new_cap.messages(logging.WARNING) if "旁白" in m or "narration" in m],
      f"WARNING={new_cap.messages(logging.WARNING)}")
check("2.4 整个 WARNING 级别零记录（干净的剧本不该触发任何告警）",
      not new_cap.messages(logging.WARNING),
      f"WARNING={new_cap.messages(logging.WARNING)}")

# ============================================================
print()
print("=" * 72)
print("D-11b §3　行为契约不变：固定输入下返回值逐项一致")
print("=" * 72)

EXPECTED_STATS = {
    "shot_count": 4,
    "speakable_lines": 2,
    "narration_line_count": 2,
    "speakable_line_count_total": 4,
    "silent_shot_count": 0,
    "no_voice_shot_count": 0,
    "overlong_speech_shot_count": 0,
    "fallback_shot_count": 0,
    "all_fallback": False,
    "blank_visual_shot_count": 0,
    "unknown_speaker_shot_count": 0,
}
check("3.1 stats 全键逐项一致（计数语义未变）", old_stats == EXPECTED_STATS,
      f"差异={ {k: (EXPECTED_STATS.get(k), old_stats.get(k)) for k in set(EXPECTED_STATS) | set(old_stats) if EXPECTED_STATS.get(k) != old_stats.get(k)} }")

EXPECTED_NARRATION_WARNING = (
    "有 2 个镜头残留了「旁白」文本（#2, #4）：本系统已不再产出旁白，"
    "这批剧本是改造前生成的，成片会带画外音解说、且旁白时长常远超画面。建议重新生成剧本。")
check("3.2 warnings 条数未变（该固定输入恰好 1 条）", len(old_res["warnings"]) == 1,
      f"warnings={old_res['warnings']}")
check("3.3 narration 提示文案逐字未变（不含日志改动引入的文案漂移）",
      old_res["warnings"][0] == EXPECTED_NARRATION_WARNING,
      f"实际={old_res['warnings'][0]!r}")

check("3.4 ok 语义未变（有台词、无空响/空洞画面、非全兜底 → 放行）",
      old_res["ok"] is True, f"ok={old_res['ok']}")
check("3.5 problem_shots 结构未变（6 个子键齐全）",
      set(old_res["problem_shots"]) == {"silent", "no_voice", "overlong_speech",
                                        "fallback", "blank_visual", "unknown_speaker"},
      f"keys={sorted(old_res['problem_shots'])}")

# ============================================================
print()
print("=" * 72)
print("D-11b §4　静态断言：novel_to_script 里 narration 已带 deprecated 标记")
print("=" * 72)

_n2s_path = os.path.join(APP_DIR, "novel_to_script.py")
_n2s_src = io.open(_n2s_path, encoding="utf-8", errors="replace").read()

_dep_value = None
_tree = ast.parse(_n2s_src)
for _node in _tree.body:
    if isinstance(_node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEPRECATED_SHOT_FIELDS" for t in _node.targets):
        try:
            _dep_value = ast.literal_eval(_node.value)
        except (ValueError, SyntaxError) as _e:   # noqa: BLE001
            _dep_value = None
            print(f"    （DEPRECATED_SHOT_FIELDS 无法字面求值：{_e}）")
        break

check("4.1 存在 DEPRECATED_SHOT_FIELDS 结构化登记表", isinstance(_dep_value, dict),
      f"value={type(_dep_value).__name__}")
check("4.2 登记表含 narration 键", isinstance(_dep_value, dict) and "narration" in _dep_value,
      f"keys={list(_dep_value) if isinstance(_dep_value, dict) else None}")
_nar_meta = (_dep_value or {}).get("narration") if isinstance(_dep_value, dict) else None
check("4.3 narration 条目标记 deprecated=True（机器可判定的废弃标记）",
      isinstance(_nar_meta, dict) and _nar_meta.get("deprecated") is True,
      f"meta={_nar_meta}")
check("4.4 narration 条目带非空说明字符串（why 可追溯）",
      isinstance(_nar_meta, dict) and bool(str(_nar_meta.get("reason") or "").strip()),
      f"reason={(_nar_meta or {}).get('reason')!r}")
check("4.5 登记表在字段白名单（_norm_shots）处被实际引用（非仅注释）",
      _n2s_src.count("DEPRECATED_SHOT_FIELDS") >= 2,
      f"出现次数={_n2s_src.count('DEPRECATED_SHOT_FIELDS')}")

# ============================================================
print()
print("=" * 72)
total = _PASSES[0] + len(_FAILS)
print(f"结果：{_PASSES[0]}/{total} 通过")
if _FAILS:
    print("失败项：")
    for f in _FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("✅ D-11b narration 遗留字段废弃告警全部通过")
sys.exit(0)
