# -*- coding: utf-8 -*-
"""总控 AI 自主执行内核（function-calling agent）。

设计前提：用户要求「不手动确认，全交给总控 AI 自主判断并执行」。
因此这里**没有**任何 human-in-the-loop 弹窗，安全完全靠自动护栏保证：

1. **工具白名单**：只暴露本文件里声明的动作，删除项目 / 删角色 / 清记忆等
   破坏性能力压根不在 schema 里，模型想调也调不到。
2. **昂贵动作配额**：单轮最多 MAX_EXPENSIVE 次烧 GPU/耗时的动作（重生成、超分、
   配音、混音、导出、整集生产），超了直接拒绝并告诉模型，逼它收敛。
3. **步数与时长上限**：MAX_STEPS / MAX_TURN_SEC，防死循环。
4. **同参数冷却**：完全相同的「工具 + 参数」在 COOLDOWN_SEC 内直接复用上次结果，
   避免模型反复重试把显卡烧穿。
5. **全局急停 + 单项目互斥**：POST /api/agent/kill 一键刹车；同一项目同时只允许
   一个 agent 任务在跑。
6. **全量审计**：每次工具调用落 output/agent/audit-<date>.jsonl，事后可追责。

工具调用走「进程内直连」（Flask test_client），不依赖端口、不走网络，
避免 agent 在后台线程里自己发 HTTP 打到自己导致的端口耦合与代理问题。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime
from urllib.parse import quote

# ===================== 护栏参数 =====================

MAX_STEPS = 12             # 单轮最多几步（1 步 = 一次模型决策）
MAX_EXPENSIVE = 4          # 单轮最多几次昂贵动作
MAX_TURN_SEC = 1800        # 单轮总时长上限（30 分钟）
COOLDOWN_SEC = 120         # 同参数冷却窗口
JOB_TTL_SEC = 3600         # 任务结果保留时长
MAX_RESULT_CHARS = 1600    # 单个工具结果喂回模型的字符上限

_HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_DIR = os.path.join(os.path.dirname(_HERE), "output", "agent")

# ===================== 运行时状态 =====================

_APP = None
_LOCK = threading.Lock()
_JOBS: dict = {}
_BUSY: set = set()
_KILL = {"on": False, "reason": "", "at": 0.0}
_COOLDOWN: dict = {}


def bind_app(flask_app):
    """由 app.py 在启动时注入 Flask 实例（避免循环 import）"""
    global _APP
    _APP = flask_app


# ===================== 工具定义 =====================
#
# call(args, ctx) -> (method, path, payload)
#   ctx = {"project": str, "episode": int|None}
# risk: safe=只读 / write=改配置 / expensive=烧 GPU 或耗时

def _p(name):
    return str(name or "").strip()


def _qp(project):
    return quote(_p(project), safe="")


def _schema(props: dict, required: list = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}
_ARR_STR = {"type": "array", "items": {"type": "string"}}
_OBJ = {"type": "object"}


def _proj_prop(desc="项目名（缺省用当前项目）"):
    return {"type": "string", "description": desc}


TOOLS = [
    # ---------- 只读探针 ----------
    {
        "name": "get_status",
        "description": "查询项目托管状态：自动生产开关、当前在做的事、待验收数、异常数。参数可选，不传则用当前项目上下文。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", f"/api/autopilot/status{_qp(a.get('project') or c.get('project'))}", {}),
    },
    {
        "name": "get_progress",
        "description": "查询某个项目的生产进度（已完成集数、当前集、当前步骤、失败原因）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", f"/api/autopilot/progress/{_qp(a.get('project') or c.get('project'))}", {}),
    },
    {
        "name": "list_projects",
        "description": "列出所有项目及其关联小说，用于判断该让哪个项目跑 24 小时自动生产。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/projects", {}),
    },
    {
        "name": "list_plans",
        "description": "列出所有项目的托管计划（含是否开启托管 enabled、目标集数、章节数进度）。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/autopilot/plans", {}),
    },
    {
        "name": "get_plan",
        "description": "读取某项目的自动生产计划（目标集数、镜头数、风格、各步骤开关、超分倍率等）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", f"/api/autopilot/plan/{_qp(a.get('project') or c.get('project'))}", {}),
    },
    {
        "name": "readiness_check",
        "description": "自检「是否具备无人值守条件」：还缺哪些配置/模型/环境。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/autopilot/ready", {}),
    },
    {
        "name": "get_shots",
        "description": "读取分镜画布：镜头列表、每镜的画面描述/时长/状态/已有产物。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/storyboard/canvas/" + _qp(a.get("project") or c.get("project"))
                              + (f"?episode_no={int(a['episode'])}" if a.get("episode") else ""), {}),
    },
    {
        "name": "get_qc_summary",
        "description": "读取质检总览：各镜头通过/未通过、重试次数、主要扣分原因。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/qc/project-summary?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_deliverables",
        "description": "列出已产出的成片交付物及其验收状态。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/autopilot/deliverables?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_exports",
        "description": "列出已导出的剪映/FCPXML/SRT 等文件。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/export/list?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_characters",
        "description": "列出项目角色卡（名称/定位/服装/描述/状态）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/characters?project="
                              + quote(_p(a.get("project") or c.get("project")), safe=""), {}),
    },
    {
        "name": "list_exceptions",
        "description": "列出自动生产中的异常（失败集、卡住的步骤），判断要不要处理。",
        "parameters": _schema({}),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET", "/api/autopilot/exceptions", {}),
    },
    {
        "name": "search_memory",
        "description": "在经验记忆库里检索历史成功/失败经验，用于决定这次怎么调参。",
        "parameters": _schema({"q": {"type": "string", "description": "检索关键词"}}, ["q"]),
        "risk": "safe", "expensive": False,
        "call": lambda a, c: ("GET",
                              "/api/memory/search?q=" + quote(_p(a.get("q")), safe=""), {}),
    },

    # ---------- 配置写入 ----------
    {
        "name": "update_plan",
        "description": "修改自动生产计划。patch 只接受计划字段，例如 "
                       "episodes('all' 或 [1,2,3]) / target_shots / style / enable_upscale / "
                       "upscale_scale / enable_tts / enable_mix / step_max_retries / video_mode / "
                       "auto_accept(产出即自动验收，默认 false) / auto_revive_hours(失败集挂起多久后"
                       "自动复活重试，默认 6 小时，0=永不) / max_episode_attempts 等。",
        "parameters": _schema({"project": _proj_prop(), "patch": _OBJ},
                              ["patch"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST",
                              f"/api/autopilot/plan/{_qp(a.get('project') or c.get('project'))}",
                              dict(a.get("patch") or {})),
    },
    {
        "name": "apply_creative_settings",
        "description": "把创作设定（风格、色调、角色外形、镜头语言等）写入项目，后续生成都会带上。",
        "parameters": _schema({"project": _proj_prop(), "settings": _OBJ}, ["settings"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/ai/chat/apply",
                              {"project": a.get("project") or c.get("project"),
                               "settings": a.get("settings") or {}}),
    },
    {
        "name": "update_character",
        "description": "修改角色卡（例如换服装、改描述）。char_id 从 list_characters 拿。",
        "parameters": _schema({
            "char_id": {"type": "string", "description": "角色 id"},
            "project": _proj_prop(),
            "name": _STR, "role": _STR, "description": _STR, "outfit": _STR, "status": _STR,
        }, ["char_id"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("PUT", f"/api/characters/{quote(_p(a.get('char_id')), safe='')}",
                              {k: v for k, v in {
                                  "project": a.get("project") or c.get("project"),
                                  "name": a.get("name"), "role": a.get("role"),
                                  "description": a.get("description"),
                                  "outfit": a.get("outfit"), "status": a.get("status"),
                              }.items() if v is not None}),
    },
    {
        "name": "resolve_exception",
        "description": "处理一条生产异常（标记已解决/忽略），让托管继续往下跑。",
        "parameters": _schema({"project": _proj_prop(), "exception_id": _STR,
                               "action": {"type": "string", "description": "resolve 或 ignore"}},
                              ["exception_id"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/exceptions/resolve",
                              {"project": a.get("project") or c.get("project"),
                               "exception_id": a.get("exception_id"),
                               "action": a.get("action") or "resolve"}),
    },
    {
        "name": "review_deliverable",
        "description": "验收/打回一条成片交付物。action=approve 或 reject。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT,
                               "action": _STR, "reason": _STR}, ["episode", "action"]),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/deliverables/review",
                              {"project": a.get("project") or c.get("project"),
                               "episode": a.get("episode"), "action": a.get("action"),
                               "reason": a.get("reason") or ""}),
    },

    # ---------- 生产调度 ----------
    {
        "name": "start_production",
        "description": "启动全自动生产（24h 托管跑多集）。novel_id 可省略，会自动关联。",
        "parameters": _schema({"project": _proj_prop(), "novel_id": _STR}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autonomous/start",
                              {"project_name": a.get("project") or c.get("project"),
                               "novel_id": a.get("novel_id") or ""}),
    },
    {
        "name": "stop_production",
        "description": "停止全自动生产。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autonomous/stop",
                              {"project_name": a.get("project") or c.get("project")}),
    },
    {
        "name": "resume_production",
        "description": "恢复被暂停的全自动生产。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autonomous/resume",
                              {"project_name": a.get("project") or c.get("project")}),
    },
    {
        "name": "enable_autopilot",
        "description": "开启某项目的托管（到点自动生产下一集）。",
        "parameters": _schema({"project": _proj_prop(), "novel_id": _STR}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/enable",
                              {"project": a.get("project") or c.get("project"),
                               "novel_id": a.get("novel_id") or ""}),
    },
    {
        "name": "disable_autopilot",
        "description": "关闭某项目的托管。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/disable",
                              {"project": a.get("project") or c.get("project")}),
    },
    {
        "name": "pause_autopilot",
        "description": "暂停托管（保留进度，可 resume）。",
        "parameters": _schema({"project": _proj_prop()}),
        "risk": "write", "expensive": False,
        "call": lambda a, c: ("POST", "/api/autopilot/pause",
                              {"project": a.get("project") or c.get("project")}),
    },

    # ---------- 昂贵动作（烧 GPU / 耗时） ----------
    {
        "name": "produce_episode",
        "description": "立即完整生产指定一集（走完整流水线：剧本→资产→分镜→关键帧→视频→"
                       "成片→配音→混音→超分）。耗时长，一次只能一集。",
        "parameters": _schema({"project": _proj_prop(),
                               "episode": {"type": "integer", "description": "章节序号，默认 1"}}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/autopilot/run-once",
                              {"project": a.get("project") or c.get("project"),
                               "episode_no": int(a.get("episode") or c.get("episode") or 1)}),
    },
    {
        "name": "retry_shot",
        "description": "重生成某一镜的分镜图（不动其它镜头）。用户说「第N镜重做」时用这个。",
        "parameters": _schema({"project": _proj_prop(),
                               "shot_id": {"type": "string", "description": "镜头 id，如 3 或 shot_03"},
                               "episode": _INT}, ["shot_id"]),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/storyboard/retry-shot",
                              {"project_name": a.get("project") or c.get("project"),
                               "shot_id": str(a.get("shot_id")),
                               "episode_no": a.get("episode") or c.get("episode")}),
    },
    {
        "name": "retry_shot_video",
        "description": "重生成某一镜的视频片段（分镜图不动）。mode: reference（默认）或 keyframe。",
        "parameters": _schema({"project": _proj_prop(),
                               "shot_id": {"type": "string", "description": "镜头 id"},
                               "episode": _INT,
                               "mode": _STR}, ["shot_id"]),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/video/retry-shot",
                              {"project_name": a.get("project") or c.get("project"),
                               "shot_id": str(a.get("shot_id")),
                               "mode": a.get("mode") or "reference",
                               "episode_no": a.get("episode") or c.get("episode")}),
    },
    {
        "name": "generate_tts",
        "description": "为整集批量配音（TTS）。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/tts/generate",
                              {"project_name": a.get("project") or c.get("project"),
                               "episode": a.get("episode") or c.get("episode") or 1}),
    },
    {
        "name": "mix_audio",
        "description": "把配音与画面合成为带声音的成片。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/mix/generate",
                              {"project_name": a.get("project") or c.get("project"),
                               "episode": a.get("episode") or c.get("episode") or 1}),
    },
    {
        "name": "upscale_video",
        "description": "对成片做超分（FlashVSR）。scale 只支持 2/3/4。默认自动挑该项目最新成片，"
                       "并保留音轨。很慢，别对同一条片反复跑。",
        "parameters": _schema({"project": _proj_prop(),
                               "scale": {"type": "integer", "description": "倍率 2/3/4，默认 2"},
                               "video_path": {"type": "string", "description": "可选，指定输入视频绝对路径"}}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/upscale/video",
                              {k: v for k, v in {
                                  "project_name": a.get("project") or c.get("project"),
                                  "video_path": a.get("video_path"),
                                  "scale": int(a.get("scale") or 2),
                                  "attach_audio": True,
                              }.items() if v is not None}),
    },
    {
        "name": "export_project",
        "description": "导出交付文件（剪映草稿 / FCPXML / SRT / 帧序列）。formats 为空则全导。",
        "parameters": _schema({"project": _proj_prop(), "episode": _INT,
                               "formats": _ARR_STR}),
        "risk": "expensive", "expensive": True,
        "call": lambda a, c: ("POST", "/api/export/run",
                              {k: v for k, v in {
                                  "project_name": a.get("project") or c.get("project"),
                                  "episode_no": a.get("episode") or c.get("episode"),
                                  "formats": a.get("formats"),
                              }.items() if v is not None}),
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        },
    }
    for t in TOOLS
]

# 降级模式（模型不支持 tools 协议时）用的工具清单文本
_TOOL_INDEX = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOLS)

SYSTEM_PROMPT = """你是这部漫剧的**总控导演 AI**，有权直接操作系统里的全部生产功能。

工作方式（重要）：
- 你**不需要请示用户**，也不许问「要不要我帮你做？」。收到指令后自己判断要动哪些环节，
  直接调用工具执行，最后用中文汇报「做了什么 + 结果 + 下一步建议」。
- 信息不够时（比如没说第几集）**自己取合理默认值**（默认第 1 集 / 当前集），
  执行完在汇报里说明你取了什么默认值。
- 先查后做：不确定项目状态时，先用 get_status / get_progress / get_qc_summary 等只读工具看一眼，
  再决定动什么。只读工具不花钱不烧卡，多用没关系。
- 一次决策里可以并行调用多个**互不依赖**的只读工具。
- 昂贵动作（produce_episode / retry_shot / retry_shot_video / generate_tts / mix_audio /
  upscale_video / export_project）单轮有次数上限，超了会被系统拒绝。
  **不要为了「保险」重复调用同一个昂贵动作**——完全相同的参数在冷却期内会被直接复用缓存结果。
- 工具返回 success:false 时，先读 error 判断原因（环境没配好 / 依赖产物不存在 / 参数非法），
  能修的自己修（比如先补跑配音再混音），修不了就如实告诉用户卡在哪、缺什么。
- 绝对不要调用工具去删除任何东西；系统没有给你删除能力。

汇报风格：简短、说人话、讲结果，不复述工具返回的原始 JSON。

启动自动生产前的沟通要求（重要）：
- 当用户说「开始生产」「启动托管」「开始跑」等指令时，**不要立即调用 start_production / enable_autopilot**
- 先调用 get_status 或 get_plan 查看当前项目状态，然后向用户询问/确认以下生产风格参数：
  1. 目标集数（要生产几集？）
  2. 风格偏好（画风、色调、镜头语言，从项目配置中读取已有设定）
  3. 是否需要超分
  4. 其他特殊要求
- 只有当用户明确确认了风格参数后，才调用生产工具
- 如果项目已有配置的风格，直接引用并确认，不要重复询问
- 汇报时把确认的风格参数清晰列给用户看，让用户知道你将按什么风格生产"""


# ===================== 内部调用 =====================

def _trim(obj, limit: int = MAX_RESULT_CHARS) -> str:
    """把工具结果压成喂给模型的短文本，避免上下文爆炸"""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(obj)
    if len(s) > limit:
        s = s[:limit] + f"…（已截断，原长 {len(s)} 字符）"
    return s


def _call_api(method: str, path: str, payload: dict = None) -> dict:
    """进程内直连调用自身路由（不经过网络）"""
    if _APP is None:
        return {"ok": False, "error": "agent 内核未绑定 Flask 应用"}
    try:
        client = _APP.test_client()
        fn = getattr(client, method.lower())
        resp = fn(path) if method.upper() == "GET" else fn(path, json=payload or {})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"内部调用异常：{e}",
                "trace": traceback.format_exc()[-400:]}
    try:
        data = resp.get_json(silent=True)
    except Exception:  # noqa: BLE001
        data = None
    if data is None:
        data = {"raw": (resp.data or b"")[:400].decode("utf-8", "ignore")}
    ok = 200 <= resp.status_code < 300
    if ok and isinstance(data, dict) and data.get("success") is False:
        ok = False
    return {"ok": ok, "status": resp.status_code, "data": data}


def _canon(args: dict) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(args)


def _audit(project: str, job_id: str, name: str, args: dict, result: dict):
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        path = os.path.join(AUDIT_DIR, f"audit-{datetime.now().strftime('%Y%m%d')}.jsonl")
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "job_id": job_id, "project": project, "tool": name,
            "args": args,
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "elapsed_sec": result.get("elapsed_sec"),
            "error": (result.get("error") or "")[:300],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ===================== 工具执行（含护栏） =====================

def execute_tool(name: str, args: dict, ctx: dict, stats: dict, job_id: str = "") -> dict:
    tool = TOOL_MAP.get(name)
    if not tool:
        return {"ok": False, "error": f"未知工具：{name}（可用工具见工具清单）"}

    if _KILL["on"]:
        return {"ok": False, "blocked": True,
                "error": f"急停开关已触发（{_KILL['reason'] or '手动刹车'}），本轮后续动作全部中止"}

    if tool["expensive"] and stats.get("expensive", 0) >= MAX_EXPENSIVE:
        return {"ok": False, "blocked": True,
                "error": f"本轮昂贵动作已达上限 {MAX_EXPENSIVE} 次，拒绝执行 {name}。"
                         f"请先向用户汇报当前结果，需要继续请让用户重新下指令。"}

    key = hashlib.md5((name + "|" + _canon({**args, "__p": ctx.get("project")})).encode("utf-8")).hexdigest()
    now = time.time()
    hit = _COOLDOWN.get(key)
    # ⚠️ 只读探针不进冷却：状态类数据缓存了会让模型看到 2 分钟前的旧进度，
    # 从而做出错误判断。冷却只用于「重复执行会真花钱/烧卡」的动作。
    if hit and tool["risk"] != "safe" and (now - hit[0]) < COOLDOWN_SEC:
        left = int(COOLDOWN_SEC - (now - hit[0]))
        return {"ok": True, "cached": True, "status": hit[1].get("status"),
                "note": f"{left}s 内同参数已执行过，直接复用上次结果（防重复烧 GPU）",
                "data": hit[1].get("data")}

    try:
        method, path, payload = tool["call"](args or {}, ctx)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"参数构造失败：{e}"}

    t0 = time.time()
    res = _call_api(method, path, payload)
    res["elapsed_sec"] = round(time.time() - t0, 1)
    if tool["expensive"]:
        stats["expensive"] = stats.get("expensive", 0) + 1
    if res.get("ok") and tool["risk"] != "safe":
        _COOLDOWN[key] = (now, res)
    _audit(ctx.get("project", ""), job_id, name, args or {}, res)
    return res


# ===================== 任务执行 =====================

def _new_job(project: str, message: str) -> dict:
    jid = hashlib.md5(f"{project}|{message}|{time.time()}".encode("utf-8")).hexdigest()[:12]
    job = {
        "id": jid, "project": project, "message": message,
        "status": "running", "steps": [], "reply": "", "error": "",
        "created": time.time(), "updated": time.time(),
        "expensive_used": 0,
    }
    with _LOCK:
        _JOBS[jid] = job
    return job


def get_job(jid: str):
    with _LOCK:
        job = _JOBS.get(jid)
        return dict(job) if job else None


def _finish(job, status, reply="", error=""):
    job["status"] = status
    job["reply"] = reply
    job["error"] = error
    job["updated"] = time.time()
    with _LOCK:
        _JOBS[job["id"]] = job


def _fallback_parse_actions(text: str):
    """模型不支持 tools 协议时的降级解析：从正文里抠 JSON 动作块"""
    m = re.search(r"```(?:json)?\s*(.+?)```", text or "", re.S)
    raw = m.group(1) if m else (text or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return [], text
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return [], text
    actions = obj.get("actions") or []
    if isinstance(actions, dict):
        actions = [actions]
    out = []
    for a in actions:
        if isinstance(a, dict) and a.get("tool"):
            out.append({"name": str(a["tool"]), "args": a.get("args") or {}})
    return out, obj.get("reply") or re.sub(r"```.*?```", "", text or "", flags=re.S).strip()


def run_job(job: dict, ep: dict, history: list, timeout: int):
    """agent 主循环（在后台线程里跑）"""
    project = job["project"]
    ctx = {"project": project, "episode": None}
    stats = {"expensive": 0}
    started = time.time()
    text_mode = False

    try:
        from llm_client import LLMClient, LLMError
    except Exception as e:  # noqa: BLE001
        _finish(job, "failed", error=f"加载 LLM 客户端失败：{e}")
        return

    try:
        # config 直接由调用方传入（已配置好的 chat 模块 endpoint），config_path 不再读盘
        client = LLMClient("", config=ep, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        _finish(job, "failed", error=f"初始化模型失败：{e}")
        return

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-8:]:
        role = h.get("role")
        if role in ("user", "assistant") and h.get("content"):
            messages.append({"role": role, "content": str(h["content"])[:2000]})
    messages.append({"role": "user", "content": job["message"]})

    for step in range(MAX_STEPS):
        if _KILL["on"]:
            job["steps"].append({"tool": "_kill", "ok": False, "summary": "急停，本轮中止"})
            _finish(job, "killed", reply="已触发急停，本轮动作全部中止。")
            return
        if time.time() - started > MAX_TURN_SEC:
            job["steps"].append({"tool": "_timeout", "ok": False, "summary": "超出单轮时长上限"})
            _finish(job, "timeout", reply=f"单轮执行超过 {MAX_TURN_SEC}s，已自动停止。"
                                          f"已完成 {len(job['steps'])} 步，请查看上面的执行记录。")
            return

        # ---- 让模型决策 ----
        try:
            if text_mode:
                content = client.chat(messages, temperature=0.4, max_tokens=2048)
                calls, final_reply = _fallback_parse_actions(content)
            else:
                resp = client.chat_tools(messages, TOOL_SCHEMAS, temperature=0.3, max_tokens=2048)
                calls = [{"id": c["id"], "name": c["name"], "args": _safe_args(c["arguments"])}
                         for c in (resp.get("tool_calls") or [])]
                final_reply = resp.get("content") or ""
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if not text_mode and ("400" in msg or "tool" in msg.lower() or "unsupported" in msg.lower()):
                text_mode = True
                messages.append({
                    "role": "user",
                    "content": "（系统提示：当前模型不支持 function-calling 协议。"
                               "请改为直接输出一个 JSON 代码块，格式：\n"
                               "```json\n{\"actions\":[{\"tool\":\"工具名\",\"args\":{...}}],"
                               "\"reply\":\"给用户的话\"}\n```\n"
                               "没有要执行的动作时 actions 填空数组。"
                               f"可用工具：\n{_TOOL_INDEX}）",
                })
                continue
            _finish(job, "failed", error=f"模型调用失败：{msg[:300]}")
            return

        # ---- 没有动作 → 结束 ----
        if not calls:
            _finish(job, "done", reply=final_reply or "（模型没有给出回复）")
            return

        # ---- 执行动作 ----
        messages.append({
            "role": "assistant",
            "content": final_reply or " ",
            **({"tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                for c in calls]} if not text_mode else {}),
        })

        for c in calls:
            name, args = c["name"], c.get("args") or {}
            res = execute_tool(name, args, ctx, stats, job_id=job["id"])
            job["steps"].append({
                "tool": name, "args": args,
                "ok": bool(res.get("ok")),
                "cached": bool(res.get("cached")),
                "blocked": bool(res.get("blocked")),
                "elapsed_sec": res.get("elapsed_sec"),
                "summary": res.get("note") or (res.get("error") or "")[:200]
                or (f"ok ({res.get('elapsed_sec')}s)" if res.get("ok") else "失败"),
                "result": _trim(res.get("data")),
            })
            job["updated"] = time.time()
            tool_msg = {"role": "tool", "tool_call_id": c["id"],
                        "content": _trim({"ok": res.get("ok"), "error": res.get("error"),
                                          "note": res.get("note"), "data": res.get("data")}, 2400)}
            if text_mode:
                messages.append({"role": "user", "content":
                    f"[工具结果] {name}: {tool_msg['content']}"})
            else:
                messages.append(tool_msg)

        job["expensive_used"] = stats.get("expensive", 0)

    _finish(job, "done",
            reply=f"已完成 {len(job['steps'])} 步，但单轮步数达到上限 {MAX_STEPS}。"
                  f"如需继续请再下一条指令。")


def _safe_args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ===================== 对外入口 =====================

def start_job(message: str, project: str, ep: dict, history: list, timeout: int = 120) -> dict:
    """启动一个 agent 任务（后台线程）。同一项目同时只能跑一个。"""
    proj_key = project or "__global__"
    if _KILL["on"]:
        return {"ok": False, "error": "急停开关处于开启状态，请先关闭后再下发指令"}
    with _LOCK:
        if proj_key in _BUSY:
            return {"ok": False, "error": "该项目已有一个 AI 任务在执行，请等它结束"}
        _BUSY.add(proj_key)

    job = _new_job(project, message)
    job["steps"] = []

    def _runner():
        try:
            run_job(job, ep, history, timeout)
        except Exception as e:  # noqa: BLE001
            _finish(job, "failed", error=f"内核异常：{e}")
        finally:
            with _LOCK:
                _BUSY.discard(proj_key)

    threading.Thread(target=_runner, daemon=True).start()
    return {"ok": True, "job_id": job["id"], "project": project}


def set_kill(on: bool, reason: str = ""):
    _KILL["on"] = bool(on)
    _KILL["reason"] = reason
    _KILL["at"] = time.time()
    return dict(_KILL)


def kill_state():
    return dict(_KILL)


def tool_index():
    return [{"name": t["name"], "description": t["description"],
             "risk": t["risk"], "expensive": bool(t["expensive"])} for t in TOOLS]


def cleanup_jobs():
    now = time.time()
    with _LOCK:
        for jid in [k for k, v in _JOBS.items() if now - v.get("updated", now) > JOB_TTL_SEC]:
            _JOBS.pop(jid, None)
