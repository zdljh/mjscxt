# -*- coding: utf-8 -*-
"""环境配置加载（单一入口）

为什么单独抽一个模块
--------------------
原先 `.env` 的加载写在 config.py 里，导致**只有 import config 的模块才能读到 .env**。
而 secret_store / qc_client 等模块可能在 config 之前被导入（例如只跑一个质检脚本），
此时 `MJSCXT_SECRET_KEY` 等环境变量尚未注入 → 主密钥取不到 → 已加密的密钥全部解密失败。
（这是实际踩到的坑：qc_config 的密钥迁移成功，但独立调用时解密失败。）

因此把「项目根目录 + .env 加载」下沉到本模块，config 与 secret_store 都依赖它，
保证**任何入口导入任一模块时 .env 都已生效**。

加载策略：不覆盖已存在的环境变量（系统环境变量优先级高于 .env）。
未安装 python-dotenv 时使用内置最简解析器，保证零依赖也能启动。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# 项目根目录（app/ 的上一级）
PROJECT_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_LOADED = False


def _parse_env_file(path: str) -> None:
    """内置最简 .env 解析器（python-dotenv 缺失时的兜底）"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k.startswith("export "):
                    k = k[7:].strip()
                if k and k not in os.environ:
                    os.environ[k] = v
        logger.debug(f"已加载环境配置（内置解析器）：{path}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f".env 解析失败（忽略，按默认值启动）：{e}")


def load_project_env(force: bool = False) -> str:
    """加载项目根目录 .env（幂等）。返回 .env 路径（不存在则返回空串）。"""
    global _LOADED
    if _LOADED and not force:
        return os.path.join(PROJECT_ROOT_DIR, ".env")
    _LOADED = True
    env_path = os.path.join(PROJECT_ROOT_DIR, ".env")
    if not os.path.isfile(env_path):
        return ""
    # 优先用 python-dotenv（正确处理引号、转义、多行）
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        logger.debug(f"已加载环境配置：{env_path}")
        return env_path
    except ImportError:
        _parse_env_file(env_path)
        return env_path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"python-dotenv 加载失败，改用内置解析器：{e}")
        _parse_env_file(env_path)
        return env_path


def env(key: str, default: str = "") -> str:
    """取环境变量（去空白）；空串视为未设置，回退默认值"""
    val = (os.getenv(key) or "").strip()
    return val if val else default


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


# 导入即加载，保证任何模块引用本模块时环境已就绪
load_project_env()
