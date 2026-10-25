#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快手抓取可行性探针（最小脚本）。

复用本项目的 browser_compat 启动本机 Chromium（与抖音绕反爬同一套），
打开快手页面，拦截 graphql / JSON 响应，看能否抓到作品数据（photoId / workId / caption 等）。

用法：
    python probe_kuaishou.py <快手主页链接> [滚动次数(默认8)]
    python probe_kuaishou.py                      # 仅测 www.kuaishou.com 是否可达

探针只「看」不「下」，不会修改任何文件，也不会触发登录。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 与项目同目录，复用 browser_compat / launch_chromium
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

HIT_KEYS = ("photoId", "workId", "visionProfile", "caption", "photoCount",
            "visionWorkList", "photoUrl", "playUrl", "ownerName", "userId")


async def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "https://www.kuaishou.com"
    scrolls = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    # 可选：--profile <目录> 指向已登录的浏览器档案（集成时与抖音共用 .browser-profile）
    profile = None
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        profile = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    from browser_compat import ensure_browsers_path, launch_chromium
    from playwright.async_api import async_playwright

    ensure_browsers_path()

    hits = []  # (key, url, bytes)
    sample_bodies = []  # (url, raw_json_str)

    async with async_playwright() as p:
        if profile:
            print(f"[探针] 使用已登录档案: {profile}")
            browser = await launch_chromium(
                p, persistent=True, user_data_dir=profile, headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
            page = await browser.new_page()
        else:
            browser = await launch_chromium(
                p, headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(user_agent=UA, locale="zh-CN")
            page = await ctx.new_page()

        async def on_response(resp):
            try:
                url = resp.url
                if "kuaishou.com" not in url and "gifshow.com" not in url:
                    return
                ct = resp.headers.get("content-type", "") or ""
                if "json" not in ct.lower():
                    return
                body = await resp.text()
            except Exception:
                return
            if len(body) < 80:
                return
            try:
                data = json.loads(body)
                s = json.dumps(data, ensure_ascii=False)
            except Exception:
                return
            for key in HIT_KEYS:
                if key in s:
                    hits.append((key, url, len(s)))
                    if len(sample_bodies) < 6:
                        sample_bodies.append((url, s))
                    break

        page.on("response", on_response)

        print(f"[探针] 打开: {target}")
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            print(f"[探针] 初始加载完成，标题: {await page.title()!r}")
        except Exception as e:
            print(f"[探针] goto 失败: {e}")

        # 滚动触发懒加载 / 接口请求
        for i in range(scrolls):
            try:
                await page.mouse.wheel(0, 1600)
                await page.wait_for_timeout(1100)
            except Exception:
                pass
        await page.wait_for_timeout(2000)

        print(f"[探针] 命中疑似作品数据的响应: {len(hits)} 条")
        for key, url, n in hits[:25]:
            print(f"  命中[{key}] {n}B  {url[:96]}")

        # 从抓到的响应里抽样展示作品（视频ID / 作者 / 标题）
        import re
        pid_re = re.compile(r'"photoId"\s*:\s*"([^"]{4,40})"')
        oid_re = re.compile(r'"ownerName"\s*:\s*"([^"]{1,30})"')
        cap_re = re.compile(r'"caption"\s*:\s*"((?:[^"\\]|\\.){1,60})"')
        shown = 0
        print("[探针] 抽样作品：")
        for url, s in sample_bodies:
            for m in pid_re.finditer(s):
                pid = m.group(1)
                oid = (oid_re.search(s[m.start():m.start() + 2000]) or (None,)) and oid_re.search(s[m.start():m.start()+2000]).group(1)
                cap = cap_re.search(s[m.start():m.start() + 4000])
                cap = cap.group(1)[:30] if cap else ""
                print(f"  - 视频ID={pid}  作者={oid}  标题={cap}")
                shown += 1
                if shown >= 5:
                    break
            if shown >= 5:
                break
        if shown == 0:
            print("  （未在响应体中直接解析出 photoId，可能结构不同，需进一步分析）")

        # 看看页面里有没有登录墙/验证码提示
        txt = ""
        try:
            txt = (await page.evaluate("document.body ? document.body.innerText : ''")) or ""
        except Exception:
            pass
        for marker in ("验证码", "slider", "登录后", "请登录", "安全验证", "网络异常", "访问过于频繁"):
            if marker in txt:
                print(f"[探针] 页面出现提示词: {marker!r}")

        await browser.close()

    if hits:
        print("[结论] 本环境能抓到快手作品数据 —— 可行性 OK，可进入完整集成。")
        return 0
    print("[结论] 未抓到作品数据（可能需登录、被风控、或网络不可达）—— 需进一步判断。")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
