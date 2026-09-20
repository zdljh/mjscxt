"""
AI 对话（创作总控）：多轮对话敲定漫剧创作设定，并把确认结果结构化落盘为项目配置

- 会话历史：内存中按请求读写 + 落盘 output/ai_chat/chat_history.json
- 创作设定：output/ai_chat/project_settings.json（按项目名归档，含 active 标记）
- 模型调用：「AI 设置 · 对话总控模型」的独立 base_url / api_key / model（ai_config.json → modules.chat）
- 与文本分析 / 质检模型完全独立：对话链路只读 chat 模块，不受另两个模块影响
"""
from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# =====================================================================
# 并发保护（审计 S8）
# ---------------------------------------------------------------------
# 会话历史/项目设定都是「load → 改内存 → save」的读改写，此前**零锁**。
# 典型症状：AI 对话连发两条消息，两条都基于同一份旧历史 → 后写的把前一条覆盖掉
# （用户看到「我发的消息只留下最后一条」）；并且 `_write_json` 曾用固定 `.tmp` 名，
# 并发写者互相截断对方写了一半的临时文件 → `os.replace` 发布出损坏 JSON。
# =====================================================================

_CHAT_LOCK = threading.RLock()


def _locked(fn):
    """把读改写放回同一把可重入锁的临界区（函数级装饰，签名/文档不变）"""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        with _CHAT_LOCK:
            return fn(*args, **kwargs)
    return _wrapper

HISTORY_MAX_MESSAGES = 40      # 会话历史最多保留的消息条数（超出丢弃最早）
CONTEXT_MESSAGES = 20          # 每次请求送入模型的最大历史消息条数
MAX_CHARS_PER_MESSAGE = 4000   # 单条消息送入模型时的截断长度

# 创作设定字段（顺序即前端展示顺序）；options 为 AI 可主动给出的候选项
SETTING_FIELDS = [
    {"key": "title", "label": "剧名 / 项目名", "hint": "整部漫剧的名称", "options": []},
    {"key": "genre", "label": "题材", "hint": "故事类型", "options": ["古风仙侠", "都市悬疑", "科幻未来", "校园青春", "玄幻热血", "民国传奇", "职场逆袭"]},
    {"key": "style", "label": "风格基调", "hint": "整体叙事与氛围基调", "options": ["爽感逆袭", "温情治愈", "暗黑悬疑", "轻松搞笑", "热血燃向", "虐心催泪"]},
    {"key": "art_style", "label": "画风", "hint": "画面美术风格", "options": ["3D 动漫渲染", "日式赛璐璐", "国风水墨", "写实电影感", "厚涂插画", "美漫硬朗线条"]},
    {"key": "era_world", "label": "世界观 / 时代背景", "hint": "故事发生的世界与年代", "options": ["古代架空", "现代都市", "近未来赛博", "末世废土", "仙侠三界"]},
    {"key": "tone", "label": "情绪基调", "hint": "情绪色彩", "options": ["热血", "温情", "紧张悬疑", "搞笑", "压抑", "明快"]},
    {"key": "audience", "label": "目标受众", "hint": "主要面向的人群", "options": ["男性向", "女性向", "全年龄", "青少年", "成年向"]},
    {"key": "aspect_ratio", "label": "画面比例", "hint": "成片画幅", "options": ["9:16 竖屏", "16:9 横屏", "1:1 方形"]},
    {"key": "episode_duration", "label": "单集时长", "hint": "每集大致时长", "options": ["60 秒", "90 秒", "2 分钟", "3 分钟"]},
    {"key": "shots_per_episode", "label": "单集镜头数", "hint": "每集分镜数量", "options": ["8 个", "10 个", "12 个", "16 个"]},
    {"key": "pacing", "label": "分镜节奏", "hint": "镜头切换与叙事节奏", "options": ["快节奏卡点", "平缓叙事", "张弛有度"]},
    {"key": "color_palette", "label": "色彩倾向", "hint": "主色调倾向", "options": ["高饱和明快", "低饱和冷调", "暖色调复古", "黑白+点缀色"]},
    {"key": "characters", "label": "主要角色设定", "hint": "角色名 + 身份 + 外形 + 性格", "options": []},
    {"key": "forbidden", "label": "规避项 / 禁忌", "hint": "需要规避的内容或元素", "options": []},
    {"key": "extra_notes", "label": "补充说明", "hint": "其它创作要求", "options": []},
]

FIELD_LABELS = {f["key"]: f["label"] for f in SETTING_FIELDS}
LIST_FIELDS = {"characters", "forbidden"}
MAX_VALUE_CHARS = 240          # 单个字符串字段最长长度
MAX_LIST_ITEMS = 12            # 列表字段最多条目


def fields_meta() -> list:
    """给前端的字段元信息（含候选项）"""
    return [dict(f) for f in SETTING_FIELDS]


# ===================== 时间 / 文件 =====================

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _read_json(path: str, default: dict) -> dict:
    if not path or not os.path.isfile(path):
        return default
    # ⚠️ Windows 上 `_atomic_replace` 换目录项的瞬间，另一线程 open() 会抛
    #    PermissionError（暂时拿不到 ≠ 文件坏了）。旧实现直接返回默认值 →
    #    表现为「聊天记录突然清空」。这里退避重试。
    err = None
    for i in range(6):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or default
        except PermissionError as e:
            err = e
            time.sleep(0.02 * (i + 1))
        except Exception as e:  # noqa: BLE001
            err = e
            break
    logger.warning(f"读取 {os.path.basename(path)} 失败（按默认值处理）：{err}")
    return default


def _atomic_replace(tmp: str, path: str) -> None:
    """`os.replace` + Windows 共享冲突退避重试。

    ⚠️ Windows 上若目标文件正被另一个线程/进程打开读取（例如 `load_index` 刚读完、
    句柄尚未释放），`os.replace` 会抛 `PermissionError: [WinError 5] 拒绝访问`。
    这属于「暂时拿不到」而不是「文件坏了」：直接失败会让新建项目/保存设置偶发 500
    （实测并发读写时必现）。退避重试即可。
    """
    last = None
    for i in range(8):
        try:
            _atomic_replace(tmp, path)
            return
        except PermissionError as e:        # WinError 5 / 32：目标被占用
            last = e
            time.sleep(0.02 * (i + 1))
    raise last


def _write_json(path: str, data: dict) -> None:
    _ensure_dir(path)
    # ⚠️ 临时名必须每次唯一（固定 `.tmp` 会被并发写者互相截断，见文件头并发注释）
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.{os.urandom(3).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


# ===================== 会话历史 =====================

def default_history() -> dict:
    return {"version": 2, "messages": [], "projects": {}, "drafts": {},
            "active_project": "", "updated_at": None}


def _clean_messages(msgs) -> list:
    if not isinstance(msgs, list):
        return []
    return [m for m in msgs if isinstance(m, dict) and m.get("role") in ("user", "assistant")]


def project_messages(history: dict, project: str = "") -> list:
    """取某项目的对话消息（按项目隔离；未指定项目时取全局，兼容旧调用）

    为什么必须按项目隔离：之前所有项目共用一份 messages，总控 AI 在为一个项目
    定风格时会看到别的项目的历史，实测出现「您说的是《剑冢》，但之前讨论的是
    《剑心初醒》，请问是替换还是并行项目」这类串台反问，直接影响设定质量。
    """
    key = history_project_key(project) if (project or "").strip() else ""
    if key:
        proj = (history.get("projects") or {}).get(key)
        if isinstance(proj, dict):
            return _clean_messages(proj.get("messages"))
        return []
    return _clean_messages(history.get("messages"))


def history_project_key(project: str) -> str:
    """历史/草稿的存储键：与直接落盘的 JSON 结构对齐（不做 safe_key 的大小写折叠）"""
    return canonical_project_key(project)


def _project_bucket(history: dict, project: str) -> dict:
    key = canonical_project_key(project) or "default"
    projects = history.setdefault("projects", {})
    bucket = projects.get(key)
    if not isinstance(bucket, dict):
        bucket = {"messages": []}
        projects[key] = bucket
    return bucket


def load_history(path: str) -> dict:
    data = _read_json(path, default_history())
    out = default_history()
    out["messages"] = _clean_messages(data.get("messages"))
    # 按项目的历史（v2 结构）
    raw_projects = data.get("projects")
    if isinstance(raw_projects, dict):
        for k, v in raw_projects.items():
            if isinstance(v, dict):
                out["projects"][str(k)] = {"messages": _clean_messages(v.get("messages"))}
    # 旧数据迁移：v1 只有一份全局 messages，归到当时活跃的项目，避免历史丢失
    if not out["projects"] and out["messages"]:
        legacy_key = _legacy_canonical_key(data)
        out["projects"][legacy_key] = {"messages": list(out["messages"])}
        logger.info("AI 对话历史已迁移为按项目存储：%s（%d 条）",
                    legacy_key, len(out["messages"]))
    if isinstance(data.get("drafts"), dict):
        out["drafts"] = {}
        for k, v in data["drafts"].items():
            if isinstance(v, dict):
                # 草稿同样做旧键归一，避免与生效设定失配
                out["drafts"][canonical_project_key(k)] = v
    out["active_project"] = str(data.get("active_project") or "")
    out["updated_at"] = data.get("updated_at")
    return out


def _legacy_canonical_key(data: dict) -> str:
    """旧版单份历史归属的项目键：优先活跃项目，其次 default"""
    return canonical_project_key(str(data.get("active_project") or "")) or "default"


@_locked
def save_history(path: str, history: dict) -> dict:
    history["messages"] = _clean_messages(history.get("messages"))[-HISTORY_MAX_MESSAGES:]
    for k, v in (history.get("projects") or {}).items():
        if isinstance(v, dict):
            v["messages"] = _clean_messages(v.get("messages"))[-HISTORY_MAX_MESSAGES:]
    history["updated_at"] = _now()
    _write_json(path, history)
    return history


def append_message(history: dict, role: str, content: str, project: str = "") -> dict:
    """追加一条消息。指定 project 时写入该项目独立的历史（避免跨项目串台）"""
    msg = {"role": role, "content": str(content or ""), "time": _now()}
    if (project or "").strip():
        _project_bucket(history, project)["messages"].append(msg)
    else:
        history.setdefault("messages", []).append(msg)
    return history


def drop_last_message(history: dict, project: str = "") -> dict:
    """回滚最后一条消息（模型未配置 / 调用失败时避免脏历史）"""
    if (project or "").strip():
        msgs = _project_bucket(history, project)["messages"]
    else:
        msgs = history.setdefault("messages", [])
    if msgs:
        msgs.pop()
    return history


@_locked
def clear_history(path: str, keep_settings: bool = True, project: str = "") -> dict:
    """清空会话（指定 project 时只清该项目的历史，不动别的项目）"""
    history = load_history(path)
    if (project or "").strip():
        key = canonical_project_key(project)
        history.setdefault("projects", {}).pop(key, None)
        for k in project_key_candidates(project):
            if k != key:
                (history.get("projects") or {}).pop(k, None)
    else:
        history["messages"] = []
        history["projects"] = {}
    if not keep_settings:
        history["drafts"] = {}
        history["active_project"] = ""
    return save_history(path, history)


def clear_draft(history: dict, project: str) -> dict:
    drafts = history.setdefault("drafts", {})
    for k in project_key_candidates(project):
        drafts.pop(k, None)
    return history


# ===================== 创作设定（草稿 / 生效） =====================

def canonical_project_key(project_name: str) -> str:
    """项目键（规范规则）：与项目注册表 project_store.safe_key 完全一致

    必须与「产物目录键」一致。AI 创作设定、对话草稿、托管生产计划都以项目为键，
    两套规则一旦不同就会出现静默失配：项目名含《》书名号、空格或「·」时，
    设定被写到 A 键、而查询按 B 键去找，表现为「明明谈好了风格，一键设定却报没有设定」。
    """
    cleaned = re.sub(r"[《》〈〉【】「」『』]", "", str(project_name or "")).strip()
    key = "".join(c if c.isalnum() or c in "_-" else "_" for c in cleaned)
    return (key.strip("_") or "project")[:60]


def legacy_project_key(project_name: str) -> str:
    """旧规则（仅替换文件系统非法字符，保留空格与《》等）

    仅用于兼容读取历史落盘数据；新写入一律使用 canonical_project_key。
    """
    name = (project_name or "").strip() or "default"
    return re.sub(r"[\\/:*?\"<>|]+", "_", name)[:60] or "default"


def project_key(project_name: str) -> str:
    """项目键（写入用）"""
    return canonical_project_key(project_name)


def project_key_candidates(project_name: str) -> list:
    """读取用候选键（去重保序）：新规则 → 旧规则 → 原样名

    读取必须宽松：历史数据可能按旧规则落盘，宽松匹配才能既修好新数据又不丢老配置。
    """
    out = []
    for k in (canonical_project_key(project_name),
              legacy_project_key(project_name),
              str(project_name or "").strip()[:60]):
        if k and k not in out:
            out.append(k)
    return out


def normalize_settings(raw: dict) -> dict:
    """只保留已知字段，做长度与类型清洗，全部转成可 JSON 落盘的值"""
    out = {}
    if not isinstance(raw, dict):
        return out
    for f in SETTING_FIELDS:
        k = f["key"]
        if k not in raw:
            continue
        v = raw[k]
        if v in (None, "", [], {}):
            continue
        if k in LIST_FIELDS:
            if isinstance(v, str):
                items = [s.strip() for s in re.split(r"[\n;；,，]+", v) if s.strip()]
            elif isinstance(v, list):
                items = []
                for it in v:
                    if isinstance(it, dict):
                        txt = "；".join(f"{kk}：{vv}" for kk, vv in it.items() if vv)
                    else:
                        txt = str(it)
                    if txt.strip():
                        items.append(txt.strip()[:MAX_VALUE_CHARS])
            else:
                items = [str(v).strip()]
            if items:
                out[k] = items[:MAX_LIST_ITEMS]
        else:
            if isinstance(v, (dict, list)):
                txt = json.dumps(v, ensure_ascii=False)
            else:
                txt = str(v)
            txt = txt.strip()[:MAX_VALUE_CHARS]
            if txt:
                out[k] = txt
    return out


def merge_settings(base: dict, patch: dict) -> dict:
    """合并设定：字符串字段后值覆盖前值；列表字段按去重追加"""
    merged = dict(base or {})
    for k, v in normalize_settings(patch).items():
        if k in LIST_FIELDS:
            old = list(merged.get(k) or [])
            for item in v:
                if item not in old:
                    old.append(item)
            merged[k] = old[:MAX_LIST_ITEMS]
        else:
            merged[k] = v
    return merged


def get_draft(history: dict, project_name: str) -> dict:
    """取草稿：按候选键宽松查找，兼容历史旧规则键"""
    drafts = history.get("drafts") or {}
    for k in project_key_candidates(project_name):
        rec = drafts.get(k)
        if rec:
            return normalize_settings(rec)
    return {}


def set_draft(history: dict, project_name: str, settings: dict) -> dict:
    """写草稿：按规范键写入，并清掉同项目的旧规则键（避免出现两份草稿）"""
    drafts = history.setdefault("drafts", {})
    key = project_key(project_name)
    drafts[key] = normalize_settings(settings)
    for k in project_key_candidates(project_name):
        if k != key:
            drafts.pop(k, None)
    return history


def load_settings_file(path: str) -> dict:
    data = _read_json(path, {"version": 1, "settings": {}})
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    return data


@_locked
def save_project_settings(path: str, project_name: str, settings: dict) -> dict:
    """把确认后的创作设定落盘为项目配置（同项目覆盖更新，保留其它项目）"""
    data = load_settings_file(path)
    key = project_key(project_name)
    clean = normalize_settings(settings)
    data["settings"][key] = {
        "project_name": (project_name or "").strip() or "default",
        "settings": clean,
        "updated_at": _now(),
    }
    # 迁移：同名项目若曾按旧规则落盘，合并过去并删除旧键，避免出现两份互相打架的设定
    for old in project_key_candidates(project_name):
        if old == key:
            continue
        legacy = data["settings"].pop(old, None)
        if isinstance(legacy, dict) and (legacy.get("settings") or {}):
            merged = {**normalize_settings(legacy.get("settings")), **clean}
            data["settings"][key]["settings"] = merged
    data["active_project"] = key
    data["updated_at"] = _now()
    _write_json(path, data)
    return data


def active_settings(path: str, project_name: str = "") -> dict:
    """读取当前生效的创作设定：指定项目优先，否则取最近应用的项目

    项目查找按候选键宽松匹配，兼容历史旧规则落盘的键。
    """
    data = load_settings_file(path)
    items = data.get("settings") or {}
    keys = (project_key_candidates(project_name) if (project_name or "").strip()
            else [data.get("active_project") or ""])
    key, rec = "", None
    for k in keys:
        if k and isinstance(items.get(k), dict):
            key, rec = k, items[k]
            break
    if not isinstance(rec, dict):
        return {"project_name": "", "settings": {}, "active": False,
                "settings_file": os.path.abspath(path), "updated_at": data.get("updated_at")}
    return {
        "project_name": rec.get("project_name") or key,
        "settings": normalize_settings(rec.get("settings") or {}),
        "active": True,
        "settings_file": os.path.abspath(path),
        "updated_at": rec.get("updated_at"),
    }


def style_brief(settings: dict) -> str:
    """把生效设定压成一段风格纲要，供剧本生成 / 提示词 / 分镜链路追加引用"""
    s = normalize_settings(settings or {})
    if not s:
        return ""
    tail = {
        "genre": "题材", "style": "风格基调", "art_style": "画风", "era_world": "世界观",
        "tone": "情绪基调", "audience": "目标受众", "aspect_ratio": "画面比例",
        "episode_duration": "单集时长", "shots_per_episode": "单集镜头数",
        "pacing": "分镜节奏", "color_palette": "色彩倾向",
    }
    parts = []
    for k, label in tail.items():
        if s.get(k):
            parts.append(f"{label}：{s[k]}")
    for k, label in (("characters", "主要角色"), ("forbidden", "规避项"), ("extra_notes", "补充要求")):
        if s.get(k):
            val = s[k]
            parts.append(f"{label}：" + ("、".join(val) if isinstance(val, list) else str(val)))
    if not parts:
        return ""
    title = f"《{s['title']}》" if s.get("title") else ""
    return f"创作设定{title}—" + "；".join(parts)


def settings_view(path: str, project_name: str = "") -> dict:
    """生效设定的展示视图（供界面「当前已生效设定」区）"""
    active = active_settings(path, project_name)
    s = active.get("settings") or {}
    rows = []
    for f in SETTING_FIELDS:
        v = s.get(f["key"])
        if v in (None, "", []):
            continue
        rows.append({"key": f["key"], "label": f["label"],
                     "value": v, "display": "、".join(v) if isinstance(v, list) else str(v)})
    return {
        "active": active.get("active"),
        "project_name": active.get("project_name"),
        "settings": s,
        "rows": rows,
        "filled": len(rows),
        "total": len(SETTING_FIELDS),
        "missing": [f["label"] for f in SETTING_FIELDS if s.get(f["key"]) in (None, "", [])],
        "style_brief": style_brief(s),
        "settings_file": active.get("settings_file"),
        "updated_at": active.get("updated_at"),
    }


# ===================== 模型调用（对话总控） =====================

def _field_spec_text() -> str:
    lines = []
    for f in SETTING_FIELDS:
        opt = f"（候选：{' / '.join(f['options'])}）" if f["options"] else ""
        lines.append(f"- {f['key']}｜{f['label']}：{f['hint']}{opt}")
    return "\n".join(lines)


SYSTEM_PROMPT = """你是漫剧（AI 动态漫画短剧）创作总控导演，负责通过对话帮用户敲定整部作品的创作设定。

【工作方式】
1. 每轮聚焦 1~2 个尚未确定的设定项，用简洁中文推进，不要一次抛出所有问题。
2. 主动给出 2~4 个候选方案供用户选择（可带一句话理由），用户也能自由输入其它答案。
3. 用户给出模糊回答时，先复述你的理解再确认；用户明确否定就换方向重提案。
4. 保持对话连贯：结合已确认的设定给出专业建议，语气简洁、专业、不啰嗦。
5. 当主要设定（题材 / 风格 / 画风 / 世界观 / 角色 / 分镜节奏）基本齐全时，提醒用户可以点击「应用设定」落盘生效。

【可敲定的设定项】
{fields}

【每轮回复格式】
先写 1~3 句自然语言（说明当前进度、候选方案或确认结果），然后在最末尾追加一个 JSON 代码块，只包含本轮新确认或修改的字段（键用上面英文 key，未变更的字段不要出现；没有新确认内容时输出空对象 {}）：
```json
{"genre": "都市悬疑", "art_style": "写实电影感"}
```
JSON 必须是合法 JSON，不要写注释。

【当前已确认的创作设定（草稿）】
{draft}
"""


def build_messages(history: dict, draft: dict, project_name: str = "") -> list:
    draft_txt = json.dumps(normalize_settings(draft) or {}, ensure_ascii=False, indent=2) if draft else "（暂无）"
    system = SYSTEM_PROMPT.replace("{fields}", _field_spec_text()).replace("{draft}", draft_txt)
    messages = [{"role": "system", "content": system}]
    for m in project_messages(history, project_name)[-CONTEXT_MESSAGES:]:
        messages.append({"role": m.get("role"), "content": str(m.get("content") or "")[:MAX_CHARS_PER_MESSAGE]})
    return messages


def extract_settings(reply: str) -> dict:
    """从模型回复中抽取结构化设定（取最后一个 JSON 代码块 / JSON 对象）"""
    text = (reply or "").strip()
    candidates = []
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.S):
        candidates.append(m.group(1).strip())
    start, end = text.rfind("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in reversed(candidates):
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", cand))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            known = normalize_settings(data)
            if known or not data:
                return known
    return {}


def strip_json_block(reply: str) -> str:
    """去掉回复末尾用于结构化的 JSON 代码块，避免在聊天气泡里重复展示"""
    text = re.sub(r"```(?:json)?\s*.+?```\s*$", "", (reply or "").strip(), flags=re.S).strip()
    return text or (reply or "").strip()
