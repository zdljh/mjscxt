"""最终回归：覆盖本轮所有修复点。

默认打 5000（正式实例）。验证临时实例时用 REG_BASE 覆盖：
    REG_BASE=http://127.0.0.1:5077 python _final_regression.py
"""
import os
import urllib.parse

import requests

BASE = os.environ.get("REG_BASE", "http://127.0.0.1:5000")
results = []


def check(name, method, path, *, json_body=None, params=None, expect=(200,), timeout=60):
    try:
        r = requests.request(method, BASE + path, json=json_body, params=params, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        results.append((name, "EXC", str(e)[:60]))
        print(f"  [EXC] {name}: {e}")
        return None
    ok = r.status_code in expect
    results.append((name, r.status_code, "OK" if ok else "FAIL"))
    print(f"  [{'OK ' if ok else 'FAIL'}] {r.status_code:<4} {name}")
    return r


print("=" * 96)
print("A. 只读与配置接口")
print("=" * 96)
check("系统状态", "GET", "/api/status")
check("AI 配置（三模块 modules 结构）", "GET", "/api/ai/config")
check("记忆统计", "GET", "/api/memory/stats")
check("记忆列表", "GET", "/api/memory/list")
check("记忆洞察", "GET", "/api/memory/insights")
check("分析汇总", "GET", "/api/analytics/summary")
check("导出列表（files 字段）", "GET", "/api/export/list")
check("水印配置", "GET", "/api/watermark/config")
check("质检配置", "GET", "/api/qc/config")
check("项目列表", "GET", "/api/projects")
check("小说列表", "GET", "/api/novels")
check("关系列表", "GET", "/api/relations", params={"project": "剑心初醒"})
check("关系图谱", "GET", "/api/relations/graph", params={"project": "剑心初醒"})
check("托管状态", "GET", "/api/autopilot/status")
check("i18n zh-CN", "GET", "/api/i18n/zh-CN")

print()
print("=" * 96)
print("B. 本轮新修的写接口（原先缺装饰器 → 404/405）")
print("=" * 96)
check("保存水印配置", "POST", "/api/watermark/config", json_body={"enabled": False})
check("保存质检配置", "POST", "/api/qc/config", json_body={})
check("LLM 配置校验（应 400 合法报错）", "POST", "/api/llm/config", json_body={}, expect=(400,))
check("托管计划字段校验（应 400）", "POST", "/api/autopilot/plan/__x__", json_body={}, expect=(400,))

print()
print("=" * 96)
print("C. 畸形请求不应产生 5xx")
print("=" * 96)
check("九宫格 畸形JSON → 400", "POST", "/api/storyboard/nine-grid",
      json_body=None, expect=(400,))
r = requests.post(f"{BASE}/api/relations", headers={"content-type": "application/json"},
                  data="{not json", timeout=30)
ok = r.status_code == 400
results.append(("关系 畸形JSON → 400", r.status_code, "OK" if ok else "FAIL"))
print(f"  [{'OK ' if ok else 'FAIL'}] {r.status_code:<4} 关系 畸形JSON → 400")
r = requests.post(f"{BASE}/api/storyboard/nine-grid", headers={"content-type": "application/json"},
                  data="{not json", timeout=30)
ok = r.status_code == 400
results.append(("九宫格 畸形JSON → 400", r.status_code, "OK" if ok else "FAIL"))
print(f"  [{'OK ' if ok else 'FAIL'}] {r.status_code:<4} 九宫格 畸形JSON → 400")

print()
print("=" * 96)
print("D. 九宫格生成 → 选定（原先 500 且选定必 404）")
print("=" * 96)
r = check("生成九宫格", "POST", "/api/storyboard/nine-grid",
          json_body={"project": "__final_check__", "scene_description": "深夜山道，少年持断剑独立",
                     "character_ids": [], "emotion": "tense"})
gid = None
if r is not None:
    d = r.json()
    gid = d.get("grid_id")
    print(f"        grid_id = {gid} | 镜头数 = {len(d.get('shots') or [])}")
if gid:
    check("选定最佳构图", "POST", f"/api/storyboard/nine-grid/{gid}/select",
          json_body={"project": "__final_check__", "selected_index": 2})

print()
print("=" * 96)
print("E. 导出（原先空壳 + 占位名 项目）")
print("=" * 96)
for proj in ["剑心初醒", "E2E短测试集_2镜"]:
    r = check(f"生成导出 {proj}", "POST", "/api/export/" + urllib.parse.quote(proj),
              json_body={"formats": ["fcpml", "edl", "json"]}, timeout=90)
    if r is not None:
        d = r.json()
        print(f"        shot_count = {d.get('shot_count')} | total_sec = {d.get('total_sec')}")
    check(f"下载 FCPML {proj}", "GET",
          "/api/export/" + urllib.parse.quote(proj) + "/fcpml")

print()
print("=" * 96)
print("F. 视频生成（原先路由被截断 → 500）")
print("=" * 96)
check("视频生成 参数校验（应 400）", "POST", "/api/videos/generate", json_body={}, expect=(400,))

print()
print("=" * 96)
bad = [x for x in results if x[2] != "OK"]
print(f"总计 {len(results)} 项 · 通过 {len(results) - len(bad)} · 失败 {len(bad)}")
if bad:
    print("失败项:")
    for n, c, f in bad:
        print(f"  - {n} ({c}) {f}")
else:
    print("全部通过 ✅")
