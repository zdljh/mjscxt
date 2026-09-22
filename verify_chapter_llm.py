# -*- coding: utf-8 -*-
"""章节 LLM 识别（chapter_llm + split_chapters extra_patterns）· 离线回归脚本

需求：每本小说的章节标记格式都可能不同，让前端「文本分析模型」（OpenAI 兼容）
看样本归纳出「这本书的章节标题正则」，并入 ``novel_parser.split_chapters`` 的正则
扫描；LLM 未配置/失败时自动退回纯正则，绝不阻断上传。

判定逻辑在 ``app/chapter_llm.py``（零第三方依赖）与
``app/novel_parser.split_chapters``（新增 ``extra_patterns`` 参数），均可离线单测。

验收断言（含**合成样本自检**——坏样本必须被抓到，否则守卫无意义）：
  1. parse_patterns：从 dict / 字符串 / 坏结构里都能正确抽出「可编译正则」，
     坏项（超长 / 无法编译 / 非字符串）被丢弃；
  2. 去重：重复正则只保留一条；
  3. 数量上限：超过 MAX_PATTERNS 时截断；
  4. build_prompt：产出合法提示词且包含样本与 JSON 契约；
  5. split_chapters 的 extra_patterns：能识别**纯正则漏检**的章节格式（合成：
     带「【场景三】」/「一、」这类正文标题，纯正则 CHAPTER_PATTERNS 认不出，
     靠 extra_patterns 才认得出）；
  6. split_chapters 回退：extra_patterns 为空 / None / 编译失败时，行为与改造前
     完全一致（只按内置 CHAPTER_PATTERNS 切）；
  7. derive_patterns：client 为 None / 无 chat_json_robust / 调用抛错时返回 []（不抛）。

运行（仅需标准库）：
    MJSCXT_AUTOPILOT=0 python verify_chapter_llm.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)

import chapter_llm  # noqa: E402
import novel_parser  # noqa: E402

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _fake_client(patterns_resp, raise_on_call: bool = False):
    """构造一个只实现 chat_json_robust 的最小假客户端。"""

    class _Fake:
        def chat_json_robust(self, prompt, system=None, temperature=0.4,
                             max_tokens=4096, max_attempts=3, **kw):
            if raise_on_call:
                raise RuntimeError("LLM 上游炸了")
            return patterns_resp

    return _Fake()


print("=" * 72)
print("C1　chapter_llm.parse_patterns（纯函数，含坏样本自检）")
print("=" * 72)

# 正常：从 dict 抽出合法正则
good = {"patterns": [r"(?m)^【场景[一二三四五六七八九十\d]+】", r"(?m)^一、.+"],
        "note": "两种章节标记"}
got = chapter_llm.parse_patterns(good)
check("C1.1 dict 响应抽出 2 条正则",
      len(got) == 2 and all(isinstance(p, str) for p in got), f"got={got}")

# 坏样本自检①：超长正则必须被丢弃（_sanitize_one 限长 MAX_PATTERN_LEN）
huge = "x" * (chapter_llm.MAX_PATTERN_LEN + 1)
got_huge = chapter_llm.parse_patterns({"patterns": [huge, r"(?m)^第\d+章"]})
check("C1.2 超长正则被丢弃、合法项保留",
      len(got_huge) == 1 and huge not in got_huge, f"got={got_huge}")

# 坏样本自检②：无法编译的正则必须被丢弃
broken = r"(?m)^第\d+章[未闭合"
got_broken = chapter_llm.parse_patterns({"patterns": [broken, r"(?m)^Chapter \d+"]})
check("C1.3 无法编译的正则被丢弃、合法项保留",
      len(got_broken) == 1 and broken not in got_broken, f"got={got_broken}")

# 坏样本自检③：patterns 非 list / 含非字符串项 → 丢弃
got_mixed = chapter_llm.parse_patterns({"patterns": [123, r"(?m)^一、.+"]})
check("C1.4 非字符串项被丢弃、合法项保留",
      got_mixed == [r"(?m)^一、.+"], f"got={got_mixed}")

# 去重
got_dup = chapter_llm.parse_patterns(
    {"patterns": [r"(?m)^第\d+章", r"(?m)^第\d+章", r"(?m)^一、.+"]})
check("C1.5 重复正则只保留一条", len(got_dup) == 2, f"got={got_dup}")

# 数量上限
many = [f"(?m)^第{i}章" for i in range(50)]
got_many = chapter_llm.parse_patterns({"patterns": many}, max_patterns=5)
check("C1.6 超过 max_patterns 被截断",
      len(got_many) == 5, f"len={len(got_many)}")

# 字符串输入（含 JSON）
got_str = chapter_llm.parse_patterns('{"patterns": ["(?m)^一、.+"]}')
check("C1.7 字符串 JSON 输入也能解析", got_str == [r"(?m)^一、.+"], f"got={got_str}")

# 全坏 → 空
check("C1.8 无有效 patterns → 返回 []",
      chapter_llm.parse_patterns({"patterns": [huge, broken]}) == [])

print()
print("=" * 72)
print("C2　chapter_llm.build_prompt / make_sample")
print("=" * 72)
sample = chapter_llm.make_sample("第一行\n\n\n\n\n第五行", sample_chars=100)
check("C2.1 make_sample 压缩连续空行",
      "\n\n\n" not in sample and "第一行" in sample, f"sample={sample!r}")
check("C2.2 make_sample 空文本返回空串", chapter_llm.make_sample("") == "")
check("C2.3 make_sample 截断超长样本",
      len(chapter_llm.make_sample("x" * 99999, sample_chars=500)) == 500)

prompt = chapter_llm.build_prompt("第X章 …")
check("C2.4 build_prompt 含样本与 JSON 契约",
      "第X章" in prompt and '"patterns"' in prompt, "缺关键内容")
check("C2.5 build_prompt 空样本有占位", "（样本为空）" in
      chapter_llm.build_prompt("  "))

print()
print("=" * 72)
print("C3　split_chapters 的 extra_patterns（识别纯正则漏检的格式）")
print("=" * 72)

# 合成正文：章节标题用「【场景一】」「一、」这类内置 CHAPTER_PATTERNS 认不出的格式
text = (
    "【场景一】村口。\n林川推开门，天色将晚。\n\n"
    "一、 风波起。\n他听见远处传来喊声。\n\n"
    "【场景二】山道。\n他加快脚步。\n"
)
# 先证伪：只用内置正则，正文里没有「第N章/Chapter/序章」等，应切不出标题章（走兜底或空）
regex_only = novel_parser.split_chapters(text)
# 内置正则匹配不到【场景/一、，regex_only 会走 _fallback_split（按字数）得到 1 节「全文/第N节」
check("C3.1 基线：纯内置正则对该格式无标题识别（1 节兜底）",
      len(regex_only) == 1, f"len={len(regex_only)} titles={[c['title'] for c in regex_only]}")

# 加入 LLM 归纳出的正则后，应识别出 3 个章节标题（【场景一】/ 一、风波起 / 【场景二】）
# 注意：【场景一】 后面还有「村口。」等内容，所以正则不带行尾 $ 锚定，只锚定行首
llm_patterns = [r"(?m)^【场景[一二三四五六七八九十\d]+】",
                r"(?m)^[一二三四五六七八九十\d]+、\s*\S.{0,20}$"]
with_llm = novel_parser.split_chapters(text, extra_patterns=llm_patterns)
titles = [c["title"] for c in with_llm]
check("C3.2 加 extra_patterns 后识别出 3 个章节",
      len(with_llm) == 3, f"titles={titles}")
check("C3.3 章节 title 取自标题行（【场景一】/ 一、 风波起）",
      any("场景一" in t for t in titles) and any("风波起" in t for t in titles),
      f"titles={titles}")
check("C3.4 章节 start/end 边界合法（升序、末章 end=len(text)）",
      with_llm[0]["start"] == text.index("【场景一】")
      and with_llm[-1]["end"] == len(text)
      and with_llm[0]["start"] < with_llm[1]["start"],
      f"bounds={[(c['start'], c['end']) for c in with_llm]}")

# 回退：extra_patterns=None / [] / 编译失败 → 行为等同纯内置正则
check("C3.5 extra_patterns=None 与不传参数结果一致",
      [c["index"] for c in novel_parser.split_chapters(text)] ==
      [c["index"] for c in novel_parser.split_chapters(text, extra_patterns=None)])
check("C3.6 extra_patterns 含坏正则时被静默跳过、不影响合法项",
      len(novel_parser.split_chapters(
          text, extra_patterns=llm_patterns + ["(未闭合"])) == 3)

print()
print("=" * 72)
print("C4　chapter_llm.derive_patterns（降级路径：绝不抛）")
print("=" * 72)
check("C4.1 client=None → []",
      chapter_llm.derive_patterns(None, text) == [])
check("C4.2 无 chat_json_robust → []",
      chapter_llm.derive_patterns(object(), text) == [])
check("C4.3 LLM 抛错 → []（不 raise）",
      chapter_llm.derive_patterns(_fake_client(None, raise_on_call=True), text) == [])
ok_client = _fake_client({"patterns": [r"(?m)^【场景[一二三\d]+】"]})
check("C4.4 LLM 返回合法正则 → 透传给调用方",
      chapter_llm.derive_patterns(ok_client, text) == [r"(?m)^【场景[一二三\d]+】"])
check("C4.5 LLM 返回空 patterns → []",
      chapter_llm.derive_patterns(_fake_client({"patterns": []}), text) == [])

# 守卫自检：确认上面的判据真能区分「LLM 命中」与「未命中」，避免永远返回通过的假守卫
_probe_hit = chapter_llm.derive_patterns(_fake_client(
    {"patterns": [r"(?m)^【场景[一二三\d]+】"]}), text)
_probe_miss = chapter_llm.derive_patterns(_fake_client({"patterns": []}), text)
check("C4.6 自检：命中/未命中 两条路径结果确实不同",
      len(_probe_hit) == 1 and len(_probe_miss) == 0,
      f"hit={_probe_hit} miss={_probe_miss}")

print()
_total = _PASSES[0] + len(_FAILS)
print(f"verify_chapter_llm：{_PASSES[0]}/{_total} 通过")
if _FAILS:
    print("失败项：")
    for _f in _FAILS:
        print(f"  - {_f}")
    sys.exit(1)
print("✅ 章节 LLM 识别离线回归全部通过")
sys.exit(0)
