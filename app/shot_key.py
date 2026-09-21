# -*- coding: utf-8 -*-
"""镜号归一化的**唯一实现**（缺陷 P1-19 / 任务 A-10）。

## 为什么要有这个模块

全项目曾散落 **6 套**「镜号 → 序号」实现、**两种互斥语义**：

======================  ====================================  ==================
实现                     取号方式                               ``"S01-C02"`` 结果
======================  ====================================  ==================
``app.py _shot_num_key``  ``re.search(r"\\d+")``（首段数字）      ``"1"``
``app.py _shot_seq``      ``re.search(r"\\d+")``（首段数字）      ``1``
``app.py _norm_shot_key`` 仅纯数字串才归一                      ``"S01-C02"``（原样）
``keyframe.py _seq``      拼接**全部**数字                       ``102``
======================  ====================================  ==================

写侧（``keyframe.end_frame_path``）用「拼接全部数字」写 ``shot_102_end.png``，
读侧（``app._shot_seq``）却按「首段数字」找 ``shot_01_end.png`` —— **keyframe 模式
下尾帧永远找不到**，``kf_end_map`` 为空 → 链式「上镜尾帧 = 下镜首帧」静默退化为无尾帧。

## 收敛口径

落盘命名统一为 ``shot_%02d``（与分镜图对齐），故语义**以落盘命名（写侧）为准**，
定为「取**第一段**数字」：``"S01-C02" → 1``、``"shot_03" → 3``、``"01" → 1``。
全项目「写侧 + 读侧」必须共用 :func:`shot_seq`，否则会再次出现「写 ``shot_102``、
读 ``shot_01``」的静默错位。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

#: 镜号里的「首段数字」——canonical：与落盘命名 ``shot_%02d`` 对齐
_FIRST_NUM_RE = re.compile(r"\d+")
#: 镜号里的**全部**数字字符——仅用于读取历史旧命名的一次性兼容回退
_ALL_DIGIT_RE = re.compile(r"\d")


def shot_seq(shot_id, fallback: Optional[int] = None) -> Optional[int]:
    """镜号 → 序号（**全项目唯一实现**，canonical 语义）。

    规则：
      - 整数原样返回（``1 → 1``）；
      - 其余取**第一段**数字：``"1" → 1``、``"01" → 1``、``"shot_03" → 3``、
        ``"shot_1" → 1``、``"S01-C02" → 1``；
      - 完全取不到数字时返回 ``fallback``（默认 ``None``）。
    """
    if isinstance(shot_id, int) and not isinstance(shot_id, bool):
        return shot_id
    m = _FIRST_NUM_RE.search(str(shot_id if shot_id is not None else ""))
    if not m:
        return fallback
    try:
        return int(m.group(0))
    except ValueError:                      # 理论上 \d+ 一定能转 int；防御性兜底
        return fallback


def norm_shot_key(key) -> str:
    """镜头**键**归一化（用于「剧本 shot_id」与「分镜/尾帧文件名」互相对齐）。

      - 纯数字串：去前导零（``"01" → "1"``、``"0" → "0"``）；
      - 含字母但含数字的串：按 :func:`shot_seq` 归一为字符串
        （``"shot_03" → "3"``、``"S01-C02" → "1"``）；
      - 完全不含数字的串：原样返回。
    """
    s = str(key).strip()
    if s.isdigit():
        return s.lstrip("0") or "0"
    n = shot_seq(s)
    return str(n) if n is not None else s


def legacy_shot_seq(shot_id) -> Optional[int]:
    """**仅用于读取历史旧命名**的一次性兼容回退：拼接**全部**数字字符。

    历史 ``keyframe._seq`` 曾用此语义写盘（``"S01-C02" → 102`` →
    ``shot_102_end.png``）。新代码一律用 :func:`shot_seq`；本函数只让**旧文件**仍能被
    读到（读到时记 warning），不参与任何写盘。整数镜号下新旧同名，调用方应据此短路。
    """
    if isinstance(shot_id, int) and not isinstance(shot_id, bool):
        return shot_id
    digits = "".join(_ALL_DIGIT_RE.findall(str(shot_id if shot_id is not None else "")))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None
