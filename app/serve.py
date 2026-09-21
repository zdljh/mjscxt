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

import atexit
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


#: 回环地址白名单（含 IPv6 回环）。127.0.0.0/8 另按前缀判定。
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}

#: 显式放行非回环监听的开关（容器 / 受控网络部署场景；默认不放行）
_ALLOW_NON_LOOPBACK_ENV = "MJSCXT_ALLOW_NON_LOOPBACK"


def _is_loopback_host(host: str) -> bool:
    """判断监听地址是否为回环（含 127.0.0.0/8、::1、localhost）。空值按回环处理。"""
    h = (host or "").strip().lower()
    if not h:
        return True
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _LOOPBACK_HOSTS:
        return True
    return h.startswith("127.")


def _guard_host(host: str) -> None:
    """F-02 单机护栏：非回环 ``APP_HOST`` 的启动校验 / 强告警。

    ⚠️ 本应用**没有鉴权**、CORS 也未限制 origin，安全边界完全押在「只监听回环」。
    `APP_HOST` 一旦被误设为 `0.0.0.0` / 局域网 IP，破坏性 API（删项目 / 写文件 /
    agent 工具 / 启停托管）就会对同网段全裸暴露。

    护栏策略（**只做护栏，不引入用户体系/鉴权**——那属独立立项）：
      - 回环 host → 正常放行；
      - 非回环 host → 默认 **拒绝启动**（fail-closed）并打印强告警；
      - 显式设置 ``MJSCXT_ALLOW_NON_LOOPBACK=1``（容器/受控网络）→ 放行但**强告警**。

    校验不通过时抛 ``RuntimeError``，由 ``main()`` 捕获并以非零码退出（不触发崩溃重启）。
    """
    if _is_loopback_host(host):
        return
    allow = (os.getenv(_ALLOW_NON_LOOPBACK_ENV) or "").strip().lower() \
        in ("1", "true", "yes", "on")
    logger.warning("=" * 68)
    logger.warning("⚠️  检测到非回环监听地址 APP_HOST=%r", host)
    logger.warning("    本应用当前**没有鉴权**、CORS 未限制 origin：一旦对同网段暴露，")
    logger.warning("    「删项目 / 写文件 / agent 工具 / 启停托管」等破坏性接口将全部放开。")
    logger.warning("    单机使用请设 APP_HOST=127.0.0.1；网络部署请另行补充鉴权。")
    if not allow:
        logger.warning("    已拒绝启动（如需强制放行，设 %s=1）。", _ALLOW_NON_LOOPBACK_ENV)
        logger.warning("=" * 68)
        raise RuntimeError(
            f"拒绝在非回环地址启动：APP_HOST={host!r}（零鉴权，"
            f"如需强制放行请设 {_ALLOW_NON_LOOPBACK_ENV}=1）")
    logger.warning("    已按 %s=1 放行（⚠️ 仍无鉴权，风险自负）。", _ALLOW_NON_LOOPBACK_ENV)
    logger.warning("=" * 68)


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


def _install_shutdown_hooks() -> None:
    """O16：优雅停机钩子。

    背景：`autopilot.stop()` 原本**全库无调用方**，`serve.py` 里 `import signal` 也未使用。
    于是 Ctrl+C 是「硬杀」——托管循环线程（daemon）被进程直接带死，可能留下：
      - SQLite 里的 `interrupted` 记录（当前步骤没被正常收尾）；
      - 正在写一半的 JSON / manifest（非原子写）。
    本钩子让进程退出**之前**先优雅停掉托管循环（步骤边界生效），再正常终止。

    实现（Python 官方推荐的「优雅停机 + 二次信号强制」模式）：
      - 首次收到 SIGINT/SIGTERM/SIGBREAK → 调 `autopilot.stop(timeout=15)` 给在飞步骤
        一个到边界停下的窗口，然后**恢复默认处理并重新抛出该信号**，让进程走常规终止
        流程（触发 atexit 兜底）；
      - `atexit.register(_atexit_stop)` 作为最后兜底：即使信号 handler 没跑到，
        进程正常/异常退出前也保证 `autopilot.stop()` 被执行一次（幂等，重复调用安全）。

    ⚠️ 与 S9 的「cancellation 检查点刻意不进 ComfyUI 渲染循环」不冲突：这里只停托管
    循环线程本身，不强行打断已在 GPU 上渲染的段（避免留半成品），交由各自的超时兜底。
    """
    import atexit
    import signal

    def _do_stop() -> None:
        try:
            import autopilot
        except Exception:  # noqa: BLE001  导入失败（如 app 尚未就绪）不应阻塞停机
            return
        try:
            autopilot.stop(timeout=15)
            logger.info("托管循环已优雅停止（步骤边界）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"优雅停止托管循环失败：{e}")

    def _atexit_stop() -> None:
        # 进程退出兜底；_do_stop 内部幂等（autopilot.stop 重复调用安全）
        _do_stop()

    def _sig_handler(signum, frame) -> None:
        logger.info(f"收到停机信号（{signum}），优雅停止托管循环后终止进程...")
        _do_stop()
        # 恢复默认处理，重新抛出同一信号 → 走常规终止（会再触发 atexit，幂等）
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    # 注册信号（Windows 上 SIGTERM/SIGBREAK 语义有限，能注册哪个就注册哪个）
    for _sig, _name in ((signal.SIGINT, "SIGINT"),
                        (getattr(signal, "SIGBREAK", signal.SIGINT), "SIGBREAK"),
                        (getattr(signal, "SIGTERM", None), "SIGTERM")):
        if _sig is None or _sig == signal.SIG_DFL:
            continue
        try:
            signal.signal(_sig, _sig_handler)
            logger.info(f"已注册优雅停机 handler：{_name}")
        except (ValueError, OSError, RuntimeError) as e:
            logger.debug(f"注册 {_name} handler 失败（{e}），跳过")

    atexit.register(_atexit_stop)
    logger.info("已注册 atexit 优雅停机兜底")


def main() -> int:
    restart_count = 0

    logger.info("漫剧生成系统启动器开始运行（含自动重启保护）")

    # F-02 护栏：非回环 APP_HOST 启动校验（默认拒绝，显式放行才继续）。放在重启循环
    # **之外**，避免被当作「崩溃」触发最多 MAX_RESTARTS 次无意义重启。
    try:
        _guard_host((os.getenv("APP_HOST") or "127.0.0.1").strip())
    except RuntimeError as e:
        logger.error("启动被安全护栏拦截（F-02）：%s", e)
        return 2

    _install_shutdown_hooks()

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

