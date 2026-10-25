#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""应用根目录解析（PyInstaller 打包安全）。

背景：PyInstaller onefile 运行时会把自身解压到临时目录 ``_MEIxxxxxx``，
此时模块的 ``__file__`` 指向该临时目录，退出时整个目录被删除。
任何用 ``Path(__file__).parent`` 定位的数据文件（config.yml / .cookies.json /
.browser-profile / .avatars / subscriptions.json 等）都会写进临时目录而丢失，
表现为「登录后依旧显示未登录」「订阅不持久」等问题。

正确做法：frozen 模式下用 ``sys.executable``（exe 真实路径）定位；
源码模式维持 ``__file__`` 行为。两种模式下返回值一致：
    - 源码版：项目根目录（如 D:\\douyin-downloader）
    - 打包版：exe 所在目录（安装目录 / 解压目录 / 桌面）
"""
from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["app_root", "is_frozen"]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """返回应用数据根目录（exe 所在目录 / 源码项目根目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
