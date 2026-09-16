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
    """运行Web模式"""
    from serve import main
    print(f"🚀 漫剧工坊启动: http://{host}:{port}")
    print("按 Ctrl+C 停止服务")
    main(port, host)

def run_desktop_mode(port):
    """运行桌面模式 - 使用内置浏览器或Electron包装"""
    url = f"http://{port}"
    print(f"🖥️  桌面模式启动: {url}")
    open_in_browser(url)
    run_web_mode(port, '127.0.0.1')

def main():
    args = parse_args()
    
    # 创建输出目录
    project_root = Path(__file__).parent
    output_dir = project_root / 'output'
    output_dir.mkdir(exist_ok=True)
    
    if args.desktop:
        run_desktop_mode(args.port)
    else:
        run_web_mode(args.port, args.host)

if __name__ == '__main__':
    main()
