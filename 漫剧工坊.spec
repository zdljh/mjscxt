# -*- mode: python ; coding: utf-8 -*-
"""漫剧工坊 - PyInstaller打包配置"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# 隐藏导入所有app子模块
hidden_imports = collect_submodules('app')

# 收集所有数据文件
datas = [
    ('app/templates', 'app/templates'),
    ('app/static', 'app/static'),
    ('locales', 'locales'),
    ('output', 'output'),
]

# 排除不必要的模块
excludes = [
    'matplotlib', 'scipy', 'pandas', 'jupyter', 
    'tkinter', 'IPython', 'notebook',
]

a = Analysis(
    ['main.py'],
    pathex=['C:\\Users\\liujianghua\\WorkBuddy\\2026-09-09-16-55-22\\漫剧生成系统'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='漫剧工坊',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 调试模式显示控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='x86_64',
    codesign_identity=None,
    entitlements_file=None,
)
