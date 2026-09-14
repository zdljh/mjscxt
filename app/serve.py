"""
生产级启动器：无人值守 24/7 场景使用真正的 WSGI 服务器，而不是 Flask 自带的开发服务器。

为什么用 waitress 而不是开发服务器：
    - Flask 自带的开发服务器官方明确「不得用于生产」，单线程/调试导向、无优雅超时、
      带自动重载器，长期挂机容易被中间层掐断或受代码变更干扰；
    - 本项目单集生产可能持续数十分钟（H3 视频生成 + 超分），需要稳定的长连接与
      可控的连接空闲超时；
    - waitress 为纯 Python、跨平台，Windows 上无需额外系统依赖，适合桌面挂机。

设计要点：
    - 默认 waitress；未安装时回退到 Flask 开发服务器并显著告警，不让服务起不来；
    - 单进程多线程：ComfyUI 侧已有全局串行队列，这里不需要多进程；
    - channel_timeout 放宽：单次生成任务动辄数十分钟，不能被中间层掐断连接。
"""
from __future__ import annotations

import logging
import os
import sys

# 让脚本无论从哪个目录启动都能找到 app 包内的模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app import app  # noqa: E402

logger = logging.getLogger("serve")


def _env_int(key: str, default: int) -> int:
    try:
        return int((os.getenv(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def main() -> int:
    host = (os.getenv("APP_HOST") or "127.0.0.1").strip()
    port = _env_int("APP_PORT", 5000)
    threads = _env_int("APP_THREADS", 8)
    # 单集生产可能持续很久（H3 视频生成 + 超分），连接空闲超时给足
    channel_timeout = _env_int("APP_CHANNEL_TIMEOUT", 1800)

    try:
        from waitress import serve
    except ImportError:
        logger.warning("=" * 68)
        logger.warning("未安装 waitress，回退到 Flask 开发服务器。")
        logger.warning("开发服务器无法可靠处理文件上传，请执行：")
        logger.warning("    pip install waitress")
        logger.warning("=" * 68)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
        return 0

    logger.info("漫剧生成系统已启动（waitress）：http://%s:%d", host, port)
    logger.info("线程数 %d · 连接空闲超时 %ds", threads, channel_timeout)
    serve(app, host=host, port=port, threads=threads,
          channel_timeout=channel_timeout, ident="mjscxt")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
