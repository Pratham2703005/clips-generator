"""
instagram-publisher: posts a reel to Instagram, as a Reel and/or a story.

    python tools/instagram-publisher/publish.py --folder content/<date> [--no-reel] [--no-story] [--dry-run]

Needs IG_ACCESS_TOKEN (the system-user token of the Meta app's Unified login configuration, graph.facebook.com) and
IG_USER_ID (the Instagram account ID), from the environment or .env on the PC. The video file is uploaded straight to
Instagram (resumable upload, rupload.facebook.com): no hosting.
Each post: container -> upload -> wait until processed (status_code FINISHED) -> publish.
  - reel: caption from description.txt (else script.json's caption); cover = compose.json's cover_seconds (the frame
    with the whole hook card), else [instagram] cover_seconds
  - story: the same video; a story takes no caption, and the API has no stickers (poll, link)
Writes posted.json (reel id and permalink, story id). The token is never printed and never put in a URL.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"


def load_config(path: Path) -> dict:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    over = os.environ.get("VG_SET_FILE")
    if over and Path(over).is_file():
        for section, values in tomllib.loads(Path(over).read_text(encoding="utf-8")).get("instagram-publisher", {}).items():
            cfg.setdefault(section, {}).update(values)
    return cfg


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


class Api:
    def __init__(self, token: str, version: str):
        self.token, self.version = token, version
        self.base = f"https://graph.facebook.com/{version}"

    def call(self, method: str, path: str, fields: dict | None = None, url: str | None = None,
             body: bytes | None = None, headers: dict | None = None) -> dict:
        target = url or f"{self.base}/{path.lstrip('/')}"
        hdrs = dict(headers or {})
        if url:  # the upload host takes the token as a header
            fields, hdrs["Authorization"] = dict(fields or {}), f"OAuth {self.token}"
        else:  # graph.facebook.com: as a parameter (the request body for POST); URLs are never printed
            fields = {**(fields or {}), "access_token": self.token}
        if method == "GET":
            target += ("&" if "?" in target else "?") + urllib.parse.urlencode(fields) if fields else ""
        elif body is None:
            body = urllib.parse.urlencode(fields).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(target, data=body if method != "GET" else None, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body_json = json.loads(raw)
                err = body_json.get("error")
                # the upload host answers with {"debug_info": {"retriable": ..., "type": ..., "message": ...}}
                msg = (f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})" if err
                       else json.dumps(body_json.get("debug_info") or body_json)[:600])
            except (ValueError, AttributeError):
                msg = raw[:600]
            raise RuntimeError(f"{method} {path or 'upload'}: HTTP {e.code}: {msg}") from None


def caption_for(folder: Path, cfg: dict) -> str:
    desc, script = folder / "description.txt", folder / "script.json"
    if desc.is_file():
        text = desc.read_text(encoding="utf-8").strip()
    elif script.is_file():
        text = json.loads(script.read_text(encoding="utf-8")).get("caption", "").strip()
    else:
        text = ""
    if not text:
        print("warning: no description.txt or script.json caption: posting without a caption")
    tags = re.findall(r"#\w+", text)
    for extra in tags[cfg["hashtags_max"]:]:  # drop the ones past Instagram's limit, from the end
        text = text[::-1].replace(extra[::-1], "", 1)[::-1]
    return text[: cfg["caption_max"]].rstrip()


def wait_until_ready(api: Api, container: str, poll: dict) -> None:
    deadline = time.time() + poll["max_minutes"] * 60
    while True:
        s = api.call("GET", container, {"fields": "status_code,status"})
        code = s.get("status_code")
        print(f"  processing: {code}", flush=True)
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {s.get('status') or code}")
        if time.time() > deadline:
            raise RuntimeError(f"still {code} after {poll['max_minutes']} min; container {container} expires in 24 h")
        time.sleep(poll["every_seconds"])


def post(api: Api, user: str, video: Path, media_type: str, fields: dict, poll: dict, chunk_mb: int = 8) -> str:
    """Container -> upload -> processed -> published; returns the media id. The container takes only the documented
    fields (2026-11-02: share_to_feed made the upload fail with a bare HTTP 400)."""
    data = video.read_bytes()

    def container_and_upload(chunk: int) -> str:
        made = api.call("POST", f"{user}/media", {"media_type": media_type, **fields, "upload_type": "resumable"})
        container = made["id"]
        upload_url = made.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{api.version}/{container}"
        up: dict = {}
        for off in range(0, len(data), chunk or len(data)):  # the offset header resumes where the last piece ended
            up = api.call("POST", "", url=upload_url, body=data[off: off + (chunk or len(data))],
                          headers={"offset": str(off), "file_size": str(len(data))})
        if not up.get("success"):
            raise RuntimeError(f"upload refused: {up}")
        return container

    # Meta's upload host answers ProcessingFailedError now and then, for files of any size: 2026-11-02 twice in a row
    # and then fine for the same file; 2026-11-04 the nightly run failed in one piece AND in 8 MB pieces, and a rerun 6 h
    # later went through in pieces. So: one piece, then pieces, then pieces again after a pause, each in a new container.
    attempts = [(0, 0), (chunk_mb, 0), (chunk_mb, 90), (max(1, chunk_mb // 2), 180)]
    for i, (mb, pause) in enumerate(attempts):
        if pause:
            print(f"  waiting {pause} s before trying again", flush=True)
            time.sleep(pause)
        try:
            container = container_and_upload(mb * 1024 * 1024)
            break
        except RuntimeError as e:
            if "ProcessingFailedError" not in str(e) or i == len(attempts) - 1:
                raise
            nxt = attempts[i + 1][0]
            print(f"  upload failed ({'one piece' if not mb else f'{mb} MB pieces'}): {e}; next: {nxt} MB pieces", flush=True)
    print(f"{media_type.lower()} container {container}: uploaded, waiting for Instagram", flush=True)
    wait_until_ready(api, container, poll)
    return api.call("POST", f"{user}/media_publish", {"creation_id": container})["id"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True, help="the reel's folder (reel.mp4 + description.txt or script.json)")
    p.add_argument("--no-reel", action="store_true", help="skip the reel (e.g. it is already up)")
    p.add_argument("--no-story", action="store_true", help="skip the story")
    p.add_argument("--dry-run", action="store_true", help="check the token, then show what would be posted")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()

    cfg = load_config(a.config)
    ig = cfg["instagram"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in ig.get("env_files", [])])
    token = os.environ.get(ig["token_env"], "").strip()
    user = os.environ.get(ig["user_env"], "").strip()
    if not token or not user:
        sys.exit(f"set {ig['token_env']} and {ig['user_env']} (repository secrets in the workflow, .env on the PC)")
    if a.no_reel and a.no_story:
        sys.exit("nothing to post (--no-reel and --no-story)")
    api = Api(token, ig["api_version"])
    try:
        acct = api.call("GET", user, {"fields": "username"})
    except RuntimeError as e:
        sys.exit(f"token check failed ({len(token)}-character token): {e}")
    print(f"account: @{acct.get('username')}")

    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    caption = caption_for(folder, ig)
    comp = folder / "compose.json"
    cover = json.loads(comp.read_text(encoding="utf-8")).get("cover_seconds") if comp.is_file() else None
    cover = ig["cover_seconds"] if cover is None else cover
    print(f"reel: {video} ({video.stat().st_size / 1e6:.1f} MB), caption {len(caption)} characters, cover at {cover} s; "
          f"posting: {' + '.join(x for x, skip in (('reel', a.no_reel), ('story', a.no_story)) if not skip)}")
    if a.dry_run:
        print("---\n" + caption + "\n---\n(dry run: nothing posted)")
        return

    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    record["account"] = acct.get("username")
    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    # posted.json already has it (a Post reel run of a reel that is partly up): never post the same thing twice
    if not a.no_reel and record.get("permalink"):
        print(f"reel already posted: {record['permalink']}")
        a.no_reel = True
    if not a.no_story and record.get("story_id"):
        print(f"story already posted: {record['story_id']}")
        a.no_story = True
    if not a.no_reel:
        media = post(api, user, video, "REELS", {"caption": caption, "thumb_offset": str(int(cover * 1000))}, cfg["poll"],
                     ig.get("chunk_mb", 8))
        link = api.call("GET", media, {"fields": "permalink"}).get("permalink", "")
        record.update({"media_id": media, "permalink": link, "posted_at": now()})
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"reel posted: {link or media}")
    if not a.no_story:
        try:  # a failed story never undoes the reel; the step still fails so it shows
            story = post(api, user, video, "STORIES", {}, cfg["poll"], ig.get("chunk_mb", 8))
        except RuntimeError as e:
            sys.exit(f"story not posted: {e}")
        record.update({"story_id": story, "story_at": now()})
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"story posted: {story}")


if __name__ == "__main__":
    main()
