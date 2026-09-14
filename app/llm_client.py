"""
自定义 LLM API 客户端（OpenAI 兼容 chat/completions 协议）

职责：
- 配置持久化：base_url / api_key / model 写入项目根目录 llm_config.json
- 对外只暴露脱敏后的 api_key（明文永不回显）
- 统一 chat 调用、JSON 输出解析、连接测试
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 240
# 服务端瞬时不可用（限流/过载/网关错误）：同一请求内可安全重试
HTTP_TRANSIENT_CODES = (429, 500, 502, 503, 504)
HTTP_RETRY_MAX = 3            # 单次请求最大尝试次数（含首次）
HTTP_RETRY_BACKOFF = (3, 8)   # 每次重试前的等待秒数
JSON_RETRY_HINT = "上一次输出不是合法 JSON。请只输出一个合法 JSON 对象，不要包含 markdown 代码块、注释或任何解释文字。"
TRUNCATION_RETRY_HINT = (
    "上一次输出因长度上限被截断（finish_reason=length），JSON 未闭合。"
    "请务必精简描述、严格压缩每个字段的文本长度，只输出结构完整且闭合的 JSON 对象，不要复述指令、不要输出思考过程。"
)
# 截断时的 max_tokens 提升阶梯（按顺序尝试，取第一个大于当前值的档位）
DEFAULT_TOKEN_LADDER = (2048, 4096, 8192, 16384, 24576)

# 是否默认关闭「思考模式」：Nemotron / Qwen3 等混合推理模型经 NVIDIA NIM 等 OpenAI 兼容网关
# 调用时，思考内容会写进 content（而非 reasoning_content），导致 JSON 解析失败与 finish_reason=length
# 截断。这里默认注入 chat_template_kwargs.enable_thinking=false；可用配置项 disable_thinking 覆盖。
DISABLE_THINKING_DEFAULT = True
MAX_TOKENS_CEILING = 32768


class LLMError(Exception):
    """LLM 调用失败（面向用户的友好错误）"""


class LLMTruncatedError(LLMError):
    """连续重试后仍因输出截断（finish_reason=length）而无法得到完整 JSON"""

    def __init__(self, message: str, detail: dict = None):
        super().__init__(message)
        self.detail = detail or {}
        self.truncated = True


# ===================== 配置持久化 =====================

def _empty_config() -> dict:
    return {"base_url": "", "api_key": "", "model": "", "updated_at": None}


def load_config(config_path: str) -> dict:
    cfg = _empty_config()
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            for k in ("base_url", "api_key", "model", "updated_at"):
                if data.get(k) is not None:
                    cfg[k] = str(data[k])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM 配置读取失败（按未配置处理）：{e}")
    return cfg


def save_config(config_path: str, base_url: str, api_key: str = None,
                model: str = None, keep_key_if_blank: bool = True) -> dict:
    cfg = load_config(config_path)
    if base_url is not None:
        cfg["base_url"] = (base_url or "").strip()
    if model is not None:
        cfg["model"] = (model or "").strip()
    if api_key is not None and api_key.strip():
        cfg["api_key"] = api_key.strip()
    elif api_key is not None and not keep_key_if_blank:
        cfg["api_key"] = ""
    cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return cfg


def clear_config(config_path: str) -> dict:
    cfg = _empty_config()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def mask_key(key: str) -> str:
    """api_key 脱敏：仅保留首尾少量字符"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def public_view(cfg: dict) -> dict:
    """对外（前端）可见的配置视图，绝不包含 api_key 明文"""
    key = cfg.get("api_key") or ""
    base_url = cfg.get("base_url") or ""
    model = cfg.get("model") or ""
    return {
        "configured": bool(base_url and key and model),
        "has_api_key": bool(key),
        "base_url": base_url,
        "model": model,
        "api_key_masked": mask_key(key),
        "chat_url": build_chat_url(base_url) if base_url else "",
        "updated_at": cfg.get("updated_at"),
    }


# ===================== URL 规范化 =====================

def build_chat_url(base_url: str) -> str:
    """把用户填写的 base_url 规范化为 chat/completions 完整地址

    - https://api.deepseek.com                     → https://api.deepseek.com/v1/chat/completions
    - https://api.openai.com/v1                    → https://api.openai.com/v1/chat/completions
    - http://127.0.0.1:8899/v1                     → http://127.0.0.1:8899/v1/chat/completions
    - .../v1/chat/completions（已是完整地址）        → 原样返回
    """
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    path = urlparse(u).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return u
    if path in ("", "/"):
        return f"{u}/v1/chat/completions"
    return f"{u}/chat/completions"


def _is_local(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or host.endswith(".local")


# ===================== 客户端 =====================

class LLMClient:
    """OpenAI 兼容协议客户端（base_url + api_key + model 由用户在页面配置）"""

    def __init__(self, config_path: str, config: dict = None, timeout: int = DEFAULT_TIMEOUT):
        self.config_path = config_path
        self.config = config or load_config(config_path)
        self.timeout = timeout
        self.base_url = self.config.get("base_url") or ""
        self.api_key = self.config.get("api_key") or ""
        self.model = self.config.get("model") or ""
        self.chat_url = build_chat_url(self.base_url)
        self.last_error = ""
        # 思考模式开关（默认关闭，可由配置项 disable_thinking 覆盖）
        self.disable_thinking = bool(self.config.get("disable_thinking", DISABLE_THINKING_DEFAULT))
        # 最近一次 chat_json_robust 的诊断信息（attempts / finish_reason / max_tokens / truncated）
        self.last_json_meta = {}

    # ---- 状态 ----
    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def require_configured(self):
        if not self.configured:
            missing = []
            if not self.base_url:
                missing.append("base_url")
            if not self.api_key:
                missing.append("api_key")
            if not self.model:
                missing.append("model")
            raise LLMError(
                "尚未配置自定义 AI 接口（缺少：" + "、".join(missing) +
                "）。请点击右上角「AI 接口配置」填写 base_url / api_key / model 后再试。"
            )

    # ---- 底层调用 ----
    def _post(self, payload: dict, timeout: int = None, chat_url: str = None) -> dict:
        url = chat_url or self.chat_url
        session = requests.Session()
        # 本地/内网地址不走系统代理，避免 localhost 被代理拦截
        if _is_local(url):
            session.trust_env = False
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        t0 = time.time()
        attempts = max(1, int(HTTP_RETRY_MAX))
        resp = None
        last_err = None
        for idx in range(attempts):
            if idx:
                time.sleep(HTTP_RETRY_BACKOFF[min(idx - 1, len(HTTP_RETRY_BACKOFF) - 1)])
            try:
                resp = session.post(url, headers=headers, json=payload,
                                    timeout=timeout or self.timeout)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_err = e
                resp = None
                logger.warning(f"LLM 请求瞬时失败（第 {idx + 1}/{attempts} 次重试）：{e}")
                continue
            except Exception as e:  # noqa: BLE001
                raise LLMError(f"请求异常：{e}") from e
            if resp.status_code in HTTP_TRANSIENT_CODES:
                last_err = LLMError(f"接口返回 HTTP {resp.status_code}：{(resp.text or '')[:200]}")
                logger.warning(f"LLM 接口瞬时不可用（HTTP {resp.status_code}，"
                               f"第 {idx + 1}/{attempts} 次），将退避后重试")
                resp = None
                continue
            break
        latency = int((time.time() - t0) * 1000)
        if resp is None:
            raise LLMError(f"接口连续 {attempts} 次瞬时不可用（含超时/连接失败）：{last_err}")

        if resp.status_code >= 400:
            body = (resp.text or "")[:400]
            raise LLMError(f"接口返回 HTTP {resp.status_code}：{body}")
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"接口返回非 JSON 内容：{(resp.text or '')[:300]}") from e
        data["_latency_ms"] = latency
        return data

    @staticmethod
    def _extract_choice(data: dict) -> tuple:
        """返回 (content, finish_reason)，同时保留 reasoning_content 供诊断"""
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"接口返回缺少 choices 字段：{json.dumps(data, ensure_ascii=False)[:300]}")
        ch = choices[0]
        finish_reason = ch.get("finish_reason")
        msg = ch.get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # 兼容 content 为分段数组的实现
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not content:
            content = ch.get("text") or ""
        if not content:
            reasoning = str(msg.get("reasoning_content") or "")
            if reasoning:
                raise LLMError(
                    "模型未返回正文（content 为空），仅返回了思考内容（reasoning_content）。"
                    f"思考片段：{reasoning[:200]}"
                )
            raise LLMError("接口返回内容为空")
        return str(content), finish_reason

    @staticmethod
    def _extract_content(data: dict) -> str:
        return LLMClient._extract_choice(data)[0]

    @staticmethod
    def is_truncated(finish_reason) -> bool:
        """是否因长度上限被截断"""
        return str(finish_reason or "").lower() in ("length", "max_tokens", "max_output_tokens")

    def _build_payload(self, messages: list, temperature: float, max_tokens: int) -> dict:
        """构造 OpenAI 兼容请求体

        disable_thinking 为真时注入 chat_template_kwargs.enable_thinking=false，
        避免混合推理模型把思考过程写进 content（会撑爆 max_tokens 并导致 JSON 解析失败）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if getattr(self, "disable_thinking", DISABLE_THINKING_DEFAULT):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def chat(self, messages: list, temperature: float = 0.7, max_tokens: int = 4096,
             timeout: int = None) -> str:
        self.require_configured()
        payload = self._build_payload(messages, temperature, max_tokens)
        data = self._post(payload, timeout=timeout)
        return self._extract_content(data)

    def chat_ex(self, messages: list, temperature: float = 0.7, max_tokens: int = 4096,
                timeout: int = None) -> dict:
        """与 chat 相同，但额外返回 finish_reason 等诊断信息

        返回：{content, finish_reason, truncated, max_tokens, latency_ms, usage}
        """
        self.require_configured()
        payload = self._build_payload(messages, temperature, max_tokens)
        data = self._post(payload, timeout=timeout)
        content, finish_reason = self._extract_choice(data)
        return {
            "content": content,
            "finish_reason": finish_reason,
            "truncated": self.is_truncated(finish_reason),
            "max_tokens": max_tokens,
            "latency_ms": data.get("_latency_ms"),
            "usage": data.get("usage") or {},
        }

    # ---- JSON 输出 ----
    @staticmethod
    def parse_json(content: str) -> dict:
        text = (content or "").strip()
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
            if m:
                text = m.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidate = text[start:end + 1]
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)  # 去尾逗号
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        start, end = text.find("["), text.rfind("]")
        # 顶层已是对象（以 { 开头）时不再用中括号片段兜底，否则会返回内层数组，
        # 导致 _repair_truncated_json 无从发挥、调用方拿到语义错位的片段
        if start != -1 and end > start and (text.find("{") == -1 or start < text.find("{")):
            candidate = re.sub(r",\s*([}\]])", r"\1", text[start:end + 1])
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        repaired = LLMClient._repair_truncated_json(text)
        if repaired is not None:
            logger.warning("JSON 不完整，已按截断修复（补全尾部结构）后解析成功")
            return repaired
        raise LLMError(f"模型输出无法解析为 JSON：{text[:300]}")

    @staticmethod
    def _repair_truncated_json(text: str):
        """修复被截断的 JSON（模型中途停止或 max_tokens 用尽）

        从右往左回退到最近的合法结构边界，删掉尾部残缺的键值片段，
        再按未闭合的括号/引号补齐；逐个候选尝试 json.loads，全部失败返回 None。
        """
        s = text[text.find("{"):] if "{" in text else text
        if not s:
            return None
        cands = [i for i, ch in enumerate(s) if ch in "}],\""]

        def _try_fix(i: int):
            head = s[:i + 1]
            head = re.sub(r'[,{]\s*"[^"]*"\s*:\s*"?[^"]*$', "", head)  # 去掉尾部残缺键值
            head = re.sub(r"[,{]\s*$", "", head)
            stack, in_str, esc = [], False, False
            for ch in head:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch in "{[":
                    stack.append(ch)
                elif ch in "}]" and stack:
                    stack.pop()
            fixed = head + ('"' if in_str else "")
            for op in reversed(stack):
                fixed += "}" if op == "{" else "]"
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                return None

        # 两趟扫描：优先返回 dict（顶层对象语义完整，如 state_in / state_out / shots），
        # 只有找不到任何可用对象时才退化为内层 list，避免调用方拿到错位的数组片段。
        for want_dict in (True, False):
            for i in reversed(cands[-200:]):
                obj = _try_fix(i)
                if not obj:
                    continue
                if want_dict and isinstance(obj, dict):
                    return obj
                if not want_dict and isinstance(obj, list):
                    return obj
        return None

    def chat_json(self, prompt: str, system: str = None, temperature: float = 0.4,
                  max_tokens: int = 4096, retries: int = 1) -> dict:
        """要求模型输出 JSON，失败自动重试（兼容旧行为，内部委托 chat_json_robust）"""
        return self.chat_json_robust(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens,
            max_attempts=max(1, retries + 1),
        )

    def chat_json_robust(self, prompt: str, system: str = None, temperature: float = 0.4,
                         max_tokens: int = 4096, max_attempts: int = 3,
                         token_ladder=None, on_event=None) -> dict:
        """带「截断感知 → 自动提高 max_tokens → 重试」的 JSON 调用（A 项③ 核心）

        重试策略：
        - finish_reason=length（被截断）→ 追加"精简输出"提示，并把 max_tokens 提升到阶梯的下一档再试；
        - 输出无法解析为 JSON → 追加 JSON 修正提示，并把 max_tokens 提升一档再试；
        - 全部尝试失败 → 抛 LLMTruncatedError（截断导致）或 LLMError，错误信息含 finish_reason 与原因。

        诊断信息写入 self.last_json_meta。
        """
        ladder = tuple(token_ladder or DEFAULT_TOKEN_LADDER)
        cur = int(max_tokens or 4096)
        attempts = 0
        truncated = False
        last_err = None
        history = []
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _next_tokens(value: int) -> int:
            for t in ladder:
                if t > value:
                    return min(t, MAX_TOKENS_CEILING)
            return min(value * 2, MAX_TOKENS_CEILING)

        repaired_fallback = None
        for _ in range(max(1, int(max_attempts))):
            attempts += 1
            r = self.chat_ex(messages, temperature=temperature, max_tokens=cur)
            truncated = r["truncated"]
            history.append({"attempt": attempts, "max_tokens": cur,
                            "finish_reason": r["finish_reason"], "truncated": truncated,
                            "content_len": len(r["content"]), "latency_ms": r["latency_ms"]})
            if on_event:
                try:
                    on_event(history[-1])
                except Exception:  # noqa: BLE001
                    pass
            data = None
            try:
                data = self.parse_json(r["content"])
            except LLMError as e:
                last_err = e
                logger.warning(f"JSON 解析失败（第 {attempts} 次，max_tokens={cur}，"
                               f"finish_reason={r['finish_reason']}）：{e}")
                messages.append({"role": "assistant", "content": r["content"][:2000]})
                messages.append({"role": "user",
                                 "content": TRUNCATION_RETRY_HINT if truncated else JSON_RETRY_HINT})
            if data is not None:
                self.last_json_meta = {
                    "attempts": attempts, "finish_reason": r["finish_reason"],
                    "max_tokens": cur, "truncated": truncated, "history": history,
                    "accepted_partial": bool(truncated),
                }
                if not truncated:
                    return data
                nxt = _next_tokens(cur)
                if nxt > cur:
                    # 被截断但结构可修复：不静默接受部分结果，先提额重试拿完整版
                    logger.warning(f"输出被截断（finish_reason=length）但已修复出部分结构，"
                                   f"提升 max_tokens {cur}→{nxt} 重试以获取完整内容")
                    repaired_fallback = data
                    messages.append({"role": "assistant", "content": r["content"][:2000]})
                    messages.append({"role": "user", "content": TRUNCATION_RETRY_HINT})
                else:
                    logger.warning("输出被截断且 max_tokens 已到上限，接受修复后的部分结果（可能不完整）")
                    return data
            cur = _next_tokens(cur)

        if repaired_fallback is not None:
            self.last_json_meta["accepted_partial"] = True
            logger.warning("连续提额后仍未取得完整 JSON，降级返回修复后的部分结果")
            return repaired_fallback

        last = history[-1] if history else {}
        detail = {"attempts": attempts, "truncated": truncated, "history": history,
                  "max_tokens": last.get("max_tokens", max_tokens),
                  "finish_reason": last.get("finish_reason")}
        self.last_json_meta = {
            "attempts": attempts, "finish_reason": last.get("finish_reason"),
            "max_tokens": last.get("max_tokens", max_tokens), "truncated": truncated,
            "history": history,
        }
        if truncated:
            raise LLMTruncatedError(
                f"连续 {attempts} 次调用均因输出被截断（finish_reason=length，最后一次 max_tokens="
                f"{detail['max_tokens']}）而未能得到完整 JSON，建议进一步缩小单次分析规模。"
                f"最后错误：{last_err}", detail=detail)
        raise LLMError(f"连续 {attempts} 次调用均未返回合法 JSON。最后错误：{last_err}")

    # ---- 连接测试 ----
    def test_connection(self) -> dict:
        if not self.base_url:
            return {"success": False, "error": "请先填写 base_url"}
        if not self.api_key:
            return {"success": False, "error": "请先填写 api_key"}
        if not self.model:
            return {"success": False, "error": "请先填写 model"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是连接测试助手。"},
                {"role": "user", "content": "请只回复两个字：连通"},
            ],
            "temperature": 0,
            "max_tokens": 32,
            "stream": False,
        }
        try:
            data = self._post(payload, timeout=45)
            content = self._extract_content(data).strip()
            return {
                "success": True,
                "chat_url": self.chat_url,
                "model": self.model,
                "latency_ms": data.get("_latency_ms"),
                "reply": content[:80],
            }
        except LLMError as e:
            return {"success": False, "chat_url": self.chat_url, "model": self.model, "error": str(e)}


def client_from_config(config_path: str, timeout: int = DEFAULT_TIMEOUT) -> LLMClient:
    return LLMClient(config_path=config_path, timeout=timeout)
