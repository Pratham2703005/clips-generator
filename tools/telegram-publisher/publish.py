"""
telegram-publisher: posts the day's reel to the Telegram channel (@fresherjobs_isha) through the bot.

    python tools/telegram-publisher/publish.py --folder content/<date> [--dry-run]

Needs TELEGRAM_BOT_TOKEN (BotFather; the bot is an admin of the channel with "Post Messages") and TELEGRAM_CHANNEL
(@fresherjobs_isha), from repository secrets in the workflow or .env on the PC.
sendVideo with the reel (vertical, streamable, with a cover thumbnail cut at compose.json's cover_seconds when ffmpeg is
there). The caption is only the episode's title, the jobs in the reel and the app link, when the reel has them
(channel_text). A caption is capped at 1024 characters, so a longer one is split at a line break and the rest
follows as a text message (no link previews).
Writes telegram_message_id / telegram_url / telegram_posted_at into posted.json and skips when they are already there.
The bot token is part of every Bot API URL, so no URL is ever printed.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


class Bot:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, fields: dict | None = None, files: dict[str, Path] | None = None) -> dict:
        fields = {k: (json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)) for k, v in (fields or {}).items()}
        if files:
            boundary = uuid.uuid4().hex
            parts: list[bytes] = []
            for k, v in fields.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k, path in files.items():
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{path.name}"\r\n'
                             f"Content-Type: {ctype}\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            data, ctype = b"".join(parts), f"multipart/form-data; boundary={boundary}"
        else:
            data, ctype = urllib.parse.urlencode(fields).encode(), "application/x-www-form-urlencoded"
        req = urllib.request.Request(self._base + method, data=data, headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                body = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            raise RuntimeError(f"{method}: HTTP {e.code}: {body.get('description', 'no description')}") from None
        except urllib.error.URLError as e:  # its text never holds the URL, only the reason
            raise RuntimeError(f"{method}: {e.reason}") from None
        if not body.get("ok"):
            raise RuntimeError(f"{method}: {body.get('description')}")
        return body["result"]


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return {}


def channel_text(folder: Path) -> str:
    """What the channel gets with the reel (user, 2026-11-09): the episode's title, the jobs in it, and the app link -
    the last two only when this reel has them. Built from the description, so it holds exactly what the reel
    promoted: jobs are its "- Title at Company (...)" + "  Apply: <url>" lines (run.py jobs_block, write_script.py);
    the app link is the brief's platform line ("<Name>: <url>"), never the landing page."""
    script, brief = read_json(folder / "script.json"), read_json(folder / "brief.json")
    desc_file = folder / "description.txt"
    desc = desc_file.read_text(encoding="utf-8") if desc_file.is_file() else script.get("caption", "")
    title = (script.get("title") or "").strip()
    if not title:  # the description's first line is the search line; its second is the episode's
        lines = [l.strip() for l in desc.splitlines() if l.strip()]
        title = lines[1] if len(lines) > 1 else (lines[0] if lines else folder.name)
    parts = [title]
    jobs = re.findall(r"^- (.+?)\s*\n\s+Apply: (\S+)", desc, flags=re.M)
    if jobs:
        head = "Job in this reel:" if len(jobs) == 1 else "Jobs in this reel:"
        parts.append(head + "\n" + "\n".join(f"- {what}\n  Apply: {url}" for what, url in jobs))
    platform = brief.get("platform") or {}
    if platform.get("url") and platform.get("id") != "landing" and platform["url"] in desc:
        name = platform.get("name") or platform.get("id", "App")
        parts.append(f"{name[:1].upper() + name[1:]}: {platform['url']}")
    return "\n\n".join(parts)


def caption_parts(folder: Path, cap: int, msg_max: int) -> tuple[str, str]:
    """(caption, rest): the caption ends at a line break before `cap`; the rest becomes a follow-up message."""
    text = channel_text(folder)
    if len(text) <= cap:
        return text, ""
    lines, kept = text.splitlines(), []
    for line in lines:
        if len("\n".join(kept + [line])) > cap:
            break
        kept.append(line)
    if not kept:
        return text[:cap].rstrip(), text[cap:msg_max + cap].strip()
    return "\n".join(kept).rstrip(), "\n".join(lines[len(kept):]).strip()[:msg_max]


def thumbnail(video: Path, seconds: float) -> Path | None:
    """A 180x320 JPEG of the cover frame (Telegram: JPEG, <= 320 px a side, < 200 kB); None without ffmpeg."""
    if not shutil.which("ffmpeg"):
        return None
    out = Path(tempfile.mkdtemp()) / "thumb.jpg"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(seconds), "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=-2:320", "-q:v", "5", str(out)], capture_output=True)
    return out if r.returncode == 0 and out.is_file() and out.stat().st_size < 200_000 else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="check the bot and channel, then show what would be posted")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    tg = cfg["telegram"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in tg.get("env_files", [])])
    token, channel = os.environ.get(tg["token_env"], "").strip(), os.environ.get(tg["channel_env"], "").strip()
    if not token or not channel:
        sys.exit(f"set {tg['token_env']} and {tg['channel_env']}")
    if not channel.startswith(("@", "-")):
        channel = "@" + channel

    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    bot = Bot(token)
    try:
        me = bot.call("getMe")
        chat = bot.call("getChat", {"chat_id": channel})
    except RuntimeError as e:
        sys.exit(f"bot/channel check failed ({len(token)}-character token, channel {channel}): {e}")
    print(f"bot: @{me.get('username')}; channel: {chat.get('title')} ({channel})")
    caption, rest = caption_parts(folder, tg["caption_max"], tg["message_max"])
    size_mb = video.stat().st_size / 1e6
    print(f"reel: {video} ({size_mb:.1f} MB), caption {len(caption)} characters"
          + (f" + a follow-up message of {len(rest)}" if rest else ""))
    if size_mb > tg["max_mb"]:
        sys.exit(f"the reel is {size_mb:.1f} MB; the Bot API takes up to {tg['max_mb']} MB")
    if a.dry_run:
        print("---\n" + caption + ("\n--- follow-up ---\n" + rest if rest else "") + "\n---\n(dry run: nothing posted)")
        return

    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    if record.get("telegram_message_id"):
        print(f"Telegram post already up: {record.get('telegram_url') or record['telegram_message_id']}")
        return
    comp = folder / "compose.json"
    meta = json.loads(comp.read_text(encoding="utf-8")) if comp.is_file() else {}
    thumb = thumbnail(video, meta.get("cover_seconds", tg["cover_seconds"]))
    fields = {"chat_id": channel, "caption": caption, "supports_streaming": True,
              "width": tg["width"], "height": tg["height"]}
    if meta.get("seconds"):
        fields["duration"] = int(round(meta["seconds"]))
    files = {"video": video, **({"thumbnail": thumb} if thumb else {})}
    msg = bot.call("sendVideo", fields, files)
    if thumb:
        shutil.rmtree(thumb.parent, ignore_errors=True)
    mid = msg["message_id"]
    # telegram.me, not t.me: some Indian ISPs block t.me at DNS (2026-11-09)
    link = f"https://telegram.me/{channel[1:]}/{mid}" if channel.startswith("@") else ""
    record.update({"telegram_message_id": mid, "telegram_url": link,
                   "telegram_posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Telegram post up: {link or mid}")
    if rest:
        try:  # the reel is already up; a failed follow-up only warns
            bot.call("sendMessage", {"chat_id": channel, "text": rest, "reply_parameters": {"message_id": mid},
                                     "link_preview_options": {"is_disabled": True}})
            print("follow-up message sent")
        except RuntimeError as e:
            print(f"warning: follow-up message not sent: {e}")


if __name__ == "__main__":
    main()
