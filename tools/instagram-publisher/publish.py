"""
instagram-publisher: posts a reel to Instagram as a Reel.

    python tools/instagram-publisher/publish.py --folder content/<date> [--dry-run]
    python tools/instagram-publisher/publish.py --check          # token and account only, posts nothing

Needs IG_ACCESS_TOKEN and IG_USER_ID (environment, or .env on the PC). The way in depends on the token:
  - a Facebook Login for Business token (EAA..., graph.facebook.com): the reel file is uploaded straight to Instagram
    (resumable upload), no hosting
  - an Instagram Login token (IGAA..., graph.instagram.com): Instagram downloads the video from a public URL, so the
    reel goes up as an asset of a GitHub Release (`gh release upload`; the repo is public) and the asset is deleted
    after the post, posted or not
Then: wait until Instagram has processed the video (status_code FINISHED), publish, read the permalink.
Caption: description.txt in the folder, else script.json's caption. Writes posted.json (media id, permalink).
The token is never printed, and is sent in a header or the request body, not in a URL that could end up in a log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
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
        # Instagram Login tokens start with IG (IGAA..., IGQV...); everything else is a Facebook token
        self.kind = "instagram" if token.startswith("IG") else "facebook"
        self.base = f"https://graph.{'instagram' if self.kind == 'instagram' else 'facebook'}.com/{version}"

    def call(self, method: str, path: str, fields: dict | None = None, url: str | None = None,
             body: bytes | None = None, headers: dict | None = None) -> dict:
        target = url or f"{self.base}/{path.lstrip('/')}"
        hdrs = dict(headers or {})
        if url:  # the upload host takes the token as a header
            fields, hdrs["Authorization"] = dict(fields or {}), f"OAuth {self.token}"
        else:  # the Graph hosts: as a parameter (the request body for POST); URLs are never printed
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
                err = json.loads(raw).get("error", {})
                msg = f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})"
            except (ValueError, AttributeError):
                msg = raw[:300]
            raise RuntimeError(f"{method} {path or 'upload'}: HTTP {e.code}: {msg}") from None


def caption_for(folder: Path, cfg: dict) -> str:
    desc = folder / "description.txt"
    script = folder / "script.json"
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


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def host_on_release(video: Path, hosting: dict) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY") or hosting["repo"]
    tag = hosting["release_tag"]
    if gh("release", "view", tag, "-R", repo).returncode != 0:
        r = gh("release", "create", tag, "-R", repo, "--title", "Reel uploads (temporary)", "--prerelease",
               "--notes", "Reels are put here for a few minutes so Instagram can download them, then deleted.")
        if r.returncode != 0:
            raise RuntimeError(f"could not create release {tag}: {r.stderr.strip()}")
    r = gh("release", "upload", tag, str(video), "-R", repo, "--clobber")  # the asset is named after the file
    if r.returncode != 0:
        raise RuntimeError(f"could not upload the reel to release {tag}: {r.stderr.strip()}")
    return f"https://github.com/{repo}/releases/download/{tag}/{video.name}"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, help="the reel's folder (reel.mp4 + description.txt or script.json)")
    p.add_argument("--check", action="store_true", help="check the token and the account, post nothing")
    p.add_argument("--dry-run", action="store_true", help="check, then show what would be posted")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()

    cfg = load_config(a.config)
    ig = cfg["instagram"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in ig.get("env_files", [])])
    token = os.environ.get(ig["token_env"], "").strip()
    user = os.environ.get(ig["user_env"], "").strip()
    if not token or not user:
        sys.exit(f"set {ig['token_env']} and {ig['user_env']} (repository secrets in the workflow, .env on the PC)")
    api = Api(token, ig["api_version"])
    if not token.startswith(("IG", "EAA")):
        print(f"warning: the token starts with neither IG nor EAA ({len(token)} characters); is it the access token?")
    try:
        acct = api.call("GET", user, {"fields": "username"})
    except RuntimeError as e:
        sys.exit(f"token check failed ({api.kind} login, {len(token)}-character token): {e}")
    print(f"account: @{acct.get('username')} ({api.kind} login: "
          f"{'direct upload' if api.kind == 'facebook' else 'via a GitHub Release asset'})")
    if a.check:
        return

    if not a.folder:
        sys.exit("--folder is needed to post")
    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    caption = caption_for(folder, ig)
    size = video.stat().st_size
    print(f"reel: {video} ({size / 1e6:.1f} MB), caption {len(caption)} characters")
    if a.dry_run:
        print("---\n" + caption + "\n---\n(dry run: nothing posted)")
        return

    base = {"media_type": "REELS", "caption": caption, "share_to_feed": str(ig["share_to_feed"]).lower()}
    hosted = None
    try:
        if api.kind == "facebook":
            container = api.call("POST", f"{user}/media", {**base, "upload_type": "resumable"})["id"]
            up = api.call("POST", "", url=f"https://rupload.facebook.com/ig-api-upload/{ig['api_version']}/{container}",
                          body=video.read_bytes(), headers={"offset": "0", "file_size": str(size)})
            if not up.get("success"):
                raise RuntimeError(f"upload refused: {up}")
        else:
            hosted = f"{folder.name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.mp4"
            staged = folder / hosted
            staged.write_bytes(video.read_bytes())  # a unique name, so a rerun never clashes with an old asset
            try:
                url = host_on_release(staged, cfg["hosting"])
            finally:
                staged.unlink(missing_ok=True)
            print(f"  hosted at {url}")
            container = api.call("POST", f"{user}/media", {**base, "video_url": url})["id"]
        print(f"container {container}: uploaded, waiting for Instagram", flush=True)
        wait_until_ready(api, container, cfg["poll"])
        media = api.call("POST", f"{user}/media_publish", {"creation_id": container})["id"]
    finally:
        if hosted:
            r = gh("release", "delete-asset", cfg["hosting"]["release_tag"], hosted, "-y",
                   "-R", os.environ.get("GITHUB_REPOSITORY") or cfg["hosting"]["repo"])
            print(f"  release asset {hosted}: {'deleted' if r.returncode == 0 else 'NOT deleted: ' + r.stderr.strip()}")
    link = api.call("GET", media, {"fields": "permalink"}).get("permalink", "")
    (folder / "posted.json").write_text(json.dumps({
        "media_id": media, "permalink": link, "account": acct.get("username"), "login": api.kind,
        "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"posted: {link or media}")


if __name__ == "__main__":
    main()
