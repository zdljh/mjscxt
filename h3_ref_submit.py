#!/usr/bin/env python3
"""H3 视频生成测试 - 参考 D:\漫剧生成\蛊真人\第01集_3D国漫版_v6 的正确调用方式
核心：用 widgets_values_named 字典映射 + {"prompt": wf} 提交
"""
import json, urllib.request, urllib.error, time
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"
OUTPUT_DIR = Path(r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\output")
WF_PATH = r"D:\ComfyUI_portable_TE_v260619\ComfyUI\ComfyUI\user\default\workflows\H3信号10段测试001.json"


def load_frontend_api(path):
    """把前端工作流 JSON 转为 API prompt dict（参考项目 wf_loader.py）"""
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    link_src = {L[0]: (L[1], L[2]) for L in wf.get("links", [])}
    SKIP = {"MarkdownNote", "Label (rgthree)", "Note", "Reroute"}
    api = {}
    for node in wf.get("nodes", []):
        if node.get("type") in SKIP:
            continue
        nid = str(node["id"])
        wn = node.get("widgets_values_named", {}) or {}
        inputs, dynamic = {}, {}
        for inp in node.get("inputs", []):
            name = inp["name"]
            link = inp.get("link")
            if link is not None and link in link_src:
                s, o = link_src[link]
                val = [str(s), o]
            elif name in wn:
                val = wn[name]
            else:
                continue
            if name.startswith(("ref_images.", "ref_videos.", "ref_audios.")):
                pre, suf = name.split(".", 1)
                dynamic.setdefault(pre, {})[suf] = val
            else:
                inputs[name] = val
        for pre, sub in dynamic.items():
            inputs[pre] = sub
        api[nid] = {"class_type": node["type"], "inputs": inputs}
    return api


def http_json(method, path, payload=None, timeout=60):
    url = COMFYUI_URL + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---- 1. 加载并转换工作流 ----
wf = load_frontend_api(WF_PATH)
print(f"转换后节点数: {len(wf)}")

# 检查命名参数覆盖情况（诊断）
raw = json.load(open(WF_PATH, encoding="utf-8"))
missing = [n["id"] for n in raw["nodes"]
           if n["type"] not in ("MarkdownNote", "Note", "Reroute")
           and not n.get("widgets_values_named")]
print(f"缺少 widgets_values_named 的节点: {len(missing)}")

# ---- 2. 诊断：打印关键节点类型 ----
types = {}
for nid, n in wf.items():
    types[n["class_type"]] = types.get(n["class_type"], 0) + 1
print("\n节点类型统计:")
for t, c in sorted(types.items(), key=lambda x: -x[1]):
    print(f"  {t}: {c}")

# ---- 3. 找到输出节点 (SaveVideo) ----
out_ids = [nid for nid, n in wf.items() if n["class_type"] == "SaveVideo"]
print(f"\nSaveVideo 节点: {out_ids}")
for oid in out_ids:
    print(f"  {oid} inputs: {json.dumps(wf[oid]['inputs'], ensure_ascii=False)[:300]}")

json.dump(wf, open(r"C:\Users\liujianghua\WorkBuddy\2026-09-09-16-55-22\漫剧生成系统\h3_api_converted.json",
                   "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n已保存转换结果到 h3_api_converted.json")
