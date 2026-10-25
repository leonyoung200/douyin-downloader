# -*- coding: utf-8 -*-
"""用修复后的新 exe 重建 便携版/网页版 两个 zip（结构与旧版一致）。"""
import os
import time
import zipfile
from pathlib import Path

MAIN_EXE = Path(r"D:\douyin-downloader\抖快批量下载器.exe")
BROWSERS = Path(r"C:\Users\Leonado Young\AppData\Local\ms-playwright")
KEEP = Path(r"D:\pack-out\keep")
OUT = Path(r"D:\pack-out")
BROWSER_DIRS = ("chromium-1248", "chromium_headless_shell-1248", "ffmpeg-1013")


def add_browsers(zf: zipfile.ZipFile):
    for d in BROWSER_DIRS:
        base = BROWSERS / d
        n = 0
        for p in base.rglob("*"):
            if p.is_file():
                zf.write(p, f"ms-playwright/{d}/{p.relative_to(base).as_posix()}")
                n += 1
        print(f"  + ms-playwright/{d} ({n} files)", flush=True)


def build_portable():
    target = OUT / "抖快批量下载器-便携版.zip"
    t0 = time.time()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(MAIN_EXE, "抖快批量下载器.exe")
        print("+ 抖快批量下载器.exe", flush=True)
        add_browsers(zf)
        zf.write(KEEP / "portable" / "使用说明.txt", "使用说明.txt")
        print("+ 使用说明.txt", flush=True)
    print(f"PORTABLE_DONE {target.stat().st_size} bytes in {time.time()-t0:.0f}s", flush=True)


def build_web():
    target = OUT / "抖快批量下载器-网页版.zip"
    t0 = time.time()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(MAIN_EXE, "抖快批量下载器.exe")
        print("+ 抖快批量下载器.exe", flush=True)
        zf.write(KEEP / "web" / "抖快批量下载器-网页版.exe", "抖快批量下载器-网页版.exe")
        print("+ 抖快批量下载器-网页版.exe", flush=True)
        add_browsers(zf)
        zf.write(KEEP / "web" / "启动网页版.bat", "启动网页版.bat")
        zf.write(KEEP / "web" / "使用说明.txt", "使用说明.txt")
        print("+ 启动网页版.bat + 使用说明.txt", flush=True)
    print(f"WEB_DONE {target.stat().st_size} bytes in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    assert MAIN_EXE.exists(), "主 exe 不存在"
    build_portable()
    build_web()
    print("ALL_ZIPS_DONE", flush=True)
