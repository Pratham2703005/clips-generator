"""
instagram-publisher: posts a reel to Instagram, as a Reel and/or a story.

    python tools/instagram-publisher/publish.py --folder content/<date> [--no-reel] [--no-story] [--dry-run]

Needs IG_ACCESS_TOKEN (the system-user token of the Meta app's Unified login configuration, graph.facebook.com) and
IG_USER_ID (the Instagram account ID), from the environment or .env on the PC. The video file is uploaded straight to
Instagram (resumable upload, rupload.facebook.com). When that host keeps answering ProcessingFailedError, the reel goes
up for a few minutes as an asset of the public repo's release [instagram] release_tag and Instagram fetches it from
there (video_url); the asset is deleted right after (needs the gh CLI with a token that can write releases: GH_TOKEN).
Each post: container -> upload -> wait until processed (status_code FINISHED) -> publish.
  - reel: caption from description.txt (else script.json's caption); cover = compose.json's cover_seconds (the frame
    with the whole hook card), else [instagram] cover_seconds
  - story: the same video; a story takes no caption, and the API has no stickers (poll, link)
  - --trial: the folder's variants/trial/reel.mp4 as a trial reel (shown only to non-followers; trial_params with
    graduation_strategy [instagram] trial_graduation, MANUAL = never moved to the main feed by itself), caption from
    variants/trial/description.txt. No story. Nothing to do when there is no trial variant.
Writes posted.json (reel id and permalink, story id; trial_media_id / trial_permalink). The token is never printed and
never put in a URL.
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


class Hosted:
    """The reel as a temporary release asset of the public repo, made on first use and deleted by close(): the way in
    when Instagram's upload host refuses the file (Threads takes video the same way)."""

    def __init__(self, video: Path, repo: str, tag: str):
        self.video, self.repo, self.tag = video, repo, tag
        self.url = self.asset = ""

    def gh(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", *args, "-R", self.repo], capture_output=True, text=True)

    def get(self) -> str:
        if self.url:
            return self.url
        if not shutil.which("gh"):
            raise RuntimeError("no gh CLI to host the reel for a video_url post")
        if self.gh("release", "view", self.tag).returncode:
            r = self.gh("release", "create", self.tag, "--title", "Reel uploads (temporary)",
                        "--notes", "Reels sit here for a few minutes while Threads / Instagram fetch them; each is deleted after.")
            if r.returncode:
                raise RuntimeError(f"could not make release {self.tag}: {r.stderr.strip()}")
        name = f"instagram-{self.video.parent.name}-{int(time.time())}.mp4"
        tmp = Path(tempfile.mkdtemp()) / name
        shutil.copyfile(self.video, tmp)
        r = self.gh("release", "upload", self.tag, str(tmp), "--clobber")
        shutil.rmtree(tmp.parent, ignore_errors=True)
        if r.returncode:
            raise RuntimeError(f"could not upload the reel to release {self.tag}: {r.stderr.strip()}")
        self.asset, self.url = name, f"https://github.com/{self.repo}/releases/download/{self.tag}/{name}"
        print("  reel hosted as a temporary release asset", flush=True)
        return self.url

    def close(self) -> None:
        if self.asset:
            r = self.gh("release", "delete-asset", self.tag, self.asset, "--yes")
            print("temporary asset deleted" if not r.returncode else f"warning: asset {self.asset} not deleted: {r.stderr.strip()}")
            self.asset = self.url = ""


def resolved(url: str) -> str:
    """Where the github.com download link redirects to (a signed link, valid for a few minutes)."""
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
        return r.geturl()


def post(api: Api, user: str, video: Path, media_type: str, fields: dict, poll: dict, chunk_mb: int = 8,
         hosted: Hosted | None = None) -> str:
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

    def from_url(url: str) -> str:
        return api.call("POST", f"{user}/media", {"media_type": media_type, **fields, "video_url": url})["id"]

    # Meta's upload host answers ProcessingFailedError now and then, for files of any size: 2026-11-02 twice in a row
    # and then fine for the same file; 2026-11-04 the nightly run failed in one piece AND in 8 MB pieces, and a rerun 6 h
    # later went through in pieces; 2026-11-13 it refused all four tries (one piece, 8 MB, 8 MB after 90 s, 4 MB after
    # 180 s) while Facebook took the same file. Pauses don't fix it, so: one piece, then pieces, then Instagram fetches
    # the reel from a temporary public link (video_url) instead of our upload.
    attempts = [0, chunk_mb]
    container = ""
    for i, mb in enumerate(attempts):
        try:
            container = container_and_upload(mb * 1024 * 1024)
            break
        except RuntimeError as e:
            if "ProcessingFailedError" not in str(e):
                raise
            last = i == len(attempts) - 1
            if last and hosted is None:
                raise
            nxt = "a video_url post" if last else f"{attempts[i + 1]} MB pieces"
            print(f"  upload failed ({'one piece' if not mb else f'{mb} MB pieces'}): {e}; next: {nxt}", flush=True)
    if not container:
        url = hosted.get()
        try:
            container = from_url(url)
            wait_until_ready(api, container, poll)
        except RuntimeError as e:  # Meta may not follow github.com's redirect: give it the signed target itself
            print(f"  video_url failed ({e}); trying the redirect's target", flush=True)
            container = from_url(resolved(url))
        print(f"{media_type.lower()} container {container}: fetched from the temporary link", flush=True)
    else:
        print(f"{media_type.lower()} container {container}: uploaded, waiting for Instagram", flush=True)
    wait_until_ready(api, container, poll)
    return api.call("POST", f"{user}/media_publish", {"creation_id": container})["id"]


def post_trial(api: Api, user: str, folder: Path, ig: dict, cfg: dict, dry_run: bool, hosted: Hosted | None = None) -> None:
    """The trial variant as a trial reel (Meta's content publishing API, trial_params; 2026-11-08: first use)."""
    vf = folder / "variants" / "trial"
    video = vf / "reel.mp4"
    caption = caption_for(vf if (vf / "description.txt").is_file() else folder, ig)
    comp = vf / "compose.json"
    cover = json.loads(comp.read_text(encoding="utf-8")).get("cover_seconds") if comp.is_file() else None
    cover = ig["cover_seconds"] if cover is None else cover
    strategy = ig.get("trial_graduation", "MANUAL")
    print(f"trial reel: {video} ({video.stat().st_size / 1e6:.1f} MB), caption {len(caption)} characters, cover at {cover} s, "
          f"graduation {strategy}")
    if dry_run:
        print("---\n" + caption + "\n---\n(dry run: nothing posted)")
        return
    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    if record.get("trial_media_id"):
        print(f"trial reel already posted: {record.get('trial_permalink') or record['trial_media_id']}")
        return
    try:
        media = post(api, user, video, "REELS", {"caption": caption, "thumb_offset": str(int(cover * 1000)),
                                                  "trial_params": json.dumps({"graduation_strategy": strategy})},
                     cfg["poll"], ig.get("chunk_mb", 8), hosted)
    except RuntimeError as e:
        # 2207081 = this account can't post trial reels (yet): refused every night since 2026-11-09 with 8 followers.
        # Not a failure of the run: noted in posted.json so the report says "not eligible yet" instead of ❌/⚠️.
        if "2207081" not in str(e):
            raise
        record.update({"trial_status": "not eligible", "trial_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print("trial reel not posted: Instagram says this account can't post trial reels yet (subcode 2207081)")
        return
    link = api.call("GET", media, {"fields": "permalink"}).get("permalink", "")
    record.update({"trial_media_id": media, "trial_permalink": link,
                   "trial_posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"trial reel posted: {link or media}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True, help="the reel's folder (reel.mp4 + description.txt or script.json)")
    p.add_argument("--no-reel", action="store_true", help="skip the reel (e.g. it is already up)")
    p.add_argument("--no-story", action="store_true", help="skip the story")
    p.add_argument("--dry-run", action="store_true", help="check the token, then show what would be posted")
    p.add_argument("--trial", action="store_true", help="post the trial variant (variants/trial) as a trial reel")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    if a.trial and not (a.folder / "variants" / "trial" / "reel.mp4").is_file():
        print(f"no trial variant in {a.folder}: nothing to post")
        return

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
    repo = os.environ.get("GITHUB_REPOSITORY") or ig.get("repo", "")
    if a.trial:
        hosted = Hosted(folder / "variants" / "trial" / "reel.mp4", repo, ig["release_tag"]) if repo and ig.get("release_tag") else None
        try:
            post_trial(api, user, folder, ig, cfg, a.dry_run, hosted)
        finally:
            if hosted:
                hosted.close()
        return
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
    # one temporary link for the reel and the story, made only if the upload host refuses the file
    hosted = Hosted(video, repo, ig["release_tag"]) if repo and ig.get("release_tag") else None
    try:
        if not a.no_reel:
            media = post(api, user, video, "REELS", {"caption": caption, "thumb_offset": str(int(cover * 1000))}, cfg["poll"],
                         ig.get("chunk_mb", 8), hosted)
            link = api.call("GET", media, {"fields": "permalink"}).get("permalink", "")
            record.update({"media_id": media, "permalink": link, "posted_at": now()})
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(f"reel posted: {link or media}")
        if not a.no_story:
            try:  # a failed story never undoes the reel; the step still fails so it shows
                story = post(api, user, video, "STORIES", {}, cfg["poll"], ig.get("chunk_mb", 8), hosted)
            except RuntimeError as e:
                sys.exit(f"story not posted: {e}")
            record.update({"story_id": story, "story_at": now()})
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(f"story posted: {story}")
    finally:
        if hosted:
            hosted.close()


if __name__ == "__main__":
    main()
