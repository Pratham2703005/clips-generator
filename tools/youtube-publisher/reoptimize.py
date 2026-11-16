"""
reoptimize.py: gives the channel's older Shorts the same title / description / tags the publisher now writes (user's SEO
notes, 2026-11-16: "re-optimize old videos"). Older Shorts were titled by episode ("7 Din, 1 Offer | Din 6: Phone baja"),
which nobody searches for.

A one-off, run on the PC (user, 2026-11-16), with the tokens authorize.py --local wrote to .env:

    python tools/youtube-publisher/reoptimize.py            # dry run: old -> new for every public Short it can match
    python tools/youtube-publisher/reoptimize.py --apply    # write them (videos.update, 50 quota units each)

Each public upload is matched to its episode in series/ (and series/specials/) by the episode name or the search line in
its current title. Unmatched videos are listed and left alone. Job links in old descriptions are long closed, so a promo
episode's job lines are dropped and the description keeps only the lesson. Needs the youtube.force-ssl scope for --apply
(authorize.py); the listing works with youtube.readonly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import socket
import sys
import tomllib
from pathlib import Path

# this PC's IPv6 route to Google hangs (2026-11-16: yt-dlp and Python urllib both waited minutes; IPv4 answers at once)
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _getaddrinfo(*a, **k) if r[0] == socket.AF_INET] or _getaddrinfo(*a, **k)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
API = "https://www.googleapis.com/youtube/v3"
CHANNEL_ID = ""  # from config [youtube] channel_id, set in main()


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pub = load("yt_publish", HERE / "publish.py")
run = load("vg_run", ROOT / "run.py")


def get(token: str, path: str, **params) -> dict:
    import urllib.parse
    with pub.request(f"{API}/{path}?" + urllib.parse.urlencode(params), headers={"Authorization": f"Bearer {token}"}) as r:
        return json.loads(r.read())


def uploads(token: str) -> list[dict]:
    me = get(token, "channels", part="contentDetails,snippet", mine="true")["items"][0]
    if CHANNEL_ID and me["id"] != CHANNEL_ID:
        sys.exit(f"this token is for '{me['snippet']['title']}' ({me['id']}), not the Shorts channel ({CHANNEL_ID}): "
                 "run authorize.py --local again and pick the Shorts channel")
    up = me["contentDetails"]["relatedPlaylists"]["uploads"]
    # 2026-11-16: the uploads playlist ("UU...") listed only the 5 long videos; the Shorts sit in its "UUSH..." twin
    ids: list[str] = []
    for pl in (up, "UUSH" + up[2:]):
        page = None
        while True:
            try:
                res = get(token, "playlistItems", part="contentDetails", playlistId=pl, maxResults=50, **({"pageToken": page} if page else {}))
            except RuntimeError as e:
                print(f"note: playlist {pl} not readable ({str(e)[:100]})")
                break
            ids += [i["contentDetails"]["videoId"] for i in res.get("items", []) if i["contentDetails"]["videoId"] not in ids]
            page = res.get("nextPageToken")
            if not page:
                break
    videos = []
    for i in range(0, len(ids), 50):
        videos += get(token, "videos", part="snippet,status", id=",".join(ids[i:i + 50])).get("items", [])
    return videos


def episodes() -> list[dict]:
    out = []
    for path in sorted((ROOT / "series").glob("*.txt")) + sorted((ROOT / "series" / "specials").glob("*.txt")):
        ep = run.parse_series(path)
        hook = next((m.group(1).strip() for m in [re.search(r"(?mi)^\s*hook\s*:\s*(.+)$", path.read_text(encoding="utf-8"))] if m), "")
        out.append({**ep, "path": path, "hook": hook})
    return out


def lesson_only(description: str) -> str:
    """The description without its job lines ({jobs}, {jobN...}, "Apply:" lines): those jobs closed long ago."""
    keep = [ln for ln in description.splitlines() if "{job" not in ln and not re.match(r"\s*apply\s*:", ln, re.I)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def match(video_title: str, eps: list[dict]) -> dict | None:
    """By the episode name first ("din 6: phone baja"), then by the search line; a search line that is only the series name
    ("7 Din, 1 Offer", Din 1-2) never matches, or every episode of the series would."""
    t = video_title.lower()
    for ep in eps:
        name = ep["title"].split("|")[-1].strip().lower()
        if len(name) > 6 and name in t:
            return ep
    for ep in eps:
        search = (ep["description"].splitlines() or [""])[0].split("|")[0].strip().lower()
        if len(search) > 12 and search in t and search != ep["title"].split("|")[0].strip().lower():
            return ep
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
    p.add_argument("--config", type=Path, default=HERE / "config.toml")
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    yt, v = cfg["youtube"], cfg["video"]
    global CHANNEL_ID
    CHANNEL_ID = yt.get("channel_id", "")
    pub.load_env_files([(a.config.resolve().parent / f).resolve() for f in yt.get("env_files", [])])
    token = pub.access_token(yt)
    eps = episodes()
    changed = unmatched = 0
    for video in uploads(token):
        sn, status = video["snippet"], video.get("status", {})
        if status.get("privacyStatus") != "public":
            continue  # hidden / scheduled ones stay as they are
        ep = match(sn["title"], eps)
        if not ep:
            unmatched += 1
            print(f"- no episode found, left alone: {sn['title']!r}")
            continue
        desc = lesson_only(ep["description"])
        if (desc.splitlines() or [""])[0].split("|")[0].strip().lower() == ep["title"].split("|")[0].strip().lower():
            print(f"- no search line in {ep['path'].name} (the description opens with the series name), left alone: {sn['title']!r}")
            continue
        if v.get("site_url"):
            desc = re.sub(r":?\s*link in bio\.?", f": {v['site_url']}", desc, flags=re.I)
        room = v["title_max"] - (8 if v["shorts_tag"] else 0)
        tail = pub.sentence_case(ep["hook"]) if ep["hook"] else ep["title"]
        title = pub.clean(pub.make_title(tail, (desc.splitlines() or [""])[0], room)).strip()
        if v["shorts_tag"] and "#shorts" not in title.lower() and len(title) + 8 <= v["title_max"]:
            title += " #Shorts"
        tags = pub.make_tags(desc, v.get("tags_max_chars", 450), v.get("suggest_tags", True))
        new = {"title": title[: v["title_max"]], "description": pub.clean(desc)[: v["description_max"]], "tags": tags[:30],
               "categoryId": sn.get("categoryId", v["category_id"]), "defaultLanguage": v["language"],
               "defaultAudioLanguage": v["language"]}
        if new["title"] == sn["title"] and new["description"] == sn.get("description", "") and new["tags"] == sn.get("tags", []):
            continue
        changed += 1
        print(f"\n{video['id']}  ({ep['path'].name})\n  old: {sn['title']}\n  new: {new['title']}\n  tags: {', '.join(tags)}")
        if a.apply:
            body = json.dumps({"id": video["id"], "snippet": new}).encode()
            with pub.request(f"{API}/videos?part=snippet", body, {"Authorization": f"Bearer {token}",
                                                                  "Content-Type": "application/json"}, method="PUT"):
                pass
            print("  updated")
    print(f"\n{changed} to update{'' if a.apply else ' (dry run: nothing written)'}, {unmatched} public Shorts without an episode")


if __name__ == "__main__":
    sys.exit(main())
