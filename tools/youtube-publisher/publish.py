"""
youtube-publisher: uploads a reel to YouTube as a Short.

    python tools/youtube-publisher/publish.py --folder content/<date> [--dry-run]

Needs YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN (environment, or .env on the PC; authorize.py makes them).
  - title: the description's first line (what people search for: "First interview call as a fresher | HR call tips")
    + " | " + script.json's title (the episode: "7 Din, 1 Offer | Din 6: Phone baja"), shortened to fit, + #Shorts.
    A description that starts with the episode itself (Din 1-2) gives just the episode title.
  - description: description.txt (else script.json's caption); its #hashtags also become tags. "link in bio" becomes
    the website's URL ([video] site_url): a Short has no bio link next to it
  - goes public at [video] publish_at (IST) the same day, uploaded private with publishAt until then; right away when
    that time is under 15 minutes off or past (--now: always right away)
  - thumbnail: the Instagram cover frame (compose.json's cover_seconds). YouTube allows custom Shorts thumbnails only
    for Partner Program channels (2026-07), so a refusal is only a warning; the Short is up either way
  - a vertical video up to 3 minutes is a Short by itself
Upload: resumable (one PUT); the answer carries the video's status. Adds youtube_id/youtube_url to posted.json.
Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
IST = timezone(timedelta(hours=5, minutes=30))


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def request(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        return urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(raw)
            err = j.get("error")
            # the Data API: {"error": {"message": ...}}; the token endpoint: {"error": "invalid_grant", "error_description": ...}
            msg = err.get("message", raw[:400]) if isinstance(err, dict) else f"{err}: {j.get('error_description', '')}"
        except (ValueError, AttributeError):
            msg = raw[:400]
        where = urllib.parse.urlparse(url).path
        raise RuntimeError(f"{method or ('POST' if data is not None else 'GET')} {where}: HTTP {e.code}: {msg}") from None


def access_token(cfg: dict) -> str:
    ids = {k: os.environ.get(cfg[f"{k}_env"], "").strip() for k in ("client_id", "client_secret", "refresh_token")}
    missing = [cfg[f"{k}_env"] for k, v in ids.items() if not v]
    if missing:
        sys.exit(f"set {', '.join(missing)} (run authorize.py once; repository secrets in the workflow)")
    body = urllib.parse.urlencode({**ids, "grant_type": "refresh_token"}).encode()
    try:
        with request("https://oauth2.googleapis.com/token", body) as r:
            return json.loads(r.read())["access_token"]
    except RuntimeError as e:
        sys.exit(f"refresh token rejected ({e}). Is the OAuth app 'In production'? Run authorize.py again.")


def clean(text: str) -> str:
    return text.replace("<", "").replace(">", "")  # YouTube rejects angle brackets in titles and descriptions


def make_title(episode: str, first_line: str, limit: int) -> str:
    """Search line + episode, shortened to `limit`. On YouTube the title is what the Shorts feed and search show; the
    episode name alone ("Din 6: Phone baja") told a new viewer nothing (2026-11-07: 8 Shorts, ~475 views vs ~8,100 on
    Instagram, whose caption opens with the search line)."""
    episode, first_line = episode.strip(), re.sub(r"\s*#\w+", "", first_line).strip()
    head = lambda s: s.split("|")[0].strip().lower()  # noqa: E731
    if not first_line or (episode and head(first_line) == head(episode)):
        return episode or first_line  # the description opens with the episode itself: nothing to search for
    parts = [p.strip() for p in first_line.split("|") if p.strip()]
    tail = [episode, episode.split("|")[-1].strip()] if episode else []  # "7 Din, 1 Offer | Din 6: ...", "Din 6: ..."
    # the episode stays if it fits with at least the first search phrase; then the search line alone
    for n in range(len(parts), 0, -1):
        search = " | ".join(parts[:n])
        for t in tail:
            title = f"{search} | {t}"
            if t.lower() not in search.lower() and len(title) <= limit:
                return title
    for n in range(len(parts), 0, -1):
        if len(" | ".join(parts[:n])) <= limit:
            return " | ".join(parts[:n])
    return first_line[:limit]


def metadata(folder: Path, v: dict) -> dict:
    script = json.loads((folder / "script.json").read_text(encoding="utf-8")) if (folder / "script.json").is_file() else {}
    desc_file = folder / "description.txt"
    description = desc_file.read_text(encoding="utf-8").strip() if desc_file.is_file() else script.get("caption", "")
    room = v["title_max"] - (8 if v["shorts_tag"] else 0)
    title = clean(make_title(script.get("title", ""), (description.splitlines() or [""])[0], room) or folder.name).strip()
    if v["shorts_tag"] and "#shorts" not in title.lower() and len(title) + 8 <= v["title_max"]:
        title += " #Shorts"
    tags = list(dict.fromkeys(t.lstrip("#") for t in re.findall(r"#\w+", description)))
    # YouTube has no "bio" link next to a Short: the website's URL goes in the description itself (user, 2026-11-03).
    # Only where the description points to the bio: a value episode without one promotes nothing (user, 2026-11-07)
    if v.get("site_url"):
        description = re.sub(r":?\s*link in bio\.?", f": {v['site_url']}", description, flags=re.I)
    return {
        "snippet": {"title": title[: v["title_max"]], "description": clean(description)[: v["description_max"]],
                    "tags": tags[:30], "categoryId": v["category_id"],
                    "defaultLanguage": v["language"], "defaultAudioLanguage": v["language"]},
        "status": {"privacyStatus": v["privacy"], "selfDeclaredMadeForKids": v["made_for_kids"],
                   "containsSyntheticMedia": v["synthetic_media"]},
    }


def publish_time(at: str, now: datetime | None = None) -> datetime | None:
    """Today's `at` (HH:MM, IST) when it is 15+ minutes away, else None (= public right away). The nightly run uploads
    at 00:00 IST; the Short goes public in the evening, when more of its first test audience is awake (user, 2026-11-07)."""
    if not at:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    h, m = (int(x) for x in at.split(":"))
    when = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return when if when - now >= timedelta(minutes=15) else None


def cover_frame(folder: Path, video: Path, seconds: float) -> Path | None:
    """The Instagram cover (the frame with the whole hook card) as a JPEG under YouTube's 2 MB thumbnail limit."""
    out = Path(tempfile.gettempdir()) / f"yt-cover-{folder.name}.jpg"
    for q in (3, 6, 10):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{seconds:.2f}", "-i", str(video), "-frames:v", "1",
                            "-q:v", str(q), str(out)], capture_output=True, text=True)
        if r.returncode != 0 or not out.is_file():
            print(f"warning: no cover frame ({r.stderr.strip()[:200]})")
            return None
        if out.stat().st_size < 2_000_000:
            return out
    return None


def set_thumbnail(token: str, vid: str, folder: Path, video: Path, default_seconds: float) -> str:
    comp = folder / "compose.json"
    seconds = json.loads(comp.read_text(encoding="utf-8")).get("cover_seconds") if comp.is_file() else None
    image = cover_frame(folder, video, default_seconds if seconds is None else seconds)
    if not image:
        return "no cover frame"
    try:
        with request(f"{THUMBNAIL}?videoId={vid}", image.read_bytes(), {"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}):
            pass
        return "set"
    except RuntimeError as e:
        return f"not set ({e})"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True, help="the reel's folder (reel.mp4 + description.txt/script.json)")
    p.add_argument("--dry-run", action="store_true", help="check the token, then show what would be uploaded")
    p.add_argument("--now", action="store_true", help="public right away, not at [video] publish_at")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    yt, v = cfg["youtube"], cfg["video"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in yt.get("env_files", [])])

    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    done = folder / "posted.json"
    if done.is_file() and json.loads(done.read_text(encoding="utf-8")).get("youtube_url"):
        # a Post reel run of a reel that is partly up: never upload the same Short twice
        print(f"already on YouTube: {json.loads(done.read_text(encoding='utf-8'))['youtube_url']}")
        return
    meta = metadata(folder, v)
    when = None if a.now or v["privacy"] != "public" else publish_time(v.get("publish_at", ""))
    if when:  # YouTube's scheduled publishing: private until publishAt
        meta["status"].update({"privacyStatus": "private", "publishAt": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
    token = access_token(yt)
    print(f"short: {video} ({video.stat().st_size / 1e6:.1f} MB), title {meta['snippet']['title']!r}, "
          f"{len(meta['snippet']['tags'])} tags, " + (f"public at {when:%d %b %H:%M} IST" if when else meta['status']['privacyStatus']))
    if a.dry_run:
        print("---\n" + meta["snippet"]["description"] + "\n---\n(dry run: nothing uploaded)")
        return

    data = video.read_bytes()
    auth = {"Authorization": f"Bearer {token}"}
    with request(f"{UPLOAD}?uploadType=resumable&part=snippet,status", json.dumps(meta).encode(), {
        **auth, "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(data)),
    }) as r:
        session = r.headers["Location"]
    # the upload's answer is the video (snippet + status): no read-back call, which would need a read scope on top of
    # youtube.upload (2026-11-02: videos.list answered 403 "insufficient authentication scopes" after a good upload)
    with request(session, data, {**auth, "Content-Type": "video/mp4"}, method="PUT") as r:
        uploaded = json.loads(r.read())
    vid, status = uploaded["id"], uploaded.get("status", {})
    url = f"https://youtube.com/shorts/{vid}"
    print(f"uploaded: {url} (upload {status.get('uploadStatus')}, {status.get('privacyStatus')}"
          + (f", public at {status['publishAt']}" if status.get("publishAt") else "") + ")")
    if status.get("privacyStatus") and status["privacyStatus"] != meta["status"]["privacyStatus"]:
        print(f"warning: asked for {meta['status']['privacyStatus']}, YouTube has it {status['privacyStatus']} (an unverified-project lock?)")
    if when and not status.get("publishAt"):
        print("warning: YouTube kept no publish time, so the Short stays private: make it public in YouTube Studio")

    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    record.update({"youtube_id": vid, "youtube_url": url, "youtube_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if status.get("publishAt"):
        record["youtube_public_at"] = status["publishAt"]
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")  # before the thumbnail: the Short is up

    if v.get("thumbnail", True):
        thumb = set_thumbnail(token, vid, folder, video, v.get("cover_seconds", 2.2))
        print(f"thumbnail: {thumb}" if thumb == "set" else f"warning: thumbnail {thumb}; YouTube picks a frame itself")
        record["youtube_thumbnail"] = thumb[:200]
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
