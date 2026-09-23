"""D1 守卫：单镜重跑接口必须 ①有 _autopilot_guard ②整段关键区在 GPU 闸门内。

背景（2026-09-23 排查 D1）：
- `/api/storyboard/retry-shot` 与 `/api/video/retry-shot` 原先既没有异常兜底
  （抛错 → werkzeug HTML 500，前端 readError() 解不出 error 字段），
  也不在 `gpu_task_gate` 内 → 可与批量 worker 真正并发跑 ComfyUI，
  且两边**并发 read-modify-write 同一个 storyboard_manifest.json**（丢更新）。
- 另一个坑：`_autopilot_guard` 原先定义在 app.py **后段**，只能装饰其后注册的路由；
  给早注册的路由加 `@_autopilot_guard` 会直接 NameError。故守卫要同时锁
  「定义位置 < 首个使用位置」。

断言（静态 AST，不 import app，避免触发 app 的模块级副作用）：
  §1 `_autopilot_guard` 定义行号 < 所有使用它的装饰器行号（顺序正确）
  §2 两个重跑路由都带 `@_autopilot_guard`
  §3 两个路由函数体在 `with gpu_task_gate.run_gpu_task(...)` 内 `return` 到 impl
  §4 impl 存在，且分镜 impl 内含 `_update_storyboard_manifest_shot` 调用
     （证明 manifest 回写落在闸门内 → 与批量 worker 互斥）
  §5 反向探针：把 `@_autopilot_guard` 从分镜路由上抹掉 → §2 必须 FAIL

用法：`C:/Python314/python.exe verify_retry_shot_gate.py`
"""
import ast
import io
import os
import re
import sys

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "app.py")

GATED = ("api_storyboard_retry_shot", "api_video_retry_shot")
GATE_FN = "run_gpu_task"


def _decorator_names(node: ast.AST) -> list:
    out = []
    for d in getattr(node, "decorator_list", []):
        if isinstance(d, ast.Name):
            out.append(d.id)
        elif isinstance(d, ast.Attribute):
            out.append(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            out.append(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def _collect(tree: ast.AST) -> dict:
    """{函数名: (def节点, 行号, 装饰器名列表, 源码段)}"""
    src_lines = None
    funcs = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = (node, node.lineno, _decorator_names(node))
    return funcs


def _calls_in(node: ast.AST, names: tuple) -> list:
    """节点内出现的「被调用名」清单（含方法名与属性链尾名）。"""
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                found.append(f.id)
            elif isinstance(f, ast.Attribute):
                found.append(f.attr)
    return found


def _with_guard_returns(funcs: dict, route: str, impl: str) -> tuple:
    """返回 (闸门内是否 return impl, 闸门是否出现, 说明)"""
    if route not in funcs:
        return False, False, f"未找到路由函数 {route}"
    node = funcs[route][0]
    has_gate = False
    returns_impl = False
    for sub in ast.walk(node):
        if isinstance(sub, ast.With):
            items = []
            for it in sub.items:
                items.extend(_calls_in(it.context_expr, (GATE_FN,)))
            if GATE_FN in items:
                has_gate = True
                # with 内部是否直接 return impl()
                for inner in ast.walk(sub):
                    if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Call):
                        v = inner.value.func
                        if isinstance(v, ast.Name) and v.id == impl:
                            returns_impl = True
    return returns_impl, has_gate, ""


def run(src: str, probe: bool = False) -> list:
    """返回 [(ok, 节号, 描述)]"""
    res = []
    tree = ast.parse(src)
    funcs = _collect(tree)

    # ---- §1 定义位置必须在首个使用之前 ----
    if "_autopilot_guard" not in funcs:
        res.append((False, "§1", "未找到 _autopilot_guard 定义"))
        return res
    def_line = funcs["_autopilot_guard"][1]
    use_lines = []
    for name, (node, ln, decs) in funcs.items():
        if "_autopilot_guard" in decs:
            use_lines.append((name, ln))
    if not use_lines:
        res.append((False, "§1", "没有任何函数使用 @_autopilot_guard（全部丢失？）"))
    else:
        first_use = min(ln for _, ln in use_lines)
        res.append((def_line < first_use, "§1",
                    f"_autopilot_guard 定义行 {def_line} < 首个使用行 {first_use}"
                    f"（使用数 {len(use_lines)}）"))

    # ---- §2/§3/§4 ----
    for route in GATED:
        impl = route.replace("api_", "_") + "_impl"
        decs = funcs[route][2] if route in funcs else []
        if probe and route == "api_storyboard_retry_shot":
            decs = [d for d in decs if d != "_autopilot_guard"]
        res.append(("_autopilot_guard" in decs, "§2",
                    f"{route} 带 @_autopilot_guard（实际 {decs}）"))
        ok_gate, has_gate, msg = _with_guard_returns(funcs, route, impl)
        res.append((ok_gate and has_gate, "§3",
                    f"{route} 在 {GATE_FN} 闸门内 return {impl}()"
                    f"（gate={has_gate} return_impl={ok_gate}）{msg}"))
        res.append((impl in funcs, "§4", f"{impl} 存在"))

    # ---- §4b 分镜 impl 必须包含 manifest 回写（证明它在闸门内）----
    sb_impl = "_storyboard_retry_shot_impl"
    if sb_impl in funcs:
        calls = _calls_in(funcs[sb_impl][0], ("_update_storyboard_manifest_shot",))
        res.append(("_update_storyboard_manifest_shot" in calls, "§4",
                    f"{sb_impl} 内含 manifest 回写（说明回写落在闸门内）"))
    else:
        res.append((False, "§4", f"未找到 {sb_impl}"))

    # ---- §5 生成侧：impl 内确实调用 ComfyUI ----
    for impl, needle in (("_storyboard_retry_shot_impl", "generate_storyboard"),
                         ("_video_retry_shot_impl", "generate_h3_sequence")):
        if impl in funcs:
            res.append((needle in _calls_in(funcs[impl][0], (needle,)), "§5",
                        f"{impl} 调用 {needle}"))
    return res


def main() -> int:
    src = io.open(APP, encoding="utf-8").read()

    print("=" * 68)
    print("正例（当前源码）")
    print("=" * 68)
    results = run(src)
    failed = 0
    for ok, sec, desc in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {sec} {desc}")
        if not ok:
            failed += 1
    print(f"  → {sum(1 for ok, _, _ in results if ok)}/{len(results)} PASS")

    # ---- 反向探针：抹掉装饰器必须变红（证明断言真的在跑）----
    print()
    print("=" * 68)
    print("反向探针（抹掉 @_autopilot_guard）——必须出现 FAIL")
    print("=" * 68)
    probe_results = run(src, probe=True)
    probe_failed = 0
    for ok, sec, desc in probe_results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {sec} {desc}")
        if not ok:
            probe_failed += 1
    print(f"  → 探针 FAIL 数 = {probe_failed}（必须 ≥1）")

    # ---- 反向探针①：把定义移到文件末尾 → §1 必须红 ----
    print()
    print("=" * 68)
    print("反向探针②（把 _autopilot_guard 定义挪到文件末尾）——§1 必须 FAIL")
    print("=" * 68)
    m = re.search(r"^def _autopilot_guard\(fn\):.*?^    return _wrap\n",
                  src, re.S | re.M)
    if not m:
        print("  [FAIL] 探针无法构造（找不到函数体）")
        probe_failed += 1
    else:
        block = m.group(0)
        moved = src[:m.start()] + src[m.end():] + "\n\n" + block
        r2 = [x for x in run(moved) if x[1] == "§1"]
        for ok, sec, desc in r2:
            print(f"  [{'PASS' if ok else 'FAIL'}] {sec} {desc}")
            if not ok:
                probe_failed += 1

    ok_all = (failed == 0) and (probe_failed >= 2)
    print()
    print("=" * 68)
    print(f"结论：正例 FAIL={failed}；反向探针 FAIL={probe_failed}（期望 ≥2）")
    print(f"守卫{'通过 ✅' if ok_all else '未通过 ❌'}")
    print("=" * 68)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
