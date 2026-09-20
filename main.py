# -*- coding: utf-8 -*-
"""漫剧生成系统 - 桌面应用入口

支持两种运行模式:
1. Web模式: 正常Flask应用，浏览器访问
2. 桌面模式: 通过PyInstaller打包为EXE，内置浏览器
"""
import sys
import os
import argparse
import threading
import webbrowser
from pathlib import Path

# 确保app目录在路径中
sys.path.insert(0, str(Path(__file__).parent / 'app'))

def parse_args():
    parser = argparse.ArgumentParser(description='漫剧工坊 - 全自动AI漫剧生产平台')
    parser.add_argument('--desktop', action='store_true', help='启动桌面模式（内置浏览器）')
    parser.add_argument('--port', type=int, default=5000, help='Web服务器端口')
    parser.add_argument('--host', default='127.0.0.1', help='Web服务器地址')
    return parser.parse_args()

def open_in_browser(url):
    """在默认浏览器打开URL"""
    threading.Timer(1, lambda: webbrowser.open(url)).start()

def run_web_mode(port, host):
    """运行Web模式，返回进程退出码

    ⚠️ 审计 G20：`serve.main()` 是**无参**函数 —— host/port 由环境变量 `APP_HOST` /
    `APP_PORT` 读取（见 `app/serve.py` 的 `_safe_run`）。旧代码写 `main(port, host)`，
    传给一个不收参数的函数两个位置参数 → 打包成 EXE / 桌面入口启动即
    `TypeError: main() takes 0 positional arguments but 2 were given`。
    日常都走 `run_app.bat`（`python app/serve.py`），这条入口从未被真正跑过，所以一直没暴露。
    现在改为先把参数写进环境变量，再调用无参 `main()`。
    """
    import serve
    host = str(host or "127.0.0.1")
    os.environ["APP_HOST"] = host
    os.environ["APP_PORT"] = str(int(port or 5000))
    print(f"🚀 漫剧工坊启动: http://{host}:{os.environ['APP_PORT']}")
    print("按 Ctrl+C 停止服务")
    return serve.main()

def run_desktop_mode(port):
    """运行桌面模式 - 使用内置浏览器或Electron包装"""
    url = f"http://{port}"
    print(f"🖥️  桌面模式启动: {url}")
    open_in_browser(url)
    return run_web_mode(port, '127.0.0.1')

def main():
    args = parse_args()
    
    # 创建输出目录
    project_root = Path(__file__).parent
    output_dir = project_root / 'output'
    output_dir.mkdir(exist_ok=True)
    
    if args.desktop:
        return run_desktop_mode(args.port)
    return run_web_mode(args.port, args.host)

if __name__ == '__main__':
    sys.exit(main())
