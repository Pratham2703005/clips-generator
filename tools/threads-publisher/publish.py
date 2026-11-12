"""
threads-publisher: posts the day's reel to Threads (@isha.07007) as a video post.

    python tools/threads-publisher/publish.py --folder content/<date> [--dry-run]

Needs THREADS_ACCESS_TOKEN and THREADS_USER_ID (repository secrets; set by get_token.py) and the gh CLI with a token
that can write releases (GH_TOKEN in the workflow).
Threads takes video only from a public URL (no upload), so the reel goes up for a few minutes as an asset of the
public repo's release "reel-uploads" (made on first use), and the asset is deleted again right after, posted or not.
Container (media_type VIDEO, video_url, text) -> wait until FINISHED -> threads_publish -> permalink. If Threads cannot
fetch the github.com download link (it redirects), the container is made once more with the redirect's target.
Text: the episode's question (threads.txt, from its 'threads:' line) plus one topic tag; without one, description.txt
cut to Threads' 500 characters at a line break, one hashtag kept (Threads uses one topic tag).
Writes threads_id / threads_url / threads_posted_at into posted.json and skips when they are already there.
The token is never printed. The token lasts 60 days: [threads] token_expires (written by get_token.py) is checked here
and by the report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
API = "https://graph.threads.net/v1.0"


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def call(method: str, path: str, token: str, fields: dict | None = None) -> dict:
    fields = {**(fields or {}), "access_token": token}
    url = f"{API}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(fields)
    else:
        data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={} if data is None else {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error") or {}
            msg = f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})"
        except (ValueError, AttributeError):
            msg = raw[:600]
        raise RuntimeError(f"{method} {path.split('/')[-1]}: HTTP {e.code}: {msg}") from None


def threads_text(folder: Path, limit: int, site_url: str = "", topic_tag: bool = True) -> str:
    desc, script, question = folder / "description.txt", folder / "script.json", folder / "threads.txt"
    if question.is_file() and question.read_text(encoding="utf-8").strip():
        # the episode's 'threads:' line: only the question (Threads rewards replies), plus the description's first hashtag
        # as the post's topic tag when topic_tag is on (that is how Threads files a post under a topic)
        text = question.read_text(encoding="utf-8").strip()
        tags = re.findall(r"#\w+", desc.read_text(encoding="utf-8")) if topic_tag and desc.is_file() else []
        return (text + (f"\n\n{tags[0]}" if tags else ""))[:limit]
    if desc.is_file():
        text = desc.read_text(encoding="utf-8").strip()
    elif script.is_file():
        text = json.loads(script.read_text(encoding="utf-8")).get("caption", "").strip()
    else:
        return ""
    if site_url:  # as on YouTube: the link itself instead of "link in bio"
        text = re.sub(r":?\s*link in bio\.?", f": {site_url}", text, flags=re.I)
    tags = re.findall(r"#\w+", text)
    text = re.sub(r"[ \t]*#\w+", "", text)                     # all hashtags out; the first comes back at the end
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(l.rstrip() for l in text.splitlines())).strip()
    tail = f"\n\n{tags[0]}" if tags else ""
    if len(text) + len(tail) <= limit:
        return text + tail
    kept: list[str] = []
    for line in text.splitlines():                              # whole lines while they fit
        if len("\n".join(kept + [line])) + len(tail) > limit:
            break
        kept.append(line)
    out = "\n".join(kept).strip() or text[: limit - len(tail)].rstrip()
    return out + tail


def gh(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args, "-R", repo], capture_output=True, text=True)


def host(video: Path, repo: str, tag: str) -> tuple[str, str]:
    """The reel as a release asset of the public repo: (public URL, asset name)."""
    if gh(repo, "release", "view", tag).returncode:
        r = gh(repo, "release", "create", tag, "--title", "Reel uploads (temporary)",
               "--notes", "Reels sit here for a few minutes while Threads fetches them; each is deleted after.")
        if r.returncode:
            raise RuntimeError(f"could not make release {tag}: {r.stderr.strip()}")
    name = f"threads-{video.parent.name}-{int(time.time())}.mp4"
    tmp = Path(tempfile.mkdtemp()) / name
    shutil.copyfile(video, tmp)
    r = gh(repo, "release", "upload", tag, str(tmp), "--clobber")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    if r.returncode:
        raise RuntimeError(f"could not upload the reel to release {tag}: {r.stderr.strip()}")
    return f"https://github.com/{repo}/releases/download/{tag}/{name}", name


def resolved(url: str) -> str:
    """Where the github.com download link redirects to (a signed link, valid for a few minutes)."""
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
        return r.geturl()


def make_and_wait(user: str, token: str, url: str, text: str, poll: dict) -> str:
    c = call("POST", f"{user}/threads", token, {"media_type": "VIDEO", "video_url": url, "text": text})["id"]
    deadline = time.time() + poll["max_minutes"] * 60
    while True:
        time.sleep(poll["every_seconds"])
        s = call("GET", c, token, {"fields": "status,error_message"})
        print(f"  processing: {s.get('status')}", flush=True)
        if s.get("status") == "FINISHED":
            return c
        if s.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads could not process the video: {s.get('error_message') or s.get('status')}")
        if time.time() > deadline:
            raise RuntimeError(f"still {s.get('status')} after {poll['max_minutes']} min")


def days_left(th: dict) -> int | None:
    exp = th.get("token_expires", "")
    try:
        return (date.fromisoformat(exp) - date.today()).days
    except ValueError:
        return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="check the token, then show what would be posted")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    th = cfg["threads"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in th.get("env_files", [])])
    token, user = os.environ.get(th["token_env"], "").strip(), os.environ.get(th["user_env"], "").strip()
    if not token or not user:
        sys.exit(f"set {th['token_env']} and {th['user_env']} (run tools/threads-publisher/get_token.py)")
    repo = os.environ.get("GITHUB_REPOSITORY") or th["repo"]

    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    try:
        me = call("GET", "me", token, {"fields": "id,username"})
    except RuntimeError as e:
        sys.exit(f"token check failed ({len(token)}-character token): {e}. Make a new one: get_token.py")
    left = days_left(th)
    print(f"account: @{me.get('username')}; token " + (f"expires in {left} days" if left is not None else "expiry unknown"))
    if left is not None and left < 14:
        print("::warning::Threads token expires soon: generate a new one and run tools/threads-publisher/get_token.py --long")
    text = threads_text(folder, th["text_max"], th.get("site_url", ""), th.get("question_topic_tag", True))
    print(f"reel: {video} ({video.stat().st_size / 1e6:.1f} MB), text {len(text)} characters")
    if a.dry_run:
        print("---\n" + text + "\n---\n(dry run: nothing posted)")
        return

    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    if record.get("threads_id"):
        print(f"Threads post already up: {record.get('threads_url') or record['threads_id']}")
        return
    url, asset = host(video, repo, th["release_tag"])
    try:
        print("reel hosted for Threads (temporary release asset)", flush=True)
        try:
            container = make_and_wait(user, token, url, text, cfg["poll"])
        except RuntimeError as e:
            print(f"  first try failed ({e}); trying the redirect's target", flush=True)
            container = make_and_wait(user, token, resolved(url), text, cfg["poll"])
        media = call("POST", f"{user}/threads_publish", token, {"creation_id": container})["id"]
    finally:
        r = gh(repo, "release", "delete-asset", th["release_tag"], asset, "--yes")
        print("temporary asset deleted" if not r.returncode else f"warning: asset {asset} not deleted: {r.stderr.strip()}")
    link = call("GET", media, token, {"fields": "permalink"}).get("permalink", "")
    record.update({"threads_id": media, "threads_url": link,
                   "threads_posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Threads post up: {link or media}")


if __name__ == "__main__":
    main()
