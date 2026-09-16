# -*- coding: utf-8 -*-
"""修复 DEFAULT_AUDIO_PROMPT 中因 shell 转义丢失的换行符。"""
import io
import re

P = "qc_client.py"
s = io.open(P, encoding="utf-8").read()

start = s.index("DEFAULT_AUDIO_PROMPT = (")
end = s.index("DEFAULT_VIDEO_PROMPT = (")

NEW = (
    'DEFAULT_AUDIO_PROMPT = (\n'
    '    "你是漫剧配音质检员。下面两张图不是画面，而是同一段配音音频的**频谱图**与**波形图**：\\n"\n'
    '    "  第 1 张：频谱图（横轴时间，纵轴频率，颜色亮度=能量强弱）\\n"\n'
    '    "  第 2 张：波形图（横轴时间，纵轴振幅）\\n"\n'
    '    "请据此判断这段配音是否达到可直接使用的标准：\\n"\n'
    '    "1) 人声能量分布是否正常（人声主要能量集中在数百 Hz 至数千 Hz 的中频段）；\\n"\n'
    '    "2) 波形是否存在长时间「平坦零线」（说明整段无声、漏配音或合成失败）；\\n"\n'
    '    "3) 波形上下是否被削成平直横线（说明增益过大导致爆音失真）；\\n"\n'
    '    "4) 是否存在异常高频噪声、周期性爆破或明显断续（说明音频损坏或拼接异常）。\\n"\n'
    '    "配音文本参考：{line_text}\\n"\n'
    '    "请只输出一个 JSON 对象，不要任何解释文字，格式：\\n"\n'
    '    \'{"score": 0-100 的整数, "pass": true 或 false, "reason": "一句话结论", "issues": ["具体问题1", "具体问题2"]}\'\n'
    ')\n'
    '\n'
)

s = s[:start] + NEW + s[end:]
io.open(P, "w", encoding="utf-8", newline="").write(s)
print("DEFAULT_AUDIO_PROMPT 已修复")

PY = "C:/Users/liujianghua/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
