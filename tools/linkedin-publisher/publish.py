"""
linkedin-publisher: the day's post on the LinkedIn Page "Isha - Job Shorts" (user, 2026-11-23), through a Make.com scenario.

    python tools/linkedin-publisher/publish.py --folder content/<date> [--dry-run]

No video (Make's free plan takes files up to 5 MB, a reel is ~20 MB): the reel's cover frame as the image, and a text
post the user chose:
  - the reel's lesson: the description's tips block (the lines after the search line and the short label, up to the
    first blank line; the same block the Instagram DM Worker sends)
  - the YouTube Short's link (posted.json, so this step runs after YouTube)
  - the day's first `jobs` jobs (daily-jobs.json from the Telegram jobs step, else the reel's own in brief.json)
  - "Aaj ki aur jobs" + the Telegram channel, and the description's first `hashtags` hashtags
It POSTs {text, image_base64, image_name, day, title} to the webhook in MAKE_LINKEDIN_POST_WEBHOOK; the scenario
answers with {"post_id": "urn:li:share:..."} (any JSON with an id). Writes linkedin_post_id / linkedin_url /
linkedin_posted_at into posted.json and skips when they are already there. No webhook set: says so and exits 0
(the report shows the post as skipped). The webhook URL is a secret: it is never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.toml"


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def read_json(path: Path) -> dict | list:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return {}


# lines that belong to Instagram only (the DM ask, the bio link) or to another section
SKIP = re.compile(r"link in bio|\bcomment karo\b|\bDM mein\b|^#|^kal\b|^job in this episode|^- .+ at .+|^\s*apply:", re.I)


def tips_block(desc: str) -> list[str]:
    """The lesson: the lines after the search line and the short label, up to the first blank line."""
    lines = desc.splitlines()
    if len(lines) < 3:
        return []
    out = []
    for line in lines[2:]:
        if not line.strip():
            break
        if not SKIP.search(line.strip()):
            out.append(line.rstrip())
    return out[:12]


def hashtags(desc: str, n: int) -> list[str]:
    tags = re.findall(r"(?<!\w)#\w+", "\n".join(l for l in desc.splitlines() if l.strip().startswith("#")))
    return tags[:n]


def day_jobs(folder: Path, n: int) -> list[dict]:
    jobs = read_json(folder / "daily-jobs.json")
    if not isinstance(jobs, list) or not jobs:
        brief = read_json(folder / "brief.json")
        jobs = brief.get("jobs", []) if isinstance(brief, dict) else []
    return [j for j in jobs if j.get("title") and j.get("company") and j.get("apply_url")][:n]


def build_text(desc: str, youtube_url: str, jobs: list[dict], cfg: dict) -> str:
    parts = []
    tips = tips_block(desc)
    if tips:
        parts.append("\n".join(tips))
    if youtube_url:
        parts.append(f"▶️ 30 second ki video: {youtube_url}")
    if jobs:
        rows = [f"Aaj ki {len(jobs)} fresher jobs (company career page se):"]
        for j in jobs:
            where = f", {j['where']}" if j.get("where") else ""
            rows.append(f"• {j['title']} at {j['company']}{where}: {j['apply_url']}")
        parts.append("\n".join(rows))
    parts.append(f"Aaj ki aur jobs: {cfg['telegram_url']}")
    tags = hashtags(desc, cfg.get("hashtags", 3))
    if tags:
        parts.append(" ".join(tags))
    text = "\n\n".join(parts)
    return text if len(text) <= cfg["text_max"] else text[: cfg["text_max"] - 1].rsplit("\n", 1)[0]


LITTLE = re.compile(r"[\\|{}@\[\]()<>#*_~]")  # LinkedIn's reserved characters, backslash included


def little_text(text: str) -> str:
    """LinkedIn's "little text format" (the API's post text, passed through by Make): reserved characters need a
    backslash, or the post is cut at the first one (an unescaped "(" ends it); "#tag" becomes a real hashtag."""
    out = []
    for tok in re.split(r"(#\w+)", text):
        out.append("{hashtag|\\#|" + tok[1:] + "}" if re.fullmatch(r"#\w+", tok) else LITTLE.sub(lambda m: "\\" + m.group(0), tok))
    return "".join(out)


def cover_image(video: Path, seconds: float, width: int, out: Path) -> Path | None:
    """The reel's cover frame as a JPEG (~200 kB); None without ffmpeg or a reel."""
    if not shutil.which("ffmpeg") or not video.is_file():
        return None
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(seconds), "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "3", str(out)], capture_output=True)
    return out if r.returncode == 0 and out.is_file() else None


def post(webhook: str, payload: dict, timeout: int, api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:  # the webhook's API key authentication (Make checks the x-make-apikey header)
        headers["x-make-apikey"] = api_key
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:  # the message never holds the webhook URL
        raise RuntimeError(f"Make webhook: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Make webhook: {e.reason}") from None
    try:
        return json.loads(body) if body.strip() else {}
    except ValueError:
        return {"raw": body.strip()[:300]}


def post_id(answer: dict) -> str:
    for k in ("post_id", "id", "urn", "share", "post"):
        v = answer.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True, help="the reel's folder (description.txt, posted.json, reel.mp4)")
    p.add_argument("--dry-run", action="store_true", help="show the post's text; send nothing")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))["linkedin"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in cfg.get("env_files", [])])
    folder = a.folder.resolve()
    record_path = folder / "posted.json"
    record = read_json(record_path)
    if record.get("linkedin_post_id"):
        print(f"already on LinkedIn: {record.get('linkedin_url') or record['linkedin_post_id']}")
        return
    desc = (folder / "description.txt").read_text(encoding="utf-8") if (folder / "description.txt").is_file() else \
        (read_json(folder / "script.json") or {}).get("caption", "")
    text = build_text(desc, record.get("youtube_url", ""), day_jobs(folder, cfg["jobs"]), cfg)
    title = (read_json(folder / "script.json") or {}).get("title", "")
    if a.dry_run:
        print("---\n" + text + "\n---\n(dry run: nothing sent)")
        return
    webhook = os.environ.get(cfg["webhook_env"], "").strip()
    if not webhook:
        print(f"{cfg['webhook_env']} is not set: LinkedIn skipped")
        return
    seconds = (read_json(folder / "compose.json") or {}).get("cover_seconds", cfg["cover_seconds"])
    with tempfile.TemporaryDirectory() as tmp:
        img = cover_image(folder / "reel.mp4", seconds, cfg["image_width"], Path(tmp) / f"{folder.name}.jpg")
        payload = {"text": little_text(text), "day": folder.name, "title": title,
                   "image_base64": base64.b64encode(img.read_bytes()).decode() if img else "",
                   "image_name": img.name if img else ""}
        answer = post(webhook, payload, cfg["timeout"], os.environ.get(cfg.get("api_key_env", ""), "").strip())
    pid = post_id(answer)
    if not pid:
        sys.exit(f"Make answered without a post id: {json.dumps(answer)[:300]}")
    url = f"https://www.linkedin.com/feed/update/{pid}/" if pid.startswith("urn:li:") else cfg["page_url"]
    record = read_json(record_path)
    record.update({"linkedin_post_id": pid, "linkedin_url": url,
                   "linkedin_posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"LinkedIn post up: {url}")


if __name__ == "__main__":
    main()
