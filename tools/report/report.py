"""
report: emails how a Daily reel run went (reel made? Instagram reel, story, YouTube Short posted?) plus the last
[report] days of stats from Instagram and YouTube.

    python tools/report/report.py --folder content/<date> [--dry-run]

Step outcomes come from the workflow as environment variables (success / failure / skipped / cancelled / empty):
OUT_MAKE, OUT_INSTAGRAM, OUT_YOUTUBE, plus RUN_URL and DAY. The links come from the folder's posted.json.
Stats (each part is skipped with a note when its permission is missing, never failing the report):
  - Instagram: followers and posts (instagram_basic); per post likes and comments, and views/reach/saves/shares when
    the token has instagram_manage_insights
  - YouTube: subscribers, total views, and per Short views/likes/comments when the refresh token has youtube.readonly
Sent with Gmail SMTP (SMTP_USER + an app password in SMTP_PASSWORD) to REPORT_TO (default: SMTP_USER).
--dry-run prints the email instead of sending it.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
MARK = {"success": "✅", "failure": "❌", "cancelled": "⏹", "skipped": "➖", "": "➖"}


def load_env_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", raw)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def get_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error")
            msg = err.get("message", raw[:200]) if isinstance(err, dict) else str(err)
        except (ValueError, AttributeError):
            msg = raw[:200]
        raise RuntimeError(f"HTTP {e.code}: {msg}") from None


# ---------- Instagram ----------

def instagram_stats(cfg: dict, since: datetime) -> dict:
    token, user = os.environ.get(cfg["token_env"], ""), os.environ.get(cfg["user_env"], "")
    if not token or not user:
        return {"note": "IG_ACCESS_TOKEN / IG_USER_ID not set"}
    base = f"https://graph.facebook.com/{cfg['api_version']}"

    def call(path: str, **params) -> dict:
        return get_json(f"{base}/{path}?" + urllib.parse.urlencode({**params, "access_token": token}))

    out: dict = {}
    try:
        acct = call(user, fields="username,followers_count,follows_count,media_count")
        out.update({k: acct.get(k) for k in ("username", "followers_count", "media_count")})
        posts, url = [], f"{user}/media"
        page = call(url, fields="id,caption,media_product_type,timestamp,permalink,like_count,comments_count", limit=50)
        while True:  # newest first: stop at the first page that reaches past the window
            older = False
            for m in page.get("data", []):
                ts = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
                if ts >= since:
                    posts.append({**m, "ts": ts})
                else:
                    older = True
            nxt = page.get("paging", {}).get("next")
            if not nxt or older:
                break
            page = get_json(nxt)
    except RuntimeError as e:
        return {**out, "note": f"Instagram stats failed: {e}"}
    insights_ok = True
    for m in posts:
        try:
            ins = call(f"{m['id']}/insights", metric=",".join(cfg["media_metrics"]))
            m.update({d["name"]: (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value"))
                      for d in ins.get("data", [])})
        except RuntimeError as e:
            if insights_ok:
                out["note"] = (f"views/reach not available ({e}). Add instagram_manage_insights to the Meta "
                               "configuration's permissions and generate a new token.")
            insights_ok = False
            break
    out["posts"] = posts
    return out


# ---------- YouTube ----------

def youtube_stats(cfg: dict, since: datetime, extra: list[str] | None = None) -> dict:
    """`extra`: video ids to include even if the channel's uploads list doesn't show them yet (it lags after an upload)."""
    ids = {k: os.environ.get(cfg[f"{k}_env"], "") for k in ("client_id", "client_secret", "refresh_token")}
    if not all(ids.values()):
        return {"note": "YT_* secrets not set"}
    try:
        token = get_json("https://oauth2.googleapis.com/token",
                         urllib.parse.urlencode({**ids, "grant_type": "refresh_token"}).encode())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        api = "https://www.googleapis.com/youtube/v3"
        ch = get_json(f"{api}/channels?part=snippet,statistics,contentDetails&mine=true", headers=auth)["items"][0]
        out = {"channel": ch["snippet"]["title"], **{k: ch["statistics"].get(k) for k in ("subscriberCount", "viewCount", "videoCount")}}
        uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        items = get_json(f"{api}/playlistItems?part=contentDetails&maxResults=50&playlistId={uploads}", headers=auth).get("items", [])
        recent = [i["contentDetails"]["videoId"] for i in items
                  if datetime.fromisoformat(i["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00")) >= since]
        recent += [v for v in (extra or []) if v and v not in recent]
        videos = []
        if recent:
            got = get_json(f"{api}/videos?part=snippet,statistics&id={','.join(recent)}", headers=auth).get("items", [])
            videos = [{"id": v["id"], "title": v["snippet"]["title"], "published": v["snippet"]["publishedAt"][:10],
                       **{k: v["statistics"].get(k) for k in ("viewCount", "likeCount", "commentCount")}} for v in got]
        out["videos"] = videos
        return out
    except RuntimeError as e:
        hint = (" Run tools/youtube-publisher/authorize.py again (it now asks for youtube.readonly too) after adding "
                "that scope under Google Auth Platform -> Data Access." if "scope" in str(e).lower() or "403" in str(e) else "")
        return {"note": f"YouTube stats failed: {e}.{hint}"}
    except (KeyError, IndexError) as e:
        return {"note": f"YouTube stats: unexpected answer ({e})"}


# ---------- the email ----------

def num(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "-"


def build(folder: Path, day: str, run_url: str, ig: dict, yt: dict, days: int) -> tuple[str, str, str]:
    posted = json.loads((folder / "posted.json").read_text(encoding="utf-8")) if (folder / "posted.json").is_file() else {}
    script = json.loads((folder / "script.json").read_text(encoding="utf-8")) if (folder / "script.json").is_file() else {}
    out = {k: os.environ.get(f"OUT_{k.upper()}", "") for k in ("make", "instagram", "youtube")}
    title = script.get("title") or day
    if not posted.get("youtube_url"):
        # put up by an earlier run whose record wasn't kept (2026-11-02: a Post reel run with YouTube unticked showed
        # "skipped" for a Short that was live): the channel's recent uploads, by title
        same = [v for v in yt.get("videos") or [] if v["title"].replace("#Shorts", "").strip() == title.strip()]
        if same:
            posted["youtube_url"] = f"https://youtube.com/shorts/{same[0]['id']}"

    def state(done: bool, step: str) -> str:
        """posted.json says what went up; the step's outcome says why not (a successful step that posted nothing
        means that part was switched off)."""
        return "success" if done else ("skipped" if step in ("success", "") else step)

    rows = [
        ("Reel ready", out["make"], title),
        ("Instagram reel", state(bool(posted.get("permalink")), out["instagram"]), posted.get("permalink", "")),
        ("Instagram story", state(bool(posted.get("story_id")), out["instagram"]), posted.get("story_id", "")),
        ("YouTube Short", state(bool(posted.get("youtube_url")), out["youtube"]), posted.get("youtube_url", "")),
    ]
    ok = rows[0][1] == "success" and all(r[1] in ("success", "skipped") for r in rows)  # skipped = switched off
    subject = f"{'✅' if ok else '⚠️'} Job Shorts {day}: {title}"

    def link(v: str) -> str:
        return f'<a href="{html.escape(v)}">{html.escape(v)}</a>' if v.startswith("http") else html.escape(v)

    h = [f"<h2>{html.escape(subject)}</h2>", '<table cellpadding="6" style="border-collapse:collapse">']
    t = [subject, ""]
    for name, state, detail in rows:
        h.append(f"<tr><td>{MARK.get(state, '❔')}</td><td><b>{name}</b></td><td>{state or 'not run'}</td><td>{link(detail)}</td></tr>")
        t.append(f"{MARK.get(state, '?')} {name}: {state or 'not run'} {detail}")
    h.append("</table>")
    if run_url:
        h.append(f'<p>Run: <a href="{run_url}">{run_url}</a></p>')
        t.append(f"Run: {run_url}")

    h.append(f"<h3>Instagram, last {days} days</h3>")
    t += ["", f"Instagram, last {days} days"]
    if ig.get("username"):
        line = f"@{ig['username']}: {num(ig.get('followers_count'))} followers, {num(ig.get('media_count'))} posts in total"
        h.append(f"<p>{html.escape(line)}</p>")
        t.append(line)
    posts = ig.get("posts") or []
    if posts:
        h.append('<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th>Type</th>'
                 "<th>Views</th><th>Reach</th><th>Likes</th><th>Comments</th><th>Saves</th><th>Shares</th><th align=\"left\">Post</th></tr>")
        for m in posts:
            cap = (m.get("caption") or "").split("\n")[0][:50]
            cells = [m["ts"].strftime("%d %b"), m.get("media_product_type", ""), num(m.get("views")), num(m.get("reach")),
                     num(m.get("like_count")), num(m.get("comments_count")), num(m.get("saved")), num(m.get("shares"))]
            h.append("<tr>" + "".join(f"<td align=\"center\">{html.escape(c)}</td>" for c in cells)
                     + f'<td><a href="{html.escape(m.get("permalink", ""))}">{html.escape(cap)}</a></td></tr>')
            t.append(" | ".join(cells) + f" | {cap}")
        h.append("</table>")
        totals = {k: sum(int(m.get(k) or 0) for m in posts) for k in ("views", "like_count", "comments_count")}
        line = f"Totals: {num(totals['views'])} views, {num(totals['like_count'])} likes, {num(totals['comments_count'])} comments"
        h.append(f"<p>{line}</p>")
        t.append(line)
    if ig.get("note"):
        h.append(f"<p><i>{html.escape(ig['note'])}</i></p>")
        t.append(f"Note: {ig['note']}")

    h.append(f"<h3>YouTube, last {days} days</h3>")
    t += ["", f"YouTube, last {days} days"]
    if yt.get("channel"):
        line = (f"{yt['channel']}: {num(yt.get('subscriberCount'))} subscribers, {num(yt.get('viewCount'))} views in total, "
                f"{num(yt.get('videoCount'))} videos")
        h.append(f"<p>{html.escape(line)}</p>")
        t.append(line)
    if yt.get("videos"):
        h.append('<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th>Views</th>'
                 '<th>Likes</th><th>Comments</th><th align="left">Short</th></tr>')
        for v in sorted(yt["videos"], key=lambda v: v["published"], reverse=True):
            cells = [v["published"], num(v.get("viewCount")), num(v.get("likeCount")), num(v.get("commentCount"))]
            h.append("<tr>" + "".join(f"<td align=\"center\">{html.escape(c)}</td>" for c in cells)
                     + f'<td><a href="https://youtube.com/shorts/{v["id"]}">{html.escape(v["title"][:60])}</a></td></tr>')
            t.append(" | ".join(cells) + f" | {v['title'][:60]}")
        h.append("</table>")
    if yt.get("note"):
        h.append(f"<p><i>{html.escape(yt['note'])}</i></p>")
        t.append(f"Note: {yt['note']}")
    return subject, "\n".join(t) + "\n", "<html><body style=\"font-family:sans-serif\">" + "\n".join(h) + "</body></html>"


def posted_youtube_id(folder: Path) -> str:
    f = folder / "posted.json"
    return json.loads(f.read_text(encoding="utf-8")).get("youtube_id", "") if f.is_file() else ""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    rep = cfg["report"]
    load_env_files([(a.config.resolve().parent / f).resolve() for f in rep.get("env_files", [])])
    since = datetime.now(timezone.utc) - timedelta(days=rep["days"])
    day = os.environ.get("DAY") or a.folder.name
    subject, text, body = build(a.folder.resolve(), day, os.environ.get("RUN_URL", ""),
                                instagram_stats(cfg["instagram"], since), youtube_stats(cfg["youtube"], since, [posted_youtube_id(a.folder)]), rep["days"])
    if a.dry_run:
        print(f"Subject: {subject}\n\n{text}")
        return
    user, password = os.environ.get(rep["smtp_user_env"], ""), os.environ.get(rep["smtp_password_env"], "")
    if not user or not password:
        sys.exit(f"set {rep['smtp_user_env']} and {rep['smtp_password_env']} to send the report\n\n{text}")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, os.environ.get(rep["to_env"]) or user
    msg.set_content(text)
    msg.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL(rep["smtp_host"], rep["smtp_port"], timeout=60) as s:
        s.login(user, password)
        s.send_message(msg)
    print(f"report sent to {msg['To']}: {subject}")


if __name__ == "__main__":
    main()
