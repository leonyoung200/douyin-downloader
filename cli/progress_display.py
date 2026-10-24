from __future__ import annotations

import io
import os
import sys
from typing import Any, Dict, Optional, Tuple

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table


class _SafeStream:
    """容错输出流：包裹真实 stdout/stderr，吞掉进程退出时句柄失效导致的写入/刷新异常。

    背景：在 Windows 控制台（或 GUI 自调用 CLI 子进程）场景下，rich 关闭进度会话
    （transient Progress 的 __exit__）会向控制台句柄 flush，此时若句柄已失效
    （WinError 87 → errno 22 Invalid argument），``LegacyWindowsTerm.write_text`` 的
    ``self.flush()`` 会抛 ``OSError: [Errno 22]``，进而把整个 CLI 下载任务崩成
    PyInstaller 原生错误框。此包装器让任何 write/flush 失败都被静默吞掉，下载仍能
    正常完成，异常不会污染主流程。
    """

    def __init__(self, stream: Any):
        if stream is None:
            try:
                stream = open(os.devnull, "w", encoding="utf-8", errors="replace")
            except Exception:
                stream = None
        self._stream = stream
        self.encoding = (getattr(stream, "encoding", None) or "utf-8") or "utf-8"
        self.errors = (getattr(stream, "errors", None) or "replace") or "replace"

    def write(self, text):
        if self._stream is None:
            return None
        try:
            return self._stream.write(text)
        except Exception:
            return None

    def flush(self):
        if self._stream is None:
            return
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        if self._stream is None:
            return False
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self):
        if self._stream is None:
            raise OSError("no fileno")
        try:
            return self._stream.fileno()
        except Exception as exc:  # noqa: BLE001 — 对 rich 暴露为 OSError，由它自行降级
            raise OSError(str(exc))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def make_safe_console() -> Console:
    """构造一个对 Windows 控制台崩溃免疫的 rich Console。

    - 先把 stdout/stderr 固定为 UTF-8，避免 rich 输出 ``✓`` 等符号时因 GBK 编码抛错；
    - 强制 ``legacy_windows=False``，彻底绕开 ``LegacyWindowsTerm`` 的 Win32 API 调用
      （崩溃就发生在那条路径）；现代 Windows 控制台原生支持 VT 转义，渲染效果等价；
    - 用 ``_SafeStream`` 包裹底层流，任何 write/flush 异常都被吞掉。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is None:
                raise ValueError("no stream")
            enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
            if enc != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                buf = getattr(stream, "buffer", None) if stream is not None else None
                if buf is not None:
                    setattr(
                        sys,
                        name,
                        io.TextIOWrapper(buf, encoding="utf-8", errors="replace"),
                    )
                else:
                    setattr(
                        sys,
                        name,
                        open(os.devnull, "w", encoding="utf-8", errors="replace"),
                    )
            except Exception:
                pass

    return Console(file=_SafeStream(sys.stdout), legacy_windows=False)


console = make_safe_console()

_REASON_STATUS_LABELS = {"skipped": "跳过", "failed": "失败"}


class ProgressDisplay:
    _URL_STEP_TOTAL = 6

    def __init__(self):
        self.console = console
        self._progress_ctx: Optional[Progress] = None
        self._progress: Optional[Progress] = None
        self._overall_task_id: Optional[int] = None
        self._url_task_id: Optional[int] = None
        self._item_task_id: Optional[int] = None
        self._url_index = 0
        self._url_total = 0
        self._url_step_completed = 0
        self._item_total = 0
        self._item_completed = 0
        self._single_url_item_mode = False
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0}
        # 整个会话的跳过 / 失败原因计数，键是 (状态, 原因)；进度条是 transient 的，
        # 结束后只能靠 show_item_reasons 把原因留在屏幕上。
        self._reason_counts: Dict[Tuple[str, str], int] = {}
        self._reason_counts_at_url_start: Dict[Tuple[str, str], int] = {}

    def show_banner(self):
        banner = """
╔══════════════════════════════════════════╗
║     Douyin Downloader v2.0.0            ║
║     抖音批量下载工具                     ║
╚══════════════════════════════════════════╝
        """
        self._active_console().print(banner, style="bold cyan")

    def create_progress(self) -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[detail]}"),
            console=self.console,
            transient=True,
            refresh_per_second=6,
        )

    def start_download_session(self, total_urls: int):
        if self._progress is not None:
            return

        self._progress_ctx = self.create_progress()
        self._progress = self._progress_ctx.__enter__()
        self._single_url_item_mode = False
        self._overall_task_id = self._progress.add_task(
            "总体进度",
            total=max(total_urls, 1),
            completed=0,
            detail=f"共 {total_urls} 个 URL",
        )

    def stop_download_session(self):
        self._cleanup_url_tasks()

        if self._progress_ctx is not None:
            self._progress_ctx.__exit__(None, None, None)

        self._progress_ctx = None
        self._progress = None
        self._overall_task_id = None
        self._single_url_item_mode = False

    def start_url(self, index: int, total: int, url: str):
        self._url_index = index
        self._url_total = total
        self._url_step_completed = 0
        self._item_total = 0
        self._item_completed = 0
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0}
        self._reason_counts_at_url_start = dict(self._reason_counts)

        self._cleanup_url_tasks()
        if not self._progress:
            return

        self._url_task_id = self._progress.add_task(
            self._format_url_description("待开始"),
            total=self._URL_STEP_TOTAL,
            completed=0,
            detail=self._shorten(url, max_len=72),
        )

    def complete_url(self, result=None):
        if self._progress and self._url_task_id is not None:
            detail = ""
            if result:
                detail = f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped}"
            self._progress.update(
                self._url_task_id,
                completed=self._URL_STEP_TOTAL,
                description=self._format_url_description("完成"),
                detail=detail,
            )

        if self._progress and self._overall_task_id is not None:
            if self._single_url_item_mode:
                self._progress.update(self._overall_task_id, completed=self._item_total or 1)
            else:
                self._progress.advance(self._overall_task_id, 1)

    def fail_url(self, reason: str):
        if self._progress and self._url_task_id is not None:
            self._progress.update(
                self._url_task_id,
                completed=self._URL_STEP_TOTAL,
                description=self._format_url_description("失败"),
                detail=reason,
            )

        if self._progress and self._overall_task_id is not None:
            if self._single_url_item_mode:
                self._progress.update(self._overall_task_id, completed=self._item_total or 1)
            else:
                self._progress.advance(self._overall_task_id, 1)

    def advance_step(self, step: str, detail: str = ""):
        if not self._progress or self._url_task_id is None:
            return

        self._url_step_completed = min(self._url_step_completed + 1, self._URL_STEP_TOTAL)
        self._progress.update(
            self._url_task_id,
            completed=self._url_step_completed,
            description=self._format_url_description(step),
            detail=detail,
        )

    def update_step(self, step: str, detail: str = ""):
        if not self._progress or self._url_task_id is None:
            return

        self._progress.update(
            self._url_task_id,
            description=self._format_url_description(step),
            detail=detail,
        )

    def set_item_total(self, total: int, detail: str = ""):
        if not self._progress:
            return

        self._item_total = max(total, 1)
        self._item_completed = 1 if total == 0 else 0
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0}

        if self._url_total == 1 and self._overall_task_id is not None:
            self._single_url_item_mode = True
            self._progress.update(
                self._overall_task_id,
                total=self._item_total,
                completed=self._item_completed,
                detail=f"共 {total} 个作品",
            )

        description = self._format_item_description()
        item_detail = detail or ("无待下载条目" if total == 0 else "")

        if self._item_task_id is None:
            self._item_task_id = self._progress.add_task(
                description,
                total=self._item_total,
                completed=self._item_completed,
                detail=item_detail,
            )
            return

        self._progress.update(
            self._item_task_id,
            total=self._item_total,
            completed=self._item_completed,
            description=description,
            detail=item_detail,
        )

    def advance_item(self, status: str, detail: str = "", reason: str = ""):
        if reason and status in _REASON_STATUS_LABELS:
            key = (status, reason)
            self._reason_counts[key] = self._reason_counts.get(key, 0) + 1
        if not self._progress:
            return
        if self._item_task_id is None:
            self.set_item_total(1, "初始化条目进度")
        assert self._item_task_id is not None

        if status in self._item_stats:
            self._item_stats[status] += 1
        if self._item_completed < self._item_total:
            self._item_completed += 1

        status_map = {"success": "成功", "failed": "失败", "skipped": "跳过"}
        status_text = status_map.get(status, status)
        item_detail = f"最近: {status_text} {self._shorten(detail, max_len=36)}"
        if reason:
            item_detail += f" · {self._shorten(reason, max_len=36)}"

        self._progress.update(
            self._item_task_id,
            completed=self._item_completed,
            description=self._format_item_description(),
            detail=item_detail,
        )
        if self._single_url_item_mode and self._overall_task_id is not None:
            self._progress.update(
                self._overall_task_id,
                completed=self._item_completed,
                detail=f"共 {self._item_total} 个作品",
            )

    def show_result(self, result):
        table = Table(title="Download Summary", show_header=True, header_style="bold magenta")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")

        table.add_row("Total", str(result.total))
        table.add_row("Success", str(result.success))
        table.add_row("Failed", str(result.failed))
        table.add_row("Skipped", str(result.skipped))

        if result.total > 0:
            success_rate = (result.success / result.total) * 100
            table.add_row("Success Rate", f"{success_rate:.1f}%")

        self._active_console().print(table)

    def rollback_url_item_reasons(self):
        """当前 URL 要整条重跑（如重新登录后重试）时，丢掉本轮已计的原因。"""
        self._reason_counts = dict(self._reason_counts_at_url_start)

    def show_item_reasons(self):
        if not self._reason_counts:
            return
        table = Table(title="跳过 / 失败原因", show_header=True, header_style="bold magenta")
        table.add_column("状态", style="cyan")
        table.add_column("原因")
        table.add_column("数量", justify="right", style="green")
        ranked = sorted(self._reason_counts.items(), key=lambda pair: -pair[1])
        for (status, reason), count in ranked:
            table.add_row(_REASON_STATUS_LABELS[status], reason, str(count))
        self._active_console().print(table)

    def print_info(self, message: str):
        self._active_console().print(f"[blue]ℹ[/blue] {message}")

    def print_success(self, message: str):
        self._active_console().print(f"[green]✓[/green] {message}")

    def print_warning(self, message: str):
        self._active_console().print(f"[yellow]⚠[/yellow] {message}")

    def print_error(self, message: str):
        self._active_console().print(f"[red]✗[/red] {message}")

    def _cleanup_url_tasks(self):
        if not self._progress:
            self._url_task_id = None
            self._item_task_id = None
            return

        if self._item_task_id is not None:
            self._progress.remove_task(self._item_task_id)
            self._item_task_id = None
        if self._url_task_id is not None:
            self._progress.remove_task(self._url_task_id)
            self._url_task_id = None

    def _format_url_description(self, step: str) -> str:
        return f"URL {self._url_index}/{self._url_total} · {step}"

    def _format_item_description(self) -> str:
        return (
            "作品下载 "
            f"S:{self._item_stats['success']} "
            f"F:{self._item_stats['failed']} "
            f"K:{self._item_stats['skipped']}"
        )

    def _active_console(self) -> Console:
        if self._progress:
            return self._progress.console
        return self.console

    @staticmethod
    def _shorten(text: str, max_len: int = 60) -> str:
        normalized = (text or "").strip()
        if len(normalized) <= max_len:
            return normalized
        return f"{normalized[: max_len - 3]}..."
