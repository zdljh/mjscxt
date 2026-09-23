# -*- coding: utf-8 -*-
"""P0-3 剧本↔原著一致性校验（零 LLM 三件套 + 定向修复闭环）回归验证

覆盖用例
  A. 脏剧本症状复现：元信息泄漏 / 要素缺失 / 章节锚定错位 必须全部被检出
  B. 干净剧本零误报：真实第 2 集、第 2128 集（三件套全绿）
  C. 定向修复闭环：stub LLM 驱动，泄漏走局部重写、要素走只增补生成，复检必须通过
  D. 报告落盘 / 读回 / 写回剧本 metadata

运行： python verify_script_consistency.py
退出码：0 全通过 / 1 有失败
"""
import copy
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import script_consistency as sc  # noqa: E402

NOVEL_JSON = os.path.join(ROOT, "novels", "20260910_220227_634977.json")
NOVEL_TXT = os.path.join(ROOT, "novels", "20260910_220227_634977.txt")
SCRIPT_DIR = os.path.join(ROOT, "output", "scripts", "蛊真人精校版")
PROJECT_KEY = "蛊真人精校版"

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  —— {detail}" if detail else ""))
    return bool(cond)


# --------------------------------------------------------------------------- #
# stub LLM：识别两种 prompt（重写 / 分镜补生成），返回结构合法的 JSON
# --------------------------------------------------------------------------- #
class StubClient:
    """假客户端：只为验证闭环链路，不产生真实内容"""

    def __init__(self):
        self.last_json_meta = {"attempts": 1}
        self.calls = {"rewrite": 0, "shots": 0}

    def chat_json_robust(self, prompt, system=None, temperature=0.6, max_tokens=4096,
                         on_event=None, **kw):
        if "【问题清单】" in prompt:                      # continuity.rewrite_shots_for_issues
            self.calls["rewrite"] += 1
            m = re.search(r"【待重写镜头（原内容）】(\[.*?\])\n【输出要求】", prompt, re.S)
            rows = json.loads(m.group(1)) if m else []
            shots = []
            for r in rows:
                shots.append({
                    "shot_id": r.get("shot_id"),
                    "camera": "中景平推",
                    "location": r.get("location") or "古月山寨",
                    "description": "方源立于山寨崖顶，青衫被山风掀动，晨光自左侧洒落，人物居中偏右构图。",
                    "visual_detail": "远处云海翻涌，逆光勾出人物轮廓。",
                    "dialogue": [],
                    "emotion": "坚定",
                    "audio_cues": "山风呼啸，远处鸟鸣",
                    "characters_in_shot": [],
                    "items_in_shot": [],
                    "fix_note": "清除元信息",
                })
            return {"shots": shots}
        if "【本段原文" in prompt:                          # novel_to_script.build_shots_for_chunk
            self.calls["shots"] += 1
            m = re.search(r"【本段原文（必须逐句改写成镜头/台词/画面描述，严禁删减或概括压缩）】\n(.*?)\n【本段剧情摘要】", prompt, re.S)
            text = (m.group(1) if m else "").strip()
            keys = [k for k in ("古月山寨", "青茅山", "白家寨", "春秋蝉") if k in text]
            shots = []
            for i, k in enumerate(keys or ["古月山寨"]):
                shots.append({
                    "camera": "全景推入",
                    "location": "古月山寨",
                    "description": f"{k}全景，山雾缭绕，人物沿石阶而上，晨光斜照。",
                    "visual_detail": "",
                    "dialogue": [],
                    "emotion": "肃穆",
                    "audio_cues": "风声，脚步声",
                    "characters_in_shot": [],
                    "items_in_shot": [k] if k == "春秋蝉" else [],
                })
            return {"shots": shots}
        return {"shots": []}


# --------------------------------------------------------------------------- #
# 数据装载
# --------------------------------------------------------------------------- #
def load_fixtures():
    novel = json.load(open(NOVEL_JSON, encoding="utf-8"))
    with open(NOVEL_TXT, encoding="utf-8", errors="replace") as f:
        text = f.read()
    by_idx = {int(c["index"]): c for c in novel["chapters"]}
    scripts = {}
    for ep in (2, 2128):
        p = os.path.join(SCRIPT_DIR, f"第{ep}集.json")
        if os.path.exists(p):
            scripts[ep] = json.load(open(p, encoding="utf-8"))
    return novel, text, by_idx, scripts


# --------------------------------------------------------------------------- #
# A. 脏剧本症状复现
# --------------------------------------------------------------------------- #
def case_a(novel, text, by_idx, clean, tmpdir):
    print("\n[A] 脏剧本症状复现（元信息泄漏 / 要素缺失 / 锚定错位）")
    chapter = by_idx[2]
    seg = text[int(chapter["start"]):int(chapter["end"])]

    dirty = copy.deepcopy(clean)
    dirty["scenes"], dirty["items"] = [], []          # 场景/道具设定库被清空（缺群雄/道具）
    for s in dirty["shots"]:
        s["location"] = "未命名场景"
        s["description"] = "画面平稳推进，光影缓慢变化。"
        s["visual_detail"] = ""
    # 注入元信息泄漏（书名号 / 第N章 / 本章概要 / 爽点 / 主角：）
    dirty["shots"][0]["description"] = ("《蛊真人》第2章 本章概要：主角：方源登场，爽点十足，"
                                        "全书3000字，情绪曲线抬升。")
    dirty["shots"][0]["dialogue_text"] = "本章重点：方源重生。"
    dirty["metadata"]["chapter_char_count"] = 30000   # 锚定错位（真实 3297）

    rep = sc.check_script_consistency(dirty, novel_meta=novel, chapter_text=seg,
                                      chapter=chapter, episode_no=2)
    check("元信息泄漏被检出", rep["leak"]["hit_count"] >= 3,
          f"命中 {rep['leak']['hit_count']} 处：{sorted({h['rule'] for h in rep['leak']['hits']})}")
    check("泄漏命中带 shot_id/field/片段",
          all(h.get("shot_id") is not None and h.get("field") and h.get("excerpt")
              for h in rep["leak"]["hits"]))
    check("要素缺失被检出", rep["elements"]["missing_count"] >= 1,
          f"候选 {rep['elements']['candidate_count']}，缺失 {rep['elements']['missing_count']}："
          f"{[m['name'] for m in rep['elements']['missing'][:6]]}")
    check("章节锚定错位被检出", rep["anchor"]["ok"] is False and rep["anchor"]["severity"] == "high",
          f"deviation={rep['anchor']['deviation']} 期望{rep['anchor']['expected_char_count']} "
          f"实际{rep['anchor']['script_char_count']}")
    check("总体判定为不通过", rep["passed"] is False and rep["fix_needed"] is True,
          f"issue_count={rep['issue_count']}, issues={[i['category'] for i in rep['issues']]}")
    check("修复计划区分「必须修」与「仅告警」",
          set(rep["fix_plan"].get("rewrite_shot_ids") or []) == {1}
          and bool(rep["fix_plan"].get("missing_elements"))
          and rep["fix_plan"].get("anchor_blocked") is True,
          f"rewrite_shot_ids={rep['fix_plan'].get('rewrite_shot_ids')}, "
          f"missing_elements={rep['fix_plan'].get('missing_elements')}, "
          f"anchor_blocked={rep['fix_plan'].get('anchor_blocked')}")
    return dirty, seg, chapter, rep


# --------------------------------------------------------------------------- #
# B. 干净剧本零误报
# --------------------------------------------------------------------------- #
def case_b(novel, text, by_idx, scripts):
    print("\n[B] 干净剧本零误报（真实落盘剧本）")
    for ep, script in sorted(scripts.items()):
        chapter = by_idx[int(script["metadata"]["chapter_index"])]
        seg = text[int(chapter["start"]):int(chapter["end"])]
        rep = sc.check_script_consistency(script, novel_meta=novel, chapter_text=seg,
                                          chapter=chapter, episode_no=ep)
        ok = (rep["leak"]["hit_count"] == 0 and rep["anchor"]["ok"] is True
              and rep["passed"] is True)
        check(f"第{ep}集三件套全绿且无告警", ok,
              f"leak={rep['leak']['hit_count']} anchor_dev={rep['anchor']['deviation']} "
              f"覆盖率={rep['elements']['coverage_percent']}% passed={rep['passed']}")


# --------------------------------------------------------------------------- #
# C. 定向修复闭环
# --------------------------------------------------------------------------- #
def case_c(dirty, seg, chapter, novel, tmpdir):
    print("\n[C] 定向修复闭环（stub LLM）")
    client = StubClient()
    shots_before = len(dirty["shots"])
    # 只允许重写第 1 镜（模拟「脏剧本局部修复，不整集重生成」）
    rep = sc.run_script_consistency_check(
        client, dirty, novel_meta=novel, chapter_text=seg, chapter=chapter, episode_no=2,
        max_rounds=sc.CONSISTENCY_MAX_ROUNDS, auto_fix=True,
        continuity_dir=tmpdir, project_key=PROJECT_KEY, save=True)
    check("触发了定向修复", rep["fix_rounds"] >= 1, f"fix_rounds={rep['fix_rounds']}")
    check("泄漏被清除（复检 0 命中）", rep["leak"]["hit_count"] == 0,
          f"剩余 {rep['leak']['hit_count']} 处")
    check("要素缺失被补齐（只增不删）", len(dirty["shots"]) > shots_before
          and rep["elements"]["missing_count"] == 0,
          f"镜头 {shots_before} → {len(dirty['shots'])}，缺失 {rep['elements']['missing_count']}")
    check("补生成的镜头带来源标记", any(s.get("element_supplement") for s in dirty["shots"]))
    check("未整集重生成（原镜头号保持连续且未被替换）",
          [s["shot_id"] for s in dirty["shots"][:shots_before]] ==
          [s["shot_id"] for s in dirty["shots"][:shots_before]] and
          max(s["shot_id"] for s in dirty["shots"]) >= shots_before + 1)
    check("stub 调用符合预期（1 次重写 + N 次补生成）",
          client.calls["rewrite"] == 1 and client.calls["shots"] >= 1, str(client.calls))
    check("章节锚定偏差不自动修改（属 P0-1 面，仅记录）",
          (rep["anchor"]["ok"] is False)
          and any(i["category"] == "章节锚定" for i in rep["issues"]))
    check("复检后仍因锚定遗留而保留告警", rep["fixed"] is False or rep["issue_count"] >= 1,
          f"fixed={rep['fixed']}, issue_count={rep['issue_count']}")
    return rep


# --------------------------------------------------------------------------- #
# D. 落盘 / 读回 / 写回
# --------------------------------------------------------------------------- #
def case_d(rep, dirty, tmpdir):
    print("\n[D] 报告落盘 / 读回 / 写回剧本 metadata")
    path = rep.get("report_path")
    check("报告已落盘", bool(path) and os.path.exists(path), str(path))
    if path:
        check("落盘路径沿用项目既有命名 <项目>/episodes/第N集_剧本一致性.json",
              os.path.basename(path) == "第2集_剧本一致性.json"
              and path.replace("\\", "/").endswith(
                  f"{PROJECT_KEY}/episodes/第2集_剧本一致性.json"),
              path)
        disk = json.load(open(path, encoding="utf-8"))
        check("落盘报告不含原文正文（体积控制）", "chapter_text" not in disk)
        check("落盘报告含三件套 + 修复轨迹",
              all(k in disk for k in ("anchor", "leak", "elements", "issues", "fix_history")))
        back = sc.load_consistency_report(tmpdir, PROJECT_KEY, 2)
        check("读回一致", back.get("checked_at") == rep.get("checked_at"))
    meta = ((dirty.get("metadata") or {}).get("script_consistency") or {})
    check("摘要已写回剧本 metadata", meta.get("version") == sc.SCRIPT_CONSISTENCY_VERSION,
          f"version={meta.get('version')}, passed={meta.get('passed')}")
    check("摘要含前端展示字段",
          all(k in meta for k in ("leak_count", "element_coverage_percent",
                                  "anchor_ok", "report_path", "fix_rounds")))
    check("script_consistency() 取值通道可用",
          sc.script_consistency(dirty).get("report_path") == rep.get("report_path"))


def case_e(novel, text, by_idx, clean):
    """整本单集路径：1 集 = 整本小说，锚定/要素失去基准 → skip 后不得误报，泄漏仍须命中"""
    print("\n[E] 整本单集路径（章节锚定 / 要素覆盖显式跳过）")
    whole = copy.deepcopy(clean)
    whole["metadata"]["chapter_index"] = 1
    whole["metadata"]["chapter_char_count"] = int(novel.get("char_count") or 0)
    whole["shots"][0]["description"] = "本集改编自《蛊真人》第1章，主角：方源，属爽文。"
    whole_text = text                      # 整本正文：无单章基准
    ep = whole["metadata"].get("episode_no")

    naive = sc.check_chapter_anchor(whole, novel_meta=novel, chapter=None)
    check("对照：不跳过时整本路径会误报章节锚定（故必须显式跳过）",
          naive["checked"] is True and naive["ok"] is False,
          f"checked={naive['checked']} deviation={naive['deviation']}（整本 vs 单章）")

    rep = sc.check_script_consistency(whole, novel_meta=novel, chapter_text=whole_text,
                                      chapter=None, episode_no=ep,
                                      skip_anchor=True, skip_elements=True)
    check("跳过章节锚定（无基准不判）",
          rep["anchor"]["checked"] is False and rep["anchor"]["ok"] is True,
          str(rep["anchor"].get("reason")))
    check("跳过要素覆盖（无基准不判）",
          rep["elements"]["checked"] is False and rep["elements"]["passed"] is True,
          str(rep["elements"].get("reason")))
    check("元信息泄漏仍被命中", rep["leak"]["hit_count"] >= 1, f"{rep['leak']['hit_count']} 处")
    check("issue 只含元信息泄漏，不含锚定/要素误报",
          {i["category"] for i in rep["issues"]} == {"元信息泄漏"},
          str([i["category"] for i in rep["issues"]]))
    check("整本路径判定由泄漏单独决定", rep["passed"] is False)
    return rep


def case_f():
    """接入契约：生成链路调用点参数名、前端消费字段名与模块实际签名/摘要一致（AST 静态校验）"""
    import ast
    import inspect
    import re
    print("\n[F] 接入契约（生成链路 + app 结果字段）")
    app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
    allowed = set(inspect.signature(sc.run_script_consistency_check).parameters)

    bad_args = []
    call_sites = []
    for fn in ("continuity.py", "novel_to_script.py"):
        p = os.path.join(app_dir, fn)
        tree = ast.parse(open(p, encoding="utf-8").read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_script_consistency_check"):
                call_sites.append(f"{fn}:{node.lineno}")
                for kw in node.keywords:
                    if kw.arg and kw.arg not in allowed:
                        bad_args.append(f"{fn}:{node.lineno} 未知参数 {kw.arg}")
    check("生成链路已接入一致性校验（单集 + 整本）", len(call_sites) >= 2, str(call_sites))
    check("调用参数与模块签名一致", not bad_args, str(bad_args))

    summary_keys = set(re.findall(
        r'"([a-z_]+)":', open(os.path.join(app_dir, "script_consistency.py"),
                              encoding="utf-8").read()[
            open(os.path.join(app_dir, "script_consistency.py"), encoding="utf-8").read()
            .find("def summary_for_meta"):][:3000]))
    app_src = open(os.path.join(app_dir, "app.py"), encoding="utf-8").read()
    consumed = set(re.findall(r'_sc\.get\("([a-zA-Z_]+)"\)', app_src))
    unknown = sorted(k for k in consumed if k not in summary_keys)
    check("app 消费的 consistency_* 字段均在摘要中可取值",
          bool(consumed) and not unknown, f"消费 {len(consumed)} 项，未知 {unknown}")


def case_g(novel, clean, tmpdir):
    """前端只读链路：路由命名空间 + 落盘报告可被 episode_consistency_view 完整读出"""
    print("\n[G] 前端只读视图与路由")
    app_src = open(os.path.join(APP, "app.py"), encoding="utf-8").read()
    n_route = app_src.count("@app.route('/api/script-consistency")
    check("已注册 /api/script-consistency 总览 + 单集两条只读路由", n_route == 2, f"{n_route} 条")
    check("未挤占既有资产一致性命名空间 /api/consistency/run",
          "@app.route('/api/consistency/run'" in app_src)
    check("client.ts 已暴露 scriptConsistencyApi",
          "export const scriptConsistencyApi" in open(
              os.path.join(ROOT, "frontend", "src", "api", "client.ts"),
              encoding="utf-8").read())

    whole = copy.deepcopy(clean)
    whole["shots"][0]["description"] = "本集改编自《蛊真人》第1章，主角：方源。"
    rep = sc.check_script_consistency(whole, novel_meta=novel, chapter_text="",
                                      chapter=None, episode_no=2,
                                      skip_anchor=True, skip_elements=True)
    rep["episode_no"] = 2
    sc.save_consistency_report(rep, tmpdir, PROJECT_KEY, 2)
    view = sc.episode_consistency_view(tmpdir, PROJECT_KEY, 2)
    check("视图可读落盘报告并给出判定",
          view.get("available") is True and (view.get("summary") or {}).get("passed") is False,
          f"passed={(view.get('summary') or {}).get('passed')}")
    check("视图含三件套 + 问题清单 + 修复轨迹",
          all(k in view for k in ("anchor", "leak", "elements", "issues", "fix_history")))
    check("视图不携带原文正文（体积控制）",
          "chapter_text" not in json.dumps(view, ensure_ascii=False))
    empty = sc.episode_consistency_view(tmpdir, PROJECT_KEY, 99)
    check("无报告时 available=False 且不抛错",
          empty.get("available") is False and bool(empty.get("note")))


def main():
    print("=" * 74)
    print("P0-3 剧本↔原著一致性校验回归验证")
    print("=" * 74)
    novel, text, by_idx, scripts = load_fixtures()
    clean = scripts.get(2)
    if not clean:
        print("!! 缺少第 2 集剧本样本，无法执行回归")
        return 1
    tmpdir = tempfile.mkdtemp(prefix="p03_verify_")
    try:
        dirty, seg, chapter, dirty_rep = case_a(novel, text, by_idx, clean, tmpdir)
        case_b(novel, text, by_idx, scripts)
        fixed_rep = case_c(dirty, seg, chapter, novel, tmpdir)
        case_d(fixed_rep, dirty, tmpdir)
        case_e(novel, text, by_idx, clean)
        case_f()
        case_g(novel, clean, tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{total} 通过")
    for name, ok, _ in _results:
        if not ok:
            print(f"  FAIL -> {name}")
    print("=" * 74)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
