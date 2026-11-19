"""
instagram-dm: DMs the Telegram channel link to people who comment a keyword (or only emoji) on our recent reels
(user, 2026-11-18). Instagram's "private reply": one DM per comment, within 7 days of the comment.

    python tools/instagram-dm/reply_comments.py --check     # which permissions the token has; sends nothing
    python tools/instagram-dm/reply_comments.py --dry-run   # the comments it would answer; sends nothing
    python tools/instagram-dm/reply_comments.py             # send

Follow status can't be checked first: the API lists no followers, and says whether someone follows only for people who
have messaged the account. So everyone who asks gets the link, and the DM asks them to follow.
State (comments already answered) is a JSON file (--state), kept between workflow runs by actions/cache. Instagram
itself also refuses a second private reply to the same comment, so a lost state file costs a few refused calls, not
double DMs. Needs IG_ACCESS_TOKEN + IG_USER_ID (as instagram-publisher) with instagram_manage_comments and
instagram_manage_messages; META_APP_ID + IG_APP_SECRET only for --check's permission list. Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ig_pub = load("ig_publish", ROOT / "tools" / "instagram-publisher" / "publish.py")

# a comment made only of emoji (and spaces / punctuation) counts as asking (user, 2026-11-18: "add any emote as well")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⌀-⏿⬀-⯿️‍]")


def wants_link(text: str, keywords: list[str], emoji_only: bool, role_words: str = "") -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(re.search(rf"(?<![a-z]){re.escape(k.lower())}(?![a-z])", t) for k in keywords):
        return True
    if role_words and re.search(role_words, t, re.I):
        return True
    rest = EMOJI.sub("", t)
    return emoji_only and bool(EMOJI.search(t)) and not re.search(r"[a-z0-9ऀ-ॿ]", rest)


def recent_comments(api, user_id: str, days: int, own: str) -> list[dict]:
    """Top-level comments (and replies) by others on our media of the last `days` days, newest media first."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    media = api.call("GET", f"{user_id}/media", {"fields": "id,timestamp,permalink,comments_count", "limit": 30}).get("data", [])
    out = []
    for m in media:
        if datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")) < since:
            break
        if not m.get("comments_count"):
            continue
        res = api.call("GET", f"{m['id']}/comments", {"fields": "id,text,timestamp,username,replies{id,text,timestamp,username}", "limit": 50})
        for c in res.get("data", []):
            for x in [c] + (c.get("replies", {}) or {}).get("data", []):
                if x.get("username", "").lower() != own.lower():
                    out.append({**x, "media": m["id"], "permalink": m.get("permalink", "")})
    return [c for c in out if datetime.fromisoformat(c["timestamp"].replace("+0000", "+00:00")) >= since]


def card(dm: dict) -> dict | None:
    """The DM as a card with buttons (user, 2026-11-18: links in a text DM weren't clickable). Instagram's generic
    template: title + subtitle (80 characters each) and up to 3 web_url buttons (titles up to 20 characters)."""
    c = dm.get("card") or {}
    buttons = [{"type": "web_url", "url": b["url"], "title": b["title"][:20]} for b in c.get("buttons", [])[:3]]
    if not c.get("title") or not buttons:
        return None
    element = {"title": c["title"][:80], "buttons": buttons}
    if c.get("subtitle"):
        element["subtitle"] = c["subtitle"][:80]
    if c.get("image_url"):
        element["image_url"] = c["image_url"]
    return {"attachment": {"type": "template", "payload": {"template_type": "generic", "elements": [element]}}}


def private_reply(api, user_id: str, comment_id: str, dm: dict) -> str:
    """The card with buttons; when Instagram refuses it, the plain text message. Returns which one went."""
    message = card(dm)
    if message:
        try:
            _post_json(api, f"{user_id}/messages", {"recipient": {"comment_id": comment_id}, "message": message})
            return "card"
        except RuntimeError as e:
            if "(code 10," in str(e) or "(code 200," in str(e):
                raise  # a permission problem: the text would fail the same way
            print(f"    card refused ({str(e)[:160]}); sending the text instead")
    _post_json(api, f"{user_id}/messages", {"recipient": {"comment_id": comment_id}, "message": {"text": dm["message"]}})
    return "text"


def _post_json(api, path: str, payload: dict) -> dict:
    """POST a JSON body (the messages endpoint wants JSON); the token goes as a query parameter."""
    import urllib.error
    import urllib.parse
    import urllib.request
    url = f"{api.base}/{path}?" + urllib.parse.urlencode({"access_token": api.token})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw).get("error", {})
            msg = f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})"
        except (ValueError, AttributeError):
            msg = raw[:300]
        raise RuntimeError(f"POST {path}: HTTP {e.code}: {msg}") from None


def check(api, user_id: str) -> int:
    """What the token can do, without sending anything."""
    problems = 0
    app_id, secret = os.environ.get("META_APP_ID", "").strip(), os.environ.get("IG_APP_SECRET", "").strip()
    if app_id and secret:
        try:
            # not api.call: it puts the user token in access_token, and debug_token wants the app token there
            import urllib.error
            import urllib.parse
            import urllib.request
            q = urllib.parse.urlencode({"input_token": api.token, "access_token": f"{app_id}|{secret}"})
            try:
                with urllib.request.urlopen(f"{api.base}/debug_token?{q}", timeout=60) as r:
                    d = json.loads(r.read()).get("data", {})
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None
            scopes = sorted(d.get("scopes", []))
            print(f"token type {d.get('type')}, app {d.get('application')}, scopes: {', '.join(scopes) or '-'}")
            for need in ("instagram_manage_comments", "instagram_manage_messages"):
                if need not in scopes and need.replace("instagram_", "instagram_business_") not in scopes:
                    print(f"  MISSING: {need}")
                    problems += 1
        except RuntimeError as e:
            print(f"debug_token failed: {e}")
    else:
        print("note: META_APP_ID / IG_APP_SECRET not set, so no permission list; testing the calls instead")
    try:
        me = api.call("GET", user_id, {"fields": "username"})
        print(f"account: @{me.get('username')}")
        media = api.call("GET", f"{user_id}/media", {"fields": "id,comments_count", "limit": 5}).get("data", [])
        with_comments = next((m for m in media if m.get("comments_count")), None)
        if with_comments:
            n = len(api.call("GET", f"{with_comments['id']}/comments", {"fields": "id", "limit": 5}).get("data", []))
            print(f"reading comments: OK ({n} on the latest reel with comments)")
        else:
            print("reading comments: no recent reel has comments to read")
    except RuntimeError as e:
        print(f"reading comments: FAILED: {e}")
        problems += 1
    try:
        api.call("GET", f"{user_id}/conversations", {"platform": "instagram", "limit": 1})
        print("messages: OK (the conversations list answers)")
    except RuntimeError as e:
        print(f"messages: FAILED: {e}")
        problems += 1
    print("check passed" if not problems else f"{problems} problem(s): see above")
    return 1 if problems else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="show what the token can do; send nothing")
    p.add_argument("--dry-run", action="store_true", help="list the comments it would answer; send nothing")
    p.add_argument("--state", type=Path, default=ROOT / ".instagram-dm-state.json", help="comments already answered")
    p.add_argument("--config", type=Path, default=HERE / "config.toml")
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))
    dm, ig = cfg["dm"], tomllib.loads((ROOT / "tools" / "instagram-publisher" / "config.toml").read_text(encoding="utf-8"))["instagram"]
    ig_pub.load_env_files([(ROOT / "tools" / "instagram-publisher" / f).resolve() for f in ig.get("env_files", [])])
    token, user_id = os.environ.get(ig["token_env"], "").strip(), os.environ.get(ig["user_env"], "").strip()
    if not token or not user_id:
        sys.exit(f"set {ig['token_env']} and {ig['user_env']}")
    api = ig_pub.Api(token, ig["api_version"])
    if a.check:
        return check(api, user_id)

    own = api.call("GET", user_id, {"fields": "username"}).get("username", "")
    state = json.loads(a.state.read_text(encoding="utf-8")) if a.state.is_file() else {"answered": {}}
    answered: dict = state.setdefault("answered", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=dm["max_age_days"] + 1)).isoformat()
    answered = {k: v for k, v in answered.items() if v >= cutoff}  # older ones can't be answered anyway
    todo = [c for c in recent_comments(api, user_id, dm["max_age_days"], own)
            if c["id"] not in answered and wants_link(c.get("text", ""), dm["keywords"], dm.get("emoji_only", True), dm.get("role_words", ""))]
    print(f"{len(todo)} comment(s) asking for the link" + (" (dry run)" if a.dry_run else ""))
    sent = refused = 0
    for c in todo[: dm.get("max_per_run", 50)]:
        print(f"  @{c.get('username', '?')}: {c.get('text', '')[:60]!r} on {c['permalink']}")
        if a.dry_run:
            continue
        try:
            kind = private_reply(api, user_id, c["id"], dm)
            print(f"    sent ({kind})")
            sent += 1
        except RuntimeError as e:
            refused += 1
            print(f"    not sent: {e}")
            if "(code 10," in str(e) or "(code 200," in str(e) or "(code 3," in str(e):
                print("    the token lacks the messaging permission: run --check")
                break
        answered[c["id"]] = datetime.now(timezone.utc).isoformat(timespec="seconds")  # refused ones too: no retry loop
    if not a.dry_run:
        state["answered"] = answered
        a.state.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
        print(f"sent {sent}, not sent {refused}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
