#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快手抓取测试 Worker：用已登录的持久化浏览器档案（.browser-profile，与抖音共用）
打开指定快手作者主页，拦截作品数据接口，统计能抓到多少条作品。

由 gui_app.py 以子进程方式调用（exe 自带 playwright，无需用户另装 Python 环境）。
stdout 输出会被 GUI 实时显示。设计目标：登录完成后，一键验证「能抓到该作者作品」。

实测结论（2026-10-24）：
- 作品列表来自页面自己的 XHR：``/rest/v/profile/feed``（带 __NS_hxfalcon 签名参数，
  手动 fetch 会「签名验证失败」，只能靠页面自己的请求）。
- 作品 ID 在 JSON 的 ``feeds[].photo.id``；视频直链在
  ``feeds[].photo.manifestH265.adaptationSet[].representation[].url``（含 backupUrl）。
- 快手 profile 链接有两种 ID 形态：规范 ID（如 3x75hc6f9diqgmq）feed 正常返回；
  搜索结果/分享来的 ID（如 3xiabirtm8ccjss）只能解析出作者头像昵称（profile/get），
  feed 恒返回空。后者无法从外部修复，需要用户从作者视频页进主页复制规范链接。
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from browser_compat import ensure_browsers_path, launch_chromium  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 强登录态 cookie 名（出现即代表已登录快手）
_LOGIN_COOKIE_NAMES = {"passtoken", "pass_token", "kuaishou_st", "userid", "user_id"}


def _has_login_cookie(cookies: list) -> bool:
    """从持久化档案的 cookie 里判断是否有快手登录态。"""
    for c in cookies:
        name = (c.get("name") or "").lower()
        if name in _LOGIN_COOKIE_NAMES and (c.get("value") or "").strip():
            return True
    return False


async def probe(url: str, scrolls: int = 14) -> int:
    from playwright.async_api import async_playwright

    ensure_browsers_path()
    feeds_bodies = []          # 所有 /rest/v/profile/feed 响应体
    author_name = ""           # 从 /rest/v/profile/get 拿到的作者名

    async def on_response(resp):
        nonlocal author_name
        try:
            u = resp.url
            if "kuaishou.com" not in u and "gifshow.com" not in u:
                return
            if "/rest/v/profile/feed" in u:
                feeds_bodies.append(await resp.text())
            elif "/rest/v/profile/get" in u:
                try:
                    j = json.loads(await resp.text())
                    if j.get("userName"):
                        author_name = j["userName"]
                except Exception:
                    pass
        except Exception:
            pass

    async with async_playwright() as p:
        profile_dir = ROOT / ".browser-profile"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = await launch_chromium(
                p, persistent=True, user_data_dir=str(profile_dir), headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
                user_agent=UA, locale="zh-CN",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 持久化档案不可用（{exc}），改用临时窗口")
            context = await launch_chromium(p, headless=True, user_agent=UA)

        # 中和 webdriver 标记，避免被风控识别成自动化
        try:
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
            )
        except Exception:
            pass

        page = await context.new_page()
        page.on("response", on_response)

        # ---- 登录态诊断 ----
        try:
            cookies = await context.cookies("https://www.kuaishou.com/")
        except Exception:
            cookies = []
        if _has_login_cookie(cookies):
            print("[诊断] 持久化档案中检测到快手登录态 ✔")
        else:
            print("[诊断] 未检测到快手登录态 ✘ —— 请先点左侧「快手登录」完成登录再测。")

        print(f"[快手抓取测试] 打开: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 打开失败：{e}")
        await page.wait_for_timeout(2500)

        # ---- 滚动触发分页加载（feed 接口带 pcursor 分页）----
        for _ in range(scrolls):
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            try:
                await page.mouse.wheel(0, 1600)
            except Exception:
                pass
            await page.wait_for_timeout(1000)

        await page.wait_for_timeout(2000)

        # ---- 统计：作品 ID 在 feeds[].photo.id，按 ID 去重 ----
        ids = set()
        has_video_url = False
        for body in feeds_bodies:
            try:
                j = json.loads(body)
            except Exception:
                continue
            for f in (j.get("feeds") or []):
                ph = f.get("photo") or {}
                pid = ph.get("id") or ph.get("photoId") or f.get("photoId")
                if pid:
                    ids.add(str(pid))
                if ph.get("manifestH265") or ph.get("manifest"):
                    has_video_url = True
        work_count = len(ids)

        print(f"[诊断] 作者：{author_name or '（未解析到）'}；"
              f"feed 响应 {len(feeds_bodies)} 个、去重作品 {work_count} 条"
              f"{'（含视频直链）' if has_video_url else ''}")

        await context.close()
        return work_count


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    if not url or "kuaishou.com" not in url:
        print("用法: kuaishou_probe_worker.py <快手作者主页链接>")
        return 1
    print("[INFO] 正在用已登录档案抓取该作者作品列表（约 20 秒）…")
    count = asyncio.run(probe(url))
    if count > 0:
        print(f"[OK] 抓到 {count} 条作品数据 —— 快手抓取链路可用，可进入正式集成。")
        return 0
    print("[FAIL] 未抓到作品数据。可能原因：")
    print("  1) 未完成「快手登录」（见上方登录态诊断）。")
    print("  2) 该链接是搜索结果/分享来源的 ID：只能解析作者头像昵称，")
    print("     作品流接口不认这种 ID。解决办法：打开该作者任意一个视频，")
    print("     在视频页点作者名字进主页，从地址栏复制 /profile/ 链接再测。")
    print("  3) 作者确实没有公开作品。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
