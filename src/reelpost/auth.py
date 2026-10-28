"""Facebook Login for a Page token and the linked Instagram professional account."""

from __future__ import annotations

import secrets
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from reelpost.constants import DEFAULT_SCOPES, REDIRECT_HOST, REDIRECT_PATH, REDIRECT_PORT, REDIRECT_URI
from reelpost.errors import ReelpostError
from reelpost.graph import normalize_version


def build_login_url(
    *,
    app_id: str,
    redirect_uri: str,
    state: str,
    version: str,
    config_id: str | None = None,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
) -> str:
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
    }
    if config_id:
        params["config_id"] = config_id
    else:
        params["scope"] = ",".join(scopes)
    return f"https://www.facebook.com/{normalize_version(version)}/dialog/oauth?{urlencode(params)}"


def parse_oauth_redirect(value: str, expected_state: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.query:
        raise ReelpostError("Paste the full redirect URL from the browser, including the code.")
    query = parse_qs(parsed.query)
    if query.get("error"):
        description = (query.get("error_description") or query.get("error") or ["login was denied"])[0]
        raise ReelpostError(f"Facebook login failed: {description}")
    state = (query.get("state") or [""])[0]
    if not secrets.compare_digest(state, expected_state):
        raise ReelpostError("OAuth state did not match. Start login again.")
    code = (query.get("code") or [""])[0]
    if not code:
        raise ReelpostError("The redirect URL did not include a code.")
    return code


def exchange_user_token(
    session: requests.Session,
    *,
    version: str,
    app_id: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
) -> tuple[str, str | None]:
    version = normalize_version(version)
    short = _token_request(
        session,
        version,
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    try:
        long = _token_request(
            session,
            version,
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short["access_token"],
            },
        )
    except ReelpostError:
        long = short
    token = str(long["access_token"])
    expires_in = long.get("expires_in")
    expires_at = None
    if expires_in:
        try:
            seconds = int(expires_in)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    return token, expires_at


def wait_for_code(state: str, *, timeout: float = 180, port: int = REDIRECT_PORT, host: str = REDIRECT_HOST) -> str:
    box: dict[str, str] = {}
    server = _oauth_server(box, state, host=host, port=port)
    server.timeout = 0.5
    deadline = time.monotonic() + timeout
    try:
        while "code" not in box and "error" not in box:
            if time.monotonic() > deadline:
                raise ReelpostError(
                    f"Timed out waiting for Facebook login on {REDIRECT_URI}. "
                    "Run again with --paste and copy the redirect URL by hand."
                )
            server.handle_request()
    finally:
        server.server_close()
    if box.get("error"):
        raise ReelpostError(box["error"])
    return box["code"]


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        return


def _token_request(session: requests.Session, version: str, params: dict) -> dict:
    try:
        response = session.get(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            params=params,
            timeout=(30, 60),
        )
    except requests.RequestException as exc:
        raise ReelpostError(f"Could not reach Meta to exchange the login code: {exc}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise ReelpostError(f"Meta returned a non-JSON token response (HTTP {response.status_code}).") from exc
    if not isinstance(body, dict) or not body.get("access_token"):
        message = "Meta did not return an access token."
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            message = str(body["error"].get("message") or message)
        raise ReelpostError(message)
    return body


def _oauth_server(box: dict[str, str], state: str, *, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != REDIRECT_PATH:
                self._reply(404, "Not found")
                return
            try:
                box["code"] = parse_oauth_redirect(f"http://localhost{self.path}", state)
            except ReelpostError as exc:
                box["error"] = str(exc)
                self._reply(400, str(exc))
                return
            self._reply(200, "Reelpost is signed in. You can close this tab.")

        def log_message(self, fmt: str, *args) -> None:
            return

        def _reply(self, status: int, text: str) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise ReelpostError(
            f"Could not listen on http://{host}:{port}{REDIRECT_PATH}. "
            "Stop whatever is using that port, or run with --paste."
        ) from exc
