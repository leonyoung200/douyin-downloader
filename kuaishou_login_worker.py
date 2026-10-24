#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快手登录 Worker：打开有头浏览器供用户手动登录快手，检测到登录后把登录态写进
持久化浏览器档案（与抖音共用 .browser-profile），并额外把快手 Cookie 存到 config.yml 的
``kuaishou_cookies`` 键，供后续快手采集复用。

复用本项目的 browser_compat 启动本机 Chromium。Cookie 按域名隔离，所以同一个
.browser-profile 里同时存抖音 + 快手登录态互不干扰。
"""
import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from browser_compat import ensure_browsers_path, launch_chromium  # noqa: E402

KUAISHOU_URL = "https://www.kuaishou.com"
TIMEOUT_SECONDS = 600  # 最长等待 10 分钟
POLL_INTERVAL = 2


def _save_kuaishou_cookies(cookies: dict) -> None:
    """把快手登录 Cookie 合并写入 config.yml 的 kuaishou_cookies 键（不动抖音的 cookies）。"""
    cfg_path = ROOT / "config.yml"
    data = {}
    if cfg_path.exists():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
    data["kuaishou_cookies"] = cookies
    cfg_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[ERROR] Playwright 未安装，请先运行 pip install playwright")
        return 1

    async with async_playwright() as p:
        ensure_browsers_path()
        profile_dir = ROOT / ".browser-profile"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = await launch_chromium(
                p,
                persistent=True,
                user_data_dir=str(profile_dir),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
                locale="zh-CN",
                viewport={"width": 1280, "height": 800},
            )
            persistent = True
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] 持久化配置不可用（{exc}），使用临时窗口")
            browser = await launch_chromium(p, headless=False)
            context = await browser.new_context(locale="zh-CN")
            persistent = False

        page = await context.new_page()
        print("[INFO] 浏览器已打开，请在窗口中完成快手登录（扫码或账号密码均可）")
        print("[INFO] 登录成功后本窗口会自动关闭并保存登录状态，无需其他操作")
        await page.goto(KUAISHOU_URL, wait_until="domcontentloaded", timeout=60_000)

        cookies: dict = {}
        logged_in = False
        for _ in range(TIMEOUT_SECONDS // POLL_INTERVAL):
            await asyncio.sleep(POLL_INTERVAL)
            try:
                raw = await context.cookies(KUAISHOU_URL)
            except Exception:  # noqa: BLE001
                continue
            cookies = {c["name"]: c["value"] for c in raw}
            # 快手登录标志：userId / passToken 任一非空即视为已登录
            if (cookies.get("userId") or cookies.get("passToken") or cookies.get("user_id")):
                logged_in = True
                break
            print("[WAIT] 等待登录…")

        if not logged_in:
            print("[TIMEOUT] 等待登录超时（10分钟），请重试")
            await context.close()
            return 2

        print("[INFO] 检测到快手登录成功，正在保存登录态…")
        picked = {k: v for k, v in cookies.items()
                  if k in ("did", "clientid", "kpn", "userId", "user_id", "passToken",
                           "kuaishou.com", "sid", "sessionid", "webid", "visitorId")}
        _save_kuaishou_cookies(picked)
        print(f"[OK] 已保存 {len(picked)} 个快手 Cookie，登录状态已就绪")
        if persistent:
            print("[OK] 登录态已写入持久化浏览器配置，后续采集无需再次登录")
        await context.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
