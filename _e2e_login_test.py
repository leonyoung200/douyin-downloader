# -*- coding: utf-8 -*-
"""端到端验证：打包版 exe 的登录态是否落盘到 exe 同级目录。

步骤：
1. 用 venv playwright 在 <test>/.browser-profile 预置一个假 sessionid Cookie
2. 跑 <test>/抖快批量下载器.exe --login（它会打开同一 profile，轮询到 sessionid 后存 config.yml）
3. 检查 <test>/config.yml 是否生成且包含该 Cookie —— 证明 frozen 模式下 ROOT=exe目录
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TEST = Path(r"D:\fix-test")
EXE = TEST / "抖快批量下载器.exe"
PROFILE = TEST / ".browser-profile"
CONFIG = TEST / "config.yml"
FAKE_SESSION = "e2e_fake_sessionid_proof_123456"

# 1. 预置假 Cookie 到持久化 profile
sys.path.insert(0, r"D:\douyin-downloader")
from browser_compat import ensure_browsers_path, launch_chromium  # noqa: E402
import asyncio


async def seed_profile():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        ensure_browsers_path()
        PROFILE.mkdir(parents=True, exist_ok=True)
        ctx = await launch_chromium(
            p, persistent=True, user_data_dir=str(PROFILE), headless=True,
            args=["--no-sandbox"],
        )
        await ctx.add_cookies([{
            "name": "sessionid", "value": FAKE_SESSION,
            "domain": ".douyin.com", "path": "/",
        }])
        got = await ctx.cookies("https://www.douyin.com")
        names = [c["name"] for c in got]
        print("[seed] profile cookies:", names)
        await ctx.close()
        return "sessionid" in names


ok = asyncio.run(seed_profile())
print("[seed] sessionid 预置:", "OK" if ok else "FAIL")
if not ok:
    raise SystemExit("SEED_FAIL")

# 2. 跑打包版 --login（应在几秒内检测到登录并退出）
print("[run] 启动 exe --login ...")
proc = subprocess.Popen(
    [str(EXE), "--login"],
    cwd=str(TEST),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
try:
    out, _ = proc.communicate(timeout=180)
except subprocess.TimeoutExpired:
    proc.kill()
    out, _ = proc.communicate()
    print("[run] TIMEOUT（不该发生）")
print("[run] exit code:", proc.returncode)
print("[run] ---- 输出 ----")
print(out[-1500:])
print("[run] --------------")

# 3. 检查 config.yml 落点
if CONFIG.exists():
    text = CONFIG.read_text(encoding="utf-8")
    hit = FAKE_SESSION in text
    print(f"[check] {CONFIG} 已生成（{len(text)} 字符），包含假 sessionid: {hit}")
else:
    print("[check] config.yml 未生成 —— 修复失败！")

# 4. 确认没有把数据写进 _MEI 临时目录（找最近 5 分钟内的 _MEI 残留）
import tempfile
tmp = Path(tempfile.gettempdir())
leaks = []
for d in tmp.glob("_MEI*"):
    try:
        if (time.time() - d.stat().st_mtime) < 300:
            for f in d.rglob("config.yml"):
                leaks.append(str(f))
            if (d / ".browser-profile").exists():
                leaks.append(str(d / ".browser-profile"))
    except OSError:
        pass
print("[check] _MEI 临时目录数据泄漏:", leaks if leaks else "无")

verdict = CONFIG.exists() and FAKE_SESSION in CONFIG.read_text(encoding="utf-8") and not leaks
print("E2E_LOGIN_" + ("PASS" if verdict else "FAIL"))
