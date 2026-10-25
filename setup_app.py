#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖快批量下载器安装器。

功能：
- 深色 GUI 安装向导（欢迎 -> 选择目录 -> 进度 -> 完成）；
- ``--silent <目标目录>`` 无 GUI 全自动安装（供 QA 自动化测试）；
- 载荷：主程序 exe + ms-playwright 浏览器目录 + 卸载器 uninstall.exe + app_icon.ico；
- 安装动作：复制载荷、写 install_info.json、创建桌面/开始菜单快捷方式
  （纯 Python 写 .lnk，Shell Link 二进制格式，不依赖 COM/WScript.Shell）、
  写 HKCU 注册表卸载项（winreg 标准库）；
- 单实例保护：命名互斥体防止 onefile 解压期间重复双击启动。

所有安装目录均为用户级（``%LOCALAPPDATA%``），不需要管理员权限。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------- 常量

APP_NAME = "抖快批量下载器"
APP_VERSION = "1.0.0"
APP_DESC = "抖音/快手博主作品批量下载工具"

#: 安装目录内的主程序文件名（载荷内已按此命名，安装时直接复制）
MAIN_EXE_NAME = "抖快批量下载器.exe"
#: 安装目录内的卸载器文件名（载荷内已按此命名，安装时直接复制）
UNINSTALL_EXE_NAME = "uninstall.exe"
ICON_NAME = "app_icon.ico"
INFO_FILENAME = "install_info.json"

#: HKCU 卸载注册表项路径（与卸载器删除的键名保持一致）
UNINSTALL_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\抖快批量下载器"

#: 单实例互斥体名（onefile 解压要几十秒，防止用户重复双击）
MUTEX_NAME = "抖快批量下载器-Setup-9f2e7a1c-4b3d-4e8f-a6c5-1d2b3c4d5e6f"

#: 大文件分块复制阈值 / 分块大小（1MB，保证进度条按字节平滑推进）
BIG_FILE_THRESHOLD = 16 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024

# 深色主题配色（与主程序 gui_app.py 保持一致）
C_SIDEBAR = "#0b0c10"
C_BG = "#0e0f13"
C_CARD = "#16181f"
C_CARD2 = "#1b1e27"
C_BORDER = "#262a34"
C_INPUT = "#12141a"
C_TEXT = "#eef0f4"
C_TEXT2 = "#9aa1ac"
C_TEXT3 = "#6b7280"
C_ACCENT = "#fe2c55"
C_ORANGE = "#ff7a00"
GRADIENT = ("qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #fe2c55, stop:1 #ff7a00)")


# ---------------------------------------------------------------- 路径工具

def app_root() -> Path:
    """返回安装器资源根目录。

    frozen（PyInstaller onefile）时即 ``sys._MEIPASS`` 解压目录；
    源码调试时取脚本所在目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def payload_dir() -> Path:
    """载荷目录：主程序 exe + ms-playwright + uninstall.exe + app_icon.ico。

    frozen 时为 ``sys._MEIPASS/payload``（由 setup.spec 的 datas 注入）；
    源码调试时为项目下的 ``pack-staging/payload`` 暂存目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "payload"
    return Path(__file__).resolve().parent / "pack-staging" / "payload"


def icon_path() -> Path:
    """安装器自身图标（窗口图标用）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / ICON_NAME
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent / ICON_NAME


def default_install_dir() -> Path:
    """默认安装目录：%LOCALAPPDATA%\\抖快批量下载器（普通用户可写）。"""
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def desktop_lnk_path() -> Optional[Path]:
    """桌面快捷方式路径。"""
    user_profile = os.environ.get("USERPROFILE", "")
    if not user_profile:
        return None
    return Path(user_profile) / "Desktop" / f"{APP_NAME}.lnk"


def startmenu_lnk_path() -> Optional[Path]:
    """开始菜单快捷方式路径。"""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / f"{APP_NAME}.lnk")


# ---------------------------------------------------------------- .lnk 写入器（纯 Python）

# Shell Link LinkFlags（MS-SHLLINK）
_LF_HAS_LINK_INFO = 0x00000002
_LF_HAS_RELATIVE_PATH = 0x00000008
_LF_HAS_WORKING_DIR = 0x00000010
_LF_HAS_ICON_LOCATION = 0x00000040
_LF_IS_UNICODE = 0x00000080

# LinkInfoFlags
_LIF_VOLUME_ID_AND_LOCAL_BASE_PATH = 0x00000001

# VolumeID DriveType
_DRIVE_FIXED = 3


def _lnk_header(flags: int) -> bytes:
    """构造 Shell Link 文件头（固定 76 字节）。"""
    # CLSID_ShellLink = {00021401-0000-0000-C000-000000000046}（小端字节序）
    clsid = bytes.fromhex("0114020000000000c000000000000046")
    return struct.pack(
        "<I16sIIQQQIIIHHII",
        0x0000004C,          # HeaderSize
        clsid,               # LinkCLSID
        flags,               # LinkFlags
        0,                   # FileAttributes
        0, 0, 0,             # CreationTime / AccessTime / WriteTime
        0,                   # FileSize
        0,                   # IconIndex
        1,                   # ShowCommand (SW_SHOWNORMAL)
        0,                   # HotKey
        0,                   # Reserved1
        0,                   # Reserved2
        0,                   # Reserved3
    )


def _lnk_link_info(local_base_path: str) -> bytes:
    """构造 LinkInfo 结构（含 VolumeID + LocalBasePath + 空 CommonPathSuffix）。"""
    volume_id = struct.pack(
        "<IIII",
        16,                              # VolumeIDSize（无卷标，固定 16 字节）
        _DRIVE_FIXED,                    # DriveType
        0,                               # DriveSerialNumber
        0,                               # VolumeLabelOffset（0 = 无卷标）
    )
    base_bytes = local_base_path.encode("utf-16-le") + b"\x00\x00"
    header_size = 28                     # 7 个 uint32
    volume_offset = header_size
    base_offset = volume_offset + len(volume_id)
    suffix_offset = base_offset + len(base_bytes)
    total_size = header_size + len(volume_id) + len(base_bytes) + 2
    header = struct.pack(
        "<IIIIIII",
        total_size,                      # LinkInfoSize
        header_size,                     # LinkInfoHeaderSize
        _LIF_VOLUME_ID_AND_LOCAL_BASE_PATH,
        volume_offset,                   # VolumeIDOffset
        base_offset,                     # LocalBasePathOffset
        0,                               # CommonNetworkRelativeLinkOffset
        suffix_offset,                   # CommonPathSuffixOffset
    )
    return header + volume_id + base_bytes + b"\x00\x00"


def _lnk_string(text: str) -> bytes:
    """StringData 条目：USHORT 字符数 + UTF-16LE 字符串（不含结尾 null）。"""
    return struct.pack("<H", len(text)) + text.encode("utf-16-le")


def create_shortcut(
    lnk_path: Path,
    target: Path,
    work_dir: Optional[Path] = None,
    icon: Optional[Path] = None,
) -> None:
    """用纯 Python 写 Windows 快捷方式（.lnk，Shell Link 二进制格式）。

    :param lnk_path: 生成的 .lnk 路径。
    :param target: 快捷方式指向的可执行文件。
    :param work_dir: 工作目录（默认取 target 所在目录）。
    :param icon: 图标位置（默认取 target 自身，即 exe 内嵌图标）。
    """
    target = Path(target)
    work_dir = Path(work_dir) if work_dir else target.parent
    icon = Path(icon) if icon else target

    flags = (_LF_HAS_LINK_INFO | _LF_HAS_RELATIVE_PATH
             | _LF_HAS_WORKING_DIR | _LF_HAS_ICON_LOCATION | _LF_IS_UNICODE)
    data = bytearray()
    data += _lnk_header(flags)
    data += _lnk_link_info(str(target))
    data += _lnk_string(target.name)            # RelativePath
    data += _lnk_string(str(work_dir))          # WorkingDir
    data += _lnk_string(str(icon))              # IconLocation
    data += struct.pack("<I", 0)                # TerminalBlock

    lnk_path = Path(lnk_path)
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    lnk_path.write_bytes(bytes(data))


# ---------------------------------------------------------------- 注册表（winreg 标准库）

def write_uninstall_registry(
    install_dir: Path,
    main_exe: Path,
    uninstall_exe: Path,
    icon: Path,
    size_kb: int,
) -> bool:
    """写 HKCU 卸载注册表项（控制面板「程序和功能」可见）。"""
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, UNINSTALL_REG_PATH, 0,
            winreg.KEY_ALL_ACCESS,
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(icon))
            winreg.SetValueEx(
                key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_exe}"')
            winreg.SetValueEx(
                key, "QuietUninstallString", 0, winreg.REG_SZ,
                f'"{uninstall_exe}" --silent')
            winreg.SetValueEx(
                key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
            winreg.SetValueEx(
                key, "EstimatedSize", 0, winreg.REG_DWORD, int(size_kb))
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- install_info.json

def write_install_info(
    install_dir: Path,
    main_exe: Path,
    uninstall_exe: Path,
    desktop_lnk: Optional[Path],
    startmenu_lnk: Optional[Path],
) -> None:
    """把安装信息写入安装目录，供卸载器读取。"""
    info = {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "install_dir": str(install_dir),
        "main_exe": str(main_exe),
        "uninstall_exe": str(uninstall_exe),
        "desktop_lnk": str(desktop_lnk) if desktop_lnk else None,
        "startmenu_lnk": str(startmenu_lnk) if startmenu_lnk else None,
        "installed_at": datetime.now().isoformat(timespec="seconds"),
    }
    (install_dir / INFO_FILENAME).write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 载荷复制（带字节进度）

def iter_payload_files(src_root: Path) -> List[Path]:
    """载荷内全部文件（按路径排序，保证复制顺序稳定）。"""
    return sorted(p for p in src_root.rglob("*") if p.is_file())


def payload_total_bytes(src_root: Path) -> int:
    """载荷总字节数。"""
    return sum(p.stat().st_size for p in iter_payload_files(src_root))


def dir_size_bytes(root: Path) -> int:
    """目录总字节数（用于 EstimatedSize）。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def _copy_big_file(
    src: Path, dst: Path, copied: int, total: int, rel: str,
    progress_cb: Callable[[int, int, str], None],
) -> int:
    """大文件分块复制，每块回调一次进度。"""
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(CHUNK_SIZE)
            if not chunk:
                break
            fdst.write(chunk)
            copied += len(chunk)
            progress_cb(copied, total, rel)
    shutil.copystat(src, dst)
    return copied


def copy_payload(
    src_root: Path,
    dst_root: Path,
    progress_cb: Callable[[int, int, str], None],
) -> None:
    """复制载荷到目标目录，按字节数回调进度。

    :param progress_cb: callback(已复制字节, 总字节, 当前文件相对路径)。
    """
    files = iter_payload_files(src_root)
    total = sum(f.stat().st_size for f in files)
    copied = 0
    for src in files:
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.stat().st_size >= BIG_FILE_THRESHOLD:
            copied = _copy_big_file(src, dst, copied, total, str(rel), progress_cb)
        else:
            shutil.copy2(src, dst)
            copied += src.stat().st_size
            progress_cb(copied, total, str(rel))


def format_size(num_bytes: int) -> str:
    """字节数格式化为人类可读字符串。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GB"


# ---------------------------------------------------------------- 安装核心流程

def perform_install(
    target_dir: Path,
    create_desktop: bool = True,
    create_startmenu: bool = True,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, object]:
    """执行安装：复制载荷 + 快捷方式 + install_info.json + 注册表项。

    :param target_dir: 安装目录（不存在则创建）。
    :param create_desktop: 是否创建桌面快捷方式。
    :param create_startmenu: 是否创建开始菜单快捷方式。
    :param progress_cb: 复制进度回调 (copied, total, current_file)。
    :return: 安装结果信息字典。
    :raises OSError: 复制或写入失败时抛出，由调用方提示用户。
    """
    progress_cb = progress_cb or (lambda _c, _t, _f: None)
    src = payload_dir()
    if not src.is_dir():
        raise FileNotFoundError(f"载荷目录不存在: {src}")

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    # 1. 复制载荷（主 exe / ms-playwright / uninstall.exe / app_icon.ico）
    copy_payload(src, target, progress_cb)

    main_exe = target / MAIN_EXE_NAME
    uninstall_exe = target / UNINSTALL_EXE_NAME
    icon = target / ICON_NAME

    # 2. 快捷方式（开始菜单默认创建；桌面由完成页勾选控制）。
    #    快捷方式创建失败（如目录被安全策略锁定）不阻断安装，仅跳过。
    desktop_lnk: Optional[Path] = None
    startmenu_lnk: Optional[Path] = None
    if create_desktop:
        desktop_lnk = desktop_lnk_path()
        if desktop_lnk:
            try:
                create_shortcut(desktop_lnk, target=main_exe,
                                work_dir=target, icon=main_exe)
            except OSError:
                desktop_lnk = None
    if create_startmenu:
        startmenu_lnk = startmenu_lnk_path()
        if startmenu_lnk:
            try:
                create_shortcut(startmenu_lnk, target=main_exe,
                                work_dir=target, icon=main_exe)
            except OSError:
                startmenu_lnk = None

    # 3. 安装信息（卸载器读取）
    write_install_info(target, main_exe, uninstall_exe,
                       desktop_lnk, startmenu_lnk)

    # 4. HKCU 卸载注册表项
    size_kb = dir_size_bytes(target) // 1024
    write_uninstall_registry(target, main_exe, uninstall_exe, icon, size_kb)

    return {
        "install_dir": target,
        "main_exe": main_exe,
        "uninstall_exe": uninstall_exe,
        "desktop_lnk": desktop_lnk,
        "startmenu_lnk": startmenu_lnk,
    }


def check_disk_space(target_dir: Path, required_bytes: int) -> bool:
    """目标磁盘剩余空间是否足够（预留 5% 余量）。"""
    try:
        free = shutil.disk_usage(Path(target_dir).anchor).free
        return free >= int(required_bytes * 1.05)
    except OSError:
        return True


# ---------------------------------------------------------------- 单实例保护

def ensure_single_instance() -> bool:
    """命名互斥体防止重复启动；返回 False 表示已有实例在运行。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        # 句柄故意不关闭：进程存活期间互斥体必须有效
        ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


# ---------------------------------------------------------------- 安装线程（GUI 用）

class InstallWorker(QThread):
    """后台执行安装，避免 1GB+ 载荷复制阻塞 GUI 线程。"""

    progress = Signal(int, int, str)   # copied, total, current file
    done = Signal(bool, str)           # ok, error message（刻意不复用 QThread.finished）
    cancelled = Signal()

    def __init__(self, target_dir: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._target = target_dir
        self._cancel_requested = False

    def request_cancel(self) -> None:
        """请求取消（在文件复制间隙检查，不会打断正在写的大文件）。"""
        self._cancel_requested = True

    def run(self) -> None:
        try:
            def _cb(copied: int, _total: int, current: str) -> None:
                if self._cancel_requested:
                    raise _InstallCancelled()
                self.progress.emit(copied, _total, current)

            perform_install(self._target, create_desktop=False,
                            create_startmenu=True, progress_cb=_cb)
            self.done.emit(True, "")
        except _InstallCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 需要把任何错误带回 GUI
            self.done.emit(False, str(exc))


class _InstallCancelled(Exception):
    """用户取消安装的内部信号异常。"""


# ---------------------------------------------------------------- GUI 安装向导

STYLESHEET = f"""
QWidget {{ font-family:'Microsoft YaHei UI','Microsoft YaHei',sans-serif;
           font-size:13px; color:{C_TEXT}; background:{C_BG}; }}
QLabel#h1 {{ font-size:23px; font-weight:bold; color:{C_TEXT}; background:transparent; }}
QLabel#h2 {{ font-size:15px; font-weight:bold; color:{C_TEXT}; background:transparent; }}
QLabel#sub {{ font-size:12px; color:{C_TEXT2}; background:transparent; }}
QLabel#desc {{ font-size:13px; color:{C_TEXT2}; background:transparent; }}
QLabel#ver {{ font-size:11px; color:{C_TEXT3}; background:transparent; }}
QLabel#logo {{ background:transparent; }}
QFrame#card {{ background:{C_CARD}; border:1px solid {C_BORDER}; border-radius:16px; }}
QLineEdit {{ background:{C_INPUT}; border:1px solid #2a2f3a; border-radius:11px;
             padding:9px 12px; color:{C_TEXT}; selection-background-color:{C_ACCENT}; }}
QLineEdit:focus {{ border-color:{C_ACCENT}; }}
QPushButton {{ background:{C_CARD2}; border:1px solid #2a2f3a; border-radius:11px;
               padding:9px 22px; color:{C_TEXT2}; min-width:88px; }}
QPushButton:hover {{ background:#20242e; color:{C_TEXT}; }}
QPushButton#primary {{ background:{GRADIENT}; border:none; color:#ffffff; font-weight:bold; }}
QPushButton#primary:hover {{ background:{GRADIENT}; color:#ffffff; }}
QPushButton#primary:disabled {{ background:#3a2029; color:#8a8f99; }}
QPushButton:disabled {{ background:#14161c; color:#5a606c; border-color:#22262f; }}
QProgressBar {{ background:{C_INPUT}; border:1px solid #2a2f3a; border-radius:10px;
                height:16px; text-align:center; color:{C_TEXT2}; }}
QProgressBar::chunk {{ background:{GRADIENT}; border-radius:8px; }}
QCheckBox {{ spacing:8px; color:#c3c9d4; background:transparent; }}
QCheckBox::indicator {{ width:18px; height:18px; border-radius:5px;
                        border:1.5px solid #363b48; background:{C_INPUT}; }}
QCheckBox::indicator:checked {{ background:{C_ACCENT}; border-color:{C_ACCENT}; }}
"""


class SetupWizard(QWidget):
    """安装向导主窗口（欢迎 / 目录 / 进度 / 完成 四页）。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} 安装程序")
        self.setFixedSize(640, 470)
        self.target_dir = default_install_dir()
        self.install_result: Dict[str, object] = {}
        self.worker: Optional[InstallWorker] = None

        self._build_ui()
        self.setStyleSheet(STYLESHEET)
        self._center_on_screen()

    # ---------------- UI 构建 ----------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(16)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome_page())   # index 0
        self.stack.addWidget(self._build_dir_page())       # index 1
        self.stack.addWidget(self._build_progress_page())  # index 2
        self.stack.addWidget(self._build_finish_page())    # index 3
        root.addWidget(self.stack, 1)

        # 底部导航按钮
        nav = QHBoxLayout()
        nav.setSpacing(10)
        self.btn_back = QPushButton("上一步")
        self.btn_back.clicked.connect(self._on_back)
        self.btn_next = QPushButton("下一步")
        self.btn_next.setObjectName("primary")
        self.btn_next.clicked.connect(self._on_next)
        nav.addStretch(1)
        nav.addWidget(self.btn_back)
        nav.addWidget(self.btn_next)
        root.addLayout(nav)
        self._refresh_nav()

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        # 顶部 logo + 标题
        top = QHBoxLayout()
        top.setSpacing(14)
        self.logo_label = QLabel()
        self.logo_label.setObjectName("logo")
        self.logo_label.setFixedSize(72, 72)
        pixmap = QPixmap(str(icon_path()))
        if not pixmap.isNull():
            self.logo_label.setPixmap(
                pixmap.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        top.addWidget(self.logo_label)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title_box.addWidget(self._make_label(
            APP_NAME, "h1"))
        title_box.addWidget(self._make_label(
            f"版本 {APP_VERSION}", "ver"))
        title_box.addStretch(1)
        top.addLayout(title_box)
        top.addStretch(1)
        layout.addLayout(top)

        # 简介卡片
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(10)
        card_layout.addWidget(self._make_label(APP_DESC, "h2"))
        desc = QLabel(
            "批量下载抖音 / 快手博主的全部作品，支持订阅自动更新。\n"
            "安装包自带浏览器运行环境，安装后即可直接使用，无需额外配置。")
        desc.setObjectName("desc")
        desc.setWordWrap(True)
        card_layout.addWidget(desc)
        layout.addWidget(card)

        layout.addStretch(1)
        return page

    def _build_dir_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        layout.addWidget(self._make_label("选择安装目录", "h2"))
        layout.addWidget(self._make_label(
            "软件将安装到以下目录（普通用户权限即可，无需管理员）。", "sub"))

        row = QHBoxLayout()
        row.setSpacing(10)
        self.dir_edit = QLineEdit(str(self.target_dir))
        self.dir_edit.textChanged.connect(self._on_dir_changed)
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setFixedWidth(96)
        self.btn_browse.clicked.connect(self._on_browse)
        row.addWidget(self.dir_edit, 1)
        row.addWidget(self.btn_browse)
        layout.addLayout(row)

        self.space_label = QLabel()
        self.space_label.setObjectName("sub")
        self.space_label.setWordWrap(True)
        layout.addWidget(self.space_label)
        self._update_space_label()

        layout.addStretch(1)
        return page

    def _build_progress_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        layout.addWidget(self._make_label("正在安装…", "h2"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.progress_file_label = QLabel("准备中…")
        self.progress_file_label.setObjectName("sub")
        self.progress_file_label.setWordWrap(True)
        layout.addWidget(self.progress_file_label)
        layout.addStretch(1)
        return page

    def _build_finish_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(14)

        layout.addWidget(self._make_label("安装完成", "h2"))
        layout.addWidget(self._make_label(
            f"{APP_NAME} 已成功安装到您的计算机。", "sub"))

        self.cb_desktop = QCheckBox("创建桌面快捷方式")
        self.cb_desktop.setChecked(True)
        self.cb_run = QCheckBox("立即运行")
        self.cb_run.setChecked(False)
        layout.addWidget(self.cb_desktop)
        layout.addWidget(self.cb_run)

        self.finish_path_label = QLabel()
        self.finish_path_label.setObjectName("sub")
        self.finish_path_label.setWordWrap(True)
        layout.addWidget(self.finish_path_label)
        layout.addStretch(1)
        return page

    def _make_label(self, text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setWordWrap(True)
        return label

    def _center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = self.frameGeometry()
        geometry.moveCenter(screen.availableGeometry().center())
        self.move(geometry.topLeft())

    # ---------------- 窗口关闭（安装中需确认取消） ----------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名约定
        """安装过程中关闭窗口时，先请求取消并等待工作线程收敛。"""
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "取消安装",
                "安装正在进行中，确定要取消安装吗？\n已复制的文件将保留在目标目录中。")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.request_cancel()
            # 等待工作线程退出（最多 10 秒，随后强制结束）
            if not self.worker.wait(10000):
                self.worker.terminate()
                self.worker.wait(2000)
        event.accept()

    # ---------------- 导航逻辑 ----------------

    def _refresh_nav(self) -> None:
        index = self.stack.currentIndex()
        self.btn_back.setVisible(index in (1,))
        if index == 0:
            self.btn_next.setText("下一步")
            self.btn_next.setEnabled(True)
        elif index == 1:
            self.btn_next.setText("安装")
            self.btn_next.setEnabled(True)
        elif index == 2:
            self.btn_next.setText("安装中…")
            self.btn_next.setEnabled(False)
        elif index == 3:
            self.btn_next.setText("完成")
            self.btn_next.setEnabled(True)

    def _on_dir_changed(self, _text: str) -> None:
        self.target_dir = Path(self.dir_edit.text().strip()
                               or str(default_install_dir()))
        self._update_space_label()

    def _update_space_label(self) -> None:
        try:
            required = payload_total_bytes(payload_dir())
        except OSError:
            required = 0
        needed = format_size(required) if required else "未知"
        try:
            free = shutil.disk_usage(Path(self.target_dir).anchor).free
            free_text = format_size(free)
        except OSError:
            free_text = "未知"
        self.space_label.setText(
            f"需要磁盘空间约 {needed}（含浏览器运行环境），当前可用 {free_text}。")

    def _on_browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择安装目录", self.dir_edit.text().strip()
            or str(default_install_dir()))
        if chosen:
            self.dir_edit.setText(chosen)

    def _on_back(self) -> None:
        index = self.stack.currentIndex()
        if index == 1:
            self.stack.setCurrentIndex(0)
            self._refresh_nav()

    def _on_next(self) -> None:
        index = self.stack.currentIndex()
        if index == 0:
            self.stack.setCurrentIndex(1)
            self._refresh_nav()
        elif index == 1:
            self._start_install()
        elif index == 3:
            self._finish()

    # ---------------- 安装 ----------------

    def _start_install(self) -> None:
        target = Path(self.dir_edit.text().strip()
                      or str(default_install_dir()))
        self.target_dir = target

        # 目录已存在且非空时提示（覆盖安装会合并文件）
        if target.is_dir() and any(target.iterdir()):
            answer = QMessageBox.question(
                self, "目录不为空",
                f"目录 {target} 已存在且包含文件。\n继续安装将与其合并，确定吗？")
            if answer != QMessageBox.Yes:
                return

        # 磁盘空间检查
        try:
            required = payload_total_bytes(payload_dir())
        except OSError:
            required = 0
        if required and not check_disk_space(target, required):
            QMessageBox.critical(
                self, "磁盘空间不足",
                f"目标磁盘剩余空间不足，需要约 {format_size(required)}。")
            return

        self.stack.setCurrentIndex(2)
        self._refresh_nav()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_file_label.setText("准备中…")

        self.worker = InstallWorker(target, parent=self)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_install_finished)
        self.worker.cancelled.connect(self._on_install_cancelled)
        self.worker.start()

    def _on_progress(self, copied: int, total: int, current: str) -> None:
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(copied)
            self.progress_bar.setFormat(
                f"%p%  ({format_size(copied)} / {format_size(total)})")
        self.progress_file_label.setText(f"正在复制：{current}")

    def _on_install_finished(self, ok: bool, error: str) -> None:
        if not ok:
            QMessageBox.critical(
                self, "安装失败",
                f"安装过程中出错：\n{error}\n\n请检查目标目录权限后重试。")
            self.stack.setCurrentIndex(1)
            self._refresh_nav()
            return
        self.install_result = {
            "install_dir": self.target_dir,
            "main_exe": self.target_dir / MAIN_EXE_NAME,
            "startmenu_lnk": startmenu_lnk_path(),
        }
        self.finish_path_label.setText(f"安装位置：{self.target_dir}")
        self.stack.setCurrentIndex(3)
        self._refresh_nav()

    def _on_install_cancelled(self) -> None:
        QMessageBox.information(
            self, "安装已取消",
            "安装已被取消，已复制的文件保留在目标目录中，可手动清理。")
        self.stack.setCurrentIndex(1)
        self._refresh_nav()

    # ---------------- 完成 ----------------

    def _finish(self) -> None:
        install_dir = Path(self.install_result.get("install_dir", self.target_dir))
        main_exe = Path(self.install_result.get("main_exe", install_dir / MAIN_EXE_NAME))

        # 桌面快捷方式按勾选创建，并同步 install_info.json
        if self.cb_desktop.isChecked():
            desktop_lnk = desktop_lnk_path()
            if desktop_lnk:
                try:
                    create_shortcut(desktop_lnk, target=main_exe,
                                    work_dir=install_dir, icon=main_exe)
                    write_install_info(
                        install_dir, main_exe, install_dir / UNINSTALL_EXE_NAME,
                        desktop_lnk, self.install_result.get("startmenu_lnk"))
                except OSError:
                    pass

        if self.cb_run.isChecked() and main_exe.is_file():
            try:
                os.startfile(str(main_exe))  # noqa: S606 - 启动用户刚安装的程序
            except OSError:
                pass

        self.close()


# ---------------------------------------------------------------- 静默安装

def silent_install(target_dir: Path) -> int:
    """--silent 模式：无 GUI 全自动安装。"""
    src = payload_dir()
    if not src.is_dir():
        print(f"[ERROR] 载荷目录不存在: {src}")
        return 1
    try:
        required = payload_total_bytes(src)
        if not check_disk_space(target_dir, required):
            print(f"[ERROR] 磁盘空间不足，需要约 {format_size(required)}")
            return 1
        result = perform_install(target_dir)
    except Exception as exc:  # noqa: BLE001 - CLI 模式打印任何错误
        print(f"[ERROR] 安装失败: {exc}")
        return 1
    print(f"[OK] 已安装到 {result['install_dir']}")
    print(f"[OK] 主程序: {result['main_exe']}")
    return 0


# ---------------------------------------------------------------- 入口

def main() -> int:
    """解析参数：``--silent <目标目录>`` 或默认 GUI 向导。"""
    parser = argparse.ArgumentParser(description=f"{APP_NAME}安装程序")
    parser.add_argument(
        "--silent", metavar="目标目录", default=None,
        help="无 GUI 全自动安装到指定目录（供 QA 自动化测试）")
    args = parser.parse_args()

    if args.silent:
        target = Path(args.silent).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[ERROR] 无法创建目标目录 {target}: {exc}")
            return 1
        return silent_install(target)

    # GUI 模式：单实例保护
    if not ensure_single_instance():
        app = QApplication(sys.argv)
        QMessageBox.warning(
            None, APP_NAME, "安装程序已经在运行中，请勿重复启动。")
        return 1

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    wizard = SetupWizard()
    icon = icon_path()
    if icon.is_file():
        wizard.setWindowIcon(QIcon(str(icon)))
    wizard.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
