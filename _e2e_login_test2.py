# -*- coding: utf-8 -*-
"""干净复测：全新目录 + _MEI 实时监听 + 预置 Cookie 落盘校验 + 完整输出。"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TEST = Path(r"D:\fix-test3")
EXE = TEST / "抖快批量下载器.exe"
PROFILE = TEST / ".browser-profile"
CONFIG = TEST / "config.yml"
FAKE_SESSION = "e2e_fake_sessionid_proof_778899"

sys.path.insert(0, r"D:\douyin-downloader")
from browser_compat import ensure_browsers_path, launch_chromium  # noqa: E402
import asyncio


def mei_snapshot() -> set:
    return {p.name for p in Path(tempfile.gettempdir()).glob("_MEI*")}


async def seed_and_verify():
    """预置 Cookie，并用第二个 context 从磁盘读回，验证落盘。"""
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
        await ctx.close()
        # 第二个 context：从磁盘读回
        ctx2 = await launch_chromium(
            p, persistent=True, user_data_dir=str(PROFILE), headless=True,
            args=["--no-sandbox"],
        )
        got = await ctx2.cookies("https://www.douyin.com")
        names = {c["name"]: c["value"] for c in got}
        await ctx2.close()
        return names.get("sessionid")


before = mei_snapshot()
print("[pre ] _MEI 目录快照:", sorted(before) or "无")
disk_session = asyncio.run(seed_and_verify())
print("[seed] 磁盘读回 sessionid:", (disk_session or "(无)")[:30])
if disk_session != FAKE_SESSION:
    print("[seed] 预置 Cookie 未落盘 —— 测试方法问题，换用「已登录真配置」方案")

print("[run ] 启动 exe --login ...")
t0 = time.time()
proc = subprocess.Popen(
    [str(EXE), "--login"], cwd=str(TEST),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
try:
    out, _ = proc.communicate(timeout=240)
except subprocess.TimeoutExpired:
    proc.kill(); out, _ = proc.communicate(); print("[run ] TIMEOUT")
after = mei_snapshot()
print(f"[run ] exit={proc.returncode} 耗时={time.time()-t0:.0f}s")
print("[run ] ---- 完整输出 ----")
print(out)
print("[run ] -------------------")
print("[post] 新增 _MEI 目录:", sorted(after - before) or "无")

for d in sorted(after - before):
    prof = Path(tempfile.gettempdir()) / d / ".browser-profile"
    print(f"[post] {d}/.browser-profile 存在: {prof.exists()}")

if CONFIG.exists():
    text = CONFIG.read_text(encoding="utf-8")
    print(f"[check] {CONFIG} 已生成（{len(text)} 字符）")
    print(f"[check] 含假 sessionid: {FAKE_SESSION in text}")
    import yaml
    cfg = yaml.safe_load(text)
    ck = (cfg.get("cookies") or {})
    print(f"[check] config.yml cookies 键: {sorted(ck.keys())[:12]}{'...' if len(ck) > 12 else ''}")
    print(f"[check] sessionid 值: {(ck.get('sessionid') or '(无)')[:30]}")
else:
    print("[check] config.yml 未生成 —— 修复失败！")

# 关键判定：config.yml 必须落在 exe 目录（这就是用户 bug 的修复点）
verdict = CONFIG.exists()
print("E2E_MAIN_" + ("PASS" if verdict else "FAIL"))
