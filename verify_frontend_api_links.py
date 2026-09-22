# -*- coding: utf-8 -*-
"""前端→后端 API 链路核对（P2-7 / 前端排查）

背景：用户要求「从前端代码开始排查到后端代码，确保前端每个链路都是正确的、通的」。
本脚本做三件事（全部静态分析，零第三方依赖，只读源码）：
  1. 从 frontend/src/api/client.ts 抽取每个 API 方法的 (URL, method, body 字段)；
  2. 从 app/app.py 抽取 @app.route 路由表（path, methods, 函数体预切好的文本）；
  3. 对每个前端方法做：
     a. URL 归一化匹配：把 ${...} 占位符归一成 <seg>、去掉 query，必须命中某条路由
        （含 <path:x> / <x> 通配展开）——不通 = 死链（404）。
     b. method 必须在该路由声明的 methods 里——否则 Flask 405。
     c. body 字段名 vs 路由函数体里 data.get(...) 取的键：打印「前端传了 X 但后端没读 X」
        的告警（可能误报：后端读 request.args 或整体透传，需人工复核，不计入失败）。

运行：MJSCXT_AUTOPILOT=0 python verify_frontend_api_links.py
退出码：0 = 无死链/方法不匹配（字段告警不失败），1 = 存在 404/405。

守卫自检：内置一条合成坏样本（前端调 /api/__no_such_endpoint__），它必须被抓为 404；
抓不到说明匹配逻辑本身失效，守卫静默白给 → 直接退出码 2。

性能约定：路由表（含函数体）在 main() 里预切一次并缓存，cross_check 只做查表，
避免对 11k 行的 app.py 反复做 re.search/find。函数体切分用 @app.route 边界 + 4 空格缩进识别。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLIENT = ROOT / "frontend" / "src" / "api" / "client.ts"
APP = ROOT / "app" / "app.py"


# ---------------------------------------------------------------- 后端路由

def load_backend_routes():
    """返回 [dict(path, methods, body, body_keys)], 按出现顺序。
    - body: 路由函数体文本（从 def 行起到下一个 @app.route 之前）。
    - body_keys: 该函数体内所有 data.get / request.args.get 读取的键名（含默认值形态）。
    单次线性扫描，O(n)，避免 O(n²)。"""
    text = APP.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    n = len(lines)

    # 1) 单次遍历，定位每个 @app.route 装饰器及其 def 行
    route_re = re.compile(r"^\s*@app\.route\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*methods\s*=\s*\[([^\]]+)\])?")
    entries = []  # (path, methods_set, body_start_line_idx)
    i = 0
    while i < n:
        m = route_re.match(lines[i])
        if m:
            path = m.group(1)
            methods = set(re.findall(r"['\"]([A-Z]+)['\"]", m.group(2))) if m.group(2) else {"GET"}
            j = i + 1
            # 跳过 @app.route 下方可能存在的其他 @ 装饰器
            while j < n and lines[j].lstrip().startswith("@"):
                j += 1
            # j 现在应指向 def 行；若被跳过（极端情况），仍记录 body_start
            body_start = j if j < n else i + 1
            entries.append((path, methods, body_start))
            i = j + 1
        else:
            i += 1

    # 2) 用 next entry 的 body_start 做切片边界
    bodies = []
    for k, (path, methods, bstart) in enumerate(entries):
        bnext = entries[k + 1][2] if k + 1 < len(entries) else n
        seg_lines = lines[bstart:bnext]
        body = "".join(seg_lines)
        used = set()
        used |= set(re.findall(r'data\.get\(\s*[\'"](\w+)[\'"]', body))
        used |= set(re.findall(r'request\.args\.get\(\s*[\'"](\w+)[\'"]', body))
        used |= set(re.findall(r'data\.get\(\s*(\w+)', body))
        used |= set(re.findall(r'request\.args\.get\(\s*(\w+)', body))
        bodies.append({
            "path": path,
            "methods": methods,
            "body": body,
            "body_keys": used,
        })
    return bodies


def flask_path_to_regex(path):
    """Flask 动态段 → 正则：<path:x> 匹配含 / 的多段；<x> / <int:x> 匹配单段。"""
    def repl(m):
        inner = m.group(1)
        return ".*" if inner.startswith("path:") else "[^/]+"
    return re.compile("^" + re.sub(r"<([^>]+)>", repl, path) + "$")


# ---------------------------------------------------------------- 前端方法

def _read_string_literal(text, start):
    """从 start（指向引号）读一个字符串字面量，返回 (inner, end_index)。
    支持 ' " ` 三种引号；反引号内的 ${...} 模板占位符整体跳过（含嵌套引号）；
    转义符跳过。end_index 指向引号之后的位置（引号本身已含在 buf 里）。
    """
    quote = text[start]
    buf = quote
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            buf += text[i:i + 2]
            i += 2
            continue
        # 反引号字符串里 ${ 开始模板占位符，需跳过整个 ${...}（含嵌套引号）
        if quote == "`" and c == "$" and i + 1 < len(text) and text[i + 1] == "{":
            # 找到配对的 }（括号深度计数）
            depth = 1
            j = i + 2
            while j < len(text) and depth > 0:
                cj = text[j]
                if cj == "{" :
                    depth += 1
                elif cj == "}":
                    depth -= 1
                elif cj in "'\"`":
                    # 跳过嵌套字符串字面量
                    q2 = cj
                    j += 1
                    while j < len(text):
                        if text[j] == "\\" and j + 1 < len(text):
                            j += 2
                            continue
                        if text[j] == q2:
                            break
                        j += 1
                    j += 1
                    continue
                j += 1
            # j 现在指向 } 之后（depth=0 时 j 已 +1）
            buf += text[i:j]
            i = j
            continue
        buf += c
        i += 1
        if c == quote:
            break
    return buf[1:-1], i


def _find_call_end(text, open_paren_idx):
    """从 text[open_paren_idx]（= '('）找到与之配对的 ')'，返回其索引。
    处理字符串字面量（' " ` 三种）内的括号不计数；转义符跳过。
    找不到则返回 len(text)。"""
    depth = 0
    i = open_paren_idx
    while i < len(text):
        c = text[i]
        if c in "'\"`":
            # _read_string_literal 返回 (inner, end_idx)，end_idx 是引号本身的位置
            # 注意：它内部 i += 1 在 break 之前，所以返回的是「引号后」的位置
            # 我们需要跳到引号后，即 i = end_idx（不要 +1，否则会跳过引号后的字符）
            _, j = _read_string_literal(text, i)
            i = j  # j 已指向引号后的第一个字符
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def load_frontend_apis():
    """逐处定位 request(...) 调用，抽取 (url, method, body_keys, line)。

    client.ts 里 request 是本地函数（L57），前端调用形如
      request<T>('/projects', { method: 'POST', body: JSON.stringify({...}) })
    也含直接 fetch 的 novelsApi.upload。这里覆盖 request() 调用；
    fetch 直接调用单独枚举。

    关键修正（两处）：
    1. method 搜索窗口必须精确到当前 request(...) 调用的右括号闭合处，
       不能简单向后取 600 字符——否则未显式传 method 的 GET 调用
       会越过调用边界、误把下一个 API 方法的 method 拉进来，导致整批 GET 被误判 POST
       进而误报 405。这里用括号配对精确定位每个调用的真实边界。
    2. 泛型 request<...>(...) 的 '(' 位置不能用 m.end()-1 推断（\\s* 可能吃掉空格），
       必须用 text.index('(', m.start()) 显式定位。
    """
    text = CLIENT.read_text(encoding="utf-8")
    apis = []
    start_pat = re.compile(r"request\s*")
    for m in start_pat.finditer(text):
        # m.start() 指向 'r'，找紧跟的 '('（允许 <泛型> 在中间）
        # 先找 m.start() 之后最近的 '('，跳过 <...> 泛型
        after = text[m.end():m.end() + 200]
        paren_m = re.search(r"\(", after)
        if not paren_m:
            continue
        paren_in_after = paren_m.start()
        # 检查 '(' 前是否有 <泛型>，有则确认 <...> 已闭合
        before_paren = after[:paren_in_after].rstrip()
        if before_paren.endswith(">"):
            # 找到匹配的 '<'
            lt_idx = before_paren.rfind("<")
            if lt_idx == -1:
                continue
            # 验证 <...> 内容里括号平衡（简单检查：不能含未闭合的 ( )）
            inner = before_paren[lt_idx + 1:]
            if inner.count("(") != inner.count(")"):
                continue
        open_paren_abs = m.end() + paren_in_after  # 绝对位置的 '('
        pos = open_paren_abs + 1  # '(' 之后第一个字符
        # 跳过空白
        while pos < len(text) and text[pos] in " \t\n":
            pos += 1
        if pos >= len(text) or text[pos] not in "'\"`":
            continue
        url, i = _read_string_literal(text, pos)
        # 用 open_paren_abs 精确找 request(...) 的右括号
        call_end = _find_call_end(text, open_paren_abs)
        tail = text[i:call_end]
        method = "GET"
        mm = re.search(r"method\s*:\s*['\"]([A-Z]+)['\"]", tail)
        if mm:
            method = mm.group(1)
        body_keys = []
        bm = re.search(r"JSON\.stringify\(\s*\{([^{}]*)\}\s*\)", tail, re.S)
        if bm:
            seg = bm.group(1)
            # 只抓「key: value」或「key」形态的键，排除值字面量（true/false/数字/字符串）
            # 对象字面量里的键在前，后跟 ':'；展开键 { ...obj } 视为 <对象>
            for m2 in re.finditer(r"""(?:(["\'])([^"\']+)\1|\b([A-Za-z_$][\w$]*))\s*(?=[:,}]|$)""", seg):
                key = (m2.group(2) or m2.group(3) or "").strip()
                if not key or key in ("method", "body", "headers"):
                    continue
                if key in ("true", "false", "null", "undefined"):
                    continue
                # 排除纯数字字面量
                if key.isdigit():
                    continue
                if key not in body_keys:
                    body_keys.append(key)
        else:
            bm2 = re.search(r"JSON\.stringify\(\s*([A-Za-z_$][\w$]*)\s*\)", tail)
            if bm2:
                body_keys = ["<对象>" + bm2.group(1)]
        apis.append({"url": url, "method": method, "body_keys": body_keys,
                     "at_line": text[:pos].count("\n") + 1})
    # 补上直接 fetch 的调用（novelsApi.upload 等；排除 request 函数内部的通用 fetch）
    for m in re.finditer(r"fetch\s*\(\s*(['\"`])(.*?)\1", text, re.S):
        raw_url = m.group(2)
        # 排除 request 函数内部的通用 fetch（${API_BASE}${path} 形态）
        if re.match(r"^\$\{API_BASE\}\$\{path\}$", raw_url.strip()):
            continue
        # ${API_BASE}/novels/upload → /api/novels/upload（API_BASE='/api'）
        url = raw_url.replace("${API_BASE}", "/api")
        if not url.startswith("/"):
            url = "/" + url
        # fetch 调用同样精确界定边界
        open_paren = text.index("(", m.start())
        fetch_call_end = _find_call_end(text, open_paren)
        fetch_tail = text[m.end():fetch_call_end]
        fmethod = "GET"
        fmm = re.search(r"method\s*:\s*['\"]([A-Z]+)['\"]", fetch_tail)
        if fmm:
            fmethod = fmm.group(1)
        apis.append({"url": url, "method": fmethod, "body_keys": ["<对象>formData"],
                     "at_line": text[:m.start()].count("\n") + 1})
    return apis


def normalize_url(url):
    """把前端 URL 归一成后端可匹配的基础路径：
    - 嵌套模板占位符（${var ? `?x=y` : ''} 等 query 拼接）整段去掉
    - 路径段占位符 ${xxx} → <seg>
    - 末尾 query 变量占位符（${query} / ${qs}）整段去掉
    - 显式 ?x=y query 整体去掉
    - 保证 /api 前缀
    """
    s = url.strip()
    for _ in range(20):
        changed = False
        # 末尾嵌套模板占位符（${var ? `?x=y` : ''}）→ query 拼接，整段去掉
        # 匹配形如 ${...} 且内含 ? / ` / : 的完整占位符
        m = re.search(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}$", s)
        if m:
            inner = m.group(0)
            if "?" in inner or "`" in inner or ":" in inner:
                s = s[:m.start()]
                changed = True
        # 末尾 query 变量占位符（${query} / ${qs} / ${q} / ${params}）整段去掉
        m = re.search(r"\$\{(query|qs|q|params)\}$", s)
        if m:
            s = s[:m.start()]
            changed = True
        # 显式 '?' 及之后全部去掉
        if "?" in s:
            s = s.split("?", 1)[0]
            changed = True
        # 通用单段 ${...} → <seg>（路径段占位符保留）
        new_s = re.sub(r"\$\{[^{}]*\}", "<seg>", s)
        if new_s != s:
            s = new_s
            changed = True
        if not changed:
            break
    if not s.startswith("/api"):
        s = "/api" + s
    return s


# ---------------------------------------------------------------- 主流程

def build_route_index(bodies, exclude_catch_all=True):
    """预编译路由正则 + 按规范化 path 去重（同一 path 多方法合并 methods）。

    exclude_catch_all: 排除服务端顶层 catch-all 路由 `/<path:path>`（`^/.*$`）。
    该路由是 Flask 的 SPA/静态兜底，能接住任意 URL，若保留会让所有前端死链都
    「命中」它而漏报；前端 API 端点的真实存在性应排除它再判定。
    其余形如 /<prefix>/<path:x> 的真实子路由保留（它们前缀固定，仍能区分真实端点）。

    关键：同一 path 的多条路由（GET 一条 + POST 一条）必须把 methods 取**并集**，
    否则前端正确的 POST 调用会被误判成 405。下面用 seen[path] 存同一个 dict 对象，
    后续条目原地 |= 合并 methods/body_keys，保证 ordered 里每个 entry 的方法都是全集。
    """
    seen = {}
    ordered = []
    for b in bodies:
        p = b["path"]
        # 顶层 catch-all：path 去掉 <...> 段后只剩空或 '/' → 即 "/<path:path>"
        stripped = re.sub(r"<[^>]+>", "", p).rstrip("/")
        if exclude_catch_all and stripped in ("", "/"):
            continue
        if p in seen:
            # 同一 dict 对象原地合并，ordered 里那个引用也会自动看到更新
            seen[p]["methods"] |= b["methods"]
            seen[p]["body_keys"] |= b["body_keys"]
            continue
        entry = {
            "path": p,
            "methods": b["methods"],
            "body_keys": b["body_keys"],
            "rgx": flask_path_to_regex(p),
        }
        seen[p] = entry
        ordered.append(entry)
    # 双保险：再次按 path 聚合成 methods 全集，确保任何 entry 的 methods 都是该 path 的并集
    agg = {}
    for e in ordered:
        agg.setdefault(e["path"], e)
    for b in bodies:
        p = b["path"]
        if p in agg:
            agg[p]["methods"] |= b["methods"]
            agg[p]["body_keys"] |= b["body_keys"]
    return ordered


def _seg_count(path):
    """路由 path 的路径段数（静态段 + 动态段）。"""
    return [s for s in path.split("/") if s]


def cross_check(apis, route_index):
    """apis: 前端调用；route_index: build_route_index 输出。返回 (dead, bad_method, field_warn)。

    正确性：命中判定采用「最精确路由优先」——先按 (静态段数量) 降序试 fullmatch，
    精确路由命中才停止，避免 <path:x> 贪婪匹配抢先命中宽路由导致 methods 误判。
    """
    # 预切路由 path 为段列表，动态段标记 kind；静态段越多越精确
    indexed = []
    for e in route_index:
        segs = []
        for s in e["path"].split("/"):
            if not s:
                continue
            if s.startswith("<") and s.endswith(">"):
                inner = s[1:-1]
                kind = "path" if inner.startswith("path:") else "seg"
                segs.append((kind, None))
            else:
                segs.append(("lit", s))
        lit_count = sum(1 for k, _ in segs if k == "lit")
        indexed.append((e, segs, lit_count))
    # 按静态段数量降序，精确路由优先试
    indexed.sort(key=lambda x: -x[2])

    def matches(base_segs, r_segs):
        """返回 True/False；纯线性。

        path 段贪婪匹配「所有剩余段」，但如果 path 段后面还有静态段
        （如 /<path:pid>/delete），则 path 段必须"让路"。用回溯枚举 path 吃的段数。
        """
        n = len(base_segs)

        def _match(i, j):
            while j < len(r_segs):
                kind, val = r_segs[j]
                if kind == "lit":
                    if i >= n or base_segs[i] != val:
                        return False
                    i += 1
                elif kind == "seg":
                    if i >= n:
                        return False
                    i += 1
                else:  # path: 贪婪 + 尾部静态段让路（回溯 path 吃的段数）
                    # 计算 path 之后还有几个非 path 段需要让路
                    tail_fixed = 0
                    j2 = j + 1
                    while j2 < len(r_segs) and r_segs[j2][0] != "path":
                        tail_fixed += 1
                        j2 += 1
                    # path 至少 1 段；最多 = 剩余段数 - tail_fixed
                    min_take = 1
                    max_take = n - i - tail_fixed
                    for take in range(min_take, max_take + 1):
                        if _match(i + take, j + 1):
                            return True
                    return False
                j += 1
            return i == n

        return _match(0, 0)

    dead, bad_method, field_warn = [], [], []
    for a in apis:
        base = normalize_url(a["url"])
        base_segs = [s for s in base.split("/") if s]
        hit = None
        for e, r_segs, _ in indexed:
            if matches(base_segs, r_segs):
                hit = e
                break
        if hit is None:
            dead.append(a)
            continue
        if a["method"] not in hit["methods"]:
            bad_method.append((a, hit["path"], hit["methods"]))
        if a["body_keys"] and a["method"] in ("POST", "PUT"):
            used = hit["body_keys"]
            missing = [k for k in a["body_keys"]
                       if not k.startswith("<") and k not in used]
            if missing:
                field_warn.append((a, hit["path"], missing))
    return dead, bad_method, field_warn


def main():
    if not CLIENT.exists() or not APP.exists():
        print(f"[FAIL] 找不到 {CLIENT} 或 {APP}")
        return 1

    bodies = load_backend_routes()
    route_index = build_route_index(bodies)

    # ---- 守卫自检：合成坏样本必须被抓 404 ----
    sentinel = {"url": "/api/__no_such_endpoint__", "method": "GET",
                "body_keys": [], "at_line": 0}
    s_dead, _, _ = cross_check([sentinel], route_index)
    if not s_dead:
        print("[GUARD SELF-CHECK FAILED] 合成坏样本未被抓 404——匹配逻辑失效，守卫白给")
        return 2

    apis = load_frontend_apis()
    dead, bad_method, field_warn = cross_check(apis, route_index)

    print(f"后端路由 {len(bodies)} 条（去重后 {len(route_index)} 个 path）；前端 request() 调用 {len(apis)} 处\n")

    ok = True
    if dead:
        ok = False
        print("## 死链（前端 URL 归一化后匹配不到任何后端路由 → 404）")
        for a in dead:
            print(f"  [404] L{a['at_line']} {a['method']} {normalize_url(a['url'])}  body={a['body_keys']}")
    if bad_method:
        ok = False
        print("\n## 方法不匹配（命中路由但 method 未声明 → 405）")
        for a, rp, rmethods in bad_method:
            print(f"  [405] L{a['at_line']} {a['method']} {normalize_url(a['url'])} "
                  f"→ 路由 {rp} 仅支持 {sorted(rmethods)}")
    if field_warn:
        print("\n## 字段名告警（前端 body 键在后端函数体未见 data.get/args.get 读取，"
              "需人工复核；非必然 bug，不计失败）")
        for a, rp, missing in field_warn:
            print(f"  [warn] L{a['at_line']} {a['method']} {normalize_url(a['url'])} "
                  f"→ 后端 {rp} 未读: {missing}")

    total = len(apis)
    print(f"\n小结：前端调用 {total} 处 | 死链 {len(dead)} | 方法不匹配 {len(bad_method)} | 字段告警 {len(field_warn)}")
    print("结果:", "ALL PASS" if ok else "BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
