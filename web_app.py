#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖快批量下载器 · 本地网页版（FastAPI + uvicorn，单文件内嵌前端）

定位：桌面版的「浏览器形态」——不重写下载引擎，复用桌面版同款子进程链路：

    下载（抖音）  spawn 抖快批量下载器.exe --cli  -c config_webui.yml -v
    下载（快手）  spawn 抖快批量下载器.exe --kdl  kuaishou_task.json
    登录（抖音）  spawn 抖快批量下载器.exe --login
    登录（快手）  spawn 抖快批量下载器.exe --klogin
    浏览器自检    spawn 抖快批量下载器.exe --browsertest

配置生成（config_webui.yml）、快手任务 JSON、gui_settings.json schema、
日志落盘（gui_download.log）均与桌面版 gui_app.py 保持一致，两个版本可以
共用同一份运行数据。

安全约束：
- 只绑定 127.0.0.1（本机回环），不暴露局域网；
- 单实例互斥体，防止重复启动抢占端口；
- 所有运行数据（config.yml / config_webui.yml / subscriptions.json /
  gui_settings.json / gui_download.log / Downloaded / .browser-profile）
  全部落在本 exe 所在目录，便携包解压即用、删除目录即卸载。

启动方式：
    抖快批量下载器-网页版.exe                # 默认 127.0.0.1:8765，就绪后自动打开系统浏览器
    抖快批量下载器-网页版.exe --port 9000    # 换端口
    抖快批量下载器-网页版.exe --no-browser   # 不自动开浏览器（无头 / QA 用）
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------- 路径与常量（与桌面版 gui_app.py 对齐） ----------------
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

DESKTOP_EXE = ROOT / "抖快批量下载器.exe"   # 下载 / 登录 worker（同目录的桌面版 exe）
BASE_CONFIG = ROOT / "config.yml"           # 基础配置（登录后含 Cookie）
WEB_CONFIG = ROOT / "config_webui.yml"      # 每次下载生成的抖音 CLI 配置
KDL_TASK = ROOT / "kuaishou_task.json"      # 快手下载任务参数
SETTINGS_FILE = ROOT / "gui_settings.json"  # 设置（与桌面版同 schema）
LOG_FILE = ROOT / "gui_download.log"        # 运行日志（桌面版同款落盘位置）
DEFAULT_SAVE_DIR = ROOT / "Downloaded"      # 默认保存目录

APP_VERSION = "1.0.0"
DEFAULT_PORT = 8765
LOG_MEMORY_LINES = 4000          # 内存日志环形缓冲大小
LOGIN_POLL_HINT = "登录进行中：请在弹出的浏览器窗口完成扫码/账密登录"

# Windows 上启动控制台子进程（taskkill）时防止闪黑窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# 已创建的 Job 对象句柄（进程生命周期内保活；父进程被杀时子进程一并被杀）
_JOB_HANDLES: List[int] = []


def _assign_to_kill_job(pid: int) -> None:
    """把子进程放进带 KILL_ON_JOB_CLOSE 的 Job 对象。

    正常停止走 kill_tree（显式杀进程树）；但如果网页版 exe 被直接强杀
    （比如用户手点控制台窗口的 X），finally / lifespan 来不及跑，下载 /
    登录子进程会变僵尸。Job 对象保证父进程消失时子进程被操作系统一并收掉。
    """
    if sys.platform != "win32" or pid <= 0:
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            h_job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(h_job)
            return
        h_proc = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not h_proc:
            kernel32.CloseHandle(h_job)
            return
        if not kernel32.AssignProcessToJobObject(h_job, h_proc):
            kernel32.CloseHandle(h_proc)
            kernel32.CloseHandle(h_job)
            return
        kernel32.CloseHandle(h_proc)
        _JOB_HANDLES.append(h_job)  # 保活句柄，父进程退出时 Job 关闭
    except Exception:
        pass

# ---------------- 配色（与桌面版「抖快 · 冷炭 2.0」一致） ----------------
C_BG = "#0e0f13"
C_CARD = "#16181f"
C_BORDER = "#262a34"
C_INPUT = "#12141a"
C_TEXT = "#eef0f4"
C_TEXT2 = "#9aa1ac"
C_TEXT3 = "#6b7280"
C_ACCENT = "#fe2c55"   # 抖音红
C_ORANGE = "#ff7a00"   # 快手橙
C_GREEN = "#34d399"
C_RED = "#f87171"
GRADIENT = "linear-gradient(90deg,#fe2c55,#ff7a00)"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "save_dir": str(DEFAULT_SAVE_DIR),
    "use_login_cookies": True,   # 回补浏览器带登录 cookie（否则只拿到前 ~20 个作品）
    "headless": False,           # 采集浏览器是否隐藏窗口
    "verbose": True,             # CLI 详细日志
    "live_frames": True,         # 下载过程中实时生成第一帧截图（桌面版同款开关）
    "thread": 5,                 # 下载并发数
    "ks_date_folders": True,     # 快手文件名前加发布日期
    "auto_renumber": True,       # 下载完成后按发布日期重排编号
    "auto_subscribe": True,      # 启动后当日自动下载已订阅博主
}

VALID_TYPES = {"video", "cover", "frame", "music", "avatar", "json"}


# ---------------- 设置持久化（schema 与桌面版一致） ----------------
def load_settings() -> Dict[str, Any]:
    """读取 gui_settings.json；缺省值用 DEFAULT_SETTINGS，坏文件静默回退。"""
    data = dict(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
    except Exception:
        pass
    return data


def save_settings(data: Dict[str, Any]) -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------- 配置生成（逻辑与 gui_app.build_config 一致） ----------------
def load_base_config() -> Dict[str, Any]:
    """读取基础 config.yml（登录态从这里来）；不存在时返回空 dict。"""
    import yaml
    if BASE_CONFIG.exists():
        try:
            data = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def build_config(urls: List[str], save_dir: str, types: List[str],
                 number: int, start_time: str, end_time: str,
                 settings: Optional[Dict[str, Any]] = None) -> Path:
    """生成抖音 CLI 配置 config_webui.yml（桌面版 build_config 的等价实现）。"""
    import yaml
    settings = settings or load_settings()
    cfg = load_base_config()
    cfg["link"] = list(urls)
    save_dir = str(save_dir).rstrip("/\\")
    cfg["path"] = save_dir + "/"
    cfg["mode"] = ["post"]
    cfg["group_by_mode"] = False
    cfg["folderstyle"] = False
    cfg["video"] = "video" in types
    cfg["cover"] = "cover" in types
    cfg["music"] = "music" in types
    cfg["avatar"] = "avatar" in types
    cfg["json"] = "json" in types
    number_cfg = cfg.get("number") or {}
    number_cfg["post"] = int(number)
    cfg["number"] = number_cfg
    cfg["start_time"] = start_time or ""
    cfg["end_time"] = end_time or ""
    increase = cfg.get("increase") or {}
    increase["post"] = True
    cfg["increase"] = increase
    cfg["thread"] = int(settings.get("thread") or 5)
    bf = cfg.get("browser_fallback") or {}
    bf["use_login_cookies"] = bool(settings.get("use_login_cookies", True))
    bf["headless"] = bool(settings.get("headless", False))
    cfg["browser_fallback"] = bf
    WEB_CONFIG.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return WEB_CONFIG


def cookie_status() -> Dict[str, Any]:
    """抖音登录态：config.yml 的 cookies.sessionid / sid_guard。"""
    cookies = load_base_config().get("cookies") or {}
    session = str(cookies.get("sessionid") or cookies.get("sid_guard") or "")
    ms = str(cookies.get("msToken") or "")
    ok = bool(session) and not session.startswith("YOUR_")
    return {"ok": ok, "msToken_preview": (ms[:8] + "…") if ok and ms else ""}


def kuaishou_cookie_status() -> Dict[str, Any]:
    """快手登录态：config.yml 的 kuaishou_cookies 键。"""
    ok = BASE_CONFIG.exists() and bool(load_base_config().get("kuaishou_cookies"))
    return {"ok": ok}


# ---------------- 小工具（与桌面版同名实现保持一致） ----------------
def kill_tree(pid: int) -> None:
    """按进程树强杀（taskkill /T /F），失败静默。"""
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=20, creationflags=_NO_WINDOW,
        )
    except Exception:
        pass


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


_DIR_ILLEGAL = re.compile(r'[\\/:*?"<>\r\n\t]+')


def _dir_name_sanitize(name: str, fallback: str = "未知作者") -> str:
    """作者文件夹名清洗（与 kuaishou_download_worker.sanitize_name 一致）。"""
    s = _DIR_ILLEGAL.sub(" ", str(name or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return (s or fallback)[:80]


def _classify_file(p: Path) -> str:
    """把文件归入统计类别（每个文件只落一个桶）。"""
    name = p.name.lower()
    ext = p.suffix.lower()
    if name.endswith("_frame1.jpg"):
        return "frame"
    if name.endswith("_cover.jpg"):
        return "cover"
    if name.endswith("_avatar.jpg"):
        return "avatar"
    if name.endswith("_music.mp3"):
        return "music"
    if ext == ".json":
        return "json"
    if ext in {".mp4", ".mov", ".mkv", ".flv", ".webm", ".m4v"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "image"
    return "other"


CAT_LABEL = {
    "video": "视频", "frame": "第一帧截图", "cover": "封面", "music": "音乐",
    "avatar": "头像", "json": "JSON", "image": "图片", "other": "其他",
}


def dir_stats(save_dir: Path) -> Dict[str, Any]:
    """统计保存目录：视频数 / 截图数 / 作者数 / 总大小。"""
    videos = frames = 0
    size = 0
    authors = set()
    if save_dir.exists():
        for p in save_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                size += p.stat().st_size
            except OSError:
                pass
            if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".flv", ".webm"}:
                videos += 1
                try:
                    authors.add(p.relative_to(save_dir).parts[0])
                except Exception:
                    pass
            elif p.name.endswith("_frame1.jpg"):
                frames += 1
    return {"videos": videos, "frames": frames, "authors": len(authors), "size": size}


def list_authors(save_dir: Path) -> List[Dict[str, Any]]:
    """保存目录一级作者文件夹列表（按修改时间倒序）。"""
    out: List[Dict[str, Any]] = []
    if not save_dir.is_dir():
        return out
    for d in sorted(save_dir.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        files = 0
        size = 0
        mtime = 0.0
        try:
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                files += 1
                st = p.stat()
                size += st.st_size
                mtime = max(mtime, st.st_mtime)
        except OSError:
            continue
        out.append({
            "name": d.name, "files": files, "size": size,
            "size_text": human_size(size),
            "mtime": mtime,
            "mtime_text": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
        })
    out.sort(key=lambda x: -x["mtime"])
    return out


def list_author_files(save_dir: Path, author: str) -> List[Dict[str, Any]]:
    """某作者目录下的文件列表（最多 500 条，按修改时间倒序）。"""
    target = save_dir / _dir_name_sanitize(author)
    if not target.is_dir():
        return []
    files: List[Dict[str, Any]] = []
    try:
        for p in target.rglob("*"):
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({
                "name": p.name,
                "category": _classify_file(p),
                "category_label": CAT_LABEL.get(_classify_file(p), "其他"),
                "size": st.st_size,
                "size_text": human_size(st.st_size),
                "mtime": st.st_mtime,
                "mtime_text": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    except OSError:
        pass
    files.sort(key=lambda x: -x["mtime"])
    return files[:500]


def count_author_files(save_dir: str, nickname: str) -> int:
    """统计某博主已下载文件数（视频+截图），用于订阅新增量对比。"""
    base = Path(save_dir) if save_dir else None
    if not base or not base.exists():
        return 0
    dirs = [base]
    if nickname:
        nick_dir = base / _dir_name_sanitize(nickname)
        if nick_dir.is_dir():
            dirs = [nick_dir]
    n = 0
    for d in dirs:
        try:
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() in {".mp4", ".mov", ".mkv", ".flv", ".webm"} or p.name.endswith("_frame1.jpg"):
                    n += 1
        except OSError:
            continue
    return n


def lookup_author_nickname(sec_uid: str) -> str:
    """从下载数据库反查抖音博主昵称（查不到返回空串）。"""
    if not sec_uid:
        return ""
    import sqlite3
    db_path = ROOT / "dy_downloader.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    except Exception:
        return ""
    try:
        row = conn.execute(
            "SELECT author_name FROM aweme "
            "WHERE author_sec_uid = ? AND author_name IS NOT NULL AND author_name != '' "
            "ORDER BY download_time DESC LIMIT 1",
            (sec_uid,),
        ).fetchone()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""
    finally:
        conn.close()


# ---------------- 订阅 ----------------
import subscription_store  # noqa: E402  （数据落在 exe 同级目录，与桌面版共用）


# ---------------- 进程管理器：下载 / 登录 / 自检 ----------------
class Runner:
    """子进程执行器：拉起身旁目录的桌面版 exe，逐行转发日志到内存 + gui_download.log。

    桌面版的下载/登录/快手链路全部是「GUI 起子进程」模式，这里原样复用：
    - 同一时刻只允许一个下载类任务（dy / ks / 自检），登录浏览器可并行；
    - 快手下载结束后若有排队的抖音 argv（混合链接场景）会链式继续；
    - 订阅支持串行队列（立即全部下载 / 启动自动订阅）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.login_proc: Optional[subprocess.Popen] = None
        self.login_kind: str = ""
        self.mode: str = "idle"           # idle / dy / ks / login / diag
        self.started_at: float = 0.0
        self.last_exit: Optional[int] = None
        self.last_exit_at: float = 0.0
        self.pending_dy_argv: Optional[List[str]] = None
        self.sub_queue: List[Dict[str, Any]] = []
        self._current_sub: Optional[Dict[str, Any]] = None
        self._gen: int = 0                # 任务代号：stop/新任务会自增，用于作废旧 reader 的链式动作
        self.log_lines: "collections.deque[str]" = collections.deque(maxlen=LOG_MEMORY_LINES)
        self.log_total: int = 0

    # ---------- 日志 ----------
    def _log(self, text: str) -> None:
        with self._log_lock:
            self.log_lines.append(text)
            self.log_total += 1
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        except Exception:
            pass

    def tail(self, offset: int) -> Tuple[List[str], int]:
        """返回全局序号 >= offset 的日志行与最新序号（断线重连会丢被挤掉的行）。"""
        with self._log_lock:
            total = self.log_total
            lines = list(self.log_lines)
        if offset >= total:
            return [], total
        start = max(0, offset - (total - len(lines)))
        return lines[start:], total

    # ---------- 查询 ----------
    def is_downloading(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def login_running(self) -> bool:
        with self._lock:
            return self.login_proc is not None and self.login_proc.poll() is None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            pid = self.proc.pid if self.proc else 0
            return {
                "running": running,
                "mode": self.mode if running else "idle",
                "pid": pid if running else 0,
                "started_at": self.started_at,
                "last_exit": self.last_exit,
                "last_exit_at": self.last_exit_at,
                "queue_len": len(self.sub_queue),
                "login_running": self.login_proc is not None and self.login_proc.poll() is None,
                "login_kind": self.login_kind if (self.login_proc and self.login_proc.poll() is None) else "",
                "log_total": self.log_total,
            }

    # ---------- 进程拉取 ----------
    def _popen(self, argv: List[str]) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=_NO_WINDOW,
        )
        _assign_to_kill_job(proc.pid)
        return proc

    def _spawn_locked(self, argv: List[str], mode: str) -> None:
        self._log(f"$ {' '.join(str(a) for a in argv)}")
        try:
            self.proc = self._popen(argv)
        except Exception as e:
            self._log(f"[ERROR] 进程启动失败: {e}")
            self.proc = None
            self.mode = "idle"
            self.last_exit = -1
            self.last_exit_at = time.time()
            return
        self.mode = mode
        self.started_at = time.time()
        self.last_exit = None
        gen = self._gen
        t = threading.Thread(target=self._reader_loop, args=(self.proc, mode, gen), daemon=True)
        t.start()

    def _reader_loop(self, proc: subprocess.Popen, mode: str, gen: int) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    self._log(line)
        except Exception:
            pass
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            code = proc.wait()
            self._on_proc_exit(code, mode, gen)

    def _on_proc_exit(self, code: int, mode: str, gen: int) -> None:
        with self._lock:
            self.last_exit = code
            self.last_exit_at = time.time()
            # 任务代号已变（用户停止 / 发起了新任务）：这个 reader 的链式动作全部作废
            if gen != self._gen:
                self._log(f"===== 进程结束 (exit={code})，后续动作已取消 =====")
                self.mode = "idle"
                return
            # 订阅下载收尾：记录今日已跑 + 新增数
            if self._current_sub is not None:
                sub = self._current_sub
                self._current_sub = None
                try:
                    new = 0
                    if code == 0:
                        after = count_author_files(sub.get("save_dir") or "", sub.get("nickname") or "")
                        new = max(0, after - int(sub.get("_before_files") or 0))
                    subscription_store.mark_ran(
                        sub.get("sec_uid", ""), time.strftime("%Y-%m-%d"), new
                    )
                except Exception:
                    pass
            # 混合链接：快手跑完链式继续抖音
            if mode == "ks" and self.pending_dy_argv:
                argv = self.pending_dy_argv
                self.pending_dy_argv = None
                self._log("===== 继续下载抖音链接 =====")
                self._spawn_locked(argv, "dy")
                return
            # 订阅队列：串行跑下一个
            if self.sub_queue:
                nxt = self.sub_queue.pop(0)
                nick = nxt.get("nickname") or (nxt.get("sec_uid", "")[:12] + "…")
                self._log(f"[订阅队列] 下一个：{nick}")
                self._start_sub_locked(nxt)
                return
            self.mode = "idle"
            self._log(f"===== 进程结束 (exit={code}) =====")

    # ---------- 下载 ----------
    def start_download(self, urls: List[str], save_dir: str, types: List[str],
                       number: int, start_time: str, end_time: str,
                       settings: Dict[str, Any],
                       sub: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """启动一次下载（抖音走 CLI，快手走 --kdl worker，混合则快手先抖音后）。"""
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            return {"ok": False, "error": "请先粘贴博主主页链接"}
        if not types:
            types = ["video"]
        bad = [t for t in types if t not in VALID_TYPES]
        if bad:
            return {"ok": False, "error": f"不支持的下载类型: {', '.join(bad)}"}
        for d in (start_time, end_time):
            if d and (len(d) != 10 or d[4] != "-" or d[7] != "-"):
                return {"ok": False, "error": "日期格式应为 YYYY-MM-DD，或留空"}
        if not DESKTOP_EXE.exists():
            return {"ok": False, "error": f"未找到同目录的 {DESKTOP_EXE.name}，无法启动下载"}

        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "error": "已有下载任务正在进行，请先停止"}
            if self.sub_queue:
                return {"ok": False, "error": "订阅队列仍在进行，请先停止或等待完成"}
            self._gen += 1  # 作废任何在途 reader 的链式动作
            save_dir = (save_dir or str(DEFAULT_SAVE_DIR)).strip() or str(DEFAULT_SAVE_DIR)
            try:
                Path(save_dir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {"ok": False, "error": f"保存目录创建失败: {e}"}
            ks_urls = [u for u in urls if "kuaishou.com" in u]
            dy_urls = [u for u in urls if "kuaishou.com" not in u]
            self.sub_queue = []
            self.pending_dy_argv = None
            self._current_sub = dict(sub) if sub else None
            if self._current_sub is not None:
                self._current_sub["_before_files"] = count_author_files(
                    save_dir, self._current_sub.get("nickname") or ""
                )
            if ks_urls:
                task = {
                    "urls": ks_urls,
                    "save_dir": save_dir,
                    "types": types,
                    "number": int(number),
                    "start_time": start_time or "",
                    "end_time": end_time or "",
                    "date_folders": bool(settings.get("ks_date_folders", True)),
                }
                if sub and sub.get("nickname"):
                    task["author_name"] = sub["nickname"]
                try:
                    KDL_TASK.write_text(
                        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception as e:
                    self._current_sub = None
                    return {"ok": False, "error": f"快手任务文件写入失败: {e}"}
                if dy_urls:
                    try:
                        cfg = build_config(dy_urls, save_dir, types, number,
                                           start_time, end_time, settings)
                        self.pending_dy_argv = [str(DESKTOP_EXE), "--cli", "-c", str(cfg)]
                        if settings.get("verbose", True):
                            self.pending_dy_argv.append("-v")
                    except Exception as e:
                        self.pending_dy_argv = None
                        self._log(f"[WARN] 抖音配置生成失败（快手仍将继续）: {e}")
                self._log(f"===== 开始快手下载：{len(ks_urls)} 个链接 =====")
                for u in ks_urls:
                    self._log(f"  - {u}")
                if self.pending_dy_argv:
                    self._log(f"（快手完成后将继续下载 {len(dy_urls)} 个抖音链接）")
                self._spawn_locked([str(DESKTOP_EXE), "--kdl", str(KDL_TASK)], "ks")
                return {"ok": True, "mode": "ks", "urls": len(ks_urls) + len(dy_urls)}

            # 纯抖音
            try:
                cfg = build_config(dy_urls, save_dir, types, number,
                                   start_time, end_time, settings)
            except Exception as e:
                self._current_sub = None
                return {"ok": False, "error": f"配置生成失败: {e}"}
            argv = [str(DESKTOP_EXE), "--cli", "-c", str(cfg)]
            if settings.get("verbose", True):
                argv.append("-v")
            self._log(f"===== 开始下载：{len(dy_urls)} 个链接 =====")
            for u in dy_urls:
                self._log(f"  - {u}")
            self._log(
                f"保存目录: {save_dir} | 类型: {', '.join(types)} | 数量: {number or '全部'} | "
                f"登录态采集: {'开' if settings.get('use_login_cookies') else '关'}"
            )
            self._spawn_locked(argv, "dy")
            return {"ok": True, "mode": "dy", "urls": len(dy_urls)}

    def _start_sub_locked(self, sub: Dict[str, Any]) -> None:
        settings = load_settings()
        self.start_download(
            [sub.get("url") or ""],
            sub.get("save_dir") or settings.get("save_dir") or str(DEFAULT_SAVE_DIR),
            sub.get("types") or ["video"],
            0, "", "", settings, sub=sub,
        )

    def run_sub(self, sec_uid: str) -> Dict[str, Any]:
        sub = subscription_store.find(sec_uid)
        if sub is None:
            return {"ok": False, "error": "订阅不存在（可能已被删除）"}
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "error": "已有下载任务正在进行，请先停止"}
            nick = sub.get("nickname") or (sec_uid[:12] + "…")
            self._log(f"===== [订阅] 开始下载：{nick} =====")
            self._start_sub_locked(sub)
            return {"ok": True}

    def run_all_subs(self) -> Dict[str, Any]:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "error": "已有下载任务正在进行，请先停止"}
            subs = subscription_store.load()
            if not subs:
                return {"ok": False, "error": "还没有订阅任何博主"}
            self.sub_queue = list(subs)
            first = self.sub_queue.pop(0)
            nick = first.get("nickname") or (first.get("sec_uid", "")[:12] + "…")
            self._log(f"[订阅队列] 共 {len(subs)} 位博主，从 {nick} 开始")
            self._start_sub_locked(first)
            return {"ok": True, "count": len(subs)}

    def auto_subscribe_tick(self) -> None:
        """启动 10 秒后：当日没跑过的订阅排进队列（桌面版同款行为）。"""
        try:
            time.sleep(10)
            settings = load_settings()
            if not settings.get("auto_subscribe", True):
                return
            with self._lock:
                if self.proc is not None and self.proc.poll() is None:
                    return  # 下载中，顺延到下次启动
                if self.sub_queue:
                    return
                today = time.strftime("%Y-%m-%d")
                pending = subscription_store.pending_today(today)
                if not pending:
                    return
                self.sub_queue = list(pending)
                first = self.sub_queue.pop(0)
                nick = first.get("nickname") or (first.get("sec_uid", "")[:12] + "…")
                self._log(f"[自动订阅] 今日待下载 {len(pending)} 位博主，从 {nick} 开始")
                self._start_sub_locked(first)
        except Exception:
            pass

    # ---------- 登录 ----------
    def start_login(self, kind: str) -> Dict[str, Any]:
        if kind not in ("douyin", "kuaishou"):
            return {"ok": False, "error": "不支持的登录平台"}
        if not DESKTOP_EXE.exists():
            return {"ok": False, "error": f"未找到同目录的 {DESKTOP_EXE.name}，无法启动登录"}
        with self._lock:
            if self.login_proc is not None and self.login_proc.poll() is None:
                return {"ok": False, "error": "登录窗口已打开，请在浏览器中完成登录"}
            argv = [str(DESKTOP_EXE), "--login" if kind == "douyin" else "--klogin"]
            self._log(f"===== 正在打开登录浏览器，请完成{'抖音' if kind == 'douyin' else '快手'}登录 =====")
            try:
                self.login_proc = self._popen(argv)
            except Exception as e:
                self.login_proc = None
                self._log(f"[ERROR] 登录进程启动失败: {e}")
                return {"ok": False, "error": f"登录进程启动失败: {e}"}
            self.login_kind = kind
            t = threading.Thread(target=self._login_reader_loop, daemon=True)
            t.start()
            return {"ok": True}

    def _login_reader_loop(self) -> None:
        proc = self.login_proc
        if proc is None:
            return
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    self._log(line)
        except Exception:
            pass
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            code = proc.wait()
            self._log(f"===== 登录流程结束 (code={code}) =====")
            with self._lock:
                self.login_kind = ""

    # ---------- 自检 ----------
    def start_browsertest(self) -> Dict[str, Any]:
        """浏览器启动自检（headless 打开 about:blank，不访问抖音）。"""
        if not DESKTOP_EXE.exists():
            return {"ok": False, "error": f"未找到同目录的 {DESKTOP_EXE.name}"}
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "error": "已有任务正在进行，请先停止"}
            self._log("===== 浏览器启动自检（--browsertest） =====")
            self._spawn_locked([str(DESKTOP_EXE), "--browsertest"], "diag")
            return {"ok": True}

    # ---------- 停止 ----------
    def stop_all(self) -> Dict[str, Any]:
        with self._lock:
            self._gen += 1  # 作废在途 reader 的链式/队列动作
            killed = False
            for proc in (self.proc, self.login_proc):
                try:
                    if proc is not None and proc.poll() is None:
                        kill_tree(proc.pid)
                        killed = True
                except Exception:
                    pass
            self.sub_queue = []
            self.pending_dy_argv = None
            self._current_sub = None
            if killed:
                self._log("===== 已手动停止 =====")
            return {"ok": True, "killed": killed}

    def shutdown(self) -> None:
        self.stop_all()


RUNNER = Runner()


# ---------------- 前端（单页 HTML，深色风格与桌面版一致） ----------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖快批量下载器 · 网页版</title>
<style>
  :root{
    --bg:#0e0f13; --sidebar:#0b0c10; --card:#16181f; --card2:#1b1e27;
    --border:#262a34; --input:#12141a; --text:#eef0f4; --text2:#9aa1ac; --text3:#6b7280;
    --accent:#fe2c55; --orange:#ff7a00; --green:#34d399; --red:#f87171;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Microsoft YaHei UI','Microsoft YaHei','PingFang SC',sans-serif;font-size:13px;min-height:100vh}
  /* 顶栏 */
  .topbar{background:var(--sidebar);border-bottom:1px solid #1c1f27;height:64px;display:flex;align-items:center;padding:0 22px;gap:16px;position:sticky;top:0;z-index:10}
  .logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,#fe2c55,#ff7a00);display:flex;align-items:center;justify-content:center;color:#fff;font-size:19px;font-weight:bold;flex:none}
  .brand{display:flex;flex-direction:column;gap:1px}
  .brand b{font-size:15px}
  .brand span{font-size:8px;color:var(--text3);letter-spacing:2px}
  nav{display:flex;gap:4px;margin-left:14px;flex-wrap:wrap}
  nav button{background:transparent;border:none;border-radius:12px;padding:8px 17px;color:var(--text2);font-size:13px;cursor:pointer}
  nav button:hover{background:var(--card2);color:var(--text)}
  nav button.on{background:linear-gradient(90deg,#fe2c55,#ff7a00);color:#fff;font-weight:bold}
  .spacer{flex:1}
  .pill{display:flex;align-items:center;gap:7px;background:var(--card2);border:1px solid #2a2f3a;border-radius:15px;padding:7px 14px;cursor:pointer;user-select:none}
  .pill:hover{background:#20242e;border-color:#3a4150}
  .pill .dot{font-size:10px;color:var(--red)}
  .pill .dot.ok{color:var(--green)}
  .pill b{font-size:12px}
  .pill i{font-style:normal;font-size:11px;color:var(--text2)}
  /* 页面 */
  main{max-width:1180px;margin:0 auto;padding:26px 34px 60px}
  .page{display:none}
  .page.on{display:block}
  .eyebrow{font-size:11px;color:var(--orange);letter-spacing:3px;font-weight:bold;margin-bottom:6px}
  h1{font-size:24px;margin-bottom:4px}
  h1 em{font-style:normal;color:var(--accent)}
  .sub{font-size:12px;color:var(--text2);margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:18px 20px;margin-bottom:14px}
  .card h2{font-size:13px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
  .card h2 .num{color:var(--accent)}
  .grid2{display:grid;grid-template-columns:5fr 4fr;gap:14px}
  @media (max-width:900px){.grid2{grid-template-columns:1fr}}
  textarea,input[type=text],input[type=number],input[type=date]{width:100%;background:var(--input);border:1px solid #2a2f3a;border-radius:11px;padding:9px 12px;color:var(--text);font-size:13px;font-family:inherit}
  textarea{height:110px;resize:vertical;line-height:1.6}
  textarea:focus,input:focus{outline:none;border-color:var(--accent)}
  .hint{font-size:11px;color:var(--text3);margin-top:8px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .row .grow{flex:1;min-width:200px}
  .fg{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
  .fg label{font-size:12px;color:var(--text2)}
  .chips{display:flex;gap:8px;flex-wrap:wrap}
  .chip{background:var(--card2);border:1px solid #2a2f3a;border-radius:15px;padding:7px 16px;color:#c3c9d4;font-size:12px;cursor:pointer;user-select:none}
  .chip:hover{border-color:#3a4150;color:var(--text)}
  .chip.on{background:#2a1220;border:1px solid var(--accent);color:#ffd3dd;font-weight:bold}
  .btn{border:none;border-radius:11px;padding:9px 18px;font-size:13px;cursor:pointer;background:var(--card2);color:#b7bdc7;border:1px solid #2a2f3a}
  .btn:hover{background:#22262f;border-color:#3a4150}
  .btn:disabled{background:#15171d;color:#5a616c;border-color:#21242d;cursor:not-allowed}
  .btn.primary{background:linear-gradient(90deg,#fe2c55,#ff7a00);color:#fff;font-weight:bold;font-size:15px;border-radius:13px;padding:13px 34px;border:none}
  .btn.primary:hover{filter:brightness(1.1)}
  .btn.primary:disabled{background:#23262e;color:#5b616d}
  .btn.danger{background:#2a1216;color:var(--red);border:1px solid #4b1f27}
  .btn.danger:hover{background:#35161c}
  .btn.danger:disabled{background:#15171d;color:#5a616c;border-color:#21242d}
  .btn.sm{padding:5px 12px;font-size:12px;border-radius:9px}
  .badge{background:var(--card2);border:1px solid #2a2f3a;border-radius:11px;padding:3px 10px;font-size:11px;color:var(--text2)}
  /* 命令栏 */
  .cmdbar{background:#101218;border:1px solid #1c1f27;border-radius:16px;padding:14px 20px;display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap}
  .cmdbar .st{font-size:12px;color:var(--text2);margin-left:auto}
  /* 日志 */
  #logbox{background:#0a0b0f;border:1px solid #1a1d24;border-radius:12px;color:#9df5c4;font-family:Consolas,'Courier New',monospace;font-size:12px;padding:12px;height:520px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;line-height:1.55}
  /* 磁贴 */
  .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
  @media (max-width:900px){.tiles{grid-template-columns:repeat(2,1fr)}}
  .tile{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px 18px}
  .tile b{display:block;font-size:22px;margin-bottom:3px}
  .tile span{font-size:11px;color:var(--text3)}
  /* 表格 */
  table{width:100%;border-collapse:collapse;font-size:12px}
  th{color:var(--text3);font-weight:normal;text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);font-size:11px}
  td{padding:8px 10px;border-bottom:1px solid #1c1f27;color:#c3c9d4}
  tr:hover td{background:#1a1d25}
  .tag{display:inline-block;border-radius:9px;padding:2px 8px;font-size:10px;font-weight:bold}
  .tag.dy{background:#2a1220;color:#ffd3dd}
  .tag.ks{background:#2b1c0e;color:#ffe0c2}
  .empty{color:var(--text3);font-size:12px;padding:18px;text-align:center}
  /* 订阅卡 */
  .subs{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px;margin-top:12px}
  .subcard{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;display:flex;gap:12px;align-items:center}
  .subcard:hover{border-color:#343a46}
  .avatar{width:44px;height:44px;border-radius:10px;flex:none;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:bold;font-size:17px}
  .subcard .info{flex:1;min-width:0}
  .subcard .info b{display:block;font-size:13px;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .subcard .info span{font-size:11px;color:var(--text3);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .subcard .ops{display:flex;flex-direction:column;gap:6px}
  /* 复选 */
  label.ck{display:flex;gap:9px;align-items:center;color:#c3c9d4;cursor:pointer;padding:4px 0}
  label.ck input{accent-color:var(--accent);width:16px;height:16px}
  /* toast */
  #toast{position:fixed;left:50%;bottom:30px;transform:translateX(-50%);background:#1e212a;border:1px solid #343947;color:var(--text);border-radius:11px;padding:10px 18px;font-size:12px;display:none;z-index:99;max-width:70vw;box-shadow:0 8px 30px #0008}
  .foot{color:var(--text3);font-size:11px;text-align:center;padding:18px 0 26px}
  .mono{font-family:Consolas,monospace}
</style>
</head>
<body>
<header class="topbar">
  <div class="logo">↓</div>
  <div class="brand"><b>抖快批量下载器</b><span>WEB · LOCAL</span></div>
  <nav id="nav">
    <button data-page="download" class="on">链接下载</button>
    <button data-page="log">运行日志</button>
    <button data-page="subs">订阅管理</button>
    <button data-page="files">作品档案</button>
    <button data-page="settings">设置</button>
  </nav>
  <div class="spacer"></div>
  <div class="pill" id="pill-dy" title="点击登录抖音（与下载共用浏览器登录态）">
    <span class="dot" id="dot-dy">●</span><b>抖音</b><i id="st-dy">未登录</i>
  </div>
  <div class="pill" id="pill-ks" title="点击登录快手">
    <span class="dot" id="dot-ks">●</span><b>快手</b><i id="st-ks">未登录</i>
  </div>
</header>

<main>
  <!-- 链接下载 -->
  <section class="page on" id="page-download">
    <div class="eyebrow">BATCH DOWNLOAD</div>
    <h1>粘贴链接，<em>一键批量下载。</em></h1>
    <div class="sub">支持抖音 / 快手作者主页链接 · 自动跳过已保存作品 · 本地运行，数据只存在本机</div>
    <div class="grid2">
      <div>
        <div class="card">
          <h2><span class="num">①</span>博主主页链接 <span class="badge" id="url-count">已识别 0 个链接</span></h2>
          <textarea id="urls" placeholder="https://www.douyin.com/user/MS4wLjABAAAAxxxxxxxx&#10;https://www.kuaishou.com/profile/xxxxxxxxx&#10;（从浏览器地址栏复制博主主页地址，一行一个，可批量；抖音/快手可混合）"></textarea>
          <div class="hint">支持短链 / 长链；混合粘贴时按平台依次下载（快手先、抖音后）。开始下载会弹出浏览器采集作品列表，遇验证码请手动完成。</div>
        </div>
        <div class="card">
          <h2><span class="num">②</span>保存位置</h2>
          <div class="row">
            <input type="text" id="save_dir" class="grow" placeholder="留空 = 软件目录下的 Downloaded">
            <button class="btn" id="btn-open-dir">打开目录</button>
          </div>
          <div class="hint">文件按「作者昵称 / 编号_发布日期_标题」存放；重复下载自动跳过。</div>
        </div>
      </div>
      <div>
        <div class="card">
          <h2><span class="num">③</span>下载内容</h2>
          <div class="chips" id="type-chips">
            <span class="chip on" data-type="video">视频</span>
            <span class="chip" data-type="cover">封面</span>
            <span class="chip" data-type="frame">首帧截图</span>
            <span class="chip" data-type="music">音乐</span>
            <span class="chip" data-type="avatar">头像</span>
            <span class="chip" data-type="json">JSON</span>
          </div>
          <div class="hint">「首帧截图」下载时实时生成，与视频同目录。</div>
          <label class="ck" style="margin-top:10px"><input type="checkbox" id="headless"> 后台采集（隐藏浏览器窗口，遇验证码会卡住，不建议）</label>
        </div>
        <div class="card">
          <h2><span class="num">④</span>数量与日期（可选）</h2>
          <div class="fg"><label>下载数量（0 = 全部）</label><input type="number" id="number" value="0" min="0"></div>
          <div class="fg"><label>起始日期</label><input type="text" id="start_date" placeholder="YYYY-MM-DD，留空不限"></div>
          <div class="fg"><label>结束日期（含当天）</label><input type="text" id="end_date" placeholder="YYYY-MM-DD，留空不限"></div>
        </div>
      </div>
    </div>
    <div class="cmdbar">
      <button class="btn primary" id="btn-start">开始下载</button>
      <button class="btn danger" id="btn-stop" disabled>停止</button>
      <button class="btn" id="btn-subscribe">＋ 订阅该博主</button>
      <button class="btn" id="btn-browsertest" title="验证浏览器能否正常启动（不访问抖音）">浏览器自检</button>
      <span class="st" id="dl-status">空闲</span>
    </div>
  </section>

  <!-- 运行日志 -->
  <section class="page" id="page-log">
    <div class="eyebrow">CONSOLE</div>
    <h1>运行日志</h1>
    <div class="sub">实时显示下载 / 登录进程输出，同时写入 gui_download.log（重启不丢失）。</div>
    <div class="row" style="margin-bottom:10px">
      <button class="btn sm" id="btn-clear-log">清空显示</button>
      <span class="badge" id="log-state">—</span>
    </div>
    <div id="logbox"></div>
  </section>

  <!-- 订阅管理 -->
  <section class="page" id="page-subs">
    <div class="eyebrow">SUBSCRIPTIONS</div>
    <h1>订阅管理 <span class="badge" id="sub-count" style="vertical-align:middle;font-size:12px">已订阅 0 位博主</span></h1>
    <div class="sub">订阅后，每次启动网页版当日会自动下载该博主的全部视频（已下载的自动跳过）；需要重跑请点卡片上的「立即下载」。</div>
    <div class="card">
      <div class="row">
        <input type="text" id="sub-url" class="grow" placeholder="博主主页链接（抖音 / 快手）">
        <input type="text" id="sub-nick" style="width:180px" placeholder="作者名（快手必填）">
        <button class="btn" id="btn-sub-add">＋ 添加订阅</button>
        <button class="btn" id="btn-sub-runall">立即全部下载</button>
      </div>
      <div class="hint">抖音订阅会自动识别博主昵称；快手接口拿不到昵称，请填写作者主页显示的名字（用作文件夹名）。</div>
    </div>
    <div class="subs" id="sub-list"></div>
    <div class="empty" id="sub-empty" style="display:none">还没有订阅任何博主。在「链接下载」页粘贴一个主页链接，点「＋ 订阅该博主」即可。</div>
  </section>

  <!-- 作品档案 -->
  <section class="page" id="page-files">
    <div class="eyebrow">LIBRARY</div>
    <h1>作品档案</h1>
    <div class="sub" id="files-dir">保存目录：—</div>
    <div class="tiles">
      <div class="tile"><b id="st-videos">—</b><span>视频</span></div>
      <div class="tile"><b id="st-frames">—</b><span>第一帧截图</span></div>
      <div class="tile"><b id="st-authors">—</b><span>作者</span></div>
      <div class="tile"><b id="st-size">—</b><span>占用空间</span></div>
    </div>
    <div class="card">
      <h2>作者文件夹 <button class="btn sm" id="btn-refresh-files" style="margin-left:auto">刷新</button></h2>
      <div style="max-height:300px;overflow-y:auto"><table><thead><tr><th>作者</th><th>文件数</th><th>大小</th><th>最近更新</th></tr></thead><tbody id="author-rows"></tbody></table></div>
      <div class="empty" id="author-empty" style="display:none">保存目录还是空的，去「链接下载」下载几个视频吧。</div>
    </div>
    <div class="card" id="files-detail" style="display:none">
      <h2 id="files-detail-title">文件列表</h2>
      <div style="max-height:420px;overflow-y:auto"><table><thead><tr><th>文件</th><th>类别</th><th>大小</th><th>修改时间</th></tr></thead><tbody id="file-rows"></tbody></table></div>
    </div>
  </section>

  <!-- 设置 -->
  <section class="page" id="page-settings">
    <div class="eyebrow">SETTINGS</div>
    <h1>设置</h1>
    <div class="sub">与桌面版共用同一份 gui_settings.json，改这里桌面版同样生效。</div>
    <div class="card">
      <h2>浏览器采集</h2>
      <label class="ck"><input type="checkbox" id="set-cookies"> 使用已登录账号采集作品列表（强烈推荐，否则只能拿到前约 20 个作品）</label>
      <label class="ck"><input type="checkbox" id="set-headless"> 隐藏浏览器窗口（headless，遇验证码无法人工完成，不建议）</label>
    </div>
    <div class="card">
      <h2>运行</h2>
      <label class="ck"><input type="checkbox" id="set-verbose"> 详细日志（INFO 级，用于排查问题）</label>
      <label class="ck"><input type="checkbox" id="set-frames"> 下载过程中实时生成第一帧截图</label>
      <label class="ck"><input type="checkbox" id="set-ksdate"> 快手文件名前加发布日期（如 2026-10-24_标题.mp4）</label>
      <label class="ck"><input type="checkbox" id="set-renumber"> 文件编号：文件名前加 4 位序号（0001_2026-10-24_标题.mp4）</label>
      <label class="ck"><input type="checkbox" id="set-autosub"> 启动时自动下载已订阅博主（当日一次）</label>
      <div class="fg" style="margin-top:10px;max-width:260px"><label>下载并发数（建议 5，调大容易触发风控）</label><input type="number" id="set-thread" min="1" max="16" value="5"></div>
    </div>
    <button class="btn primary" id="btn-save-settings">保存设置</button>
  </section>

  <div class="foot">数据（登录状态 / 设置 / 下载文件）全部保存在本软件所在文件夹 · 服务只监听 127.0.0.1 本机地址 · 关闭控制台窗口即停止</div>
</main>
<div id="toast"></div>

<script>
"use strict";
const S = { page: "download", logOffset: 0, logDirty: false, settings: null, status: null, filesAuthor: "" };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, opts) {
  opts = opts || {};
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = Object.assign({"Content-Type": "application/json"}, opts.headers || {});
  }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { throw new Error("无法连接本地服务（" + e.message + "）"); }
  let d = null;
  try { d = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((d && (d.detail || d.error)) || ("HTTP " + r.status));
  return d;
}
let toastTimer = null;
function toast(msg, ms) {
  const t = $("toast");
  t.textContent = msg; t.style.display = "block";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.display = "none"; }, ms || 3200);
}

/* ---------- 页面切换 ---------- */
document.querySelectorAll("#nav button").forEach((b) => {
  b.addEventListener("click", () => switchPage(b.dataset.page));
});
function switchPage(name) {
  S.page = name;
  document.querySelectorAll("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.page === name));
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("on", p.id === "page-" + name));
  if (name === "subs") loadSubs();
  if (name === "files") loadFiles();
  if (name === "settings") loadSettingsUI();
  if (name === "log") { S.logDirty = true; renderLogFull(); }
}

/* ---------- 登录胶囊 ---------- */
$("pill-dy").addEventListener("click", () => doLogin("douyin"));
$("pill-ks").addEventListener("click", () => doLogin("kuaishou"));
async function doLogin(kind) {
  if (!confirm(kind === "douyin" ? "将弹出浏览器窗口，请在其中完成抖音登录（扫码或账密）。现在打开吗？"
                                : "将弹出浏览器窗口，请在其中完成快手登录（扫码或账密）。现在打开吗？")) return;
  try {
    await api("/api/login/" + kind, { method: "POST", body: "{}" });
    toast("登录浏览器已打开，请完成扫码/账密登录");
    switchPage("log");
  } catch (e) { toast(e.message); }
}
async function refreshLogin() {
  try {
    const d = await api("/api/login/status");
    paintPill("dy", d.douyin);
    paintPill("ks", d.kuaishou);
  } catch (e) { /* 服务未就绪 */ }
}
function paintPill(kind, st) {
  const dot = $(kind === "dy" ? "dot-dy" : "dot-ks");
  const txt = $(kind === "dy" ? "st-dy" : "st-ks");
  dot.classList.toggle("ok", !!st.ok);
  txt.textContent = st.ok ? ("已登录" + (st.preview ? " " + st.preview : "")) : "未登录";
}

/* ---------- 下载 ---------- */
const chipSet = new Set(["video"]);
document.querySelectorAll("#type-chips .chip").forEach((c) => {
  c.addEventListener("click", () => {
    c.classList.toggle("on");
    c.classList.contains("on") ? chipSet.add(c.dataset.type) : chipSet.delete(c.dataset.type);
  });
});
$("urls").addEventListener("input", () => {
  const n = $("urls").value.split("\n").map((s) => s.trim()).filter(Boolean).length;
  $("url-count").textContent = "已识别 " + n + " 个链接";
});
$("headless").addEventListener("change", async () => {
  try {
    S.settings = await api("/api/settings", { method: "POST", body: { headless: $("headless").checked } });
    toast($("headless").checked ? "采集浏览器已设为隐藏（后台采集）" : "采集浏览器已设为可见");
  } catch (e) { toast(e.message); }
});
$("btn-start").addEventListener("click", startDownload);
$("btn-stop").addEventListener("click", stopDownload);
$("btn-subscribe").addEventListener("click", subscribeCurrent);
$("btn-browsertest").addEventListener("click", async () => {
  try { await api("/api/diag/browsertest", { method: "POST", body: "{}" }); toast("自检已启动，请在「运行日志」查看结果"); switchPage("log"); }
  catch (e) { toast(e.message); }
});
$("btn-open-dir").addEventListener("click", () => {
  const d = ($("save_dir").value || "").trim();
  toast(d ? "保存目录：" + d + "（请在资源管理器中打开）" : "保存目录：软件目录下的 Downloaded");
});
async function startDownload() {
  const urls = $("urls").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const body = {
    urls: urls,
    save_dir: ($("save_dir").value || "").trim(),
    types: Array.from(chipSet),
    number: parseInt($("number").value || "0", 10) || 0,
    start_time: ($("start_date").value || "").trim(),
    end_time: ($("end_date").value || "").trim(),
  };
  if (!body.types.length) body.types = ["video"];
  if (!body.urls.length) { toast("请先粘贴博主主页链接"); return; }
  try {
    const d = await api("/api/download", { method: "POST", body: body });
    toast(d.mode === "ks" ? "快手下载已启动（抖音链接将在快手后继续）" : "下载已启动");
    switchPage("log");
    refreshStatus();
  } catch (e) { toast(e.message); }
}
async function stopDownload() {
  try { const d = await api("/api/stop", { method: "POST", body: "{}" }); toast(d.killed ? "已停止" : "当前没有运行中的任务"); refreshStatus(); }
  catch (e) { toast(e.message); }
}
async function subscribeCurrent() {
  const urls = $("urls").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (urls.length !== 1) { toast("一次只能订阅一位博主（请只粘贴一个主页链接）"); return; }
  try {
    const d = await api("/api/subs", { method: "POST", body: { url: urls[0], nickname: "" } });
    toast(d.nickname ? ("已订阅：" + d.nickname) : "已订阅");
    loadSubs();
  } catch (e) { toast(e.message); }
}

/* ---------- 状态轮询 ---------- */
async function refreshStatus() {
  try {
    const d = await api("/api/status");
    S.status = d;
    $("btn-start").disabled = d.running;
    $("btn-stop").disabled = !d.running && !d.login_running;
    $("btn-browsertest").disabled = d.running;
    let txt = "空闲";
    if (d.running) {
      const label = d.mode === "ks" ? "快手下载中" : d.mode === "diag" ? "浏览器自检中" : "下载中";
      txt = label + "… (pid " + d.pid + ")";
      if (d.queue_len) txt += " · 队列还剩 " + d.queue_len + " 位";
    } else if (d.login_running) {
      txt = d.login_kind === "kuaishou" ? "等待快手登录…（请在弹出的浏览器里登录）" : "等待抖音登录…（请在弹出的浏览器里登录）";
    } else if (d.last_exit != null) {
      txt = "上次退出码 " + d.last_exit;
    }
    $("dl-status").textContent = txt;
  } catch (e) { $("dl-status").textContent = "服务未连接"; }
}

/* ---------- 日志 ---------- */
$("btn-clear-log").addEventListener("click", () => { $("logbox").textContent = ""; });
async function refreshLog() {
  try {
    const d = await api("/api/log?offset=" + S.logOffset);
    if (d.lines && d.lines.length) {
      const box = $("logbox");
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      box.textContent += (box.textContent ? "\n" : "") + d.lines.join("\n");
      if (atBottom || S.page !== "log") box.scrollTop = box.scrollHeight;
      if (box.textContent.length > 400000) box.textContent = box.textContent.slice(-300000);
    }
    S.logOffset = d.offset;
    $("log-state").textContent = "已接收 " + d.offset + " 行";
  } catch (e) { /* 忽略 */ }
}
function renderLogFull() { $("logbox").textContent = ""; S.logOffset = 0; refreshLog(); }

/* ---------- 订阅 ---------- */
$("btn-sub-add").addEventListener("click", addSub);
$("btn-sub-runall").addEventListener("click", runAllSubs);
async function addSub() {
  const url = ($("sub-url").value || "").trim();
  const nick = ($("sub-nick").value || "").trim();
  if (!url) { toast("请先粘贴博主主页链接"); return; }
  try {
    const d = await api("/api/subs", { method: "POST", body: { url: url, nickname: nick } });
    toast(d.nickname ? ("已订阅：" + d.nickname + (d.platform === "kuaishou" ? "（快手）" : "（抖音）")) : "已订阅");
    $("sub-url").value = ""; $("sub-nick").value = "";
    loadSubs();
  } catch (e) { toast(e.message); }
}
async function runAllSubs() {
  try { const d = await api("/api/subs/run_all", { method: "POST", body: "{}" }); toast("已开始下载 " + d.count + " 位订阅博主"); switchPage("log"); refreshStatus(); }
  catch (e) { toast(e.message); }
}
async function loadSubs() {
  try {
    const d = await api("/api/subs");
    const list = $("sub-list");
    $("sub-count").textContent = "已订阅 " + d.subs.length + " 位博主";
    $("sub-empty").style.display = d.subs.length ? "none" : "block";
    list.innerHTML = "";
    d.subs.forEach((s) => {
      const nick = s.nickname || (s.sec_uid || "").slice(0, 12) + "…";
      const hue = (Array.from(nick).reduce((a, c) => a + c.charCodeAt(0), 0) * 47) % 360;
      const card = document.createElement("div");
      card.className = "subcard";
      card.innerHTML =
        '<div class="avatar" style="background:hsl(' + hue + ',55%,45%)">' + esc(nick.slice(0, 1)) + "</div>" +
        '<div class="info"><b>' + esc(nick) + "</b>" +
        '<span class="tag ' + (s.platform === "kuaishou" ? "ks" : "dy") + '">' + (s.platform === "kuaishou" ? "快手" : "抖音") + "</span> " +
        esc((s.last_run_date ? "上次自动下载 " + s.last_run_date + "（新增 " + (s.last_run_new || 0) + "）" : "尚未自动下载")) + "</span></div>" +
        '<div class="ops">' +
        '<button class="btn sm" data-op="run">立即下载</button>' +
        '<button class="btn sm danger" data-op="del">取消订阅</button></div>';
      card.querySelector('[data-op="run"]').addEventListener("click", async () => {
        try { await api("/api/subs/" + encodeURIComponent(s.sec_uid) + "/run", { method: "POST", body: "{}" }); toast("已开始下载：" + nick); switchPage("log"); refreshStatus(); }
        catch (e) { toast(e.message); }
      });
      card.querySelector('[data-op="del"]').addEventListener("click", async () => {
        if (!confirm("确定取消订阅「" + nick + "」？（已下载的文件会保留）")) return;
        try { await api("/api/subs/" + encodeURIComponent(s.sec_uid), { method: "DELETE" }); toast("已取消订阅"); loadSubs(); }
        catch (e) { toast(e.message); }
      });
      list.appendChild(card);
    });
  } catch (e) { /* 忽略 */ }
}

/* ---------- 作品档案 ---------- */
$("btn-refresh-files").addEventListener("click", loadFiles);
async function loadFiles() {
  try {
    const d = await api("/api/files");
    $("files-dir").textContent = "保存目录：" + d.save_dir;
    $("st-videos").textContent = d.stats.videos;
    $("st-frames").textContent = d.stats.frames;
    $("st-authors").textContent = d.stats.authors;
    $("st-size").textContent = humanText(d.stats.size);
    const rows = $("author-rows");
    rows.innerHTML = "";
    $("author-empty").style.display = d.authors.length ? "none" : "block";
    d.authors.forEach((a) => {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.innerHTML = "<td>" + esc(a.name) + "</td><td>" + a.files + "</td><td>" + esc(a.size_text) + "</td><td class='mono'>" + esc(a.mtime_text) + "</td>";
      tr.addEventListener("click", () => loadAuthorFiles(a.name));
      rows.appendChild(tr);
    });
  } catch (e) { /* 忽略 */ }
}
function humanText(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; n = Number(n) || 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + u[i];
}
async function loadAuthorFiles(author) {
  S.filesAuthor = author;
  try {
    const d = await api("/api/files?author=" + encodeURIComponent(author));
    $("files-detail").style.display = "block";
    $("files-detail-title").textContent = "文件列表 · " + author + "（" + d.files.length + " 个）";
    const rows = $("file-rows");
    rows.innerHTML = "";
    d.files.forEach((f) => {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>" + esc(f.name) + "</td><td>" + esc(f.category_label) + "</td><td>" + esc(f.size_text) + "</td><td class='mono'>" + esc(f.mtime_text) + "</td>";
      rows.appendChild(tr);
    });
    $("files-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) { toast(e.message); }
}

/* ---------- 设置 ---------- */
function loadSettingsUI() {
  const s = S.settings;
  if (!s) return;
  $("set-cookies").checked = !!s.use_login_cookies;
  $("set-headless").checked = !!s.headless;
  $("set-verbose").checked = !!s.verbose;
  $("set-frames").checked = !!s.live_frames;
  $("set-ksdate").checked = !!s.ks_date_folders;
  $("set-renumber").checked = !!s.auto_renumber;
  $("set-autosub").checked = !!s.auto_subscribe;
  $("set-thread").value = s.thread || 5;
}
$("btn-save-settings").addEventListener("click", async () => {
  const body = {
    save_dir: S.settings.save_dir,
    use_login_cookies: $("set-cookies").checked,
    headless: $("set-headless").checked,
    verbose: $("set-verbose").checked,
    live_frames: $("set-frames").checked,
    ks_date_folders: $("set-ksdate").checked,
    auto_renumber: $("set-renumber").checked,
    auto_subscribe: $("set-autosub").checked,
    thread: parseInt($("set-thread").value || "5", 10) || 5,
  };
  try {
    S.settings = await api("/api/settings", { method: "POST", body: body });
    if (!$("save_dir").value.trim()) $("save_dir").value = S.settings.save_dir || "";
    toast("设置已保存");
  } catch (e) { toast(e.message); }
});

/* ---------- 启动 ---------- */
(async function init() {
  try {
    const h = await api("/api/health");
    S.settings = await api("/api/settings");
    if (S.settings.save_dir) $("save_dir").value = S.settings.save_dir;
    if (S.settings.headless) $("headless").checked = true;
    if (S.settings.save_dir && S.settings.save_dir.indexOf("Downloaded") >= 0) $("save_dir").value = "";
  } catch (e) { toast("初始化失败：" + e.message); }
  refreshLogin(); refreshStatus(); refreshLog();
  setInterval(refreshStatus, 1500);
  setInterval(refreshLogin, 4000);
  setInterval(refreshLog, 1000);
})();
</script>
</body>
</html>
"""


# ---------------- FastAPI 应用 ----------------
from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402


def _or_400(res: Dict[str, Any]) -> Dict[str, Any]:
    """Runner 返回 {ok: False, error} 时转成 HTTP 400，让前端能拿到真实原因。"""
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=str(res.get("error") or "操作失败"))
    return res


class DownloadReq(BaseModel):
    """下载请求体（字段全部可选，默认值中性）。"""
    urls: List[str] = []
    save_dir: str = ""
    types: List[str] = []
    number: int = 0
    start_time: str = ""
    end_time: str = ""


class SubReq(BaseModel):
    """添加订阅请求体。"""
    url: str = ""
    nickname: str = ""
    types: List[str] = []


class SettingsReq(BaseModel):
    """设置更新请求体（只更新出现的键）。"""
    save_dir: Optional[str] = None
    use_login_cookies: Optional[bool] = None
    headless: Optional[bool] = None
    verbose: Optional[bool] = None
    live_frames: Optional[bool] = None
    thread: Optional[int] = None
    ks_date_folders: Optional[bool] = None
    auto_renumber: Optional[bool] = None
    auto_subscribe: Optional[bool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动 10 秒后检查今日订阅自动下载（与桌面版同款时机）
    threading.Thread(target=RUNNER.auto_subscribe_tick, daemon=True).start()
    yield
    RUNNER.shutdown()


app = FastAPI(title="抖快批量下载器 · 网页版", version=APP_VERSION, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """单页前端（深色风格，与桌面版一致）。"""
    return HTMLResponse(INDEX_HTML)


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    """健康检查：服务就绪即可返回 200。"""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/status")
async def api_status() -> Dict[str, Any]:
    """下载 / 登录进程状态。"""
    return RUNNER.status()


@app.post("/api/download")
async def api_download(req: DownloadReq) -> Dict[str, Any]:
    """启动下载：抖音走 --cli，快手走 --kdl，混合则快手完成后链式跑抖音。"""
    settings = load_settings()
    save_dir = (req.save_dir or "").strip() or settings.get("save_dir") or str(DEFAULT_SAVE_DIR)
    # 手动输入的目录即时记住（与桌面版一致）
    if save_dir != settings.get("save_dir"):
        settings["save_dir"] = save_dir
        save_settings(settings)
    return _or_400(RUNNER.start_download(
        req.urls, save_dir, req.types, req.number,
        req.start_time or "", req.end_time or "", settings,
    ))


@app.post("/api/stop")
async def api_stop() -> Dict[str, Any]:
    """停止当前下载 / 登录子进程（按进程树杀）。"""
    return RUNNER.stop_all()


@app.get("/api/log")
async def api_log(offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    """日志 tail：返回全局序号 >= offset 的行与最新序号。"""
    lines, total = RUNNER.tail(offset)
    return {"lines": lines, "offset": total}


@app.get("/api/login/status")
async def api_login_status() -> Dict[str, Any]:
    """抖音 / 快手登录态（干净包应返回双双未登录）。"""
    dy = cookie_status()
    ks = kuaishou_cookie_status()
    return {
        "douyin": {"ok": dy["ok"], "preview": dy["msToken_preview"]},
        "kuaishou": {"ok": ks["ok"], "preview": ""},
    }


@app.post("/api/login/douyin")
async def api_login_douyin() -> Dict[str, Any]:
    """弹出有头浏览器完成抖音登录（轮询 config.yml 的 sessionid）。"""
    return _or_400(RUNNER.start_login("douyin"))


@app.post("/api/login/kuaishou")
async def api_login_kuaishou() -> Dict[str, Any]:
    """弹出有头浏览器完成快手登录。"""
    return _or_400(RUNNER.start_login("kuaishou"))


@app.get("/api/files")
async def api_files(author: str = Query("")) -> Dict[str, Any]:
    """作品档案：总览统计 / 作者列表 / 单个作者的文件列表。"""
    settings = load_settings()
    save_dir = Path(settings.get("save_dir") or DEFAULT_SAVE_DIR)
    if author:
        return {"author": author, "files": list_author_files(save_dir, author)}
    return {
        "save_dir": str(save_dir),
        "stats": dir_stats(save_dir),
        "authors": list_authors(save_dir),
    }


@app.get("/api/settings")
async def api_get_settings() -> Dict[str, Any]:
    return load_settings()


@app.post("/api/settings")
async def api_post_settings(req: SettingsReq) -> Dict[str, Any]:
    """更新设置（只更新出现的键，schema 与桌面版一致）。"""
    data = load_settings()
    patch = req.model_dump(exclude_none=True)
    for k, v in patch.items():
        if k == "thread":
            data[k] = max(1, min(16, int(v)))
        else:
            data[k] = v
    save_settings(data)
    return data


@app.get("/api/subs")
async def api_subs() -> Dict[str, Any]:
    """订阅列表（subscription_store，初始为空）。"""
    return {"subs": subscription_store.load()}


@app.post("/api/subs")
async def api_sub_add(req: SubReq) -> Dict[str, Any]:
    """添加订阅：抖音自动反查昵称；快手必须提供作者名。"""
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请先粘贴博主主页链接")
    sec_uid = subscription_store.sec_uid_from_url(url)
    if not sec_uid:
        raise HTTPException(status_code=400, detail="无法从链接识别博主 ID，请粘贴抖音或快手作者主页链接")
    platform = subscription_store.platform_from_url(url)
    nickname = (req.nickname or "").strip()
    if platform != "kuaishou" and not nickname:
        nickname = lookup_author_nickname(sec_uid)
    if platform == "kuaishou" and not nickname:
        raise HTTPException(status_code=400, detail="快手订阅需要填写作者名（列表显示 + 下载文件夹名）")
    types = [t for t in (req.types or []) if t in VALID_TYPES] or ["video"]
    if "avatar" not in types:
        types.append("avatar")  # 订阅顺带下载作者头像（桌面版同款行为）
    settings = load_settings()
    save_dir = settings.get("save_dir") or str(DEFAULT_SAVE_DIR)
    existed = subscription_store.find(sec_uid) is not None
    subscription_store.add({
        "sec_uid": sec_uid,
        "platform": platform,
        "nickname": nickname,
        "url": url,
        "save_dir": save_dir,
        "types": types,
    })
    return {
        "ok": True, "existed": existed, "sec_uid": sec_uid,
        "nickname": nickname, "platform": platform,
    }


@app.delete("/api/subs/{sec_uid}")
async def api_sub_del(sec_uid: str) -> Dict[str, Any]:
    """取消订阅（已下载文件保留）。"""
    return {"ok": subscription_store.remove(sec_uid)}


@app.post("/api/subs/{sec_uid}/run")
async def api_sub_run(sec_uid: str) -> Dict[str, Any]:
    """立即下载一位订阅博主（不受每日一次限制）。"""
    return _or_400(RUNNER.run_sub(sec_uid))


@app.post("/api/subs/run_all")
async def api_sub_run_all() -> Dict[str, Any]:
    """立即下载全部订阅博主（串行队列）。"""
    return _or_400(RUNNER.run_all_subs())


@app.post("/api/diag/browsertest")
async def api_diag_browsertest() -> Dict[str, Any]:
    """浏览器启动自检（headless about:blank，不访问抖音，用于排查浏览器缺失）。"""
    return _or_400(RUNNER.start_browsertest())


# ---------------- 启动 ----------------
def _acquire_single_instance() -> bool:
    """单实例互斥体：已有一个网页版在跑就退出（防止端口抢占）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ERROR_ALREADY_EXISTS = 183
            mutex = ctypes.windll.kernel32.CreateMutexW(
                None, False, "抖快批量下载器-网页版-单实例互斥体"
            )
            if mutex == 0:
                return True  # 创建失败不阻塞正常启动
            return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
        except Exception:
            return True
    # 非 Windows：锁文件兜底
    lock = ROOT / ".webui_single_instance.lock"
    try:
        if lock.exists():
            return False
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return True


def _pick_port(preferred: int) -> int:
    """默认端口被占用时顺序 +1 试探（最多试 20 个）。"""
    import socket
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred


def _open_browser_when_ready(port: int) -> None:
    """服务就绪后打开系统浏览器（失败不影响服务）。"""
    import urllib.request
    url = f"http://127.0.0.1:{port}/"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "api/health", timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.3)
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="抖快批量下载器 · 本地网页版")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}，被占用时自动顺延）")
    parser.add_argument("--no-browser", action="store_true",
                        help="就绪后不自动打开系统浏览器（无头 / QA 用）")
    args = parser.parse_args()

    if not _acquire_single_instance():
        print("已经有一个网页版实例在运行（单实例互斥体生效）。")
        print("如确认没有，请重启电脑或检查任务管理器中的「抖快批量下载器-网页版.exe」。")
        raise SystemExit(1)

    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print(" 抖快批量下载器 · 网页版")
    print(f" 本机地址 : {url}")
    print(f" 数据目录 : {ROOT}")
    print(" 只监听 127.0.0.1（本机），局域网/他人都访问不到")
    print(" 关闭本窗口 = 停止服务（下载子进程会一并结束）")
    if port != args.port:
        print(f" 注意：默认端口 {args.port} 被占用，已顺延到 {port}")
    print("=" * 56, flush=True)

    if not args.no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(port,), daemon=True).start()

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    try:
        server.run()
    finally:
        RUNNER.shutdown()


if __name__ == "__main__":
    main()
