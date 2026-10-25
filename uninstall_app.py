#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖快批量下载器卸载器。

功能：
- 删除安装目录整个文件夹（含自带的 ms-playwright 浏览器，约 700MB）；
- 删除桌面快捷方式与开始菜单快捷方式；
- 删除 HKCU 注册表卸载项
  （``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\抖快批量下载器``）。

用法：
- GUI 模式（默认）：弹确认框 -> 执行卸载 -> 完成提示；
- ``--silent`` 模式：无 GUI 全自动执行（供 QA 自动化测试）。

自删除处理：卸载器自身位于要被删的安装目录里，Windows 下运行中的 exe
无法直接删除，但可以重命名。因此先把安装目录其余内容删掉，再将自身重命名为
``uninstall.exe.del``，然后 spawn
``cmd /c timeout /t 1 & del /q "<自己>" & rd /s /q "<安装目录>"``
让 cmd 延迟收尾，最后自己退出。

安装信息（安装目录、快捷方式路径）优先读取同目录下的 ``install_info.json``，
缺失时回退到注册表卸载项。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APP_NAME = "抖快批量下载器"
#: HKCU 卸载注册表项路径（与安装器写入的键名保持一致）
UNINSTALL_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\抖快批量下载器"
#: 安装信息文件名（由安装器写入安装目录）
INFO_FILENAME = "install_info.json"


# ---------------------------------------------------------------- 基础工具

def app_base_dir() -> Path:
    """返回卸载器自身所在目录。

    frozen（PyInstaller onefile 解压后 exe 已被复制到安装目录运行）时即安装目录；
    源码模式调试时取脚本所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def self_path() -> Path:
    """返回卸载器自身的真实路径。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def _read_uninstall_reg() -> Dict[str, str]:
    """读取 HKCU 卸载注册表项；不存在或读取失败时返回空 dict。"""
    try:
        import winreg
    except ImportError:
        return {}
    values: Dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH) as key:
            count = winreg.QueryInfoKey(key)[1]
            for i in range(count):
                name, value, _ = winreg.EnumValue(key, i)
                values[name] = value
    except OSError:
        return {}
    return values


def load_install_info(base: Path) -> Dict[str, Optional[str]]:
    """汇总卸载所需信息。

    优先 install_info.json；文件缺失/损坏时用注册表卸载项兜底
    （InstallLocation 即安装目录）。
    """
    info: Dict[str, Optional[str]] = {
        "install_dir": str(base),
        "desktop_lnk": None,
        "startmenu_lnk": None,
    }
    info_file = base / INFO_FILENAME
    try:
        data = json.loads(info_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if data.get("install_dir"):
                info["install_dir"] = str(data["install_dir"])
            if data.get("desktop_lnk"):
                info["desktop_lnk"] = str(data["desktop_lnk"])
            if data.get("startmenu_lnk"):
                info["startmenu_lnk"] = str(data["startmenu_lnk"])
            return info
    except (OSError, ValueError):
        pass
    # 注册表兜底
    reg = _read_uninstall_reg()
    if reg.get("InstallLocation"):
        info["install_dir"] = str(reg["InstallLocation"])
    return info


# ---------------------------------------------------------------- 卸载动作

def _cmd_del(path: Path) -> bool:
    """cmd del 兜底删除。

    少数安全策略会拦截进程内 ``unlink`` 删除 .lnk 类文件；此时通过
    ``cmd /c del /q`` 再尝试一次（无窗口、15 秒超时）。
    """
    try:
        result = subprocess.run(
            ["cmd", "/c", "del", "/q", str(path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0 and not path.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def remove_shortcut(path_str: Optional[str]) -> bool:
    """删除单个快捷方式文件，返回是否实际删除了文件。"""
    if not path_str:
        return False
    path = Path(path_str)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    # 兜底：unlink 失败（如被安全策略拦截）时改用 cmd del 再试一次
    if path.exists():
        return _cmd_del(path)
    return False


def default_shortcut_paths() -> List[str]:
    """install_info.json 缺失时的快捷方式默认路径兜底。"""
    paths: List[str] = []
    user_profile = os.environ.get("USERPROFILE", "")
    appdata = os.environ.get("APPDATA", "")
    if user_profile:
        paths.append(str(Path(user_profile) / "Desktop" / f"{APP_NAME}.lnk"))
    if appdata:
        paths.append(
            str(Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / f"{APP_NAME}.lnk")
        )
    return paths


def remove_shortcuts(info: Dict[str, Optional[str]]) -> Tuple[List[str], List[str]]:
    """删除桌面 + 开始菜单快捷方式。

    :param info: 卸载信息（含 desktop_lnk / startmenu_lnk）。
    :return: (成功删除的路径列表, 仍未能删除的路径列表)；
             本来就不存在的快捷方式不计入任何一侧。
    """
    candidates: List[str] = []
    if info.get("desktop_lnk"):
        candidates.append(str(info["desktop_lnk"]))
    if info.get("startmenu_lnk"):
        candidates.append(str(info["startmenu_lnk"]))
    for fallback in default_shortcut_paths():
        if fallback not in candidates:
            candidates.append(fallback)
    removed: List[str] = []
    failed: List[str] = []
    for candidate in candidates:
        if not Path(candidate).exists():
            continue  # 本来就不存在，无需清理也不算失败
        if remove_shortcut(candidate):
            removed.append(candidate)
        else:
            failed.append(candidate)
    return removed, failed


def remove_uninstall_reg() -> bool:
    """删除 HKCU 卸载注册表项；键不存在视为成功。"""
    try:
        import winreg
    except ImportError:
        return False
    try:
        winreg.DeleteKeyEx(
            winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH,
            winreg.KEY_WOW64_64KEY, 0,
        )
        return True
    except FileNotFoundError:
        return True
    except (AttributeError, OSError):
        # DeleteKeyEx 不可用或带 WOW64 标志失败时，退回普通 DeleteKey
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


def remove_dir_contents(install_dir: Path, keep: Path) -> None:
    """删除安装目录下的全部内容，但保留 ``keep``（卸载器自身）。

    单项删除失败时静默跳过（例如被占用的大文件），最终由 cmd 的
    ``rd /s /q`` 兜底。
    """
    if not install_dir.is_dir():
        return
    keep_resolved = keep.resolve()
    for child in sorted(install_dir.iterdir()):
        try:
            if child.resolve() == keep_resolved:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            continue


def spawn_self_delete(dirs: List[Path], me: Path) -> None:
    """重命名自身并通过 cmd 延迟删除自身及整个安装目录。

    Windows 允许重命名运行中的 exe（不允许直接删除），因此先改名再删。
    ``dirs`` 为待删除的目录列表（通常只有安装目录；安装信息异常导致
    卸载器所在目录与安装目录不一致时，两个目录都要清理）。
    """
    trash = me.with_name(me.name + ".del")
    try:
        if trash.exists():
            trash.unlink()
        os.rename(me, trash)
    except OSError:
        # 重命名失败（极端情况）则退化为直接删，成败由 rd 兜底
        trash = me
    rd_cmds = " & ".join(f'rd /s /q "{d}"' for d in dirs)
    cmd = f'cmd /c timeout /t 1 & del /q "{trash}" & {rd_cmds}'
    subprocess.Popen(
        cmd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


# ---------------------------------------------------------------- GUI（MessageBoxW 原生对话框）

# MessageBoxW 标志位与返回值（WinUser.h）
_MB_OK = 0x00000000
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_ICONINFORMATION = 0x00000040
_MB_SETFOREGROUND = 0x00010000
_IDYES = 6


def _message_box(text: str, title: str, flags: int) -> int:
    """弹原生 Windows 消息框。

    用 ctypes 调 ``user32.MessageBoxW``，不依赖 tkinter / PySide6 等
    任何第三方库（构建环境的 Python 可能不带 tkinter），保持卸载器
    小体积且对话框为系统原生样式。
    """
    try:
        return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))
    except Exception:
        return 0


def _gui_confirm() -> bool:
    """确认框：返回 True 表示用户确认卸载。"""
    result = _message_box(
        f"确定要卸载{APP_NAME}吗？\n卸载不会删除你已下载的视频文件",
        f"卸载 {APP_NAME}",
        _MB_YESNO | _MB_ICONQUESTION | _MB_SETFOREGROUND,
    )
    return result == _IDYES


def _gui_finish(failed_shortcuts: List[str]) -> None:
    """完成提示（按快捷方式实际清理结果输出）。"""
    if failed_shortcuts:
        paths = "\n".join(failed_shortcuts)
        message = (
            f"{APP_NAME}已卸载。\n\n"
            f"但以下快捷方式未能自动删除，请手动删除：\n{paths}"
        )
    else:
        message = f"{APP_NAME}已卸载。\n安装目录、快捷方式和注册表信息均已清理。"
    _message_box(
        message, "卸载完成",
        _MB_OK | _MB_ICONINFORMATION | _MB_SETFOREGROUND,
    )


# ---------------------------------------------------------------- 主流程

def do_uninstall(silent: bool = False) -> int:
    """执行卸载主流程。

    :param silent: True 时无 GUI 全自动执行（供 QA 自动化测试）。
    :return: 进程退出码，0 表示成功。
    """
    base = app_base_dir()
    info = load_install_info(base)
    install_dir = Path(info["install_dir"] or str(base))
    me = self_path()

    if not silent and not _gui_confirm():
        return 0

    # 1. 快捷方式（桌面 + 开始菜单）
    _removed, failed_shortcuts = remove_shortcuts(info)
    # 2. HKCU 卸载注册表项
    remove_uninstall_reg()
    # 3. 安装目录内容（保留卸载器自身，稍后自删除）；
    #    安装信息异常导致卸载器目录与安装目录不一致时，两个目录都清理
    dirs_to_clean: List[Path] = []
    for candidate in (install_dir, base):
        if candidate.is_dir() and candidate not in dirs_to_clean:
            dirs_to_clean.append(candidate)
    for directory in dirs_to_clean:
        remove_dir_contents(directory, me)
    # 4. spawn cmd 收尾：删自身 + 删整个安装目录
    if dirs_to_clean:
        spawn_self_delete(dirs_to_clean, me)

    # 静默模式下把未能自动删除的快捷方式打到 stderr，便于 QA 自动化捕获
    if silent:
        for path in failed_shortcuts:
            print(f"[WARN] 快捷方式未能自动删除，请手动删除: {path}",
                  file=sys.stderr)

    if not silent:
        _gui_finish(failed_shortcuts)
    return 0


def main() -> int:
    """解析命令行参数并执行卸载。"""
    parser = argparse.ArgumentParser(description=f"{APP_NAME}卸载器")
    parser.add_argument(
        "--silent", action="store_true",
        help="无 GUI 全自动卸载（供 QA 自动化测试）",
    )
    args = parser.parse_args()
    return do_uninstall(silent=args.silent)


if __name__ == "__main__":
    sys.exit(main())
