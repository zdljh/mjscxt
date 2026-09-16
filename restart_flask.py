#!/usr/bin/env python3
"""
漫剧生成系统 - Flask 服务重启脚本
用法: python restart_flask.py
"""
import os
import sys
import subprocess
import time
import signal

def kill_process_by_port(port=5000):
    """终止占用指定端口的进程"""
    import psutil
    for conn in psutil.net_connections(kind='inet'):
        if conn.laddr.port == port and conn.status == 'LISTEN':
            try:
                os.kill(conn.pid, signal.SIGTERM)
                print(f"已终止进程 PID={conn.pid}")
                time.sleep(1)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"权限不足，无法终止 PID={conn.pid}")

def start_flask():
    """启动 Flask 服务"""
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # 尝试终止旧进程
    print("[1/3] 停止旧进程...")
    try:
        kill_process_by_port(5000)
    except ImportError:
        print("  未安装 psutil，跳过自动终止")
    
    time.sleep(1)
    
    # 启动新服务
    print("[2/3] 启动新服务...")
    proc = subprocess.Popen(
        [sys.executable, 'app/serve.py'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.abspath(__file__))
    )
    
    print(f"[3/3] 等待服务启动...")
    time.sleep(4)
    
    # 检查服务是否启动
    try:
        import urllib.request
        resp = urllib.request.urlopen('http://127.0.0.1:5000/api/status', timeout=2)
        print("\n" + "=" * 50)
        print("服务已启动！")
        print(f"PID: {proc.pid}")
        print("访问地址: http://127.0.0.1:5000")
        print("=" * 50)
    except Exception as e:
        print(f"\n服务启动失败: {e}")
        print("请手动运行: python app/serve.py")

if __name__ == '__main__':
    start_flask()
