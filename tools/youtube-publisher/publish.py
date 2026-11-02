"""
youtube-publisher: uploads a reel to YouTube as a Short.

    python tools/youtube-publisher/publish.py --folder content/<date> [--dry-run]

Needs YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN (environment, or .env on the PC; authorize.py makes them).
  - title: script.json's title (a series episode: "7 Din, 1 Offer | Din 2: Resume post-mortem"), + #Shorts if it fits
  - description: description.txt (else script.json's caption); its #hashtags also become tags. Links in a Shorts
    description are not clickable, so the description says "link in bio"/channel as it is
  - a vertical video up to 3 minutes is a Short by itself; the API cannot set a Shorts thumbnail
Upload: resumable (one PUT), then reads the video's status. Adds youtube_id/youtube_url to posted.json.
Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"


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


def metadata(folder: Path, v: dict) -> dict:
    script = json.loads((folder / "script.json").read_text(encoding="utf-8")) if (folder / "script.json").is_file() else {}
    desc_file = folder / "description.txt"
    description = desc_file.read_text(encoding="utf-8").strip() if desc_file.is_file() else script.get("caption", "")
    title = clean(script.get("title") or (description.splitlines() or [folder.name])[0]).strip()
    if v["shorts_tag"] and "#shorts" not in title.lower() and len(title) + 8 <= v["title_max"]:
        title += " #Shorts"
    tags = list(dict.fromkeys(t.lstrip("#") for t in re.findall(r"#\w+", description)))
    return {
        "snippet": {"title": title[: v["title_max"]], "description": clean(description)[: v["description_max"]],
                    "tags": tags[:30], "categoryId": v["category_id"],
                    "defaultLanguage": v["language"], "defaultAudioLanguage": v["language"]},
        "status": {"privacyStatus": v["privacy"], "selfDeclaredMadeForKids": v["made_for_kids"],
                   "containsSyntheticMedia": v["synthetic_media"]},
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True, help="the reel's folder (reel.mp4 + description.txt/script.json)")
    p.add_argument("--dry-run", action="store_true", help="check the token, then show what would be uploaded")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    yt, v = cfg["youtube"], cfg["video"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in yt.get("env_files", [])])

    folder = a.folder.resolve()
    video = folder / "reel.mp4"
    if not video.is_file():
        sys.exit(f"no reel at {video}")
    meta = metadata(folder, v)
    token = access_token(yt)
    print(f"short: {video} ({video.stat().st_size / 1e6:.1f} MB), title {meta['snippet']['title']!r}, "
          f"{len(meta['snippet']['tags'])} tags, {meta['status']['privacyStatus']}")
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
    with request(session, data, {**auth, "Content-Type": "video/mp4"}, method="PUT") as r:
        vid = json.loads(r.read())["id"]
    with request(f"{API}/videos?part=status&id={vid}", headers=auth) as r:
        items = json.loads(r.read()).get("items", [])
    status = items[0]["status"] if items else {}
    url = f"https://youtube.com/shorts/{vid}"
    print(f"uploaded: {url} (upload {status.get('uploadStatus')}, {status.get('privacyStatus')})")
    if status.get("privacyStatus") and status["privacyStatus"] != v["privacy"]:
        print(f"warning: asked for {v['privacy']}, YouTube has it {status['privacyStatus']} (an unverified-project lock?)")

    record_path = folder / "posted.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    record.update({"youtube_id": vid, "youtube_url": url, "youtube_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
