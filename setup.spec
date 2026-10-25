# -*- mode: python ; coding: utf-8 -*-
"""setup.spec — 安装器打包配置。

onefile / windowed / icon=app_icon.ico / name=抖快批量下载器-Setup。

载荷（datas，安装时原样复制到安装目录）：
- ``pack-staging/payload/抖快批量下载器.exe``   R3 产出的主程序
- ``pack-staging/payload/ms-playwright/``       chromium-1248 +
                                                  chromium_headless_shell-1248 +
                                                  ffmpeg-1013
- ``pack-staging/payload/uninstall.exe``         R4 产出的卸载器
- ``pack-staging/payload/app_icon.ico``         安装目录图标

载荷中严禁包含开发者个人数据（config.yml / db / cookies / 订阅 / 日志等），
见任务约束；打包前由人工核对 pack-staging 目录内容。
"""
from PyInstaller.utils.hooks import collect_all

datas = [
    ('pack-staging/payload', 'payload'),
    ('app_icon.ico', '.'),
]
binaries = []
hiddenimports = []
tmp_ret = collect_all('PySide6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

a = Analysis(
    ['setup_app.py'],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='抖快批量下载器-Setup',
    icon='app_icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
