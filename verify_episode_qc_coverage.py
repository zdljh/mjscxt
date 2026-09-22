# -*- coding: utf-8 -*-
"""D-05（P1）整集视频质检覆盖率 · 离线回归脚本

对应缺陷：全项目缺陷审计报告 D-05 ——「整集视频 QC 双重降采样（12 镜摘要 + ≤6 帧）
→ 崩坏镜必然漏检」。开发 B 工作包唯一一条 P1。

验收标准（报告 §5 逐条）：
  ① 40 段整集的抽帧数 ≥ 12，且时间戳覆盖各段中点（相邻间距 < 单段时长×1.5）
  ② 中段注入崩坏 → 整集 QC passed=False（本脚本以「中段是否被采样」可离线验证的部分来断言）
  ③ 摘要串在镜数 > limit 时包含「其余未提供」字样

运行（零第三方依赖，不需要 Flask / requests / ffmpeg）：
    python verify_episode_qc_coverage.py
    MJSCXT_AUTOPILOT=0 python verify_episode_qc_coverage.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import qc_coverage  # noqa: E402

_FAILS = []
_PASSES = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASSES[0] += 1
        print(f"  [PASS] {name}")
    else:
        _FAILS.append(name)
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _segs(n: int, dur: float) -> list:
    return [{"name": f"shot_{i + 1:02d}", "duration": dur} for i in range(n)]


# ============================================================
print("=" * 72)
print("D-05 §1　整集抽帧覆盖：40 段整集，抽帧数 ≥ 12 且覆盖各段中点")
print("=" * 72)

SEG_N, SEG_DUR = 40, 5.0
TOTAL = SEG_N * SEG_DUR          # 200s
segs = _segs(SEG_N, SEG_DUR)
ratios = qc_coverage.episode_frame_ratios(segs)

print(f"  段数={SEG_N}  单段={SEG_DUR}s  全片={TOTAL}s  抽帧占比数={len(ratios)}")

# ① 抽帧数 ≥ 12（修复前固定 3，配置上限 6）
check("1.1 抽帧数 ≥ 12（修复前 ≤6）", len(ratios) >= 12,
      f"实际 {len(ratios)}")

# ① 时间戳覆盖各段中点：把占比还原为秒，逐段核对「该段区间内恰有一个采样点」
times = [r * TOTAL for r in ratios]
in_seg = [0] * SEG_N
for t in times:
    idx = min(SEG_N - 1, max(0, int(t // SEG_DUR)))
    in_seg[idx] += 1
uncovered = [i for i, c in enumerate(in_seg) if c == 0]
check("1.2 每一段都至少被采样一次（修复前中段几十段零采样）", not uncovered,
      f"未覆盖段索引={uncovered[:10]}")

# ① 采样点落在各段中点附近（容差 0.5s）
mid_err = []
for i, t in enumerate(times):
    expected_mid = i * SEG_DUR + SEG_DUR / 2.0
    mid_err.append(abs(t - expected_mid))
worst = max(mid_err) if mid_err else 0.0
check("1.3 采样点落在各段中点（容差 0.5s）", worst <= 0.5,
      f"最大偏差 {worst:.3f}s")

# ① 相邻间距 < 单段时长 × 1.5
gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
max_gap = max(gaps) if gaps else 0.0
check("1.4 相邻间距 < 单段时长×1.5", max_gap < SEG_DUR * 1.5,
      f"最大间距 {max_gap:.3f}s vs 阈值 {SEG_DUR * 1.5:.1f}s")

# ② 中段崩坏可被采样到：取第 20 段（索引 19）区间，断言有采样点落入
CUR = 19
lo, hi = CUR * SEG_DUR, (CUR + 1) * SEG_DUR
hit = [t for t in times if lo <= t < hi]
check("2.1 中段（第 20 段）区间内有采样点 → 崩坏帧可被判读", len(hit) >= 1,
      f"区间 [{lo},{hi}) 采样点={[round(x, 2) for x in hit]}")
# 修复前对照：全片均布 3 帧 = 5%/50%/95% → 10s/100s/190s，第 20 段(95~100s)几乎不可命中
legacy = [TOTAL * f for f in (0.05, 0.50, 0.95)]
legacy_hit = [t for t in legacy if lo <= t < hi]
check("2.2 对照：旧「全片均布 3 帧」在第 20 段区间内无采样点（复现漏检）",
      len(legacy_hit) == 0, f"旧采样点={legacy}")

# ============================================================
print()
print("=" * 72)
print("D-05 §2　摘要不再静默截断：镜数 > limit 时含「其余未提供」")
print("=" * 72)

# ③ 40 镜 ≤ limit(60) → 不再截断，全部镜头可见
shots40 = [{"shot_id": i + 1, "camera": "中景", "description": f"镜头{i + 1}内容"}
           for i in range(40)]
d40 = qc_coverage.episode_qc_desc(shots40)
check("3.1 40 镜整集不再被截断（修复前 limit=12 只给前 12 镜）",
      "镜40" in d40 and qc_coverage.TRUNCATED_NOTE not in d40,
      f"含镜40={('镜40' in d40)}")

# ③ 70 镜 > limit(60) → 截断且显式声明
shots70 = [{"shot_id": i + 1, "camera": "近景", "description": f"内容{i + 1}"}
           for i in range(70)]
warns = []
d70 = qc_coverage.episode_qc_desc(shots70, warn=lambda *a, **k: warns.append(a))
check("3.2 70 镜整集截断时含「其余未提供」字样",
      qc_coverage.TRUNCATED_NOTE in d70,
      f"tail={d70[-60:]!r}")
check("3.3 截断时留下 warning 告警（可观测，不静默）",
      len(warns) >= 1, f"warn 调用 {len(warns)} 次")
check("3.4 摘要仍声明本集真实镜数 70",
      "本集共 70 镜" in d70)

# ============================================================
print()
print("=" * 72)
print("D-05 §3　边界与退化路径")
print("=" * 72)

check("4.1 空 segs → 返回 []（调用方退回配置抽帧数）",
      qc_coverage.episode_frame_ratios([]) == [])
check("4.2 段时长全 0 → 返回 []（不可用，不伪造抽帧点）",
      qc_coverage.episode_frame_ratios([{"duration": 0}] * 5) == [])
check("4.3 段时长缺失（None）→ 返回 []",
      qc_coverage.episode_frame_ratios([{}] * 5) == [])
check("4.4 空 shots → 占位串而非崩溃",
      qc_coverage.episode_qc_desc([]) == "（无镜头信息）")
check("4.5 非 dict 元素被过滤，不抛异常",
      "本集共 1 镜" in qc_coverage.episode_qc_desc([None, "x", {"shot_id": 1}]))

# 上限保护：超出 EPISODE_MAX_FRAMES 时降采样但保持升序与首尾覆盖
many = qc_coverage.episode_frame_ratios(_segs(200, 1.0))
check("4.6 极端段数触发上限降采样（≤ 上限）",
      len(many) <= qc_coverage.EPISODE_MAX_FRAMES,
      f"{len(many)} vs 上限 {qc_coverage.EPISODE_MAX_FRAMES}")
check("4.7 降采样后仍为升序", all(many[i] < many[i + 1] for i in range(len(many) - 1)))

# 不等长段：短段同样被覆盖（按中点折算，不依赖等长假设）
# 段区间：0~2 / 2~12 / 12~14（全片 14s）→ 中点 1 / 7 / 13
uneven = qc_coverage.episode_frame_ratios(
    [{"duration": 2.0}, {"duration": 10.0}, {"duration": 2.0}])
_expected_mid = [1.0, 7.0, 13.0]
check("4.8 不等长段各自中点折算正确",
      len(uneven) == 3 and all(abs(uneven[i] * 14.0 - _expected_mid[i]) < 0.01
                               for i in range(3)),
      f"ratios={[round(r, 4) for r in uneven]} → "
      f"还原={[round(r * 14, 3) for r in uneven]}，期望中点={_expected_mid}")

# 上限放宽到 24（报告建议的备选档位）亦可用
alt = qc_coverage.episode_frame_ratios(_segs(40, 5.0), max_frames=24)
check("4.9 max_frames 显式传入生效（40 段 → 24 帧）", len(alt) == 24)

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
print("✅ D-05 整集质检覆盖率全部通过")
sys.exit(0)
