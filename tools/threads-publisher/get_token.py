"""One-time Threads token setup: short-lived token -> 60-day token -> GitHub secrets.

Run it yourself, right after "Generate access token" in the Threads use case (that token lives about 1 hour):

    python tools/threads-publisher/get_token.py

It asks for the Threads app secret and the short-lived token without echoing them, exchanges the token for a
long-lived one (60 days), checks which Threads account it belongs to, and stores THREADS_ACCESS_TOKEN and
THREADS_USER_ID as GitHub Actions secrets through the gh CLI. Nothing secret is printed or written to disk.
The 60-day token is refreshed later with the token alone (GET /refresh_access_token); the secret is not kept.
"""
from __future__ import annotations

import getpass
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

API = "https://graph.threads.net"
REPO = "Pratham2703005/clips-generator"


class ApiError(Exception):
    pass


def get(path: str, params: dict) -> dict:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise ApiError(f"{path}: HTTP {e.code} {msg}".strip()) from None


def set_secret(name: str, value: str) -> None:
    # value goes in on stdin, never on the command line
    r = subprocess.run(["gh", "secret", "set", name, "-R", REPO], input=value, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"gh secret set {name} failed: {r.stderr.strip()}")


def me_of(token: str) -> dict:
    return get("v1.0/me", {"fields": "id,username", "access_token": token})


def main() -> None:
    # --long: the dashboard token is already the 60-day one (Access Token Debugger shows ~60 days); skip the exchange
    already_long = "--long" in sys.argv
    secret = "" if already_long else getpass.getpass("Threads app secret (input hidden): ").strip().strip('"')
    short = getpass.getpass("Token from 'Generate access token' (input hidden): ").strip().strip('"')
    # shape only, never the value: catches a paste that did not land (Ctrl+V into a hidden prompt can fail)
    print(f"token: {len(short)} characters, starts with '{short[:2]}'"
          + ("" if already_long else f"; secret: {len(secret)} characters"))
    if not short or (not already_long and not secret):
        sys.exit("empty input - paste with right-click (or Ctrl+Shift+V) and press Enter")

    try:
        me = me_of(short)
    except ApiError as e:
        sys.exit(f"the token itself does not work ({e}).\n"
                 "Generate a fresh one, paste it within a few minutes, and check the length printed above "
                 "(Threads tokens are ~150-250 characters).")
    print(f"token works for @{me.get('username')} (id {me.get('id')})")

    if already_long:
        token, days = short, None
    else:
        try:
            long = get("access_token", {"grant_type": "th_exchange_token", "client_secret": secret, "access_token": short})
        except ApiError as e:
            sys.exit(f"the token works but the 60-day exchange was refused ({e}).\n"
                     "It may already be a 60-day token: paste it into developers.facebook.com/tools/debug/accesstoken; "
                     "if 'Expires' is ~60 days away, run again with --long.")
        token = long.get("access_token")
        if not token:
            sys.exit("no access_token in the exchange response")
        days = int(long.get("expires_in", 0)) // 86400
        me = me_of(token)
    print(f"account: @{me.get('username')} (id {me.get('id')})" + (f", token valid ~{days} days" if days else ""))
    if me.get("username") != "isha.07007":
        print("note: this is not @isha.07007 - check the account before using it")

    set_secret("THREADS_ACCESS_TOKEN", token)
    set_secret("THREADS_USER_ID", str(me["id"]))
    print(f"saved THREADS_ACCESS_TOKEN and THREADS_USER_ID on {REPO}")

    # the expiry date the publisher and the report warn about (a dashboard token counts as 60 days from today)
    expires = (date.today() + timedelta(days=days or 60)).isoformat()
    cfg = Path(__file__).resolve().parent / "config.toml"
    text = cfg.read_text(encoding="utf-8")
    new = re.sub(r'^token_expires = "[^"]*"', f'token_expires = "{expires}"', text, count=1, flags=re.M)
    if new != text:
        cfg.write_text(new, encoding="utf-8")
        print(f"config.toml: token_expires = {expires} (commit and push it)")


if __name__ == "__main__":
    main()
