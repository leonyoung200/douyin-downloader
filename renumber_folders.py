# -*- coding: utf-8 -*-
"""按发布日期给作品文件编号：``0001_2026-10-24_标题.mp4``。

设计要点
--------
* **以“作品”为单位编号**：一个作品的视频 / 封面 / 第一帧 / JSON 共用同一个编号，
  例如 ``0007_2026-10-24_标题.mp4``、``0007_2026-10-24_标题_cover.jpg``、
  ``0007_2026-10-24_标题_frame1.jpg``、``0007_2026-10-24_标题.json``。
* **排序键 = (发布日期, 标题)**：无日期的排最后；同一天按标题稳定排序，
  因此同一批文件每次重排结果一致（幂等），新下载的作品会自动插到正确位置。
* **两阶段重命名**：先把要改名的文件统一加 ``__renum_tmp_N__`` 前缀，再改最终名，
  避免 ``0001_x`` → ``0002_x`` 这类连环改名互相踩踏。
* **不动目录结构**：文件留在原目录（兼容仍带日期子目录的旧数据）。
* **头像不参与编号**：``作者_avatar.jpg`` 属于作者而非某个作品。

只做重命名，不下载、不删文件；改名前先检查目标是否已存在，已存在则跳过并记日志。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 已有编号前缀：0001_ / 00001_（超过 9999 自动扩位，兼容 4~6 位）
_IDX_RE = re.compile(r"^\d{4,6}_")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# 附属文件标记（顺序敏感：frame1 要在 cover 前？互不为子串，无冲突）
_TAGS = ("_frame1", "_cover", "_music", "_avatar")
# aweme_id 尾缀（抖音文件名末尾的长数字 ID），分组时剥掉以对齐附属文件
_AWEME_TAIL = re.compile(r"_\d{15,20}$")
_MEDIA_SUFFIX = {".mp4", ".mov", ".mkv", ".flv", ".webm", ".m4v", ".avi"}
_NO_DATE = "9999-99-99"  # 无日期的作品排最后
_TMP_PREFIX = "__renum_tmp_{}__"


def strip_index(stem: str) -> str:
    """去掉文件名里的编号前缀（如 ``0001_2026-10-24_标题`` → ``2026-10-24_标题``）。"""
    return _IDX_RE.sub("", stem, count=1)


def _base_key(stem: str) -> str:
    """作品分组键：去掉编号前缀、附属标记与 aweme_id 尾缀。

    三者去掉后 ``2026-10-23_标题_7447860303028868002.mp4`` 与
    ``2026-10-23_标题_frame1.jpg`` 才会归到同一个作品。
    """
    s = strip_index(stem)
    low = s.lower()
    for tag in _TAGS:
        if low.endswith(tag):
            s = s[: -len(tag)]
            break
    return _AWEME_TAIL.sub("", s)


def _is_avatar(p: Path) -> bool:
    return p.stem.lower().endswith("_avatar")


def _is_primary(p: Path) -> bool:
    """主媒体文件（编号归属依据，优先视频）。"""
    if _is_avatar(p):
        return False
    low = p.stem.lower()
    if any(low.endswith(t) for t in _TAGS):
        return False
    return p.suffix.lower() in _MEDIA_SUFFIX


def _ts_from_json(obj: Any, depth: int = 0) -> int:
    """递归找发布时间戳（秒）：快手的 ``timestamp``（毫秒）/ 抖音的 ``create_time``（秒）。"""
    if depth > 6:
        return 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, (int, float)) and v > 0:
                if lk == "timestamp":
                    return int(v / 1000) if v > 1e12 else int(v)
                if lk == "create_time":
                    return int(v)
        for v in obj.values():
            ts = _ts_from_json(v, depth + 1)
            if ts:
                return ts
    elif isinstance(obj, list):
        for v in obj[:5]:
            ts = _ts_from_json(v, depth + 1)
            if ts:
                return ts
    return 0


def _date_from_json(jpath: Path) -> str:
    """从作品的 JSON 文件里恢复发布日期（文件名没有日期时的兜底）。"""
    try:
        data = json.loads(jpath.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — JSON 缺损不阻塞编号
        return ""
    ts = _ts_from_json(data)
    if not ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


def _group_date(group: Dict[str, Any]) -> str:
    """确定作品发布日期：文件名日期 → 组内 JSON 里的时间戳 → 排最后的哨兵值。"""
    for p in group["files"]:
        m = _DATE_RE.search(strip_index(p.stem))
        if m:
            return m.group(1)
    for p in group["files"]:  # 文件名无日期时从 JSON 恢复（旧格式数据）
        if p.suffix.lower() == ".json":
            d = _date_from_json(p)
            if d:
                return d
    return _NO_DATE


def scan_groups(author_dir: Path) -> List[Dict[str, Any]]:
    """按作品分组并排序，返回 [{key, files, main, date}]（已按 日期+标题 升序）。"""
    groups: Dict[str, Dict[str, Any]] = {}
    for p in sorted(author_dir.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if _is_avatar(p):  # 作者头像不属于任何作品
            continue
        key = _base_key(p.stem)
        if not key:
            continue
        g = groups.setdefault(
            key, {"key": key, "files": [], "main": None, "date": _NO_DATE}
        )
        g["files"].append(p)
        if g["main"] is None or (_is_primary(p) and not _is_primary(g["main"])):
            g["main"] = p

    for g in groups.values():
        g["date"] = _group_date(g)
        if g["main"] is None:
            g["main"] = g["files"][0]

    return sorted(
        groups.values(),
        # 纯数字文件名（如 1.mp4/10.mp4）补零后按数值序；普通标题不变序
        key=lambda g: (g["date"], (g["key"] or "").lower().zfill(12)),
    )


def plan_renumber(author_dir: Path, index_width: int = 4) -> List[Tuple[Path, Path]]:
    """生成重命名计划 [(原路径, 新路径)]，只包含确实需要改名的文件。"""
    plan: List[Tuple[Path, Path]] = []
    for i, g in enumerate(scan_groups(author_dir), start=1):
        prefix = f"{i:0{index_width}d}_"
        for p in g["files"]:
            dst = p.with_name(prefix + strip_index(p.stem) + p.suffix)
            if dst != p:
                plan.append((p, dst))
    return plan


def renumber_author_dir(
    author_dir: Path,
    log: Optional[Callable[[str], None]] = None,
    index_width: int = 4,
) -> Dict[str, Any]:
    """给单个作者目录下的作品文件统一编号。返回统计字典。"""
    stats: Dict[str, Any] = {
        "renamed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "width": index_width,
    }

    def _log(msg: str) -> None:
        if log:
            log(msg)

    plan = plan_renumber(Path(author_dir), index_width)
    if not plan:
        return stats

    # 阶段一：改成临时名，腾出最终名（防连环改名冲突）
    staged: List[Tuple[Path, Path]] = []
    for n, (src, dst) in enumerate(plan):
        tmp = src.with_name(_TMP_PREFIX.format(n) + src.name)
        try:
            src.rename(tmp)
            staged.append((tmp, dst, src))
        except Exception as e:  # noqa: BLE001 — 重命名失败需收集后继续
            stats["failed"] += 1
            stats["errors"].append(f"{src.name}: {e}")

    # 阶段二：改成最终名；目标已存在则回滚本条（保留原名）
    for tmp, dst, original in staged:
        try:
            if dst.exists():
                _log(f"    编号跳过（目标已存在）：{dst.name}")
                tmp.rename(original)
                stats["skipped"] += 1
                continue
            tmp.rename(dst)
            stats["renamed"] += 1
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            stats["errors"].append(f"{original.name}: {e}")
            try:
                tmp.rename(original)  # 尽量还原
            except OSError:
                stats["errors"].append(f"{original.name}: 还原失败，请检查 {tmp.name}")

    # 兜底：任何残留的临时文件都要还原，避免留下 __renum_tmp__
    for leftover in Path(author_dir).rglob(_TMP_PREFIX.format("*")[:-2] + "*"):
        try:
            real_name = leftover.name.split("__", 2)[-1]
            leftover.rename(leftover.with_name(real_name))
            stats["errors"].append(f"已还原残留临时文件：{real_name}")
        except Exception:  # noqa: BLE001
            stats["errors"].append(f"残留临时文件未能还原：{leftover.name}")
    return stats


def list_author_dirs(save_dir: str) -> List[Dict[str, Any]]:
    """列出保存目录下的一级子文件夹（候选作者目录），按名称排序。"""
    base = Path(save_dir) if save_dir else None
    out: List[Dict[str, Any]] = []
    if not base or not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            n = sum(1 for p in d.rglob("*") if p.is_file())
        except OSError:
            n = 0
        out.append({"name": d.name, "path": str(d), "files": n})
    return out


def renumber_all(
    save_dir: str,
    names: Optional[List[str]] = None,
    log: Optional[Callable[[str], None]] = None,
    index_width: int = 4,
) -> Dict[str, Any]:
    """对保存目录下若干作者目录统一编号（默认全部）。"""
    base = Path(save_dir) if save_dir else None

    def _log(msg: str) -> None:
        if log:
            log(msg)

    total = {
        "renamed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "dirs": 0,
        "width": index_width,
        "error": "",
    }
    if not base or not base.is_dir():
        total["error"] = f"保存目录不存在：{save_dir}"
        return total

    targets = list_author_dirs(str(base))
    if names:
        wanted = set(names)
        targets = [t for t in targets if t["name"] in wanted]
    if not targets:
        total["error"] = "没有可编号的文件夹"
        return total

    for t in targets:
        _log(f"[编号] {t['name']}：")
        st = renumber_author_dir(Path(t["path"]), log=_log, index_width=index_width)
        total["renamed"] += st["renamed"]
        total["skipped"] += st["skipped"]
        total["failed"] += st["failed"]
        total["errors"].extend(st["errors"])
        total["dirs"] += 1
        _log(
            f"    完成：重命名 {st['renamed']} 个，跳过 {st['skipped']} 个，"
            f"失败 {st['failed']} 个"
        )
    return total
