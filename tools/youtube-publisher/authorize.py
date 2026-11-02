"""
youtube-publisher, one-time setup: signs in to Google in the browser and saves YT_CLIENT_ID, YT_CLIENT_SECRET and
YT_REFRESH_TOKEN as GitHub repository secrets (`gh secret set`, values passed on stdin). Nothing secret is printed.

    python tools/youtube-publisher/authorize.py --client-json C:/Users/<you>/Downloads/client_secret_....json

The client JSON is the "Desktop app" OAuth client downloaded from Google Cloud (Google Auth Platform -> Clients).
Sign in with the Google account that owns the YouTube channel and allow "Manage your YouTube videos" (upload).
Run it again whenever the refresh token stops working (publish.py says so).
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
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-json", type=Path, required=True, help="the downloaded Desktop-app client JSON")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    a = p.parse_args()
    cfg = tomllib.loads(a.config.read_text(encoding="utf-8"))["youtube"]
    raw = json.loads(a.client_json.read_text(encoding="utf-8"))
    client = raw.get("installed") or raw.get("web")
    if not client:
        sys.exit("not an OAuth client JSON (no 'installed' section): download the Desktop app client from Google Cloud")

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
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
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

    for name, value in ((cfg["client_id_env"], client["client_id"]), (cfg["client_secret_env"], client["client_secret"]),
                        (cfg["refresh_token_env"], tokens["refresh_token"])):
        r = subprocess.run(["gh", "secret", "set", name, "-R", cfg["repo"]], input=value, text=True, capture_output=True)
        print(f"{name}: {'saved' if r.returncode == 0 else 'NOT saved: ' + r.stderr.strip()}")


if __name__ == "__main__":
    main()
