# -*- coding: utf-8 -*-
"""质检结论解析回归测试：用日志里真实导致死循环的失败样本。"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
import warnings; warnings.filterwarnings("ignore")
import qc_client

CASES = [
    ("真实失败样本：reason 内嵌中文引号",
     '{"score": 55, "pass": false, "reason": "场景与镜头描述不符：灯笼仍亮着而非"同时熄灭"，与剧本核心叙事冲突。", "issues": ["灯笼未熄灭"]}',
     False, 55),
    ("内嵌引号 + 尾逗号",
     '{"score": 82, "pass": true, "reason": "整体符合预期", "issues": [],}',
     True, 82),
    ("markdown 代码块包裹",
     '```json\n{"score": 91, "pass": true, "reason": "构图准确"}\n```',
     True, 91),
    ("分数达标但模型说 pass=false（硬闸应拦）",
     '{"score": 80, "pass": false, "reason": "手部畸形"}',
     False, 80),
    ("分数不达标但模型说 pass=true（硬闸应拦）",
     '{"score": 40, "pass": true, "reason": "还行"}',
     False, 40),
    ("字符串内裸换行（非法 JSON）",
     '{"score": 75, "pass": true, "reason": "第一行\n第二行"}',
     True, 75),
    ("passed 字段别名 + 中文布尔",
     '{"score": 88, "passed": "达标", "reason": "合格"}',
     True, 88),
    ("仅有 score，无 pass",
     '{"score": 66, "reason": "一般"}',
     False, 66),
    ("前后有解释文字（截取花括号区间）",
     '根据分析，结论如下：{"score": 93, "pass": true, "reason": "很好"} 以上。',
     True, 93),
    ("内嵌引号 + 前后文字 + 代码块",
     '```json\n{"score": 70, "pass": true, "reason": "灯笼"熄灭"符合剧本", "issues": []}\n```',
     True, 70),
]

ok = 0
for label, raw, want_pass, want_score in CASES:
    try:
        v = qc_client.parse_verdict(raw, 70)
        got_pass, got_score = v.get("passed"), v.get("score")
        good = (bool(got_pass) == want_pass) and (got_score == want_score)
        ok += good
        print(f"  [{'OK ' if good else 'FAIL'}] {label}")
        print(f"        passed={got_pass} score={got_score} reason={(v.get('reason') or '')[:46]}")
    except Exception as e:
        print(f"  [FAIL] {label}")
        print(f"        抛异常: {type(e).__name__}: {str(e)[:110]}")

print()
print(f"通过 {ok}/{len(CASES)}")

# 额外：真实样本的完整往返（模拟 check_image 的调用方式）
print()
print("=== 真实失败样本解析详情 ===")
v = qc_client.parse_verdict(CASES[0][1], 70)
print(json.dumps({k: v.get(k) for k in ("passed", "score", "reason", "issues",
                                        "blocked", "critical_issues")}, ensure_ascii=False, indent=2)[:600])
