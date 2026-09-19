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
    - 内置崩溃检测：Flask 崩溃后自动重启，无需手动介入。
"""
from __future__ import annotations

import logging
import os
import sys
import time
import signal
import types
import importlib.util

# 让脚本无论从哪个目录启动都能找到 app 包内的模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _load_flask_app():
    """按文件路径稳健加载 app/app.py 里的 Flask 实例。

    背景（对应测试缺陷 D1）：
        本项目 app/ 目录与 app.py 同名，且 app/ 下没有 __init__.py（命名空间包）。
        于是 `from app import app` 的语义随启动方式而变：
          - `python app/serve.py`  → 拿到 Flask 实例（正确）
          - `python -m app.serve`  → 拿到的是子模块 app.app（错误！）
        错误时的表现：
          waitress 报 `TypeError: 'module' object is not callable`，
          waitress 缺失时的回退分支再报 `module 'app.app' has no attribute 'run'`，
          最终所有页面与 API 全部返回 500。
        这里改为显式按文件路径加载，彻底消除同名歧义。
    """
    app_py = os.path.join(_HERE, "app.py")
    if not os.path.isfile(app_py):
        raise FileNotFoundError(f"未找到应用入口文件：{app_py}")

    spec = importlib.util.spec_from_file_location("app", app_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {app_py} 构建模块规格")

    module = importlib.util.module_from_spec(spec)
    # 先注册再执行：让 app.py 内部的 `import app` 自引用拿到同一个模块对象
    sys.modules["app"] = module
    spec.loader.exec_module(module)

    flask_app = getattr(module, "app", None)
    if flask_app is None or isinstance(flask_app, types.ModuleType):
        raise RuntimeError(
            "app.py 中未找到 Flask 应用实例 `app`"
            f"（实际拿到：{type(flask_app).__name__}）"
        )
    return flask_app


app = _load_flask_app()

logger = logging.getLogger("serve")

# 自动重启配置
MAX_RESTARTS = 10        # 最多连续重启次数
RESTART_COOLDOWN = 30    # 重启冷却时间（秒）
CHECK_INTERVAL = 10      # 健康检查间隔（秒）


def _env_int(key: str, default: int) -> int:
    try:
        return int((os.getenv(key) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def _safe_run():
    """安全运行主服务，崩溃后返回 False"""
    try:
        host = (os.getenv("APP_HOST") or "127.0.0.1").strip()
        port = _env_int("APP_PORT", 5000)
        threads = _env_int("APP_THREADS", 8)
        channel_timeout = _env_int("APP_CHANNEL_TIMEOUT", 1800)

        try:
            from waitress import serve
            logger.info("漫剧生成系统已启动（waitress）：http://%s:%d", host, port)
            logger.info("线程数 %d · 连接空闲超时 %ds", threads, channel_timeout)
            serve(app, host=host, port=port, threads=threads,
                  channel_timeout=channel_timeout, ident="mjscxt")
        except ImportError:
            logger.warning("=" * 68)
            logger.warning("未安装 waitress，回退到 Flask 开发服务器。")
            logger.warning("开发服务器无法可靠处理文件上传，请执行：")
            logger.warning("    pip install waitress")
            logger.warning("=" * 68)
            app.run(host=host, port=port, threaded=True, use_reloader=False)
        return True
    except Exception as e:
        logger.error(f"服务崩溃: {e}", exc_info=True)
        return False


def main() -> int:
    restart_count = 0

    logger.info("漫剧生成系统启动器开始运行（含自动重启保护）")

    while restart_count <= MAX_RESTARTS:
        logger.info("启动 Flask 服务... (尝试 #%d)", restart_count + 1)
        success = _safe_run()

        if success:
            # 正常退出（比如用户按下 Ctrl+C）
            logger.info("服务已正常停止")
            return 0

        # 发生崩溃，检查是否需要继续重启
        restart_count += 1
        if restart_count > MAX_RESTARTS:
            logger.error(f"已崩溃 {MAX_RESTARTS} 次，停止自动重启")
            return 1

        # 冷却期
        wait_time = min(RESTART_COOLDOWN * restart_count, 300)  # 最多等5分钟
        logger.warning(f"服务崩溃，{wait_time} 秒后自动重启...")
        time.sleep(wait_time)

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())

