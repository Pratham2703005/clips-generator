"""
youtube-publisher, one-time setup: signs in to Google in the browser and saves YT_CLIENT_ID, YT_CLIENT_SECRET and
YT_REFRESH_TOKEN as GitHub repository secrets (`gh secret set`, values passed on stdin). Nothing secret is printed.

    python tools/youtube-publisher/authorize.py --client-json C:/Users/<you>/Downloads/client_secret_....json

The client JSON is the "Desktop app" OAuth client downloaded from Google Cloud (Google Auth Platform -> Clients).
Sign in with the Google account that owns the YouTube channel and allow "Manage your YouTube videos" (upload).
Run it again whenever the refresh token stops working (publish.py says so).

Local only (2026-11-16): Google no longer shows or downloads an existing client's secret. Add a new one (Google Auth
Platform -> Clients -> the client -> Add secret; the old one keeps working, so the GitHub secrets stay as they are) and

    python tools/youtube-publisher/authorize.py --local

asks for the client ID and that secret (hidden input) and writes the three values to video-generator/.env (gitignored)
instead of GitHub: for one-off jobs on this PC such as reoptimize.py.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import socket

# this PC's IPv6 route to Google can hang for minutes (2026-11-16); IPv4 answers at once
_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _getaddrinfo(*a, **k) if r[0] == socket.AF_INET] or _getaddrinfo(*a, **k)

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
# read-only too, for the stats in the run report (tools/report): channel and video view/like/comment counts, and
# YouTube Analytics (how much of each Short people watched, where the views came from; the project needs the
# "YouTube Analytics API" enabled and this scope added under Google Auth Platform -> Data Access)
SCOPES = [SCOPE, "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly",
          # caption tracks after each upload, and reoptimize.py's title/description updates of older Shorts (2026-11-16;
          # add it under Google Auth Platform -> Data Access too)
          "https://www.googleapis.com/auth/youtube.force-ssl"]
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-json", type=Path, help="the downloaded Desktop-app client JSON")
    p.add_argument("--local", action="store_true", help="type the client ID + secret, save the tokens to .env (not GitHub)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))["youtube"]
    if a.client_json:
        raw = json.loads(a.client_json.read_text(encoding="utf-8"))
        client = raw.get("installed") or raw.get("web")
        if not client:
            sys.exit("not an OAuth client JSON (no 'installed' section): download the Desktop app client from Google Cloud")
    elif a.local:
        import getpass
        client = {"client_id": input("Client ID: ").strip(), "client_secret": getpass.getpass("Client secret (hidden): ").strip()}
        if not client["client_id"] or not client["client_secret"]:
            sys.exit("both the client ID and the secret are needed")
    else:
        sys.exit("give --client-json FILE, or --local to type the client ID and secret")

    # loopback redirect on a free port, with PKCE and a state check
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in q
            self.wfile.write(("<h3>Done. You can close this tab.</h3>" if ok else "<h3>Sign-in failed; see the terminal.</h3>").encode())

        def log_message(self, *args):  # keep the code out of the terminal
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print("Opening the browser for Google sign-in (if it doesn't open, paste this link):\n" + url, flush=True)
    webbrowser.open(url)
    thread.join(timeout=600)
    server.server_close()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"sign-in did not finish: {result.get('error', 'no answer within 10 minutes')}")

    body = urllib.parse.urlencode({
        "code": result["code"], "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(client.get("token_uri", "https://oauth2.googleapis.com/token"),
                                                       data=body), timeout=60) as r:
        tokens = json.loads(r.read())
    if "refresh_token" not in tokens:
        sys.exit("Google gave no refresh token: remove the app's access at myaccount.google.com/permissions and run again")
    if SCOPE not in tokens.get("scope", ""):
        sys.exit("the upload permission was not granted: run again and tick 'Manage your YouTube videos'")
    if SCOPES[1] not in tokens.get("scope", ""):
        print("note: read access ('View your YouTube account') was not granted: uploads work, the report has no YouTube stats")
    if SCOPES[2] not in tokens.get("scope", ""):
        print("note: YouTube Analytics access was not granted: the report has no watch time / traffic sources")
    if SCOPES[3] not in tokens.get("scope", ""):
        print("note: 'See, edit, and permanently delete your YouTube videos' was not granted: no caption tracks, no reoptimize.py")

    # which channel did the sign-in pick? The account has a personal channel too (2026-11-16: a token for it nearly went
    # into the secrets, which would have sent every Short there)
    want = cfg.get("channel_id", "")
    try:
        req = urllib.request.Request("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                                     headers={"Authorization": f"Bearer {tokens['access_token']}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            ch = (json.loads(r.read()).get("items") or [{}])[0]
        print(f"signed in as channel: {ch.get('snippet', {}).get('title', '?')} ({ch.get('id', '?')})")
        if want and ch.get("id") != want:
            sys.exit(f"that is not the Shorts channel ({want}): nothing saved. Run again and pick the "
                     "'Isha - Job Shorts and Interview tips' channel when Google asks which channel / brand account to use.")
    except urllib.error.URLError as e:
        print(f"note: could not check the channel ({e}); make sure you picked the Shorts channel")
    values = ((cfg["client_id_env"], client["client_id"]), (cfg["client_secret_env"], client["client_secret"]),
              (cfg["refresh_token_env"], tokens["refresh_token"]))
    if a.local:
        env = (a.config.resolve().parent / cfg.get("env_files", ["../../.env"])[0]).resolve()
        lines = env.read_text(encoding="utf-8").splitlines() if env.is_file() else []
        names = {n for n, _ in values}
        lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in names] + [f"{n}={v}" for n, v in values]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"saved {', '.join(names)} to {env} (gitignored; the GitHub secrets were not touched)")
        return
    for name, value in values:
        r = subprocess.run(["gh", "secret", "set", name, "-R", cfg["repo"]], input=value, text=True, capture_output=True)
        print(f"{name}: {'saved' if r.returncode == 0 else 'NOT saved: ' + r.stderr.strip()}")


if __name__ == "__main__":
    main()
