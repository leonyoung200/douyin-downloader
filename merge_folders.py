#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把多个博主文件夹合并成一个（同一博主的多账号 / 多平台去重合并）。

使用场景：一位博主在抖音、快手各有账号（甚至多个），视频有重复也有各自独有的。
本模块把若干作者文件夹里的文件移动合并到同一个目标文件夹，重复的只留一份。

判重规则（按「标题核心」匹配，而非文件哈希——跨平台同一视频转码参数不同，
二进制不一样，哈希去重无效）：
  - 完全同名                → 跳过（保留目标里已有的）；
  - 标题核心相同且类别相同  → 重复，保留体积大的（抖音「日期_标题_awemeid.mp4」
    与快手「标题.mp4」会被判为同一作品）；
  - 附属文件（_frame1/_cover/_avatar/_music/.json）按各自的标题核心匹配。

目标路径统一为 <保存目录>/<统一名>/<原文件名>（不建日期层，由用户自行整理）。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

MEDIA_SUFFIXES = {".mp4", ".mov", ".mkv", ".flv", ".webm", ".m4v"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_AWEME_ID_TAIL = re.compile(r"[_\s]?\d{15,20}$")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{6,8})[_\s]+")


def file_kind(name: str) -> str:
    """文件类别：media / frame / cover / avatar / music / json / other。"""
    n = name.lower()
    if n.endswith("_frame1.jpg") or n.endswith("_frame1.jpeg") or n.endswith("_frame1.png"):
        return "frame"
    if n.endswith("_cover.jpg") or n.endswith("_cover.jpeg") or n.endswith("_cover.png"):
        return "cover"
    if n.endswith("_avatar.jpg") or n.endswith("_avatar.jpeg") or n.endswith("_avatar.png"):
        return "avatar"
    if n.endswith("_music.mp3") or n.endswith("_music.m4a") or n.endswith("_music.aac"):
        return "music"
    if n.endswith(".json"):
        return "json"
    if Path(n).suffix in MEDIA_SUFFIXES:
        return "media"
    return "other"


def core_title(name: str) -> str:
    """提取标题核心：去附属标记、aweme_id 后缀、日期前缀、扩展名。

    「2026-10-24_我家猫会后空翻_7447860303028868385.mp4」→「我家猫会后空翻」
    「我家猫会后空翻.mp4」                            →「我家猫会后空翻」
    """
    stem = Path(name).stem
    for tag in ("_frame1", "_cover", "_avatar", "_music"):
        if stem.lower().endswith(tag):
            stem = stem[: -len(tag)]
            break
    stem = _AWEME_ID_TAIL.sub("", stem)
    stem = _DATE_PREFIX.sub("", stem)
    return stem.strip().lower()


def scan_author_dirs(save_dir: str) -> List[Dict[str, Any]]:
    """扫描保存目录下的一级子文件夹（候选作者文件夹），按文件数降序。"""
    base = Path(save_dir) if save_dir else None
    if not base or not base.is_dir():
        return []
    out = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        n = sum(1 for p in d.rglob("*") if p.is_file())
        if n:
            out.append({"name": d.name, "path": str(d), "files": n})
    out.sort(key=lambda x: x["files"], reverse=True)
    return out


def _drop_dup(p: Path) -> None:
    """删除判定为重复的源文件（合并去重：重复只保留一份）。"""
    try:
        p.unlink()
    except OSError:
        pass


def _index_dir(target: Path) -> Dict[Tuple[str, str], List[Path]]:
    """给目标目录建 (kind, core_title) 索引，用于跨格式判重。"""
    idx: Dict[Tuple[str, str], List[Path]] = {}
    if not target.is_dir():
        return idx
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        key = (file_kind(p.name), core_title(p.name))
        idx.setdefault(key, []).append(p)
    return idx


def merge_folders(save_dir: str, source_names: List[str], target_name: str,
                  log=None) -> Dict[str, Any]:
    """把 source_names（保存目录下的文件夹名）合并进 <save_dir>/<target_name>/。

    返回 {"moved": int, "skipped": int, "failed": int, "removed_dirs": [..],
          "target": str, "errors": [..]}
    """
    def _log(msg: str):
        if log:
            log(msg)

    base = Path(save_dir)
    target_name = (target_name or "").strip()
    if not target_name:
        return {"error": "目标文件夹名不能为空"}
    target = base / target_name
    stats = {"moved": 0, "skipped": 0, "failed": 0, "removed_dirs": [],
             "target": str(target), "errors": []}
    target.mkdir(parents=True, exist_ok=True)
    idx = _index_dir(target)

    for name in source_names:
        src = base / name
        if not src.is_dir():
            continue
        if src.resolve() == target.resolve():
            _log(f"[合并] 跳过：{name} 就是目标文件夹本身")
            continue
        _log(f"[合并] 处理文件夹：{name}")
        # 按路径排序保证确定性；日期子目录里的文件也全部上提
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            kind = file_kind(p.name)
            if kind == "other":
                # 非媒体文件（如说明txt）按原名去重即可
                dest = target / p.name
                if dest.exists():
                    stats["skipped"] += 1
                    _drop_dup(p)
                    continue
                try:
                    shutil.move(str(p), str(dest))
                    stats["moved"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{p.name}: {e}")
                continue
            key = (kind, core_title(p.name))
            dest = target / p.name
            if dest.exists():
                stats["skipped"] += 1
                _log(f"    重复跳过（同名）：{p.name}")
                _drop_dup(p)
                continue
            dupes = idx.get(key) or []
            if dupes:
                # 标题核心相同 → 视为同一作品，保留体积大的
                try:
                    new_size = p.stat().st_size
                except OSError:
                    new_size = 0
                biggest = max(dupes, key=lambda q: q.stat().st_size if q.exists() else 0)
                try:
                    old_size = biggest.stat().st_size
                except OSError:
                    old_size = 0
                if new_size > old_size:
                    try:
                        shutil.move(str(p), str(dest))
                        biggest.unlink()
                        idx[key] = [dest]
                        stats["moved"] += 1
                        stats["skipped"] += 1  # 旧的被替换掉，记一次跳过
                        _log(f"    重复合并（保留更大的）：{p.name} 替换 {biggest.name}")
                        continue
                    except Exception as e:
                        stats["failed"] += 1
                        stats["errors"].append(f"{p.name}: {e}")
                        continue
                stats["skipped"] += 1
                _log(f"    重复跳过（已有 {biggest.name}）：{p.name}")
                _drop_dup(p)
                continue
            try:
                shutil.move(str(p), str(dest))
                stats["moved"] += 1
                idx.setdefault(key, []).append(dest)
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append(f"{p.name}: {e}")
        # 源文件夹里已无文件（只剩空目录链）就整体删除
        try:
            if src.is_dir() and not any(p.is_file() for p in src.rglob("*")):
                shutil.rmtree(src)
                stats["removed_dirs"].append(name)
                _log(f"[合并] 已删除空文件夹：{name}")
        except OSError:
            pass
    return stats
