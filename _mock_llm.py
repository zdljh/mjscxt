# -*- coding: utf-8 -*-
"""临时用的假 OpenAI 兼容服务，只服务于 _verify_agent_e2e.py 的端到端验证。

行为：
- 第一轮（带 tools、且上下文里没有 tool 结果）→ 返回两个只读工具的 tool_calls
- 第二轮（上下文里已有 tool 结果）→ 返回普通回复，结束
"""
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route('/v1/chat/completions', methods=['POST'])
def chat():
    body = request.json or {}
    msgs = body.get('messages') or []
    has_tool_result = any(m.get('role') == 'tool' for m in msgs)
    if body.get('tools') and not has_tool_result:
        return jsonify({
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "get_status", "arguments": "{}"}},
                        {"id": "call_2", "type": "function",
                         "function": {"name": "get_progress", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"total_tokens": 10},
        })
    return jsonify({
        "choices": [{
            "index": 0,
            "message": {"role": "assistant",
                        "content": "（假模型）已按工具结果汇报：托管未开启，暂无正在生产的集数。"},
            "finish_reason": "stop",
        }],
        "usage": {"total_tokens": 5},
    })


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5099, threaded=True)
