# -*- coding: utf-8 -*-
"""外部插件示例：镜头节奏分析（演示如何扩展漫剧流水线）

这个插件的实际作用
------------------
按剧本每镜的时长与台词字数，算出「语速」并提示是否偏快/偏慢——
这是真实创作中常用的检查（台词太长而镜头太短，配音会赶不上画面）。

它同时是一份**可复制的插件模板**：
1. 文件放在 app/plugins/ 下即被自动加载，无需改主程序；
2. 必须实现 `register(registry)`；
3. 通过 `registry.add(PluginSpec(...))` 注册，run 接收 context 返回 dict。

context 约定：调用方通过 /api/plugins/<id>/run 传入任意参数，
例如 {"script": {...}} 或 {"project_name": "xxx", "episode_no": 1}。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 中文口播的舒适语速区间（字/秒）；超过上限说明台词塞不下，低于下限说明镜头偏空
COMFORT_MIN_CPS = 3.0
COMFORT_MAX_CPS = 6.5


def analyze_pacing(script: dict) -> dict:
    """按镜头计算语速并给出提示"""
    shots = (script or {}).get("shots") or []
    rows, warnings = [], []
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        try:
            dur = float(shot.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0

        dlg = shot.get("dialogue")
        text = ""
        if isinstance(dlg, list) and dlg:
            first = dlg[0]
            text = (first.get("text") if isinstance(first, dict) else str(first)) or ""
        elif isinstance(dlg, str):
            text = dlg
        if not text:
            text = str(shot.get("dialogue_text") or "")
        # 去掉说话人前缀与标点，只数正文字数
        text = text.split("：", 1)[-1]
        chars = len([c for c in text if c.strip() and c not in "，。！？；：、…—《》（）\"'"])

        cps = round(chars / dur, 2) if dur > 0 else 0.0
        row = {"shot_id": shot.get("shot_id", i + 1), "duration_sec": dur,
               "chars": chars, "chars_per_sec": cps,
               "camera": shot.get("camera") or ""}
        if dur > 0 and chars > 0:
            if cps > COMFORT_MAX_CPS:
                row["hint"] = "偏快：台词对镜头来说太长，建议加长镜头或精简台词"
                warnings.append(row)
            elif cps < COMFORT_MIN_CPS:
                row["hint"] = "偏慢：镜头偏空，可考虑加画面动作或缩短镜头"
        rows.append(row)

    avg = round(sum(r["chars_per_sec"] for r in rows) / len(rows), 2) if rows else 0.0
    return {
        "ok": True,
        "shot_count": len(rows),
        "avg_chars_per_sec": avg,
        "comfort_range": [COMFORT_MIN_CPS, COMFORT_MAX_CPS],
        "warn_count": len(warnings),
        "warnings": warnings,
        "shots": rows,
    }


def register(registry):
    """插件入口：由 plugin_registry 自动调用"""
    from plugin_registry import PluginSpec

    def _run(context: dict) -> dict:
        script = (context or {}).get("script")
        if not script:
            # 支持只传项目名，由插件自行读剧本
            project = (context or {}).get("project_name")
            if project:
                try:
                    import novel_to_script
                    import project_store
                    from config import SCRIPT_DIR
                    key = project_store.safe_key(project)
                    ep = int((context or {}).get("episode_no") or 1)
                    script = novel_to_script.load_episode_script(SCRIPT_DIR, key, ep)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "error": f"剧本读取失败：{e}"}
        if not script:
            return {"ok": False, "error": "请在 context 中提供 script 或 project_name"}
        return analyze_pacing(script)

    registry.add(PluginSpec(
        id="example.pacing",
        name="镜头节奏分析",
        category="script",
        description="按每镜时长与台词字数计算语速，提示偏快/偏慢的镜头",
        inputs=["script"],
        outputs=["pacing_report"],
        depends_on=["builtin.script"],
        builtin=False,
        implemented=True,
        run=_run,
    ))
