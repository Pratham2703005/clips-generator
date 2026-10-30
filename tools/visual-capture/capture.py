"""
visual-capture: screenshots the real pages a reel talks about, so the reel shows them instead of only saying them.
See README.md.

    python tools/visual-capture/capture.py [--date YYYY-MM-DD | --folder DIR] [--config FILE] [--fresh]

Reads script.json and brief.json from the day's folder and decides per line:
  - a line about a job (job_id)  -> that job's own apply page (proof the job is real and fresh)
  - a line naming a product      -> that product's page (n8n template, Make template, Apify, RapidAPI, ...)
  - the last line (the CTA)      -> the day's platform page
Each page is opened in headless Chrome at phone width and saved at 2x (1080 px wide). Writes <date>/visuals/*.png and
visuals.json ({line index -> file}); the composer shows them in a panel while that line is spoken.
Product pages are cached in public/visual-cache for cache_days; job pages are taken fresh every time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
SECTION = "visuals"  # this tool's table in the master config and the mode files

def load_config(path: Path, section: str = SECTION) -> dict:
    """This tool's own config (complete: the tool runs and is configured on its own), then the master config's
    [<section>] (config.toml at the repo root, or $VG_MASTER) and the run mode's [<section>] ($VG_MODE_FILE, set by
    run.py) on top. A path in an overlay is relative to that overlay file."""
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    for f in (os.environ.get("VG_MASTER") or root / "config.toml", os.environ.get("VG_MODE_FILE"), os.environ.get("VG_SET_FILE")):
        if f and Path(f).is_file():
            over = tomllib.loads(Path(f).read_text(encoding="utf-8")).get(section)
            if over:
                _merge(cfg, over, Path(f).resolve().parent, section)
    return cfg


def _merge(dst: dict, src: dict, src_dir: Path, where: str) -> None:
    for k, v in src.items():
        if k not in dst:
            print(f"config: {where}.{k} is not in the tool's own config.toml; add it there too", file=sys.stderr)
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v, src_dir, f"{where}.{k}")
            continue
        is_path = where.endswith(".paths") or k.endswith(("_dir", "_file")) or k in ("env_files", "file")
        if is_path and isinstance(v, str) and v and not Path(v).is_absolute():
            v = str((src_dir / v).resolve())
        elif is_path and isinstance(v, list):
            v = [str((src_dir / x).resolve()) if isinstance(x, str) and not Path(x).is_absolute() else x for x in v]
        dst[k] = v



def find_chrome(paths: list[str]) -> str:
    for p in paths:
        if Path(p).exists() or shutil.which(p):
            return p if Path(p).exists() else shutil.which(p)
    sys.exit("no Chrome/Edge/Chromium found; install one or add its path to [chrome].paths")


# Removes cookie/consent popups and other fixed overlays before the shot (the n8n page had a cookie dialog over half
# of it, 2026-10-30). Same effect as dismissing them; nothing is accepted or stored (throwaway profile).
CLEAN_JS = r"""
(() => {
  const words = /cookie|consent|gdpr|privacy preferences|we use cookies/i;
  for (const el of Array.from(document.querySelectorAll('body *'))) {
    const s = getComputedStyle(el);
    if ((s.position === 'fixed' || s.position === 'sticky') && el.offsetHeight > 60 && words.test(el.innerText || '')) el.remove();
  }
  for (const sel of ['#onetrust-banner-sdk', '#onetrust-consent-sdk', '.cc-window', '#CybotCookiebotDialog', '[id*="cookie" i][role="dialog"]',
                     '[class*="cookie-banner" i]', '[class*="consent" i][role="dialog"]']) document.querySelectorAll(sel).forEach(e => e.remove());
  document.documentElement.style.overflow = 'auto'; document.body.style.overflow = 'auto';
  return true;
})()
"""


def shoot_cdp(chrome: str, url: str, out: Path, c: dict, log) -> bool:
    """Headless Chrome driven over the DevTools protocol: phone viewport at 2x, wait for the page, remove cookie
    popups, then capture a tall shot (shot_height css px) so the composer has something to scroll."""
    import base64
    import urllib.request
    import websocket  # websocket-client
    # ignore_cleanup_errors: on Windows Chrome's child processes hold profile files for a moment after it is killed
    with tempfile.TemporaryDirectory(prefix="vc-chrome-", ignore_cleanup_errors=True) as prof:
        proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--no-first-run",
                                 "--disable-extensions", "--remote-debugging-port=0", f"--user-data-dir={prof}", "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = Path(prof) / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists() and port_file.read_text().strip():
                    break
                time.sleep(0.1)
            port = port_file.read_text().split()[0]
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10).read())
            ws = websocket.create_connection(next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page"), timeout=c["timeout"])
            ids = iter(range(1, 10_000))

            def call(method, **params):
                i = next(ids)
                ws.send(json.dumps({"id": i, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == i:
                        return msg.get("result", {})
            call("Emulation.setDeviceMetricsOverride", width=c["width"], height=c["height"], deviceScaleFactor=c["scale"], mobile=c.get("mobile", False))
            call("Page.enable")
            t = time.time()
            call("Page.navigate", url=url)
            deadline = t + c["budget_ms"] / 1000
            ws.settimeout(1)
            while time.time() < deadline:  # wait for the load event, then a little for late scripts
                try:
                    if json.loads(ws.recv()).get("method") == "Page.loadEventFired":
                        break
                except websocket.WebSocketTimeoutException:
                    pass
            ws.settimeout(c["timeout"])
            # JS-built pages (Workday showed only grey skeletons right after load): wait until the page's text has
            # stopped growing for a moment, up to wait_ms
            stable_since, last_len, end = time.time(), -1, time.time() + c.get("wait_ms", 15000) / 1000
            while time.time() < end:
                n = call("Runtime.evaluate", expression="document.body ? document.body.innerText.length : 0").get("result", {}).get("value", 0)
                if n != last_len:
                    last_len, stable_since = n, time.time()
                elif n > 300 and time.time() - stable_since >= c.get("settle_ms", 2500) / 1000:
                    break
                time.sleep(0.5)
            call("Runtime.evaluate", expression=CLEAN_JS)
            text = call("Runtime.evaluate", expression="document.body ? document.body.innerText : ''").get("result", {}).get("value", "")
            out.with_suffix(".txt").write_text(text or "", encoding="utf-8")  # read by the job-page checks in main()
            time.sleep(0.4)
            shot = call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                        clip={"x": 0, "y": 0, "width": c["width"], "height": c.get("shot_height", c["height"]), "scale": 1})
            ws.close()
            out.write_bytes(base64.b64decode(shot["data"]))
            log(f"  {out.name}: {time.time() - t:.0f} s  {url}")
            return out.stat().st_size > 20_000
        finally:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


def shoot(chrome: str, url: str, out: Path, c: dict, log) -> bool:
    """One headless screenshot. A fresh throwaway profile each time: no cookies, no logins, nothing kept.
    Uses the DevTools protocol when websocket-client is installed (removes cookie popups), else Chrome's --screenshot."""
    for attempt in range(1 + c.get("retries", 1)):
        try:
            import websocket  # noqa: F401
            try:
                if shoot_cdp(chrome, url, out, c, log):
                    return True
            except Exception as e:  # a page that never settles, a dropped socket: retry or fall back below
                log(f"  devtools shot failed ({e!r})"[:200])
            continue
        except ImportError:
            pass
        with tempfile.TemporaryDirectory(prefix="vc-chrome-", ignore_cleanup_errors=True) as prof:
            cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--no-first-run",
                   "--disable-extensions", f"--force-device-scale-factor={c['scale']}", f"--window-size={c['width']},{c['height']}",
                   f"--virtual-time-budget={c['budget_ms']}", f"--user-data-dir={prof}", f"--screenshot={out}", url]
            t = time.time()
            try:
                subprocess.run(cmd, capture_output=True, timeout=c["timeout"])
            except subprocess.TimeoutExpired:
                log(f"  timed out after {c['timeout']} s: {url}")
            if out.exists() and out.stat().st_size > 20_000:  # a blank or error page is tiny
                log(f"  {out.name}: {time.time() - t:.0f} s  {url}")
                return True
        log(f"  attempt {attempt + 1} failed: {url}")
    return False


TRACKER_ROLES = ["Software Engineer", "Data Analyst", "Product Manager", "Business Analyst", "Frontend Developer", "Customer Success Associate",
                 "Marketing Associate", "QA Engineer", "Operations Analyst", "Sales Development Rep"]


def tracker_html(n: int) -> str:
    """The hook's pain on screen: an illustrative "my applications" tracker with n rows, all "No response".
    Our own drawing, labelled as an illustration; generic roles, no real companies or people."""
    rows = "\n".join(
        f"<tr><td>#{i}</td><td>{TRACKER_ROLES[i % len(TRACKER_ROLES)]}</td><td>Job board</td><td>{(n - i) // 9 + 1} days ago</td>"
        f"<td><span class='st'>No response</span></td></tr>" for i in range(n, 0, -1))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;font:18px/1.4 Segoe UI,Arial,sans-serif;background:#f4f5f7;color:#1f2328}}
.top{{display:flex;justify-content:space-between;align-items:center;padding:22px 36px;background:#fff;border-bottom:1px solid #e3e5e8}}
h1{{font-size:28px;margin:0}} .tag{{font-size:13px;color:#8a8f98;border:1px solid #d6d9de;border-radius:6px;padding:3px 8px}}
.stats{{display:flex;gap:18px;padding:22px 36px}} .card{{background:#fff;border:1px solid #e3e5e8;border-radius:12px;padding:16px 24px;min-width:200px}}
.card b{{display:block;font-size:40px}} .zero b{{color:#d32f2f}}
table{{width:calc(100% - 72px);margin:0 36px;border-collapse:collapse;background:#fff;border:1px solid #e3e5e8;border-radius:12px}}
th,td{{text-align:left;padding:12px 16px;border-bottom:1px solid #eef0f2}} th{{color:#6b7280;font-size:14px;text-transform:uppercase}}
.st{{background:#fdecea;color:#b42318;border-radius:999px;padding:3px 12px;font-size:15px}}
</style></head><body>
<div class="top"><h1>My job applications</h1><span class="tag">illustration</span></div>
<div class="stats"><div class="card"><b>{n}</b>Applied</div><div class="card zero"><b>0</b>Replies</div><div class="card zero"><b>0</b>Interviews</div></div>
<table><tr><th>#</th><th>Role</th><th>Where</th><th>Applied</th><th>Status</th></tr>
{rows}
</table></body></html>"""


def plan(script: dict, brief: dict, cfg: dict) -> list[dict]:
    """[{line, kind, key, url}] in line order, at most max_visuals."""
    r, pages = cfg["rules"], cfg["pages"]
    jobs = {j["id"]: j for j in brief.get("jobs", [])}
    out = []
    for i, ln in enumerate(script["lines"]):
        if ln.get("show"):  # a custom script's own "[show: URL]" for this line
            out.append({"line": i, "kind": "page", "key": f"show-{i}", "url": ln["show"]})
            continue
        if r.get("job_pages") and ln.get("job_id") in jobs:
            out.append({"line": i, "kind": "job", "key": f"job-{ln['job_id'][:10]}", "url": jobs[ln["job_id"]]["apply_url"]})
            continue
        text = ln["text"].lower()
        for m in r.get("mentions", []):
            if re.search(m["pattern"], text) and m["page"] in pages:
                out.append({"line": i, "kind": "page", "key": m["page"], "url": pages[m["page"]]})
                break
    last = len(script["lines"]) - 1
    platform = (brief.get("platform") or {}).get("id")
    if r.get("cta_page") and platform in pages and not any(v["line"] == last for v in out):
        out.append({"line": last, "kind": "page", "key": platform, "url": pages[platform]})
    # each page once per reel (user, 2026-10-30: the same Apify page kept coming back); the same URL under two keys
    # (apify / apify-mcp) counts as one. The CTA keeps its page if it was used earlier: the earlier use is dropped.
    seen, kept = set(), []
    for v in sorted(out, key=lambda v: (v["line"] != last, v["line"])):
        if v["url"] not in seen:
            seen.add(v["url"])
            kept.append(v)
    return sorted(kept, key=lambda v: v["line"])[: r.get("max_visuals", 6)]


def main() -> None:
    p = argparse.ArgumentParser(description="Screenshot the pages a reel talks about.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--date", help="YYYY-MM-DD (default: the latest day with a script)")
    p.add_argument("--folder", type=Path, help="a day's folder anywhere")
    p.add_argument("--fresh", action="store_true", help="ignore the product-page cache")
    args = p.parse_args()

    cfg = load_config(args.config)
    base = args.config.resolve().parent
    log = lambda m: print(m, file=sys.stderr, flush=True)  # noqa: E731
    content = (base / cfg["paths"]["content_dir"]).resolve()
    if args.folder:
        folder = args.folder.resolve()
    elif args.date:
        folder = content / args.date
    else:
        days = sorted(d for d in content.iterdir() if (d / "script.json").exists()) if content.exists() else []
        if not days:
            sys.exit(f"no scripts in {content}; run script-writer first")
        folder = days[-1]
    script = json.loads((folder / "script.json").read_text(encoding="utf-8"))
    brief = json.loads((folder / "brief.json").read_text(encoding="utf-8")) if (folder / "brief.json").exists() else {}
    chrome = find_chrome(cfg["chrome"]["paths"])
    cache = (base / cfg["paths"]["cache_dir"]).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    out_dir = folder / "visuals"
    out_dir.mkdir(exist_ok=True)

    todo = plan(script, brief, cfg)
    done = []
    # the hook's own visual (virality review: something the viewer recognises, moving, from frame 0)
    # the on-screen hook card has the number in digits even when the line spells it ("Two hundred applications")
    m = re.search(r"\b(\d{2,4})\b", f"{script.get('hook_card', '')} {script['lines'][0]['text']}") if cfg["rules"].get("hook_tracker") else None
    if m and not any(v["line"] == 0 for v in todo):
        html = out_dir / "hook-tracker.html"
        html.write_text(tracker_html(int(m.group(1))), encoding="utf-8")
        dst = out_dir / "hook-tracker.png"
        # 1x and very tall: 200 rows are ~10,000 css px, and the reel scrolls through them fast
        if shoot(chrome, html.resolve().as_uri(), dst, {**cfg["chrome"], "scale": 1, "shot_height": 6000, "settle_ms": 500}, log):
            done.append({"line": 0, "kind": "hook", "key": "hook-tracker", "url": "", "file": "visuals/hook-tracker.png",
                         "text": script["lines"][0]["text"], "scroll": cfg["rules"].get("hook_scroll", 500)})
    log(f"{folder.name}: {len(todo) + len(done)} visual(s) for {len(script['lines'])} lines")
    for v in todo:
        dst = out_dir / f"{v['key']}.png"
        if v["kind"] == "page":
            cached = cache / f"{v['key']}-{hashlib.sha1(v['url'].encode()).hexdigest()[:8]}.png"
            fresh = cached.exists() and time.time() - cached.stat().st_mtime < cfg["paths"]["cache_days"] * 86400
            if fresh and not args.fresh:
                shutil.copyfile(cached, dst)
                log(f"  {dst.name}: from cache")
            elif shoot(chrome, v["url"], cached, cfg["chrome"], log):
                shutil.copyfile(cached, dst)
            else:
                continue
        elif not (dst.exists() and dst.stat().st_mtime > (folder / "script.json").stat().st_mtime) and not shoot(chrome, v["url"], dst, cfg["chrome"], log):
            continue
        item = {**v, "file": f"visuals/{dst.name}", "text": script["lines"][v["line"]]["text"]}
        page_text = dst.with_suffix(".txt").read_text(encoding="utf-8") if dst.with_suffix(".txt").exists() else ""
        if v["kind"] == "job" and page_text:
            # the page itself can show the job is not what the reel says (10-30: "Technical Writer" at SIL/Wycliffe
            # was a volunteer role, which the API data didn't show)
            hits = [m.group(0) for p in cfg["rules"].get("job_red_flags", []) for m in [re.search(p, page_text, re.I)] if m]
            if hits:
                item["warnings"] = hits
                log(f"  WARNING {dst.name}: the job page says {', '.join(repr(h) for h in hits)}; check before posting")
        done.append(item)
    (folder / "visuals.json").write_text(json.dumps({"items": done, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                                                    indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"wrote {len(done)} visual(s) and visuals.json in {folder}")


if __name__ == "__main__":
    main()
