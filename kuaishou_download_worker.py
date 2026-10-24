#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快手下载 Worker：用已登录的持久化浏览器档案（.browser-profile）抓取快手作者
全部作品并下载到本地。

由 gui_app.py 以子进程方式调用（exe 自带 playwright，无需用户另装 Python 环境）：

    抖音批量下载器.exe --kdl <task.json>

task.json 字段：
    urls          快手作者主页链接列表（规范 /profile/<id> 形态）
    save_dir      保存目录（文件落在 <save_dir>/<作者名>/ 下）
    types         下载类型：video/cover/frame/music/avatar/json
    number        数量限制（0=全部）
    start_time    起始日期 YYYY-MM-DD（可空）
    end_time      截止日期 YYYY-MM-DD（可空）
    date_folders  是否把发布日期加在文件名前（如 2026-10-24_标题.mp4，默认 true；
                  不建日期子文件夹，文件平铺在作者目录下）
    author_name   作者名覆盖（订阅场景由用户指定；为空则自动识别）

实现要点（2026-10-24 实测）：
- 作品列表唯一通道 = 页面自己的 POST /rest/v/profile/feed（URL 带 __NS_hxfalcon
  签名，body 为 {"user_id","pcursor","page"}；签名不校验 body，可用页面捕获的
  签名 URL 重放翻页，pcursor 取自上一次响应，每页 20 条）。
- 作者名**不能**用 /rest/v/profile/get——那个接口返回的是当前登录用户本人。
  真实作者名从作品视频页（/short-video/<id>）的接口响应里提取；拿不到则用
  链接里的 profile ID 兜底；订阅场景直接用用户指定的名字。
- 作品 ID 在 feeds[].photo.id；视频直链优先取 photo.manifest（H264）的
  adaptationSet[].representation[].url（按码率取最高），兜底 manifestH265 /
  photoUrls；每个 representation 还带 backupUrl。
- 第一帧截图不在这里做：GUI 的 ExtractWorker 会扫描保存目录自动补 _frame1.jpg。
- 快手接口不提供音乐信息，types 含 music 时明确提示跳过。
- 快手返回 result=109 + loginUrl = 登录态失效/被风控，需重新登录（明确报出）。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_compat import ensure_browsers_path, launch_chromium  # noqa: E402
from renumber_folders import strip_index as _strip_index  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
FEED_PATH = "/rest/v/profile/feed"
VIDEO_PATH = "/short-video/"
MAX_PAGES = 300          # 翻页上限（300 页 * 20 条 = 6000 条）
MAX_WORKS = 6000
DL_CONCURRENCY = 6       # 下载并发数（抖音 CLI 默认 5，这里略高）
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_LOGIN_COOKIE_NAMES = {"passtoken", "pass_token", "kuaishou_st", "userid", "user_id"}


def log(msg: str) -> None:
    print(msg, flush=True)


def sanitize_name(name: str, fallback: str = "未命名") -> str:
    """把标题清理成合法文件名（保留 emoji，截断超长）。"""
    s = ILLEGAL.sub(" ", str(name or "")).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    if not s:
        s = fallback
    return s[:80]


def _pick_video_urls(photo: Dict[str, Any]) -> List[str]:
    """从作品 JSON 里挑视频直链：优先 H264(manifest) 最高码率，兜底 H265/photoUrls。"""
    urls: List[str] = []

    def collect(manifest: Any) -> List[Tuple[float, str]]:
        out: List[Tuple[float, str]] = []
        if not isinstance(manifest, dict):
            return out
        for aset in manifest.get("adaptationSet") or []:
            if not isinstance(aset, dict):
                continue
            for rep in aset.get("representation") or []:
                if not isinstance(rep, dict):
                    continue
                u = rep.get("url")
                if not u:
                    continue
                br = rep.get("avgBitrate") or rep.get("maxBitrate") or 0
                try:
                    br = float(br)
                except (TypeError, ValueError):
                    br = 0.0
                out.append((br, u))
                for b in rep.get("backupUrl") or []:
                    if b:
                        out.append((br - 1, b))  # 备用地址略低优先级
        return out

    for key in ("manifest", "manifestH265"):  # H264 优先（兼容性更好）
        cands = collect(photo.get(key))
        cands.sort(key=lambda x: -x[0])
        if cands:
            urls.extend(u for _, u in cands)
            break
    if not urls:
        for item in photo.get("photoUrls") or []:
            if isinstance(item, dict) and item.get("url"):
                urls.append(item["url"])
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _http_download(urls: List[str], dest: Path, min_size: int = 8 * 1024) -> bool:
    """普通 HTTP 下载（CDN 直链一般无需登录态），流式写 .part 再改名。"""
    for u in urls:
        if not u:
            continue
        tmp = dest.with_name(dest.name + ".part")
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": UA, "Referer": "https://www.kuaishou.com/"}
            )
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, length=512 * 1024)
            if tmp.exists() and tmp.stat().st_size >= min_size:
                tmp.replace(dest)
                return True
            tmp.unlink(missing_ok=True)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
    return False


async def _browser_download(context, urls: List[str], dest: Path,
                            min_size: int = 8 * 1024) -> bool:
    """HTTP 失败时用浏览器上下文下载（带上登录态 Cookie）。"""
    for u in urls:
        if not u:
            continue
        try:
            resp = await context.request.get(u, timeout=120_000)
            if resp.ok:
                body = await resp.body()
                if len(body) >= min_size:
                    dest.write_bytes(body)
                    return True
        except Exception:
            continue
    return False


# ---------- 作者名识别 ----------
_NAME_KEYS = ("userName", "user_name", "author_name", "authorName", "nickname")
_AUTHOR_HINT_KEYS = ("eid", "userId", "user_id", "fans", "fan", "kwaiId",
                     "headUrl", "userHead", "avatar", "follow")


def _scan_author(obj: Any, out: List[Dict[str, Any]], depth: int = 0) -> None:
    """递归扫描 JSON，收集「看起来是作者」的对象（带昵称 + 作者特征字段）。"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str)), None)
        if name and any(k in obj for k in _AUTHOR_HINT_KEYS):
            out.append({
                "name": name,
                "avatar": next((obj[k] for k in ("userHead", "headUrl", "avatar")
                                if isinstance(obj.get(k), str)), ""),
            })
        for v in obj.values():
            _scan_author(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:30]:
            _scan_author(v, out, depth + 1)


class KuaishouCrawler:
    """抓取 + 下载一位作者的全部作品。"""

    def __init__(self, context, opts: Dict[str, Any]):
        self.ctx = context
        self.types = opts.get("types") or ["video"]
        self.number = int(opts.get("number") or 0)
        self.start_time = (opts.get("start_time") or "").strip()
        self.end_time = (opts.get("end_time") or "").strip()
        self.save_root = Path(opts.get("save_dir") or ".")
        self.date_folders = bool(opts.get("date_folders", True))
        self.author_override = (opts.get("author_name") or "").strip()
        self.stats = {"ok": 0, "skip": 0, "fail": 0, "collected": 0}
        self.author: Dict[str, Any] = {}

    # ---------- 日期过滤 / 日期文件夹 ----------
    def _in_date_range(self, ts_ms: Any) -> bool:
        if not self.start_time and not self.end_time:
            return True
        try:
            d = datetime.datetime.fromtimestamp(int(ts_ms) / 1000).date()
        except Exception:
            return True
        if self.start_time and d < datetime.date.fromisoformat(self.start_time):
            return False
        if self.end_time and d > datetime.date.fromisoformat(self.end_time):
            return False
        return True

    @staticmethod
    def _date_folder(ts_ms: Any) -> str:
        try:
            return datetime.datetime.fromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d")
        except Exception:
            return "未知日期"

    # ---------- 采集 ----------
    async def collect(self, url: str) -> List[Dict[str, Any]]:
        """打开作者主页，拦截 feed 接口并翻页，返回作品列表。"""
        works: List[Dict[str, Any]] = []
        seen_ids = set()
        feed_url: Optional[str] = None
        first_pcursor = ""
        login_wall = False
        user_id = ""
        m = re.search(r"/profile/([^/?#]+)", url)
        if m:
            user_id = m.group(1)

        async def on_response(resp):
            nonlocal feed_url, first_pcursor, login_wall
            try:
                u = resp.url
                if "kuaishou.com" not in u and "gifshow.com" not in u:
                    return
                if FEED_PATH not in u or resp.request.method != "POST":
                    return
                feed_url = u
                body = await resp.text()
                try:
                    j = json.loads(body)
                except Exception:
                    return
                if j.get("loginUrl") or (j.get("result") not in (None, 1)):
                    login_wall = True
                    return
                if not first_pcursor:
                    first_pcursor = str(j.get("pcursor") or "")
                for f in j.get("feeds") or []:
                    ph = f.get("photo") or {}
                    pid = str(ph.get("id") or "")
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    works.append(f)
            except Exception:
                pass

        page = await self.ctx.new_page()
        page.on("response", on_response)
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"[WARN] 打开失败：{e}")
            await page.wait_for_timeout(3000)
            for _ in range(4):  # 轻滚几下，确保首个 feed 请求发出
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                await page.wait_for_timeout(600)
        finally:
            await page.close()

        if login_wall:
            log("[KS][FAIL] 快手返回了登录墙（result=109 / loginUrl）：")
            log("      登录态已失效或被风控。请回到软件左侧点「快手登录」重新登录，")
            log("      稍等几分钟再试（频繁自动请求会触发风控）。")
            return []

        if not feed_url:
            return works

        # ---- 用页面签名 URL 重放翻页（游标从第一页响应播种）----
        pcursor = first_pcursor
        pages = 0
        while pages < MAX_PAGES and len(works) < MAX_WORKS:
            if not pcursor or pcursor == "no_more":
                break
            pages += 1
            body = json.dumps(
                {"user_id": user_id, "pcursor": pcursor, "page": "profile"}
            )
            try:
                resp = await self.ctx.request.post(
                    feed_url, data=body,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json",
                        "referer": url,
                    },
                )
                j = json.loads(await resp.text())
            except Exception as e:
                log(f"[WARN] 翻页请求失败（第 {pages} 页）：{e}")
                break
            if j.get("loginUrl") or (j.get("result") not in (None, 1)):
                log("[KS][FAIL] 翻页时遇到登录墙，停止翻页（请重新登录后再试）。")
                break
            new_feeds = j.get("feeds") or []
            added = 0
            for f in new_feeds:
                ph = f.get("photo") or {}
                pid = str(ph.get("id") or "")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                works.append(f)
                added += 1
            pcursor = str(j.get("pcursor") or "")
            if not new_feeds or added == 0 or not pcursor or pcursor == "no_more":
                break
            if self.number and len(works) >= self.number:
                break
            await asyncio.sleep(0.3)
        return works

    # ---------- 作者名 ----------
    async def resolve_author(self, works: List[Dict[str, Any]], url: str) -> str:
        """确定作者名：任务覆盖 > 视频页接口提取 > 链接里的 profile ID 兜底。

        注意：/rest/v/profile/get 返回的是当前登录用户本人，不能用来当作者名。
        """
        if self.author_override:
            log(f"[KS] 作者名（订阅指定）：{self.author_override}")
            self.author["name"] = self.author_override
            return self.author_override

        # 用第一条作品的视频页提取真实作者
        first_id = ""
        for w in works:
            first_id = str((w.get("photo") or {}).get("id") or "")
            if first_id:
                break
        if first_id:
            candidates: List[Dict[str, Any]] = []
            page = await self.ctx.new_page()

            async def on_response(resp):
                try:
                    u = resp.url
                    if "kuaishou.com" not in u and "gifshow.com" not in u:
                        return
                    if "json" not in (resp.headers.get("content-type") or "").lower():
                        return
                    body = await resp.text()
                    if len(body) < 80:
                        return
                    _scan_author(json.loads(body), candidates)
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                await page.goto(
                    f"https://www.kuaishou.com{VIDEO_PATH}{first_id}",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await page.wait_for_timeout(4000)
            except Exception:
                pass
            finally:
                await page.close()
            if candidates:
                name = candidates[0]["name"]
                log(f"[KS] 作者名（视频页识别）：{name}"
                    + (f"（候选 {len(candidates)} 个）" if len(candidates) > 1 else ""))
                self.author["name"] = name
                if candidates[0].get("avatar"):
                    self.author["avatar"] = candidates[0]["avatar"]
                return name

        m = re.search(r"/profile/([^/?#]+)", url)
        fallback = m.group(1) if m else "未知作者"
        log(f"[KS][WARN] 没能自动识别作者名，文件夹暂用链接 ID：{fallback}")
        log("      如需改名：下载完成后手动重命名作者文件夹即可。")
        self.author["name"] = fallback
        return fallback

    # ---------- 下载 ----------
    async def download_all(self, works: List[Dict[str, Any]], author_name: str) -> None:
        author_dir = self.save_root / sanitize_name(author_name, "未知作者")
        author_dir.mkdir(parents=True, exist_ok=True)
        log(f"[KS] 保存目录：{author_dir}"
            + ("（文件名前加发布日期）" if self.date_folders else ""))

        total = len(works)
        # ---- 已有文件索引（键 = 去掉编号前缀后的文件名小写）----
        # 「重新编号」会给文件加 0001_ 这类前缀，直接 dest.exists() 匹配不到
        # 已编号的旧文件，会导致同批作品重复下载；这里按去编号名建索引兜底。
        existing: Dict[str, Path] = {}
        try:
            for p in author_dir.rglob("*"):
                if p.is_file():
                    existing.setdefault(
                        _strip_index(p.stem).lower() + p.suffix.lower(), p
                    )
        except OSError:
            pass

        # ---- 组装下载任务 ----
        jobs = []  # (kind, urls, dest, 标题)
        for feed_item in works:
            photo = feed_item.get("photo") or {}
            title = sanitize_name(photo.get("caption") or "", f"作品_{photo.get('id')}")
            if self.date_folders:
                stem = f"{self._date_folder(photo.get('timestamp'))}_{title}"
            else:
                stem = title
            if "video" in self.types:
                jobs.append(("video", _pick_video_urls(photo),
                             author_dir / f"{stem}.mp4", title))
            if "cover" in self.types:
                cover = photo.get("coverUrl") or ""
                if cover:
                    jobs.append(("cover", [cover],
                                 author_dir / f"{stem}_cover.jpg", title))
            if "json" in self.types:
                dest = author_dir / f"{stem}.json"
                if not dest.exists():
                    try:
                        dest.write_text(
                            json.dumps(feed_item, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except Exception as e:
                        log(f"    [失败] JSON 写入失败：{e}")
        # 作者头像放作者目录根部（不属于某个日期）
        if "avatar" in self.types:
            av = self.author.get("avatar") or ""
            if av:
                jobs.append(("avatar", [av],
                             author_dir / f"{sanitize_name(author_name)}_avatar.jpg",
                             "作者头像"))

        # ---- 并发下载 ----
        sem = asyncio.Semaphore(DL_CONCURRENCY)
        done_count = {"n": 0}

        async def run_one(job):
            kind, urls, dest, title = job
            async with sem:
                hit = existing.get(dest.name.lower())
                if hit is not None and hit.stat().st_size > 0:
                    self.stats["skip"] += 1
                    log(f"    已存在，跳过：{hit.name}" + (
                        f"（编号名 {dest.name}）" if hit.name != dest.name else ""))
                    return
                if dest.exists() and dest.stat().st_size > 0:
                    self.stats["skip"] += 1
                    log(f"    已存在，跳过：{dest.name}")
                    return
                if not urls:
                    self.stats["fail"] += 1
                    log(f"    [失败] {title} 没有{('视频' if kind == 'video' else '文件')}直链")
                    return
                ok = await asyncio.to_thread(_http_download, urls, dest)
                if not ok:
                    ok = await _browser_download(self.ctx, urls, dest)
                done_count["n"] += 1
                if ok:
                    self.stats["ok"] += 1
                    try:
                        size = dest.stat().st_size // 1024
                    except OSError:
                        size = 0
                    log(f"    [{done_count['n']}/{total}] {title} · {kind} 完成（{size} KB）")
                else:
                    self.stats["fail"] += 1
                    log(f"    [{done_count['n']}/{total}] [失败] {title} · {kind}")

        await asyncio.gather(*(run_one(j) for j in jobs))

        if "music" in self.types:
            log("[提示] 快手作品接口不提供音乐信息，music 类型跳过（视频已含音频）。")
        if "frame" in self.types:
            log("[提示] 第一帧截图由软件在下载结束后自动提取，无需在此开启。")


async def _run(task_path: str) -> int:
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    urls = [u.strip() for u in (task.get("urls") or []) if u.strip()]
    if not urls:
        log("[ERROR] 任务文件里没有链接")
        return 1
    opts = {
        "types": task.get("types") or ["video"],
        "number": int(task.get("number") or 0),
        "start_time": (task.get("start_time") or "").strip(),
        "end_time": (task.get("end_time") or "").strip(),
        "save_dir": (task.get("save_dir") or "").strip(),
        "date_folders": bool(task.get("date_folders", True)),
        "author_name": (task.get("author_name") or "").strip(),
    }
    Path(opts["save_dir"]).mkdir(parents=True, exist_ok=True)

    ensure_browsers_path()
    from playwright.async_api import async_playwright

    total_collected = 0
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
        except Exception as exc:
            log(f"[ERROR] 持久化档案不可用（{exc}）")
            return 1
        try:
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
            )
        except Exception:
            pass

        # 登录态检查（只看 Cookie 是否存在，不发起请求）
        try:
            cookies = await context.cookies("https://www.kuaishou.com/")
        except Exception:
            cookies = []
        logged = any(
            (c.get("name") or "").lower() in _LOGIN_COOKIE_NAMES
            and (c.get("value") or "").strip()
            for c in cookies
        )
        if not logged:
            log("[WARN] 未检测到快手登录态，匿名通常抓不到作者作品。"
                "请先在软件左侧点「快手登录」。")

        for url in urls:
            log(f"===== 快手下载：{url} =====")
            crawler = KuaishouCrawler(context, opts)
            works = await crawler.collect(url)
            works = [w for w in works if crawler._in_date_range(
                (w.get("photo") or {}).get("timestamp"))]
            if opts["number"]:
                works = works[:opts["number"]]
            log(f"[KS] 采集到 {len(works)} 条作品（日期/数量筛选后）")
            if not works:
                log("[KS][FAIL] 没抓到作品：链接可能是搜索结果/分享来源的 ID"
                    "（只能解析作者信息、作品流不认），或作者无公开作品。")
                log("      请打开该作者任意视频 → 视频页点作者名进主页 → "
                    "从地址栏复制 /profile/ 链接。")
                continue
            total_collected += len(works)
            author_name = await crawler.resolve_author(works, url)
            await crawler.download_all(works, author_name)
            s = crawler.stats
            log(f"[KS] 本链接完成：成功 {s['ok']}｜跳过 {s['skip']}｜失败 {s['fail']}")

        await context.close()

    log(f"[KS_SUMMARY] collected={total_collected}")
    return 0 if total_collected > 0 else 2


def main() -> int:
    if len(sys.argv) < 2:
        log("用法: kuaishou_download_worker.py <task.json>")
        return 1
    return asyncio.run(_run(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
