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
import shutil
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


ACRONYMS = {"CGPA", "GD", "HR", "CTC", "LPA", "PPO", "AI", "IT", "CV", "PF", "JD", "MBA", "BTECH", "SQL", "API", "TCS"}


def sentence_case(card: str) -> str:
    """The on-screen hook card ("'I WORK TOO HARD' = GALAT") as title text ("'I work too hard' = galat"): caps lock reads
    as shouting in a title. Known acronyms stay upper case."""
    words = []
    for w in card.strip().split():
        core = re.sub(r"[^A-Za-z]", "", w)
        words.append(w if core.upper() in ACRONYMS or core == "I" else w.lower())
    out = " ".join(words)
    # capital letter at the start and after a full stop / question mark
    return re.sub(r"(^|[.?!]\s+)(['\"]?)([a-z])", lambda m: m.group(1) + m.group(2) + m.group(3).upper(), out)


def suggestions(query: str, timeout: int = 10) -> list[str]:
    """What people type on YouTube after `query` (the public autocomplete the search box uses; India, English). Not an
    official API: any failure gives []."""
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(
        {"client": "firefox", "ds": "yt", "hl": "en", "gl": "IN", "q": query})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return [s for s in data[1] if isinstance(s, str)]
    except Exception:  # noqa: BLE001 - tags are a nice-to-have, never a reason to fail the upload
        return []


# off-topic searches, other languages than ours (Hinglish / English), and other audiences than freshers. Found in the
# 2026-11-16 dry run of reoptimize.py: short heads ("how to ask for a", "service bond in") pulled "... a kiss",
# "... in mbbs", "... after first date", "... for nurses"
OFF_TOPIC = re.compile(r"\b(meme|funny|song|status|prank|cabin crew|comedy|reaction|whatsapp|tamil|telugu|kannada|malayalam|"
                       r"bengali|marathi|gujarati|urdu|punjabi|odia|nurses?|teachers?|professor|experienced|mbbs|bams|bds|"
                       r"aiims|medical college|railway|maharashtra|assam|in up|tnpsc|pw|iim|mba|subliminal|manifestation|"
                       r"call cent(er|re)|date|kiss|girl|court|haircut|hamster|processor|antutu|weight loss|indigo|poll|"
                       r"ibew|college admission|raise)\b", re.I)
# a suggestion has to be about jobs / interviews at all
NICHE = re.compile(r"\b(fresher\w*|interview\w*|jobs?|hr|resume|cv|placement\w*|salary|offer|ctc|in.?hand|referral|linkedin|"
                   r"hire|hiring|campus|career|apply\w*|application|internship|bond|rejection|aptitude|weakness\w*|"
                   r"strength\w*|yourself|follow.?up|scam|joining|recruiter|naukri|employ\w*)\b", re.I)


def make_tags(description: str, max_chars: int = 450, suggest: bool = True) -> list[str]:
    """Tags, most specific first: the search phrases of the description's first line ("Why should we hire you answer for
    freshers | HR interview questions"), what YouTube's search box suggests for the first of them (user's SEO notes,
    2026-11-16: tags from real searches, not random), then the #hashtags. YouTube counts at most 500 characters."""
    first = re.sub(r"\s*#\w+", "", (description.splitlines() or [""])[0])
    phrases = [re.sub(r"[?!.]+$", "", p.strip().lower()) for p in first.split("|") if p.strip()]
    found: list[str] = list(phrases)
    if suggest and phrases:
        words = phrases[0].split()
        got: list[str] = []
        for n in dict.fromkeys((len(words), 5, 3)):  # the whole phrase, then shorter heads, until 5+ suggestions
            got += [s for s in suggestions(" ".join(words[:n])) if NICHE.search(s) and not OFF_TOPIC.search(s) and s not in got]
            if len(got) >= 5:
                break
        found += got
    found += [t.lstrip("#").lower() for t in re.findall(r"#\w+", description)]
    tags, used = [], 0
    for t in dict.fromkeys(found):
        cost = len(t) + (2 if " " in t else 0) + 1  # YouTube counts quotes around a tag with spaces, and the comma
        if len(t) <= 100 and used + cost <= max_chars:
            tags.append(t)
            used += cost
    return tags


def upload_name(title: str) -> str:
    """The file name sent with the upload: the title, not "reel.mp4" (user, 2026-11-16; YouTube never said it matters,
    but it costs nothing)."""
    slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"#\w+", "", title).lower()).strip("-")
    return (slug[:90].rstrip("-") or "short") + ".mp4"


def metadata(folder: Path, v: dict, desc_folder: Path | None = None, suggest: bool = False) -> dict:
    script = json.loads((folder / "script.json").read_text(encoding="utf-8")) if (folder / "script.json").is_file() else {}
    desc_file = (desc_folder or folder) / "description.txt"
    description = desc_file.read_text(encoding="utf-8").strip() if desc_file.is_file() else script.get("caption", "")
    room = v["title_max"] - (8 if v["shorts_tag"] else 0)
    # keyword first, then curiosity (user's SEO notes, 2026-11-16): the hook card ("Na hardworking. Na passionate.")
    # instead of the episode name ("Interview prep | Why should we hire you?"), which only repeated the keyword
    tail = sentence_case(script["hook_card"]) if v.get("hook_in_title", True) and script.get("hook_card") else script.get("title", "")
    title = clean(make_title(tail, (description.splitlines() or [""])[0], room) or folder.name).strip()
    if v["shorts_tag"] and "#shorts" not in title.lower() and len(title) + 8 <= v["title_max"]:
        title += " #Shorts"
    tags = make_tags(description, v.get("tags_max_chars", 450), suggest and v.get("suggest_tags", True))
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


CAPTIONS = "https://www.googleapis.com/upload/youtube/v3/captions"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def caption_lines(folder: Path, media_folder: Path) -> list[tuple[float, float, str]]:
    """(start, end, text) per spoken line, from the reel's timeline.json (the script's own words, timed by Whisper). The
    YouTube variant has the same timing with its own last line ("subscribe" for "follow"), so its texts are used."""
    tl = folder / "timeline.json"
    if not tl.is_file():
        return []
    lines = [(float(x["start"]), float(x["end"]), str(x["text"]).strip()) for x in json.loads(tl.read_text(encoding="utf-8")).get("lines", [])]
    vs = media_folder / "script.json"
    if media_folder != folder and vs.is_file():
        texts = [str(x.get("text", "")).strip() for x in json.loads(vs.read_text(encoding="utf-8")).get("lines", []) if isinstance(x, dict)]
        if len(texts) == len(lines) and all(texts):
            lines = [(s, e, t) for (s, e, _), t in zip(lines, texts)]
    return [x for x in lines if x[2]]


def srt(lines: list[tuple[float, float, str]]) -> str:
    def ts(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i}\n{ts(s)} --> {ts(max(e, s + 0.3))}\n{t}\n" for i, (s, e, t) in enumerate(lines, 1))


def translate_lines(texts: list[str], models: str | list[str], timeout: int = 60) -> list[str]:
    """Hinglish lines in English, one for one (Gemini; GEMINI_API_KEY), trying each model in turn (2026-11-16: flash-lite
    answered 503 "high demand"). [] when all fail: the English track is skipped."""
    for model in [models] if isinstance(models, str) else models:
        out = translate_with(texts, model)
        if out:
            return out
    return []


def translate_with(texts: list[str], model: str) -> list[str]:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not texts:
        return []
    prompt = ("Translate each line of this Hinglish (Hindi and English in Roman script) dialogue into natural, simple "
              "English for subtitles. Keep names and job terms as they are. Answer with a JSON array of strings, exactly "
              f"one per input line, same order.\n\n{json.dumps(texts, ensure_ascii=False)}")
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}).encode()
    try:
        with request(GEMINI.format(model=model) + f"?key={urllib.parse.quote(key)}", body, {"Content-Type": "application/json"}) as r:
            out = json.loads(json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:  # noqa: BLE001
        print(f"warning: {model} could not translate the captions ({str(e).replace(key, '***')[:160]})")
        return []
    if not isinstance(out, list) or len(out) != len(texts) or not all(isinstance(x, str) and x.strip() for x in out):
        print(f"warning: {model}'s translation did not come back line for line")
        return []
    return [x.strip() for x in out]


def upload_caption(token: str, vid: str, language: str, name: str, body_srt: str) -> str:
    boundary = "caption" + os.urandom(8).hex()
    meta = json.dumps({"snippet": {"videoId": vid, "language": language, "name": name, "isDraft": False}})
    data = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + body_srt.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode()
    try:
        with request(f"{CAPTIONS}?uploadType=multipart&part=snippet", data,
                     {"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"}):
            pass
        return "set"
    except RuntimeError as e:
        hint = " (the token needs the youtube.force-ssl scope: run authorize.py again)" if "403" in str(e) else ""
        return f"not set ({str(e)[:160]}){hint}"


def add_captions(token: str, vid: str, folder: Path, media_folder: Path, v: dict) -> dict:
    """Caption tracks from the script's own words (user's SEO notes, 2026-11-16): YouTube reads captions for search, and
    its own speech recognition hears Hinglish as Hindi. A Hinglish track (the exact script) and an English one (Gemini)."""
    lines = caption_lines(folder, media_folder)
    if not lines:
        return {"youtube_captions": "no timeline.json"}
    done = {}
    for lang in v.get("caption_languages", ["hi-Latn", "en"]):
        if lang == "en":
            english = translate_lines([t for _, _, t in lines], v.get("translate_models", ["gemini-3.5-flash-lite", "gemini-3.5-flash"]))
            if not english:
                done[lang] = "skipped"
                continue
            done[lang] = upload_caption(token, vid, "en", "English", srt([(s, e, t) for (s, e, _), t in zip(lines, english)]))
        else:
            done[lang] = upload_caption(token, vid, lang, "Hinglish", srt(lines))
    print("captions: " + ", ".join(f"{k} {r}" for k, r in done.items()))
    return {"youtube_captions": {k: r[:200] for k, r in done.items()}}


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
    if not shutil.which("ffmpeg"):  # 2026-11-10: the Post reel runner had none and the Short's step failed after upload
        print("warning: no ffmpeg here, so no cover frame")
        return None
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
    # the YouTube variant (run.py make_variants): the same reel saying "subscribe" where it says "follow"
    # (not named `yt`: that is the [youtube] config, and reusing the name broke access_token on 2026-11-10)
    variant = folder / "variants" / "youtube"
    media_folder = variant if (variant / "reel.mp4").is_file() else folder
    if media_folder is variant:
        video = variant / "reel.mp4"
        print(f"YouTube variant: {video}")
    done = folder / "posted.json"
    if done.is_file() and json.loads(done.read_text(encoding="utf-8")).get("youtube_url"):
        # a Post reel run of a reel that is partly up: never upload the same Short twice
        print(f"already on YouTube: {json.loads(done.read_text(encoding='utf-8'))['youtube_url']}")
        return
    meta = metadata(folder, v, media_folder if (media_folder / "description.txt").is_file() else None, suggest=True)
    when = None if a.now or v["privacy"] != "public" else publish_time(v.get("publish_at", ""))
    if when:  # YouTube's scheduled publishing: private until publishAt
        meta["status"].update({"privacyStatus": "private", "publishAt": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
    token = access_token(yt)
    print(f"short: {video} ({video.stat().st_size / 1e6:.1f} MB), title {meta['snippet']['title']!r}, "
          f"{len(meta['snippet']['tags'])} tags, " + (f"public at {when:%d %b %H:%M} IST" if when else meta['status']['privacyStatus']))
    name = upload_name(meta["snippet"]["title"])
    if a.dry_run:
        lines = caption_lines(folder, media_folder)
        print(f"file name: {name}\ntags: {', '.join(meta['snippet']['tags'])}\ncaptions: {len(lines)} lines "
              f"({', '.join(v.get('caption_languages', [])) if v.get('captions', True) else 'off'})")
        print("---\n" + meta["snippet"]["description"] + "\n---\n(dry run: nothing uploaded)")
        return

    data = video.read_bytes()
    auth = {"Authorization": f"Bearer {token}"}
    with request(f"{UPLOAD}?uploadType=resumable&part=snippet,status", json.dumps(meta).encode(), {
        **auth, "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(len(data)), "Slug": name,
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
        thumb = set_thumbnail(token, vid, media_folder, video, v.get("cover_seconds", 2.2))
        print(f"thumbnail: {thumb}" if thumb == "set" else f"warning: thumbnail {thumb}; YouTube picks a frame itself")
        record["youtube_thumbnail"] = thumb[:200]
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if v.get("captions", True):  # after the Short is up and recorded: a caption failure never fails the upload
        record.update(add_captions(token, vid, folder, media_folder, v))
        record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
