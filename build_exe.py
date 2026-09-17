#!/usr/bin/env python3
"""
漫剧工坊 - PyInstaller 单文件打包脚本

生成独立可执行文件 dist/漫剧工坊.exe (~2GB)，内含全部 Python 依赖。
启动时自动进入 Web 服务或桌面模式（由 main.py 决定）。

用法：
    python build_exe.py          # 构建
    python build_exe.py clean    # 清理构建缓存
"""
import os
import sys
import subprocess
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(HERE, "dist")
BUILD_DIR = os.path.join(HERE, "build")
SPEC_FILE = os.path.join(HERE, "漫剧工坊.spec")


def clean():
    for d in [BUILD_DIR, DIST_DIR]:
        if os.path.isdir(d):
            shutil.rmtree(d)
            print(f"已删除: {d}")
    print("清理完成")


def build():
    os.makedirs(DIST_DIR, exist_ok=True)
    cmd = [sys.executable, "-m", "PyInstaller", SPEC_FILE, "--clean"]
    print(f"构建命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=HERE)
    if result.returncode == 0:
        exe = os.path.join(DIST_DIR, "漫剧工坊.exe")
        size_mb = os.path.getsize(exe) / 1024 / 1024 if os.path.exists(exe) else 0
        print(f"✓ 构建成功: {exe} ({size_mb:.1f} MB)")
        print("提示：首次启动会从命令行输出启动日志，双击可直接运行桌面版")
    else:
        print("✗ 构建失败，请检查上方错误信息")
    return result.returncode


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        clean()
    else:
        build()
