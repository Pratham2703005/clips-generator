"""
facebook-publisher: posts the day's reel to the Facebook Page as a Reel and as a story.

    python tools/facebook-publisher/publish.py --folder content/<date> [--no-reel] [--no-story] [--dry-run]

Story (2026-11-10): the same video uploaded again to video_stories (start -> upload -> finish; post_id kept as
facebook_story_id). A failed reel never stops the story, and the other way round; the step fails if either did.

Needs FB_PAGE_TOKEN and FB_PAGE_ID (repository secrets in the workflow, .env on the PC). The token is the Page token of
"Isha - Job Shorts" from Graph API Explorer (clip-generator app, an extended user token with pages_show_list,
pages_read_engagement, pages_manage_posts, business_management -> the Page's access_token, which does not expire). A user
token works too: the Page token is then fetched with it.
Reels publishing API: start (video id + upload URL) -> the file straight to rupload.facebook.com (no hosting) -> finish
with the caption and video_state PUBLISHED -> wait until Facebook reports the publishing phase complete.
Caption = description.txt (else script.json's caption); links are clickable on Facebook, so it goes as it is.
Writes facebook_video_id / facebook_url / facebook_posted_at into posted.json and skips when they are already there.
The token is never printed and never put in a printed URL.
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


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def call(method: str, url: str, token: str, fields: dict | None = None, body: bytes | None = None,
         headers: dict | None = None) -> dict:
    """graph.facebook.com takes the token as a parameter (the body for POST); the upload host (body given) as a header."""
    hdrs, fields = dict(headers or {}), dict(fields or {})
    if body is not None:
        hdrs["Authorization"] = f"OAuth {token}"
        data = body
    else:
        fields["access_token"] = token
        data = None if method == "GET" else urllib.parse.urlencode(fields).encode()
        if method == "GET":
            url += "?" + urllib.parse.urlencode(fields)
        else:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(raw)
            err = j.get("error")
            msg = (f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})" if err
                   else json.dumps(j.get("debug_info") or j)[:600])
        except (ValueError, AttributeError):
            msg = raw[:600]
        where = urllib.parse.urlsplit(url).path.split("/")[-1] or "upload"
        raise RuntimeError(f"{method} {where}: HTTP {e.code}: {msg}") from None


def caption_for(folder: Path, limit: int, site_url: str = "") -> str:
    desc, script = folder / "description.txt", folder / "script.json"
    if desc.is_file():
        text = desc.read_text(encoding="utf-8").strip()
    elif script.is_file():
        text = json.loads(script.read_text(encoding="utf-8")).get("caption", "").strip()
    else:
        text = ""
    if not text:
        print("warning: no description.txt or script.json caption: posting without a caption")
    if site_url:  # as on YouTube: the link itself, clickable here, instead of "link in bio"
        text = re.sub(r":?\s*link in bio\.?", f": {site_url}", text, flags=re.I)
    return text[:limit].rstrip()


def page_token(base: str, token: str, page_id: str) -> tuple[str, str, str]:
    """(page token, page id, page name). FB_PAGE_ID may be the Business settings ID or the Page's own ID."""
    try:
        p = call("GET", f"{base}/{page_id}", token, {"fields": "id,name,access_token"})
        return p.get("access_token") or token, p.get("id", page_id), p.get("name", "")
    except RuntimeError as e:
        try:  # a Page token answers /me with the Page itself
            me = call("GET", f"{base}/me", token, {"fields": "id,name"})
        except RuntimeError:
            raise e from None
        print(f"note: {page_id} did not answer ({e}); using the token's own Page {me.get('id')}")
        return token, me["id"], me.get("name", "")


def token_expiry(base: str, token: str) -> str:
    """'never' / a date / '' (needs META_APP_ID + IG_APP_SECRET: the token comes from the same clip-generator app)."""
    app_id, secret = os.environ.get("META_APP_ID", "").strip(), os.environ.get("IG_APP_SECRET", "").strip()
    if not app_id or not secret:
        return ""
    try:
        d = call("GET", f"{base}/debug_token", f"{app_id}|{secret}", {"input_token": token}).get("data", {})
    except RuntimeError as e:
        return f"unknown ({e})"
    exp = d.get("expires_at", 0)
    kind = (d.get("type") or "").lower()
    when = "never expires" if not exp else "expires " + datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d")
    return f"{kind} token, {when}"


def listed_reel(base: str, token: str, page_id: str, video_id: str) -> dict | None:
    """The reel in the Page's own list (id, permalink_url), or None. 2026-11-10: the Page token could list the Page's
    reels but not read the new video object itself ("does not exist ... missing permissions", code 100 / 33)."""
    for r in call("GET", f"{base}/{page_id}/video_reels", token, {"fields": "id,permalink_url", "limit": 25}).get("data", []):
        if r.get("id") == video_id:
            return r
    return None


def wait_until_published(base: str, token: str, page_id: str, video_id: str, poll: dict) -> str:
    """Waits until the reel is published; returns its permalink when it is known on the way ("" otherwise)."""
    deadline = time.time() + poll["max_minutes"] * 60
    while True:
        try:
            st = call("GET", f"{base}/{video_id}", token, {"fields": "status"}).get("status", {})
        except RuntimeError as e:
            if "code 100" not in str(e):
                raise
            hit = listed_reel(base, token, page_id, video_id)
            print(f"  status not readable with this token; {'listed on the Page' if hit else 'not on the Page yet'}", flush=True)
            if hit:
                return hit.get("permalink_url", "")
            if time.time() > deadline:
                raise RuntimeError(f"reel {video_id} not on the Page after {poll['max_minutes']} min ({e})") from None
            time.sleep(poll["every_seconds"])
            continue
        phases = {k: (st.get(k) or {}).get("status") for k in ("uploading_phase", "processing_phase", "publishing_phase")}
        print(f"  status: {st.get('video_status')} | " + ", ".join(f"{k.split('_')[0]} {v}" for k, v in phases.items()),
              flush=True)
        if st.get("video_status") == "error" or "error" in phases.values():
            err = next(((st.get(k) or {}).get("errors") for k in phases if (st.get(k) or {}).get("errors")), None)
            raise RuntimeError(f"Facebook could not process the reel: {err or st}")
        if phases.get("publishing_phase") == "complete":
            return ""
        if time.time() > deadline:
            raise RuntimeError(f"not published after {poll['max_minutes']} min (video {video_id}); check the Page")
        time.sleep(poll["every_seconds"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="check the token and Page, then show what would be posted")
    p.add_argument("--no-reel", action="store_true", help="skip the reel")
    p.add_argument("--no-story", action="store_true", help="skip the story")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    fb = cfg["facebook"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in fb.get("env_files", [])])
    token, page_id = os.environ.get(fb["token_env"], "").strip(), os.environ.get(fb["page_env"], "").strip()
    if not token or not page_id:
        sys.exit(f"set {fb['token_env']} and {fb['page_env']}")
    base = f"https://graph.facebook.com/{fb['api_version']}"

    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    try:
        ptoken, page_id, name = page_token(base, token, page_id)
    except RuntimeError as e:
        sys.exit(f"token/Page check failed ({len(token)}-character token): {e}")
    print(f"page: {name} (id {page_id}); {token_expiry(base, ptoken) or 'token expiry not checked'}")
    caption = caption_for(folder, fb["caption_max"], fb.get("site_url", ""))
    print(f"reel: {video} ({video.stat().st_size / 1e6:.1f} MB), caption {len(caption)} characters")
    if a.dry_run:
        print("---\n" + caption + "\n---\n(dry run: nothing posted)")
        return

    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    data = video.read_bytes()

    def upload(edge: str) -> str:
        """start -> the file straight to rupload.facebook.com; returns the video id (a story needs its own upload:
        Meta refuses a video that is already in a published post)."""
        start = call("POST", f"{base}/{page_id}/{edge}", ptoken, {"upload_phase": "start"})
        vid = start["video_id"]
        url = start.get("upload_url") or f"https://rupload.facebook.com/video-upload/{fb['api_version']}/{vid}"
        up = call("POST", url, ptoken, body=data, headers={"offset": "0", "file_size": str(len(data))})
        if not up.get("success"):
            raise RuntimeError(f"upload refused: {up}")
        return vid

    failed = []
    if a.no_reel:
        pass
    elif record.get("facebook_video_id"):
        print(f"Facebook reel already posted: {record.get('facebook_url') or record['facebook_video_id']}")
    else:
        try:
            video_id = upload("video_reels")
            print(f"reel video {video_id}: uploaded, publishing", flush=True)
            done = call("POST", f"{base}/{page_id}/video_reels", ptoken,
                        {"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED", "description": caption})
            if not done.get("success"):
                raise RuntimeError(f"finish refused: {done}")
            # recorded as soon as Facebook accepted it, so a rerun never posts the reel twice (2026-11-10: it went up,
            # the status check failed after, and posted.json had nothing)
            record.update({"facebook_video_id": video_id, "facebook_posted_at": now()})
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            link = wait_until_published(base, ptoken, page_id, video_id, cfg["poll"])
            if not link:
                try:
                    link = call("GET", f"{base}/{video_id}", ptoken, {"fields": "permalink_url"}).get("permalink_url", "")
                except RuntimeError:
                    link = (listed_reel(base, ptoken, page_id, video_id) or {}).get("permalink_url", "")
            if link.startswith("/"):
                link = "https://www.facebook.com" + link
            record["facebook_url"] = link
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(f"Facebook reel posted: {link or video_id}")
        except RuntimeError as e:
            failed.append(f"reel: {e}")
            print(f"Facebook reel: {e}", flush=True)

    # the same video as a Page story, like the Instagram story (user, 2026-11-10). Stories take 3-90 s.
    secs = (json.loads((folder / "compose.json").read_text(encoding="utf-8")).get("seconds")
            if (folder / "compose.json").is_file() else None)
    if a.no_story:
        pass
    elif record.get("facebook_story_id"):
        print(f"Facebook story already posted: {record['facebook_story_id']}")
    elif secs and not 3 <= secs <= 90:
        print(f"Facebook story skipped: the reel is {secs:.0f} s (stories take 3-90 s)")
    else:
        try:
            story_vid = upload("video_stories")
            done = call("POST", f"{base}/{page_id}/video_stories", ptoken, {"upload_phase": "finish", "video_id": story_vid})
            if not done.get("success"):
                raise RuntimeError(f"finish refused: {done}")
            record.update({"facebook_story_id": done.get("post_id") or story_vid, "facebook_story_at": now()})
            record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            print(f"Facebook story posted: {record['facebook_story_id']}")
        except RuntimeError as e:
            failed.append(f"story: {e}")
            print(f"Facebook story: {e}", flush=True)
    if failed:
        sys.exit("Facebook: " + " | ".join(failed))


if __name__ == "__main__":
    main()
