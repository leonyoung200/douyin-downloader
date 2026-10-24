#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅数据存取：订阅博主名单 + 每日自动下载状态。

数据文件：subscriptions.json（与 gui_app.py 同目录，即 exe 所在目录）。
每条订阅记录：
    sec_uid       博主 ID（抖音 sec_uid / 快手 profile ID，唯一键）
    platform      归属平台：'douyin' / 'kuaishou'（决定每日自动下载走哪条通道）
    nickname      博主昵称（订阅时从下载数据库反查；快手由用户填写）
    url           博主主页完整链接（每日自动下载用）
    save_dir      该博主的保存目录
    types         下载类型（video/cover/frame/music/avatar/json）
    subscribed_at 订阅时间
    last_run_date 上次自动下载日期（YYYY-MM-DD，空=从未跑过）
    last_run_new  上次自动下载新增文件数
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# FROZEN（PyInstaller onefile）时 __file__ 指向 _MEI 临时解压目录，退出即删；
# 数据文件必须落在 exe 所在目录（sys.executable 的目录）才能持久化。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent

STORE_FILE = ROOT / "subscriptions.json"


def load() -> List[Dict[str, Any]]:
    if not STORE_FILE.exists():
        return []
    try:
        data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        authors = data.get("authors") if isinstance(data, dict) else None
        if isinstance(authors, list):
            return [a for a in authors if isinstance(a, dict) and a.get("sec_uid")]
    except Exception:
        pass
    return []


def save(authors: List[Dict[str, Any]]) -> None:
    try:
        STORE_FILE.write_text(
            json.dumps({"version": 1, "authors": authors}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def find(sec_uid: str) -> Optional[Dict[str, Any]]:
    for a in load():
        if a.get("sec_uid") == sec_uid:
            return a
    return None


def add(entry: Dict[str, Any]) -> bool:
    """新增订阅；已存在则更新可复原字段（目录/类型/昵称）。返回是否有变化。"""
    authors = load()
    sec_uid = (entry.get("sec_uid") or "").strip()
    if not sec_uid:
        return False
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for a in authors:
        if a.get("sec_uid") == sec_uid:
            changed = False
            for k in ("nickname", "url", "save_dir", "types", "platform"):
                if entry.get(k) and a.get(k) != entry.get(k):
                    a[k] = entry[k]
                    changed = True
            if changed:
                save(authors)
            return changed
    entry = dict(entry)
    entry["sec_uid"] = sec_uid
    entry.setdefault("nickname", "")
    entry.setdefault("platform", platform_from_url(entry.get("url") or ""))
    entry.setdefault("subscribed_at", now)
    entry.setdefault("last_run_date", "")
    entry.setdefault("last_run_new", 0)
    authors.append(entry)
    save(authors)
    return True


def remove(sec_uid: str) -> bool:
    authors = load()
    left = [a for a in authors if a.get("sec_uid") != sec_uid]
    if len(left) != len(authors):
        save(left)
        return True
    return False


def rename(sec_uid: str, new_nickname: str) -> bool:
    """更新订阅昵称（下载文件夹名、列表显示、新增统计都跟着变）。"""
    new = (new_nickname or "").strip()
    if not new:
        return False
    authors = load()
    hit = False
    for a in authors:
        if a.get("sec_uid") == sec_uid and a.get("nickname") != new:
            a["nickname"] = new
            hit = True
    if hit:
        save(authors)
    return hit


def mark_ran(sec_uid: str, date_str: str, new_count: int) -> None:
    authors = load()
    for a in authors:
        if a.get("sec_uid") == sec_uid:
            a["last_run_date"] = date_str
            a["last_run_new"] = int(new_count or 0)
    save(authors)


def pending_today(date_str: str) -> List[Dict[str, Any]]:
    """今天还没自动下载过的订阅（last_run_date != date_str）。"""
    return [a for a in load() if a.get("last_run_date") != date_str]


def sec_uid_from_url(url: str) -> str:
    """从博主主页链接提取 ID（抖音 sec_uid 或快手 profile ID），失败返回空串。"""
    u = (url or "").strip()
    if "/user/" in u:
        tail = u.split("/user/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0].strip()
    if "/profile/" in u:  # 快手
        tail = u.split("/profile/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0].strip()
    return ""


def platform_from_url(url: str) -> str:
    """链接归属平台：'kuaishou' / 'douyin'（默认按抖音处理）。"""
    return "kuaishou" if "kuaishou.com" in (url or "") else "douyin"
