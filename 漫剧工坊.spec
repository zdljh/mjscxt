#漫剧工坊.spec
# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['main.py'],
    pathex=['C:\\Users\\liujianghua\\WorkBuddy\\2026-09-09-16-55-22\\漫剧生成系统'],
    binaries=[],
    datas=[
        ('app\\templates', 'app/templates'),
        ('app\\static', 'app/static'),
        ('locales', 'locales'),
    ],
    hiddenimports=[
        'flask',
        'flask_cors',
        'llm_client',
        'script_generator',
        'novel_parser',
        'comfyui_client',
        'autonomous',
        'pipeline',
        'character_manager',
        'nine_grid_storyboard',
        'export_manager',
        'openai',
        'anthropic',
        'torch',
        'PIL',
        'cv2',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'scipy',
        'pandas',
        'jupyter',
    ],
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
    console=False,  # 不显示控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icons/app.ico' if os.path.exists('icons/app.ico') else None,
)
