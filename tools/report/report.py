"""
report: emails how a Daily reel run went (reel made? Instagram reel, story, YouTube Short posted?) plus the last
[report] days of stats from Instagram and YouTube.

    python tools/report/report.py --folder content/<date> [--dry-run]

Step outcomes come from the workflow as environment variables (success / failure / skipped / cancelled / empty):
OUT_MAKE, OUT_INSTAGRAM, OUT_YOUTUBE, plus RUN_URL and DAY. The links come from the folder's posted.json.
Stats (each part is skipped with a note when its permission is missing, never failing the report):
  - Instagram: followers and posts (instagram_basic); per post likes and comments, and views/reach/saves/shares when
    the token has instagram_manage_insights
  - YouTube: subscribers, total views, and per Short views/likes/comments when the refresh token has youtube.readonly;
    with yt-analytics.readonly also the share of each Short watched and where the views came from
  - Growth: the change since the report ~24 h and ~7 days ago, from content/stats-history.json (--save-stats adds
    this report's numbers to it; the Daily reel run does, and commits the file)
  - when the Instagram token expires (subject turns ⚠️ under 14 days), and the Threads token (from its config)
  - Facebook Page (followers; per reel plays, reach, average watch, reactions, comments, shares), Threads (followers;
    per post views, likes, replies, reposts, shares), Telegram (subscribers only: the Bot API has no post views)
  - each platform's list shows its newest [report] rows_in_mail posts; longer lists come in full as an attached HTML file
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
            j = json.loads(raw)
            err = j.get("error")
            # Graph: {"error": {"message"}}; Google: {"error": "..."}; Telegram: {"ok": false, "description"}
            msg = err.get("message", raw[:200]) if isinstance(err, dict) else (str(err) if err else j.get("description", raw[:200]))
        except (ValueError, AttributeError):
            msg = raw[:200]
        raise RuntimeError(f"HTTP {e.code}: {msg}") from None


# Readable names for the config metrics; one not listed here still shows, under its API name.
LABELS = {
    "views": "views", "reach": "accounts reached", "profile_views": "profile visits", "profile_links_taps": "bio link taps",
    "accounts_engaged": "accounts engaged", "total_interactions": "interactions", "follows_and_unfollows": "follows",
    "website_clicks": "website clicks", "ig_reels_avg_watch_time": "avg watch", "ig_reels_video_view_total_time": "total watch",
    "page_follows": "followers", "page_daily_follows_unique": "new follows", "page_views_total": "Page views",
    "page_post_engagements": "post engagements", "page_video_views": "video views", "page_impressions_unique": "reach",
    "followers_count": "followers", "likes": "likes", "replies": "replies", "reposts": "reposts", "quotes": "quotes",
    "shares": "shares", "clicks": "link clicks", "estimatedMinutesWatched": "minutes watched",
    "subscribersGained": "subscribers gained", "subscribersLost": "subscribers lost", "averageViewDuration": "avg view",
    "averageViewPercentage": "avg % watched", "engagedViews": "engaged views", "comments": "comments",
    "post_video_view_time": "total watch", "fb_reels_total_plays": "total plays", "fb_reels_replay_count": "replays",
}
FB_REEL_KNOWN = {"blue_reels_play_count", "post_impressions_unique", "post_video_avg_time_watched",
                 "post_video_likes_by_reaction_type", "post_video_social_actions"}


def metric_text(name: str, value) -> str:
    """A metric for the mail: milliseconds of watch time as seconds or minutes, YouTube's seconds and percentages."""
    if value is None:
        return "-"
    if isinstance(value, dict):  # e.g. a breakdown Meta returns as {type: count}
        return " / ".join(f"{k.lower()} {num(v)}" for k, v in value.items())
    if name in ("ig_reels_avg_watch_time", "post_video_avg_time_watched"):
        return f"{value / 1000:.1f} s"
    if name in ("ig_reels_video_view_total_time", "post_video_view_time"):
        return f"{value / 60000:.0f} min"
    if name == "averageViewDuration":
        return f"{value:.0f} s"
    if name == "averageViewPercentage":
        return f"{value:.0f}%"
    return num(value)


def account_line(platform: str, days: int, values: dict, split: dict | None = None) -> str:
    """'Instagram, last 7 days: 17,630 views (91% non-followers) · 120 profile visits · ...'"""
    parts = []
    for k, v in values.items():
        if v is None:
            continue
        part = f"{metric_text(k, v)} {LABELS.get(k, k)}"
        s = (split or {}).get(k) or {}
        total = sum(x for x in s.values() if isinstance(x, (int, float)))
        if total and s.get("non_follower") is not None:
            part += f" ({100 * s['non_follower'] / total:.0f}% from non-followers)"
        parts.append(part)
    return f"{platform}, last {days} days: " + " · ".join(parts) if parts else ""


def tolerant(fetch, metrics: list[str]) -> tuple[dict, dict]:
    """fetch(list of metric names) -> {name: value}. One call for all; when the API refuses that (one unknown or not-yet-
    available metric fails the whole call), one call per metric. Returns (values, {refused metric: reason}), so a metric
    added to a config list either shows up or is named in the mail, never breaks the report (user, 2026-11-12: new
    metrics should need a config line, not code or permission changes)."""
    if not metrics:
        return {}, {}
    try:
        return fetch(metrics), {}
    except RuntimeError:
        got, refused = {}, {}
        for m in metrics:
            try:
                got.update(fetch([m]))
            except RuntimeError as e:
                refused[m] = str(e)[:160]
        return got, refused


def window(days: int) -> tuple[int, int]:
    """(since, until) unix seconds for the last `days` days, for the insights endpoints that take a time range."""
    until = datetime.now(timezone.utc)
    return int((until - timedelta(days=days)).timestamp()), int(until.timestamp())


def refused_note(platform: str, refused: dict) -> str:
    if not refused:
        return ""
    first = next(iter(refused.values()))
    return (f"{platform} did not give: {', '.join(sorted(refused))} ({first}). Remove them from tools/report/config.toml "
            "if they stay refused.")


# ---------- Instagram ----------

def ig_account(cfg: dict, call, user: str) -> dict:
    """Account totals over [report] account_days: views, reach, profile visits, bio-link taps, accounts engaged...
    ([instagram] account_metrics), plus the follower / non-follower split of the metrics in follow_type_split. These
    say where follows are lost: many views but few profile visits = the reel gives no reason to look; many visits but
    few follows = the profile doesn't convince."""
    since, until = window(cfg.get("account_days", 7))

    def fetch(ms: list[str]) -> dict:
        d = call(f"{user}/insights", metric=",".join(ms), period="day", metric_type="total_value", since=since, until=until)
        return {x["name"]: (x.get("total_value") or {}).get("value") for x in d.get("data", [])}

    vals, refused = tolerant(fetch, cfg.get("account_metrics", []))
    split = {}
    for m in cfg.get("follow_type_split", []):
        try:
            d = call(f"{user}/insights", metric=m, period="day", metric_type="total_value", breakdown="follow_type",
                     since=since, until=until)
            for b in ((d.get("data") or [{}])[0].get("total_value") or {}).get("breakdowns", []):
                for r in b.get("results", []):
                    split.setdefault(m, {})[r["dimension_values"][0].lower()] = r.get("value")
        except RuntimeError as e:
            refused[f"{m} by follow_type"] = str(e)[:160]
    return {"values": vals, "split": split, "refused": refused, "days": cfg.get("account_days", 7)}


def instagram_stats(cfg: dict, since: datetime) -> dict:
    token, user = os.environ.get(cfg["token_env"], ""), os.environ.get(cfg["user_env"], "")
    if not token or not user:
        return {"note": "IG_ACCESS_TOKEN / IG_USER_ID not set"}
    base = f"https://graph.facebook.com/{cfg['api_version']}"

    def call(path: str, **params) -> dict:
        return get_json(f"{base}/{path}?" + urllib.parse.urlencode({**params, "access_token": token}))

    out: dict = {"token": token_info(base, token)}
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
        if not out.get("username"):  # even the account call failed: most likely the token itself
            out["token"] = {"line": f"Instagram token may have stopped working ({e}): make a new one and update IG_ACCESS_TOKEN", "warn": True}
        return {**out, "note": f"Instagram stats failed: {e}"}
    # per post: the metrics of [instagram] media_metrics; one refused for the first post is not asked again
    metrics, refused = list(cfg["media_metrics"]), {}
    for m in posts:
        def fetch(ms: list[str], mid: str = m["id"]) -> dict:
            ins = call(f"{mid}/insights", metric=",".join(ms))
            return {d["name"]: (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value"))
                    for d in ins.get("data", [])}
        got, bad = tolerant(fetch, metrics)
        m.update(got)
        if bad:
            refused.update(bad)
            metrics = [x for x in metrics if x not in bad]
        if not metrics:
            break
    if refused:
        out["note"] = refused_note("Instagram (per post)", refused)
    out["posts"] = posts
    out["account"] = ig_account(cfg, call, user)
    return out


def media_numbers(cfg: dict, mid: str) -> dict:
    """One post's numbers: likes, comments, the insights in media_metrics, and the average watch time (s) of a reel."""
    token = os.environ.get(cfg["token_env"], "")
    base = f"https://graph.facebook.com/{cfg['api_version']}"
    q = lambda path, **p: get_json(f"{base}/{path}?" + urllib.parse.urlencode({**p, "access_token": token}))  # noqa: E731
    out = {k: v for k, v in q(mid, fields="like_count,comments_count,permalink").items() if k != "id"}

    def fetch(ms: list[str]) -> dict:
        return {d["name"]: (d.get("values") or [{}])[0].get("value", d.get("total_value", {}).get("value"))
                for d in q(f"{mid}/insights", metric=",".join(ms)).get("data", [])}
    metrics = list(dict.fromkeys(list(cfg["media_metrics"]) + ["ig_reels_avg_watch_time"]))
    out.update(tolerant(fetch, metrics)[0])  # a metric this post (or this API version) doesn't have is just left out
    return out


def trial_pairs(cfg: dict, history: list[dict], current: dict | None, days: int = 10) -> list[dict]:
    """Main reel vs its trial reel (variants/trial, shown only to non-followers), newest first: which opening or
    framing works better with people who don't know the account (user, 2026-11-08)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen, pairs = set(), []
    for p in ([current] if current else []) + [s["pair"] for s in reversed(history)
                                                if s.get("pair") and datetime.fromisoformat(s["at"]) >= cutoff]:
        if not p or not p.get("main") or not p.get("trial") or p["trial"] in seen:
            continue
        seen.add(p["trial"])
        try:
            pairs.append({**p, "main_n": media_numbers(cfg, p["main"]), "trial_n": media_numbers(cfg, p["trial"])})
        except RuntimeError as e:
            pairs.append({**p, "error": str(e)})
        if len(pairs) >= 7:
            break
    return pairs


def token_info(base: str, token: str) -> dict:
    """When the Instagram token stops working (debug_token). expires_at 0 = never; the data-access window is separate (a
    user token loses data access ~90 days after the user last used the app). debug_token needs an app token
    (META_APP_ID|IG_APP_SECRET): 2026-11-05 the system-user token asking about itself got "(#100) You must provide an app
    access token". Without those two secrets the report only says whether the token still works."""
    app_id, secret = os.environ.get("META_APP_ID", "").strip(), os.environ.get("IG_APP_SECRET", "").strip()
    if not (app_id and secret):
        return {"line": "Instagram token works (the stats below come through it). Its expiry date needs an app token: add "
                        "the repository secrets META_APP_ID and IG_APP_SECRET (Meta app -> App settings -> Basic) to see it here.",
                "warn": False}
    try:
        d = get_json(f"{base}/debug_token?" + urllib.parse.urlencode({"input_token": token, "access_token": f"{app_id}|{secret}"})).get("data", {})
    except RuntimeError as e:
        return {"line": f"Instagram token check failed: {e}", "warn": False}
    now = datetime.now(timezone.utc)
    ends = [(what, datetime.fromtimestamp(d[k], timezone.utc)) for what, k in (("expires", "expires_at"), ("data access ends", "data_access_expires_at")) if d.get(k)]
    if not d.get("is_valid", True):
        return {"line": "Instagram token is NOT valid: make a new one (Meta app -> API setup -> Generate token) and update IG_ACCESS_TOKEN", "warn": True}
    if not ends:
        return {"line": f"Instagram token: never expires ({d.get('type', 'token')})", "warn": False}
    soonest = min(ends, key=lambda e: e[1])
    left = (soonest[1] - now).days
    line = "Instagram token: " + ", ".join(f"{what} {when:%d %b %Y}" for what, when in ends) + f" ({left} days left)"
    return {"line": line + (": make a new one and update IG_ACCESS_TOKEN" if left < 14 else ""), "warn": left < 14}


# ---------- Facebook Page, Threads, Telegram (2026-11-09) ----------

def _list_since(first: dict, since: datetime, ts_key: str) -> list[dict]:
    """Walk a newest-first Graph-style list (data + paging.next) until it reaches past `since`."""
    out, page = [], first
    while True:
        older = False
        for m in page.get("data", []):
            ts = datetime.fromisoformat(m[ts_key].replace("+0000", "+00:00"))
            if ts >= since:
                out.append({**m, "ts": ts})
            else:
                older = True
        nxt = page.get("paging", {}).get("next")
        if not nxt or older:
            return out
        page = get_json(nxt)


def _first_value(d: dict):
    return (d.get("values") or [{}])[0].get("value", (d.get("total_value") or {}).get("value"))


def facebook_stats(cfg: dict, since: datetime) -> dict:
    """The Page's followers and, per reel of the window, plays / reach / average watch / reactions / comments / shares
    (video_insights; each metric that the token or the API version refuses is left out, with a note)."""
    token, page = os.environ.get(cfg["token_env"], ""), os.environ.get(cfg["page_env"], "")
    if not token or not page:
        return {}
    base = f"https://graph.facebook.com/{cfg['api_version']}"

    def q(path: str, tok: str, **params) -> dict:
        return get_json(f"{base}/{path}?" + urllib.parse.urlencode({**params, "access_token": tok}))

    try:
        try:
            pg = q(page, token, fields="id,name,followers_count,fan_count,access_token")
        except RuntimeError:  # a Page token may not be allowed to read its own access_token field
            pg = q(page, token, fields="id,name,followers_count,fan_count")
        tok = pg.get("access_token") or token
        out = {"name": pg.get("name"), "followers": pg.get("followers_count", pg.get("fan_count"))}
        reels = _list_since(q(f"{pg.get('id', page)}/video_reels", tok, fields="id,description,created_time,permalink_url",
                              limit=50), since, "created_time")
    except RuntimeError as e:
        return {"note": f"Facebook stats failed: {e}"}
    metrics, refused = list(cfg["reel_metrics"]), {}
    for r in reels:
        def fetch(ms: list[str], vid: str = r["id"]) -> dict:
            return {d["name"]: _first_value(d) for d in q(f"{vid}/video_insights", tok, metric=",".join(ms)).get("data", [])}
        vals, bad = tolerant(fetch, metrics)
        if bad:  # Meta's own words go in the note (no token in them); not asked again for the next reels
            refused.update(bad)
            metrics = [x for x in metrics if x not in bad]
        r["extra"] = {k: v for k, v in vals.items() if k not in FB_REEL_KNOWN}
        social = vals.get("post_video_social_actions") if isinstance(vals.get("post_video_social_actions"), dict) else {}
        reactions = vals.get("post_video_likes_by_reaction_type")
        r.update({"plays": vals.get("blue_reels_play_count"), "reach": vals.get("post_impressions_unique"),
                  "avg_ms": vals.get("post_video_avg_time_watched"),
                  "likes": sum(reactions.values()) if isinstance(reactions, dict) else None,
                  "comments": social.get("COMMENT"), "shares": social.get("SHARE")})
        if (r.get("permalink_url") or "").startswith("/"):
            r["permalink_url"] = "https://www.facebook.com" + r["permalink_url"]
    out["posts"] = reels
    # Page totals over [report] account_days: [facebook] page_metrics = {metric = "sum" | "last"} (daily values are
    # added up, or the newest one is taken for running totals such as followers)
    since_s, until_s = window(cfg.get("account_days", 7))
    page_metrics = cfg.get("page_metrics", {})

    def fetch_page(ms: list[str]) -> dict:
        res = {}
        for d in q(f"{pg.get('id', page)}/insights", tok, metric=",".join(ms), period="day", since=since_s, until=until_s).get("data", []):
            vals = [v.get("value") for v in d.get("values") or [] if isinstance(v.get("value"), (int, float))]
            if vals:
                res[d["name"]] = vals[-1] if page_metrics.get(d["name"]) == "last" else sum(vals)
        return res
    out["account"], bad = tolerant(fetch_page, list(page_metrics))
    refused.update({f"page {k}": v for k, v in bad.items()})
    out["days"] = cfg.get("account_days", 7)
    if refused:
        out["note"] = refused_note("Facebook", refused)
        print(f"Facebook insights: {out['note']}")  # in the run log too
    return out


def threads_stats(cfg: dict, since: datetime) -> dict:
    """Followers (threads_insights) and, per post of the window, views / likes / replies / reposts / quotes / shares
    (threads_manage_insights)."""
    token, user = os.environ.get(cfg["token_env"], ""), os.environ.get(cfg["user_env"], "")
    if not token or not user:
        return {}
    base = "https://graph.threads.net/v1.0"

    def q(path: str, **params) -> dict:
        return get_json(f"{base}/{path}?" + urllib.parse.urlencode({**params, "access_token": token}))

    try:
        out = {"username": q("me", fields="username").get("username")}
        posts = _list_since(q(f"{user}/threads", fields="id,text,timestamp,permalink,media_type", limit=50), since, "timestamp")
    except RuntimeError as e:
        return {"note": f"Threads stats failed: {e}"}
    # account totals over [report] account_days ([threads] account_metrics; followers_count is a running total, the
    # others come per day and are added up)
    since_s, until_s = window(cfg.get("account_days", 7))

    def fetch_account(ms: list[str]) -> dict:
        res = {}
        for d in q(f"{user}/threads_insights", metric=",".join(ms), since=since_s, until=until_s).get("data", []):
            if d.get("total_value"):
                res[d["name"]] = d["total_value"].get("value")
            else:
                vals = [v.get("value") for v in d.get("values") or [] if isinstance(v.get("value"), (int, float))]
                res[d["name"]] = sum(vals) if vals else None
        return res
    out["account"], refused = tolerant(fetch_account, cfg.get("account_metrics", ["followers_count"]))
    out["followers"] = out["account"].get("followers_count")
    out["days"] = cfg.get("account_days", 7)
    metrics = list(cfg["post_metrics"])
    for m in posts:
        def fetch(ms: list[str], mid: str = m["id"]) -> dict:
            return {d["name"]: _first_value(d) for d in q(f"{mid}/insights", metric=",".join(ms)).get("data", [])}
        got, bad = tolerant(fetch, metrics)
        m.update(got)
        if bad:
            refused.update(bad)
            metrics = [x for x in metrics if x not in bad]
    if refused:
        out["note"] = refused_note("Threads", refused)
    out["posts"] = posts
    return out


def telegram_stats(cfg: dict) -> dict:
    """Subscribers only: the Bot API has no view counts for channel posts."""
    token, chan = os.environ.get(cfg["token_env"], ""), os.environ.get(cfg["channel_env"], "")
    if not token or not chan:
        return {}
    chan = chan if chan.startswith(("@", "-")) else "@" + chan
    try:  # the bot token is part of the URL: get_json's errors carry the response text only, never the URL
        n = get_json(f"https://api.telegram.org/bot{token}/getChatMemberCount?" + urllib.parse.urlencode({"chat_id": chan}))
    except RuntimeError as e:
        return {"note": f"Telegram stats failed: {e}"}
    return {"channel": chan, "subscribers": n.get("result")}


def threads_token_line() -> dict:
    """The Threads token lasts 60 days; get_token.py writes its expiry into the threads-publisher config."""
    path = Path(__file__).resolve().parent.parent / "threads-publisher" / "config.toml"
    try:
        exp = tomllib.loads(path.read_text(encoding="utf-8"))["threads"]["token_expires"]
        left = (datetime.fromisoformat(exp).date() - datetime.now(timezone.utc).date()).days
    except (OSError, KeyError, ValueError):
        return {}
    line = f"Threads token: expires {exp} ({left} days)"
    if left < 14:
        line += (": generate a new one (Threads app -> Use cases -> Customize -> Settings -> Generate access token), "
                 "run tools/threads-publisher/get_token.py --long, push config.toml")
    return {"line": line, "warn": left < 14}


# ---------- AI models: which ones made this reel, their specs, and what changed since the last run ----------

ROLES = [("script", "Script writer"), ("judge", "Script judge"), ("voice", "Voice (TTS)"), ("captions", "Caption timing")]


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        return {}


def models_used(folder: Path) -> dict:
    """role -> {model, spec, tried} from script.json (ai), voice.json and timeline.json."""
    script, voice, timeline = (read_json(folder / f) for f in ("script.json", "voice.json", "timeline.json"))
    out: dict = {}
    if script.get("ai"):
        out.update({k: v for k, v in script["ai"].items() if k in ("script", "judge")})
    elif script:
        out["script"] = {"model": "hand-written series episode" if script.get("track") == "series" else "hand-written script",
                         "spec": {"provider": "no AI"}}
    if voice.get("model"):
        out["voice"] = {"model": voice["model"], "spec": voice.get("spec", {}), "tried": voice.get("tried", [])}
    if timeline.get("whisper_model"):
        out["captions"] = {"model": f"faster-whisper {timeline['whisper_model']}", "spec": {"provider": "runs on the GitHub runner, free"}}
    return out


def spec_text(spec: dict) -> str:
    parts = [spec.get("provider", ""), spec.get("name", ""), f"version {spec['version']}" if spec.get("version") else "",
             f"owner {spec['owner']}" if spec.get("owner") else ""]
    for key, label in (("input_tokens", "input"), ("context_tokens", "context"), ("output_tokens", "output")):
        if spec.get(key):
            parts.append(f"{label} {num(spec[key])} tokens")
    return " · ".join(p for p in parts if p)


def change_reason(role: str, cur: dict, prev: dict) -> str:
    """Why this role's model differs from the last run's, from the 'tried' lists of both runs."""
    m, p = cur["model"], prev["model"]
    if "hand-written" in m and "hand-written" not in p:
        return "today's script came from the series file, so no AI wrote it"
    if "hand-written" in p and "hand-written" not in m:
        return "no series episode for today, so the AI pipeline wrote it"
    for t in cur.get("tried") or []:
        if t["model"] == p and not t["outcome"].startswith("used"):
            return t["outcome"]
    for t in prev.get("tried") or []:
        if t["model"] == m and not t["outcome"].startswith("used"):
            return f"{m} is higher in the list and answered this time (last run: {t['outcome']})"
    if role == "judge":
        return "the judge prefers a different provider than the writer, and the writer changed"
    return f"{p} was not tried this run (the model list changed)"


# ---------- growth: snapshots of every report, compared with ~24 h and ~7 days ago ----------

def snapshot(ig: dict, yt: dict, models: dict | None = None, pair: dict | None = None,
             others: dict | None = None) -> dict:
    s: dict = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    o = others or {}
    if o.get("fb", {}).get("posts") is not None:
        s["fb"] = {"followers": o["fb"].get("followers"), "posts": {
            r["id"]: {"views": r.get("plays"), "likes": r.get("likes")} for r in o["fb"]["posts"]}}
    if o.get("th", {}).get("posts") is not None:
        s["th"] = {"followers": o["th"].get("followers"), "posts": {
            m["id"]: {"views": m.get("views"), "likes": m.get("likes")} for m in o["th"]["posts"]}}
    if o.get("tg", {}).get("subscribers") is not None:
        s["tg"] = {"subscribers": o["tg"]["subscribers"]}
    if pair:
        s["pair"] = pair
    if models:
        s["models"] = {r: {"model": v["model"], "tried": v.get("tried", [])} for r, v in models.items()}
    if ig.get("username"):
        s["ig"] = {"followers": ig.get("followers_count"), "posts": {
            m["id"]: {"views": m.get("views"), "likes": m.get("like_count"), "comments": m.get("comments_count"),
                      "saves": m.get("saved"), "shares": m.get("shares")} for m in ig.get("posts") or []}}
    if yt.get("channel"):
        s["yt"] = {"subscribers": yt.get("subscriberCount"), "views": yt.get("viewCount"), "videos": {
            v["id"]: {"views": v.get("viewCount"), "likes": v.get("likeCount"), "comments": v.get("commentCount")} for v in yt.get("videos") or []}}
    return s


def baseline(history: list[dict], hours: float, lo: float, hi: float) -> dict | None:
    """The snapshot whose age is nearest to `hours`, among those lo..hi hours old (GitHub starts the cron at varying
    times, so 'yesterday's report' is 20-30 h old, not exactly 24)."""
    now = datetime.now(timezone.utc)
    aged = [((now - datetime.fromisoformat(s["at"])).total_seconds() / 3600, s) for s in history]
    fit = [(abs(age - hours), s) for age, s in aged if lo <= age <= hi]
    return min(fit, key=lambda f: f[0])[1] if fit else None


def gained(cur: dict, old: dict, key: str) -> int | None:
    """Sum over the current posts of (now - then); a post the old snapshot doesn't have counts from 0."""
    total, seen = 0, False
    for pid, m in cur.items():
        if m.get(key) is None:
            continue
        before = old.get(pid, {}).get(key, 0) if pid in old else 0
        if before is None:
            continue
        total += int(m[key]) - int(before)
        seen = True
    return total if seen else None


def change(now, then) -> int | None:
    try:
        return int(now) - int(then)
    except (TypeError, ValueError):
        return None


def signed(v: int | None) -> str:
    return "-" if v is None else (f"+{v:,}" if v >= 0 else f"{v:,}")


def growth_rows(cur: dict, bases: list[dict | None]) -> list[tuple[str, str, list[str]]]:
    """(metric, now, [change per baseline]) for the growth table."""
    rows = []
    ig, yt = cur.get("ig"), cur.get("yt")
    if ig:
        posts = ig["posts"]
        rows.append(("Instagram followers", num(ig.get("followers")), [signed(change(ig.get("followers"), (b or {}).get("ig", {}).get("followers"))) if b and b.get("ig") else "-" for b in bases]))
        for key, label in (("views", "views"), ("likes", "likes"), ("comments", "comments"), ("shares", "shares"), ("saves", "saves")):
            now = sum(int(m[key]) for m in posts.values() if m.get(key) is not None)
            rows.append((f"Instagram {label} (last 60 days' posts)", num(now),
                         [signed(gained(posts, b["ig"]["posts"], key)) if b and b.get("ig") else "-" for b in bases]))
    if yt:
        for key, label in (("subscribers", "subscribers"), ("views", "views (whole channel)")):
            rows.append((f"YouTube {label}", num(yt.get(key)), [signed(change(yt.get(key), b["yt"].get(key))) if b and b.get("yt") else "-" for b in bases]))
        vids = yt["videos"]
        for key in ("likes", "comments"):
            rows.append((f"YouTube {key} (last 60 days' Shorts)", num(sum(int(v[key]) for v in vids.values() if v.get(key) is not None)),
                         [signed(gained(vids, b["yt"]["videos"], key)) if b and b.get("yt") else "-" for b in bases]))
    for k, name, follow in (("fb", "Facebook", "followers"), ("th", "Threads", "followers"), ("tg", "Telegram", "subscribers")):
        c = cur.get(k)
        if not c:
            continue
        rows.append((f"{name} {follow}", num(c.get(follow)),
                     [signed(change(c.get(follow), b[k].get(follow))) if b and b.get(k) else "-" for b in bases]))
        if "posts" in c:
            for key in ("views", "likes"):
                label = "plays" if k == "fb" and key == "views" else key
                rows.append((f"{name} {label} (last 60 days' posts)",
                             num(sum(int(m[key]) for m in c["posts"].values() if m.get(key) is not None)),
                             [signed(gained(c["posts"], b[k]["posts"], key)) if b and b.get(k) else "-" for b in bases]))
    return rows


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
        # a Short scheduled for later (private until publishAt) has no videoPublishedAt yet: it counts as recent
        recent = [i["contentDetails"]["videoId"] for i in items if not i["contentDetails"].get("videoPublishedAt")
                  or datetime.fromisoformat(i["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00")) >= since]
        recent += [v for v in (extra or []) if v and v not in recent]
        videos = []
        if recent:
            got = get_json(f"{api}/videos?part=snippet,statistics,status&id={','.join(recent)}", headers=auth).get("items", [])
            videos = [{"id": v["id"], "title": v["snippet"]["title"], "published": v["snippet"]["publishedAt"][:10],
                       "public_at": v.get("status", {}).get("publishAt", "") if v.get("status", {}).get("privacyStatus") == "private" else "",
                       **{k: v["statistics"].get(k) for k in ("viewCount", "likeCount", "commentCount")}} for v in got]
        out["videos"] = videos
        out.update(youtube_analytics(auth, since, videos, cfg))
        return out
    except RuntimeError as e:
        hint = (" Run tools/youtube-publisher/authorize.py again (it now asks for youtube.readonly too) after adding "
                "that scope under Google Auth Platform -> Data Access." if "scope" in str(e).lower() or "403" in str(e) else "")
        return {"note": f"YouTube stats failed: {e}.{hint}"}
    except (KeyError, IndexError) as e:
        return {"note": f"YouTube stats: unexpected answer ({e})"}


SOURCES = {"SHORTS": "Shorts feed", "YT_SEARCH": "YouTube search", "SUBSCRIBER": "subscribers", "YT_CHANNEL": "channel page",
           "RELATED_VIDEO": "suggested", "YT_OTHER_PAGE": "other YouTube pages", "EXT_URL": "outside links",
           "NO_LINK_OTHER": "direct / unknown", "NOTIFICATION": "notifications", "PLAYLIST": "playlists",
           "HASHTAGS": "hashtag pages", "SOUND_PAGE": "sound pages", "END_SCREEN": "end screens"}


def youtube_analytics(auth: dict, since: datetime, videos: list[dict], cfg: dict | None = None) -> dict:
    """YouTube Analytics (yt-analytics.readonly): per Short the average share watched and seconds watched, and where the
    channel's views came from. Answers the question the counts can't: are people swiping away, and is the Shorts feed
    showing the Shorts at all (2026-11-07: YouTube got ~6% of Instagram's views for the same reels). The data lags 1-2
    days. Without the scope it adds only a note."""
    base = "https://youtubeanalytics.googleapis.com/v2/reports?" + urllib.parse.urlencode({
        "ids": "channel==MINE", "startDate": f"{since:%Y-%m-%d}", "endDate": f"{datetime.now(timezone.utc):%Y-%m-%d}"})
    try:
        per = get_json(base + "&metrics=views,averageViewDuration,averageViewPercentage&dimensions=video&sort=-views&maxResults=50",
                       headers=auth)
        src = get_json(base + "&metrics=views&dimensions=insightTrafficSourceType&sort=-views", headers=auth)
    except RuntimeError as e:
        hint = (" Run tools/youtube-publisher/authorize.py again: it now asks for YouTube Analytics too (enable the "
                "YouTube Analytics API in the Google Cloud project and add the yt-analytics.readonly scope under Google "
                "Auth Platform -> Data Access first)." if "403" in str(e) or "scope" in str(e).lower() else "")
        return {"analytics_note": f"YouTube Analytics not available ({e}).{hint}"}
    rows = {r[0]: r for r in per.get("rows") or []}
    for v in videos:
        if v["id"] in rows:
            v["avg_seconds"], v["avg_percent"] = rows[v["id"]][2], rows[v["id"]][3]
    total = sum(r[1] for r in src.get("rows") or []) or 0
    sources = [(SOURCES.get(r[0], r[0].replace("_", " ").lower()), r[1], 100 * r[1] / total) for r in src.get("rows") or [] if total]
    out: dict = {"sources": sources}
    cfg = cfg or {}

    # more per Short ([youtube] video_metrics: subscribers gained, shares...), one call per metric when the set is refused
    extra, refused = {}, {}
    def per_video(ms: list[str]) -> dict:
        d = get_json(base + f"&metrics={','.join(ms)}&dimensions=video&maxResults=50", headers=auth)
        names = [h["name"] for h in d.get("columnHeaders", [])][1:]
        return {r[0]: dict(zip(names, r[1:])) for r in d.get("rows") or []}
    ms = list(cfg.get("video_metrics", []))
    try:
        extra = per_video(ms) if ms else {}
    except RuntimeError:
        for m in ms:
            try:
                for vid, vals in per_video([m]).items():
                    extra.setdefault(vid, {}).update(vals)
            except RuntimeError as e:
                refused[m] = str(e)[:160]
    for v in videos:
        v["extra"] = extra.get(v["id"], {})

    # channel totals over [report] account_days ([youtube] channel_metrics)
    days = cfg.get("account_days", 7)
    start = datetime.now(timezone.utc) - timedelta(days=days)
    chan = "https://youtubeanalytics.googleapis.com/v2/reports?" + urllib.parse.urlencode({
        "ids": "channel==MINE", "startDate": f"{start:%Y-%m-%d}", "endDate": f"{datetime.now(timezone.utc):%Y-%m-%d}"})
    def channel(ms2: list[str]) -> dict:
        d = get_json(chan + f"&metrics={','.join(ms2)}", headers=auth)
        names = [h["name"] for h in d.get("columnHeaders", [])]
        return dict(zip(names, (d.get("rows") or [[None] * len(names)])[0]))
    out["account"], bad = tolerant(channel, list(cfg.get("channel_metrics", [])))
    refused.update({f"channel {k}": v for k, v in bad.items()})
    out["days"] = days
    if refused:
        out["metrics_note"] = refused_note("YouTube Analytics", refused)
    return out


# ---------- the email ----------

def num(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "-"


WINDOWS = [("24 h", 24, 16, 40), ("7 days", 168, 120, 240)]  # label, target age, accepted ages (hours)


def build(folder: Path, day: str, run_url: str, ig: dict, yt: dict, days: int, history: list[dict],
          pairs: list[dict] | None = None, others: dict | None = None, limit: int = 10) -> tuple[str, str, str, str]:
    """(subject, text, html, attachment html). Each platform's list shows its newest `limit` posts in the mail; when a
    list is longer, the attachment (all-posts-<day>.html) has every list in full ("" when none is longer). Mail apps
    can't run a "show more" button (Gmail drops scripts and <details>), so the rest goes in the attachment."""
    others = others or {}
    full: list[str] = []      # the attachment's body
    longer = False

    def long_table(h: list[str], t: list[str], heading: str, header: str, rows: list[tuple[str, str]]) -> None:
        nonlocal longer
        h.append(header)
        h += [r[0] for r in rows[:limit]]
        h.append("</table>")
        t += [r[1] for r in rows[:limit]]
        if len(rows) > limit:
            longer = True
            more = f"+{len(rows) - limit} older: the full list is in the attached all-posts-{day}.html"
            h.append(f"<p><i>{html.escape(more)}</i></p>")
            t.append(more)
        full.extend([f"<h3>{html.escape(heading)}</h3>", header] + [r[0] for r in rows] + ["</table>"])

    posted = json.loads((folder / "posted.json").read_text(encoding="utf-8")) if (folder / "posted.json").is_file() else {}
    script = json.loads((folder / "script.json").read_text(encoding="utf-8")) if (folder / "script.json").is_file() else {}
    out = {k: os.environ.get(f"OUT_{k.upper()}", "")
           for k in ("make", "instagram", "youtube", "trial", "facebook", "threads", "telegram")}
    title = script.get("title") or day
    if not posted.get("youtube_url"):
        # put up by an earlier run whose record wasn't kept (2026-11-02: a Post reel run with YouTube unticked showed
        # "skipped" for a Short that was live): the channel's recent uploads, by title
        # (since 2026-11-07 the YouTube title is "<search line> | <episode title>", so: contains)
        same = [v for v in yt.get("videos") or [] if title.strip() and title.strip() in v["title"]]
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
        ("YouTube Short" + (" ('subscribe' version)" if (folder / "variants" / "youtube" / "reel.mp4").is_file() else ""),
         state(bool(posted.get("youtube_url")), out["youtube"]), posted.get("youtube_url", "")),
    ]
    if (folder / "variants" / "trial" / "reel.mp4").is_file() or posted.get("trial_media_id") or out["trial"]:
        not_yet = posted.get("trial_status") == "not eligible" and not posted.get("trial_media_id")
        rows.append(("Instagram trial reel", "skipped" if not_yet else state(bool(posted.get("trial_media_id")), out["trial"]),
                     "account not eligible for trial reels yet (Instagram subcode 2207081)" if not_yet
                     else posted.get("trial_permalink") or posted.get("trial_media_id", "")))
    # Facebook Page / Threads / Telegram (2026-11-09): a row once the workflow has the step or posted.json has the post
    for label, key, ids in (("Facebook reel", "facebook", ("facebook_url", "facebook_video_id")),
                            ("Facebook story", "facebook", ("facebook_story_id",)),
                            ("Threads post", "threads", ("threads_url", "threads_id")),
                            ("Telegram post", "telegram", ("telegram_url", "telegram_message_id"))):
        done = next((str(posted[i]) for i in ids if posted.get(i)), "")
        if out[key] or done:
            rows.append((label, state(bool(done), out[key]), done))
    ok = rows[0][1] == "success" and all(r[1] in ("success", "skipped") for r in rows)  # skipped = switched off
    token = ig.get("token") or {}
    threads_token = threads_token_line()
    subject = (f"{'✅' if ok and not token.get('warn') and not threads_token.get('warn') else '⚠️'} "
               f"Job Shorts {day}: {title}")

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
    for tk in (token, threads_token):
        if tk.get("line"):
            h.append(f"<p>{'⚠️ <b>' if tk.get('warn') else ''}{html.escape(tk['line'])}{'</b>' if tk.get('warn') else ''}</p>")
            t.append(tk["line"])

    # the AI models behind this reel, and what changed since the last run (red, with the reason)
    models = models_used(folder)
    if models:
        prev = next((s["models"] for s in reversed(history) if s.get("models")), {})
        h += ["<h3>AI models</h3>", '<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Step</th>'
              '<th align="left">Model</th><th align="left">Specs</th></tr>']
        t += ["", "AI models"]
        for role, label in ROLES:
            if role not in models:
                continue
            m = models[role]
            note = ""
            if role in prev and prev[role]["model"] != m["model"]:
                note = f"(changed from {prev[role]['model']}: {change_reason(role, m, prev[role])})"
            h.append(f"<tr><td>{label}</td><td><b>{html.escape(m['model'])}</b>"
                     + (f' <span style="color:#cf222e">{html.escape(note)}</span>' if note else "")
                     + f"</td><td>{html.escape(spec_text(m.get('spec', {})))}</td></tr>")
            t.append(f"{label}: {m['model']}" + (f" {note}" if note else "") + f" | {spec_text(m.get('spec', {}))}")
        h.append("</table>")

    # growth since the reports ~24 h and ~7 days ago (stats-history.json, one snapshot per Daily reel run)
    cur = snapshot(ig, yt, others=others)
    bases = [baseline(history, *w[1:]) for w in WINDOWS]
    growth = growth_rows(cur, bases)
    if growth:
        h.append("<h3>Growth</h3>")
        t += ["", "Growth"]
        h.append('<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left"></th><th>Now</th>'
                 + "".join(f"<th>Last {w[0]}</th>" for w in WINDOWS) + "</tr>")
        for name, now, ch in growth:
            colour = lambda c: "#1a7f37" if c.startswith("+") and c != "+0" else ("#cf222e" if c.startswith("-") and c != "-" else "inherit")  # noqa: E731
            h.append(f"<tr><td>{html.escape(name)}</td><td align=\"center\">{now}</td>"
                     + "".join(f'<td align="center" style="color:{colour(c)}">{c}</td>' for c in ch) + "</tr>")
            t.append(f"{name}: {now} | " + " | ".join(f"{w[0]}: {c}" for w, c in zip(WINDOWS, ch)))
        h.append("</table>")
        missing = [w[0] for w, b in zip(WINDOWS, bases) if not b]
        if missing:
            note = f"No report from about {' / '.join(missing)} ago yet, so those columns show '-' until there is one."
            h.append(f"<p><i>{note}</i></p>")
            t.append(note)
    day_ig = ((bases[0] or {}).get("ig") or {}).get("posts")  # per post: views gained since ~24 h ago
    day_yt = ((bases[0] or {}).get("yt") or {}).get("videos")

    def with_day(value, pid: str, old: dict | None, key: str) -> str:
        if old is None or value is None:
            return num(value)
        if pid not in old:
            return f"{num(value)} (new)"
        return f"{num(value)} ({signed(change(value, old[pid].get(key)))})"

    if pairs:
        h += ["<h3>Main reel vs trial reel</h3>",
              "<p><i>The trial reel goes only to people who don't follow the account. Compare the two openings: watch time "
              "and shares say which one holds strangers.</i></p>",
              '<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th align="left">Reel</th>'
              "<th></th><th>Views</th><th>Reach</th><th>Avg watch</th><th>Likes</th><th>Shares</th><th>Saves</th></tr>"]
        t += ["", "Main reel vs trial reel (views | reach | avg watch s | likes | shares | saves)"]
        for p in pairs:
            if p.get("error"):
                h.append(f"<tr><td>{html.escape(p.get('date', ''))}</td><td colspan=8>{html.escape(p['error'][:200])}</td></tr>")
                continue
            for label, n in (("main", p["main_n"]), ("trial", p["trial_n"])):
                watch = n.get("ig_reels_avg_watch_time")
                cells = [num(n.get("views")), num(n.get("reach")), f"{watch / 1000:.1f} s" if isinstance(watch, (int, float)) else "-",
                         num(n.get("like_count")), num(n.get("shares")), num(n.get("saved"))]
                first = label == "main"
                h.append("<tr>" + (f"<td rowspan=2>{html.escape(p.get('date', ''))}</td><td rowspan=2>{html.escape(p.get('title', '')[:50])}</td>" if first else "")
                         + f'<td><a href="{html.escape(n.get("permalink", ""))}">{label}</a></td>'
                         + "".join(f'<td align="center">{c}</td>' for c in cells) + "</tr>")
                t.append(f"{p.get('date', '')} {label}: " + " | ".join(cells) + (f" | {p.get('title', '')[:50]}" if first else ""))
        h.append("</table>")

    h.append(f"<h3>Instagram, last {days} days</h3>")
    t += ["", f"Instagram, last {days} days"]
    def para(line: str, italic: bool = False) -> None:
        if line:
            h.append(f"<p>{'<i>' if italic else ''}{html.escape(line)}{'</i>' if italic else ''}</p>")
            t.append(("Note: " if italic else "") + line)

    def extra_cols(items: list[dict], skip: set) -> list[str]:
        """Metrics beyond the fixed columns (added in config): each becomes a column, labelled from LABELS."""
        keys: list[str] = []
        for it in items:
            keys += [k for k in it if k not in skip and k not in keys]
        return keys

    if ig.get("username"):
        para(f"@{ig['username']}: {num(ig.get('followers_count'))} followers, {num(ig.get('media_count'))} posts in total")
    acc = ig.get("account") or {}
    para(account_line("Account", acc.get("days", 7), acc.get("values") or {}, acc.get("split")))
    posts = ig.get("posts") or []
    if posts:
        ig_extra = extra_cols(posts, {"id", "caption", "media_product_type", "timestamp", "permalink", "like_count",
                                      "comments_count", "ts", "views", "reach", "saved", "shares"})
        rows_ig = []
        for m in posts:
            cap = (m.get("caption") or "").split("\n")[0][:50]
            cells = [m["ts"].strftime("%d %b"), m.get("media_product_type", ""), with_day(m.get("views"), m["id"], day_ig, "views"),
                     num(m.get("reach")), with_day(m.get("like_count"), m["id"], day_ig, "likes"), num(m.get("comments_count")),
                     num(m.get("saved")), num(m.get("shares"))] + [metric_text(k, m.get(k)) for k in ig_extra]
            rows_ig.append(("<tr>" + "".join(f"<td align=\"center\">{html.escape(c)}</td>" for c in cells)
                            + f'<td><a href="{html.escape(m.get("permalink", ""))}">{html.escape(cap)}</a></td></tr>',
                            " | ".join(cells) + f" | {cap}"))
        long_table(h, t, f"Instagram, last {days} days",
                   '<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th>Type</th>'
                   "<th>Views</th><th>Reach</th><th>Likes</th><th>Comments</th><th>Saves</th><th>Shares</th>"
                   + "".join(f"<th>{html.escape(LABELS.get(k, k).capitalize())}</th>" for k in ig_extra)
                   + "<th align=\"left\">Post</th></tr>", rows_ig)
        totals = {k: sum(int(m.get(k) or 0) for m in posts) for k in ("views", "like_count", "comments_count")}
        line = f"Totals: {num(totals['views'])} views, {num(totals['like_count'])} likes, {num(totals['comments_count'])} comments"
        h.append(f"<p>{line}</p>")
        t.append(line)
    para(ig.get("note", ""), italic=True)
    para(refused_note("Instagram (account)", acc.get("refused") or {}), italic=True)

    h.append(f"<h3>YouTube, last {days} days</h3>")
    t += ["", f"YouTube, last {days} days"]
    if yt.get("channel"):
        line = (f"{yt['channel']}: {num(yt.get('subscriberCount'))} subscribers, {num(yt.get('viewCount'))} views in total, "
                f"{num(yt.get('videoCount'))} videos")
        h.append(f"<p>{html.escape(line)}</p>")
        t.append(line)
    para(account_line("Channel", yt.get("days", 7), yt.get("account") or {}))
    if yt.get("videos"):
        rows_yt = []
        yt_extra = extra_cols([v.get("extra") or {} for v in yt["videos"]], {"views", "averageViewDuration", "averageViewPercentage"})
        for v in sorted(yt["videos"], key=lambda v: v["published"], reverse=True):
            watched = f"{v['avg_percent']:.0f}% ({v['avg_seconds']:.0f} s)" if v.get("avg_percent") is not None else "-"
            cells = [v["published"], with_day(v.get("viewCount"), v["id"], day_yt, "views"), num(v.get("likeCount")),
                     num(v.get("commentCount")), watched] + [metric_text(k, (v.get("extra") or {}).get(k)) for k in yt_extra]
            name = v["title"][:60]
            if v.get("public_at"):
                when = datetime.fromisoformat(v["public_at"].replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=5, minutes=30)))
                name += f" (scheduled: public {when:%d %b %H:%M} IST)"
            rows_yt.append(("<tr>" + "".join(f"<td align=\"center\">{html.escape(c)}</td>" for c in cells)
                            + f'<td><a href="https://youtube.com/shorts/{v["id"]}">{html.escape(name)}</a></td></tr>',
                            " | ".join(cells) + f" | {name}"))
        long_table(h, t, f"YouTube, last {days} days",
                   '<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th>Views</th>'
                   '<th>Likes</th><th>Comments</th><th>Watched</th>'
                   + "".join(f"<th>{html.escape(LABELS.get(k, k).capitalize())}</th>" for k in yt_extra)
                   + '<th align="left">Short</th></tr>', rows_yt)
        if any(v.get("avg_percent") is not None for v in yt["videos"]):
            line = "Watched = average share of the Short people watched (seconds); YouTube Analytics lags 1-2 days."
            h.append(f"<p><i>{line}</i></p>")
            t.append(line)
    if yt.get("sources"):
        line = (f"Where the YouTube views came from (last {days} days): "
                + ", ".join(f"{name} {pct:.0f}% ({num(n)})" for name, n, pct in yt["sources"][:6]))
        h.append(f"<p>{html.escape(line)}</p>")
        t.append(line)
    for key in ("note", "analytics_note", "metrics_note"):
        if yt.get(key):
            h.append(f"<p><i>{html.escape(yt[key])}</i></p>")
            t.append(f"Note: {yt[key]}")

    def ago(prev_key: str) -> dict | None:  # yesterday's per-post numbers of one platform, for the "(+N)" next to views
        return (((bases[0] or {}).get(prev_key) or {}).get("posts"))

    fb, th, tg = others.get("fb") or {}, others.get("th") or {}, others.get("tg") or {}
    if fb:
        h.append(f"<h3>Facebook, last {days} days</h3>")
        t += ["", f"Facebook, last {days} days"]
        if fb.get("name"):
            line = f"{fb['name']}: {num(fb.get('followers'))} followers"
            h.append(f"<p>{html.escape(line)}</p>")
            t.append(line)
        para(account_line("Page", fb.get("days", 7), fb.get("account") or {}))
        if fb.get("posts"):
            day_fb, rows_fb = ago("fb"), []
            fb_extra = extra_cols([r.get("extra") or {} for r in fb["posts"]], set())
            for r in fb["posts"]:
                cap = (r.get("description") or "").split("\n")[0][:50] or r["id"]
                avg = r.get("avg_ms")
                cells = [r["ts"].strftime("%d %b"), with_day(r.get("plays"), r["id"], day_fb, "views"),
                         f"{avg / 1000:.1f} s" if isinstance(avg, (int, float)) else "-", num(r.get("likes")),
                         num(r.get("comments")), num(r.get("shares"))] + [metric_text(k, (r.get("extra") or {}).get(k)) for k in fb_extra]
                rows_fb.append(("<tr>" + "".join(f"<td align=\"center\">{html.escape(c)}</td>" for c in cells)
                                + f'<td><a href="{html.escape(r.get("permalink_url", ""))}">{html.escape(cap)}</a></td></tr>',
                                " | ".join(cells) + f" | {cap}"))
            long_table(h, t, f"Facebook, last {days} days",
                       '<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th>Plays</th>'
                       '<th>Avg watch</th><th>Reactions</th><th>Comments</th><th>Shares</th>'
                       + "".join(f"<th>{html.escape(LABELS.get(k, k).capitalize())}</th>" for k in fb_extra)
                       + '<th align="left">Reel</th></tr>', rows_fb)
    if th:
        h.append(f"<h3>Threads, last {days} days</h3>")
        t += ["", f"Threads, last {days} days"]
        if th.get("username"):
            line = f"@{th['username']}: {num(th.get('followers'))} followers"
            h.append(f"<p>{html.escape(line)}</p>")
            t.append(line)
        para(account_line("Account", th.get("days", 7), {k: v for k, v in (th.get("account") or {}).items() if k != "followers_count"}))
        if th.get("posts"):
            day_th, rows_th = ago("th"), []
            for m in th["posts"]:
                cap = (m.get("text") or "").split("\n")[0][:50] or m["id"]
                cells = [m["ts"].strftime("%d %b"), with_day(m.get("views"), m["id"], day_th, "views"), num(m.get("likes")),
                         num(m.get("replies")), num(m.get("reposts")), num(m.get("shares"))]
                rows_th.append(("<tr>" + "".join(f"<td align=\"center\">{html.escape(c)}</td>" for c in cells)
                                + f'<td><a href="{html.escape(m.get("permalink", ""))}">{html.escape(cap)}</a></td></tr>',
                                " | ".join(cells) + f" | {cap}"))
            long_table(h, t, f"Threads, last {days} days",
                       '<table cellpadding="4" style="border-collapse:collapse"><tr><th align="left">Date</th><th>Views</th>'
                       '<th>Likes</th><th>Replies</th><th>Reposts</th><th>Shares</th><th align="left">Post</th></tr>', rows_th)
    if tg:
        h.append("<h3>Telegram</h3>")
        t += ["", "Telegram"]
        if tg.get("channel"):
            line = (f"{tg['channel']}: {num(tg.get('subscribers'))} subscribers "
                    "(the Bot API gives no views per post; Telegram shows them under each post in the channel)")
            h.append(f"<p>{html.escape(line)}</p>")
            t.append(line)
    for name, o in (("Facebook", fb), ("Threads", th), ("Telegram", tg)):
        if o.get("note"):
            h.append(f"<p><i>{html.escape(o['note'])}</i></p>")
            t.append(f"Note ({name}): {o['note']}")

    page = lambda parts: "<html><body style=\"font-family:sans-serif\">" + "\n".join(parts) + "</body></html>"  # noqa: E731
    attachment = page([f"<h2>{html.escape(f'Job Shorts {day}: every post of the last {days} days')}</h2>"] + full) if longer else ""
    return subject, "\n".join(t) + "\n", page(h), attachment


def posted_youtube_id(folder: Path) -> str:
    f = folder / "posted.json"
    return json.loads(f.read_text(encoding="utf-8")).get("youtube_id", "") if f.is_file() else ""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--folder", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--save-stats", action="store_true", help="add this report's numbers to the stats history (the Daily reel run does)")
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    rep = cfg["report"]
    for k in ("instagram", "facebook", "threads", "youtube"):  # one window for every platform's account totals
        cfg.setdefault(k, {}).setdefault("account_days", rep.get("account_days", 7))
    load_env_files([(a.config.resolve().parent / f).resolve() for f in rep.get("env_files", [])])
    since = datetime.now(timezone.utc) - timedelta(days=rep["days"])
    day = os.environ.get("DAY") or a.folder.name
    stats_file = (a.config.resolve().parent / rep.get("stats_file", "../../content/stats-history.json")).resolve()
    try:
        history = json.loads(stats_file.read_text(encoding="utf-8")).get("snapshots", []) if stats_file.is_file() else []
    except ValueError:
        history = []
    ig, yt = instagram_stats(cfg["instagram"], since), youtube_stats(cfg["youtube"], since, [posted_youtube_id(a.folder)])
    posted = read_json(a.folder.resolve() / "posted.json")
    script = read_json(a.folder.resolve() / "script.json")
    pair = ({"date": day, "title": script.get("title", ""), "main": posted["media_id"], "trial": posted["trial_media_id"]}
            if posted.get("media_id") and posted.get("trial_media_id") else None)
    pairs = trial_pairs(cfg["instagram"], history, pair) if ig.get("username") else []
    others = {"fb": facebook_stats(cfg.get("facebook", {}), since) if cfg.get("facebook") else {},
              "th": threads_stats(cfg.get("threads", {}), since) if cfg.get("threads") else {},
              "tg": telegram_stats(cfg["telegram"]) if cfg.get("telegram") else {}}
    subject, text, body, attachment = build(a.folder.resolve(), day, os.environ.get("RUN_URL", ""), ig, yt, rep["days"],
                                            history, pairs, others, rep.get("rows_in_mail", 10))
    if a.save_stats:
        cutoff = datetime.now(timezone.utc) - timedelta(days=rep.get("keep_stats_days", 120))
        history = [s for s in history if datetime.fromisoformat(s["at"]) >= cutoff] + [
            snapshot(ig, yt, models_used(a.folder.resolve()), pair, others)]
        stats_file.write_text(json.dumps({"snapshots": history}, indent=1) + "\n", encoding="utf-8")
        print(f"stats saved to {stats_file} ({len(history)} snapshots)")
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
    if attachment:  # the full lists, when a platform has more posts than the mail shows
        msg.add_attachment(attachment.encode("utf-8"), maintype="text", subtype="html", filename=f"all-posts-{day}.html")
    with smtplib.SMTP_SSL(rep["smtp_host"], rep["smtp_port"], timeout=60) as s:
        s.login(user, password)
        s.send_message(msg)
    print(f"report sent to {msg['To']}: {subject}")


if __name__ == "__main__":
    main()
