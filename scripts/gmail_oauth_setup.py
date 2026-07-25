"""One-time local script: obtain a Gmail API refresh token for the digest sender.

Run this once on your machine (never on Railway) after creating a "Desktop app"
OAuth 2.0 client in Google Cloud Console (APIs & Services -> Credentials) with the
Gmail API enabled and the ``gmail.send`` scope. Uses only ``requests`` (already a
project dependency) plus the stdlib loopback HTTP server for the redirect —
no google-auth-oauthlib dependency needed for this one-off flow.

Usage:
    GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... uv run python scripts/gmail_oauth_setup.py

Prints GMAIL_REFRESH_TOKEN to paste into .env / Railway once the browser consent
flow completes. Sign in with the Gmail account you want digests sent *from*.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_PORT = 8765
_REDIRECT_URI = f"http://localhost:{_PORT}/"


def _get_code(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"
    print(f"Opening browser for consent:\n{url}\n")
    webbrowser.open(url)

    holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            code = urllib.parse.parse_qs(qs).get("code", [None])[0]
            holder["code"] = code
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Consent captured, you can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", _PORT), Handler)
    while "code" not in holder:
        server.handle_request()
    if not holder["code"]:
        sys.exit("No authorization code received.")
    return holder["code"]


def main() -> None:
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET before running this script.")

    code = _get_code(client_id)
    resp = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        sys.exit(
            "No refresh_token in response — you may have already consented once before "
            "without 'prompt=consent'. Revoke access at https://myaccount.google.com/permissions "
            "and re-run."
        )
    print(f"\nGMAIL_REFRESH_TOKEN={refresh_token}")


if __name__ == "__main__":
    main()
