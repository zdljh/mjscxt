# -*- coding: utf-8 -*-
"""密钥保管中心（P0-3 安全加固）

目标
----
1. **仓库零明文密钥**：api_key 不再以明文写入 ai_config.json / llm_config.json / qc_config.json；
2. **多层取值优先级**（高 → 低）：
   ① 环境变量（.env / 系统环境）—— 推荐的生产与团队协作方式
   ② 本地加密文件（output/.secrets.enc）—— 兜底，供 Web UI 保存密钥时使用
   ③ 遗留明文 json（读取时自动迁移，迁移后清除明文）
3. **对外只回显脱敏值**：mask_key 逻辑保持与既有实现一致，前端体验不变。

加密方案
--------
- Fernet 对称加密（cryptography 库）；
- 主密钥（SECRET_KEY）来源：环境变量 MJSCXT_SECRET_KEY > 本地 .secret_key 文件（自动生成，已 gitignore）；
- 加密文件格式：{"version": 1, "secrets": {"<namespace>": {"api_key": "<ciphertext>"}}}
- 若 cryptography 未安装，降级为「仅环境变量」模式并给出明确日志告警（不阻断启动）。

命名空间约定
------------
- "llm"                → 旧版单模型配置（llm_config.json）
- "ai.text" / "ai.qc" / "ai.chat" → AI 设置三模块
- "qc"                 → 质检配置（qc_client）
"""
from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
from typing import Optional

logger = logging.getLogger(__name__)

# ===================== 主密钥解析 =====================

_CRYPTO_AVAILABLE = True
try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # noqa: BLE001  cryptography 未安装
    _CRYPTO_AVAILABLE = False
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

SECRET_KEY_ENV = "MJSCXT_SECRET_KEY"
SECRET_KEY_FILE = ".secret_key"
SECRETS_FILE = "secrets.enc"

# 环境变量兜底映射：namespace.key → 环境变量名
# 例：MJSCXT_API_KEY_TEXT / MJSCXT_API_KEY_QC / MJSCXT_API_KEY_CHAT / MJSCXT_API_KEY
ENV_KEY_MAP = {
    "ai.text": "MJSCXT_API_KEY_TEXT",
    "ai.qc": "MJSCXT_API_KEY_QC",
    "ai.chat": "MJSCXT_API_KEY_CHAT",
    "llm": "MJSCXT_API_KEY",
    "qc": "MJSCXT_API_KEY_QC",
}

_ENV_BASE_URL_MAP = {
    "ai.text": "MJSCXT_BASE_URL_TEXT",
    "ai.qc": "MJSCXT_BASE_URL_QC",
    "ai.chat": "MJSCXT_BASE_URL_CHAT",
    "llm": "MJSCXT_BASE_URL",
    "qc": "MJSCXT_BASE_URL_QC",
}

_ENV_MODEL_MAP = {
    "ai.text": "MJSCXT_MODEL_TEXT",
    "ai.qc": "MJSCXT_MODEL_QC",
    "ai.chat": "MJSCXT_MODEL_CHAT",
    "llm": "MJSCXT_MODEL",
    "qc": "MJSCXT_MODEL_QC",
}


def _secret_key_path(root_dir: str) -> str:
    return os.path.join(root_dir, SECRET_KEY_FILE)


def _secrets_path(root_dir: str) -> str:
    return os.path.join(root_dir, "output", SECRETS_FILE)


def get_or_create_secret_key(root_dir: str) -> Optional[bytes]:
    """取主密钥：环境变量优先，其次本地文件（不存在则生成）。返回 Fernet 可用的 key bytes。"""
    if not _CRYPTO_AVAILABLE:
        return None
    env_key = (os.getenv(SECRET_KEY_ENV) or "").strip()
    if env_key:
        return env_key.encode("utf-8") if isinstance(env_key, str) else env_key
    path = _secret_key_path(root_dir)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key.encode("utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"主密钥文件读取失败，将重新生成：{e}")
    # 生成新密钥并落盘（文件本身已 gitignore）
    try:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except Exception:  # noqa: BLE001  Windows 上可能不支持
            pass
        logger.info(f"已生成新的本地主密钥：{path}（请勿提交到版本库）")
        return key
    except Exception as e:  # noqa: BLE001
        logger.warning(f"主密钥生成失败，密钥将仅从环境变量读取：{e}")
        return None


class SecretStore:
    """密钥保管器（按 root_dir 实例化；加密文件位于 output/secrets.enc）"""

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self._fernet = None
        if _CRYPTO_AVAILABLE:
            key = get_or_create_secret_key(root_dir)
            if key:
                try:
                    self._fernet = Fernet(key)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"主密钥格式非法，加密功能降级：{e}")

    # ---------- 加密文件读写 ----------

    def _read_store(self) -> dict:
        path = _secrets_path(self.root_dir)
        if not os.path.isfile(path):
            return {"version": 1, "secrets": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            if not isinstance(data.get("secrets"), dict):
                data["secrets"] = {}
            return data
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥库读取失败（按空处理）：{e}")
            return {"version": 1, "secrets": {}}

    def _write_store(self, data: dict) -> None:
        path = _secrets_path(self.root_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:  # noqa: BLE001
            pass

    def _encrypt(self, plain: str) -> Optional[str]:
        if not self._fernet:
            return None
        try:
            return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥加密失败：{e}")
            return None

    def _decrypt(self, token: str) -> Optional[str]:
        if not self._fernet:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            logger.warning("密钥解密失败（主密钥可能已更换），该密钥需重新配置")
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥解密异常：{e}")
            return None

    # ---------- 对外 API ----------

    @property
    def encryption_available(self) -> bool:
        return self._fernet is not None

    def get_api_key(self, namespace: str) -> str:
        """按 环境变量 → 加密文件 的顺序取明文密钥；都没有则返回空串。"""
        env_name = ENV_KEY_MAP.get(namespace)
        if env_name:
            val = (os.getenv(env_name) or "").strip()
            if val:
                return val
        data = self._read_store()
        entry = (data.get("secrets") or {}).get(namespace) or {}
        token = entry.get("api_key")
        if token:
            plain = self._decrypt(token)
            if plain:
                return plain
        return ""

    def set_api_key(self, namespace: str, plain: str) -> bool:
        """加密写入密钥。返回是否成功（加密不可用时返回 False，调用方应提示改用环境变量）。"""
        if not plain:
            return self.clear_api_key(namespace)
        token = self._encrypt(plain)
        if not token:
            return False
        data = self._read_store()
        data.setdefault("secrets", {}).setdefault(namespace, {})["api_key"] = token
        try:
            self._write_store(data)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥落盘失败：{e}")
            return False

    def clear_api_key(self, namespace: str) -> bool:
        data = self._read_store()
        entry = (data.get("secrets") or {}).get(namespace)
        if not entry:
            return True
        entry.pop("api_key", None)
        try:
            self._write_store(data)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"密钥清除失败：{e}")
            return False

    # ---------- 非密钥字段的环境变量覆盖 ----------

    @staticmethod
    def env_base_url(namespace: str) -> str:
        name = _ENV_BASE_URL_MAP.get(namespace)
        return (os.getenv(name) or "").strip() if name else ""

    @staticmethod
    def env_model(namespace: str) -> str:
        name = _ENV_MODEL_MAP.get(namespace)
        return (os.getenv(name) or "").strip() if name else ""


# ===================== 全局单例 =====================

_STORE: Optional[SecretStore] = None
_STORE_ROOT: Optional[str] = None


def get_store(root_dir: str) -> SecretStore:
    """按根目录取（缓存）密钥保管器"""
    global _STORE, _STORE_ROOT
    root_dir = os.path.abspath(root_dir)
    if _STORE is None or _STORE_ROOT != root_dir:
        _STORE = SecretStore(root_dir)
        _STORE_ROOT = root_dir
    return _STORE


def mask_key(key: str) -> str:
    """api_key 脱敏：仅保留首尾少量字符（与 llm_client.mask_key 保持一致）"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def scrub_plaintext_key(config_path: str, namespace: str, root_dir: str) -> bool:
    """遗留明文迁移：把 json 里的 api_key 搬进加密库，并把 json 中该字段清空。

    返回是否发生了迁移（True = 本次把明文换成了加密存储）。
    """
    if not config_path or not os.path.isfile(config_path):
        return False
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"遗留配置读取失败，跳过迁移：{e}")
        return False

    plain = ""
    # 兼容两种结构：单模块 {"api_key": ...} 与多模块 {"modules": {"text": {"api_key": ...}}}
    if isinstance(data.get("api_key"), str) and data["api_key"].strip():
        plain = data["api_key"].strip()
        data["api_key"] = ""
    modules = data.get("modules")
    if isinstance(modules, dict):
        for m, cfg in modules.items():
            if isinstance(cfg, dict) and isinstance(cfg.get("api_key"), str) and cfg["api_key"].strip():
                ns = f"ai.{m}" if m in ("text", "qc", "chat") else m
                store = get_store(root_dir)
                if store.set_api_key(ns, cfg["api_key"].strip()):
                    cfg["api_key"] = ""
                    plain = plain or ""  # 多模块场景由循环内各自处理
                else:
                    logger.warning(f"模块 {m} 的密钥无法加密存储，保留明文（请尽快配置 MJSCXT_SECRET_KEY）")
                    continue

    if plain:
        store = get_store(root_dir)
        if not store.set_api_key(namespace, plain):
            logger.warning(f"{os.path.basename(config_path)} 的密钥无法加密存储，保留明文；"
                           f"请安装 cryptography 或改用环境变量")
            return False

    try:
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config_path)
        logger.info(f"已迁移 {os.path.basename(config_path)} 中的明文密钥到加密库")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"遗留配置回写失败：{e}")
        return False
