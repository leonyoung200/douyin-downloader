# -*- mode: python ; coding: utf-8 -*-
"""网页版打包配置：onefile + 控制台可见（朋友需要看到地址与日志）。

与桌面版（抖快批量下载器.spec）的差异：
- 入口是 web_app.py（FastAPI + uvicorn），不需要 PySide6；
- 下载/登录全部通过子进程调用同目录的「抖快批量下载器.exe」，
  所以本 exe 自身不打包 playwright / imageio_ffmpeg / rich / certifi；
- console=True（windowed=False）：控制台要显示本机 URL 和运行日志。
"""
from PyInstaller.utils.hooks import collect_all

datas = [('app_icon.ico', '.')]
binaries = []
hiddenimports = [
    # --- Web 栈（collect_all 只收包自身，这里补足直接依赖） ---
    'fastapi', 'uvicorn', 'pydantic', 'yaml',
    'starlette', 'anyio', 'anyio._backends._asyncio', 'sniffio', 'h11',
    # --- 本地模块（与桌面版共用的进程链路辅助） ---
    'subscription_store',      # 订阅数据存取（web 进程内直接用）
    'browser_compat',          # 浏览器路径解析（桌面 exe 用；保持一致）
    'kuaishou_download_worker',  # 快手任务链路（hiddenimport 对齐桌面版）
    'kuaishou_login_worker',
    'login_worker',
    'renumber_folders',
    'merge_folders',
]
for pkg in ('fastapi', 'uvicorn', 'pydantic', 'yaml', 'anyio', 'starlette'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]


a = Analysis(
    ['web_app.py'],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6', 'PySide6_Addons', 'PySide6_Essentials', 'PySide6_Pdf', 'PySide6_WebEngine', 'shiboken6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='抖快批量下载器-网页版',
    icon='app_icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
