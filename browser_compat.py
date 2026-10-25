#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Playwright 浏览器启动兼容层。

背景：PyInstaller 的 ``--collect-all playwright`` 只打包 node 驱动，Chromium
浏览器二进制（``.local-browsers``，约 170MB）不会进 exe。exe 内直接 launch 会报：

    Executable doesn't exist at ...\\_MEIxxxx\\playwright\\driver\\package\\
    .local-browsers\\chromium-1248\\chrome-win64\\chrome.exe

本模块按优先级解析可用浏览器（全部不可用才抛错）：

1. exe 内自带 ``.local-browsers``（将来若把浏览器也打进包）；
2. 安装目录自带 ``ms-playwright``（安装版把浏览器装到 exe 同级目录）；
3. ``PLAYWRIGHT_BROWSERS_PATH`` 环境变量指向的目录；
4. ``%LOCALAPPDATA%\\ms-playwright``（本机 venv ``playwright install`` 的默认位置，
   与 exe 内 playwright 版本同 revision 时直接复用，行为与源码模式一致）；
5. 系统安装的 Edge（``channel="msedge"``）；
6. 系统安装的 Chrome（``channel="chrome"``）。

用法（launch 与 launch_persistent_context 统一入口，user_data_dir 用关键字传）::

    from browser_compat import ensure_browsers_path, launch_chromium

    async with async_playwright() as p:
        browser = await launch_chromium(p, headless=True, args=[...])
        # 或持久化：
        context = await launch_chromium(
            p, persistent=True, user_data_dir=str(profile_dir), headless=False
        )
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["ensure_browsers_path", "system_browser_channel", "launch_chromium"]

# 常见系统浏览器安装路径（按优先级）
_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def _has_chromium(root: Optional[Path]) -> bool:
    if not root:
        return False
    try:
        return any(root.glob("chromium-*"))
    except Exception:
        return False


def ensure_browsers_path() -> None:
    """把 Playwright 的浏览器查找路径固定到有 Chromium 的目录。

    只在打包版（frozen）下生效：源码模式 venv 的 playwright 默认就会找到
    ``%LOCALAPPDATA%\\ms-playwright``，无需干预。必须在 launch（driver 启动）
    之前调用，且调用是幂等的。
    """
    if not getattr(sys, "frozen", False):
        return
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return

    # 1. 打包内自带（当前不会命中，留给将来把浏览器打进包的情况）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "playwright" / "driver" / "package" / ".local-browsers"
        if _has_chromium(bundled):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)
            return

    # 2. 安装目录自带浏览器（安装版 setup.exe 把 ms-playwright 装到 exe 同级目录）
    installed = Path(sys.executable).resolve().parent / "ms-playwright"
    if _has_chromium(installed):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(installed)
        return

    # 3. 本机用户级浏览器缓存（venv `playwright install` 的落点）
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if _has_chromium(local):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local)


def system_browser_channel() -> Optional[str]:
    """返回系统安装的 Edge/Chrome 对应的 Playwright channel 名，没有则 None。"""
    for path in _EDGE_PATHS:
        if Path(path).exists():
            return "msedge"
    for path in _CHROME_PATHS:
        if Path(path).exists():
            return "chrome"
    local_chrome = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe"
    )
    if local_chrome.exists():
        return "chrome"
    return None


def _is_missing_executable(exc: Exception) -> bool:
    return "executable doesn't exist" in str(exc).lower()


async def launch_chromium(playwright, *, persistent: bool = False, **kwargs: Any):
    """带 fallback 的 Chromium 启动入口。

    persistent=False  → chromium.launch(**kwargs)
    persistent=True   → chromium.launch_persistent_context(user_data_dir=..., **kwargs)

    先按调用方给的参数启动；只有错误是「浏览器可执行不存在」时才依次尝试
    系统 Edge / Chrome 的 channel，其他异常直接抛出（不掩盖真实问题）。
    """
    ensure_browsers_path()
    fn = (
        playwright.chromium.launch_persistent_context
        if persistent
        else playwright.chromium.launch
    )

    attempts: List[Dict[str, Any]] = [dict(kwargs)]
    channel = system_browser_channel()
    if channel:
        attempts.append({**kwargs, "channel": channel})

    last_exc: Optional[Exception] = None
    for index, attempt_kwargs in enumerate(attempts):
        try:
            return await fn(**attempt_kwargs)
        except Exception as exc:
            last_exc = exc
            if _is_missing_executable(exc) and index + 1 < len(attempts):
                continue
            if _is_missing_executable(exc):
                break
            raise

    tried = " -> ".join(a.get("channel", "chromium") for a in attempts)
    raise RuntimeError(
        "找不到可用的 Chromium 浏览器（已尝试：%s）。\n"
        "解决办法（任选其一）：\n"
        "  1. 在本机运行一次： %PYTHON% -m playwright install chromium\n"
        "  2. 安装 Microsoft Edge 或 Google Chrome（Windows 11 自带 Edge）\n"
        "原始错误：%s"
        % (tried, last_exc)
    )
