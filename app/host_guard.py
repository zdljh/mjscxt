"""F-02 监听护栏（P2-T4 抽模块：serve.py 与 app.py 直跑分支共用同一事实源）。

背景
----
本应用当前**没有鉴权**、CORS 未限制 origin，安全边界完全押在「只监听回环」。
`APP_HOST` 一旦被误设为 `0.0.0.0` / 局域网 IP，破坏性 API（删项目 / 写文件 /
agent 工具 / 启停托管）就会对同网段全裸暴露。

此前 `_guard_host` 只在 `serve.py` 里被调用；`python app/app.py` 直跑分支
（原 P2 尾巴 T-4）绕过这套护栏——`APP_HOST=0.0.0.0` 时 debug 模式直接裸暴露。

⚠️ 抽独立模块的原因：
    `serve.py` 顶层 `app = _load_flask_app()` 会 exec 整份 app.py。若
    app.py `__main__` 里 `import serve`，会再次触发 app.py 顶层重跑，
    Flask 路由重复注册报错。抽无副作用的 `host_guard` 模块，两侧都 import 它，
    消除循环依赖且护栏保持单一事实源（未来阈值/白名单调整只改一处）。
"""
import logging
import os

logger = logging.getLogger("serve.host_guard")   # 归到 serve logger 之下，日志命名不变

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

    校验不通过时抛 ``RuntimeError``，由调用方（serve.main 或 app.py `__main__`）
    捕获并以非零码退出（不触发崩溃重启）。
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


__all__ = ["_is_loopback_host", "_guard_host",
           "_LOOPBACK_HOSTS", "_ALLOW_NON_LOOPBACK_ENV"]
