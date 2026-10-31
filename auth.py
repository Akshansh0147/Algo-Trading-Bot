"""Access-token lifecycle: reuse a valid token, otherwise run the browser login."""

from __future__ import annotations

import time
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import InputException, KiteException, TokenException
from loguru import logger

from config import AppConfig
from market_data import AuthenticationError


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_CallbackServer"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        # Ignore favicon requests, browser prefetches, and anything without params.
        if parsed.path != self.server.callback_path or not params:
            self._reply(404, "Waiting for the Kite login redirect...")
            return

        token = params.get("request_token", [None])[0]
        status = params.get("status", ["unknown"])[0]
        self.server.request_token = token
        self.server.status = status
        self.server.received = True

        if token and status == "success":
            self._reply(200, "Login complete. You can close this tab and return to the terminal.")
        else:
            self._reply(400, "Login was not completed. Return to the terminal for details.")

    def _reply(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # silence default logging
        return


class _CallbackServer(HTTPServer):
    """One-shot local server that captures Kite's redirect."""

    def __init__(self, host: str, port: int, callback_path: str) -> None:
        super().__init__((host, port), _CallbackHandler)
        self.callback_path = callback_path
        self.request_token: str | None = None
        self.status = "unknown"
        self.received = False


def is_token_valid(api_key: str, access_token: str) -> bool:
    """Return True if Kite accepts the token. Network problems raise instead."""
    if not access_token:
        return False
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        kite.profile()
    except (TokenException, InputException):
        return False
    except (KiteException, requests.RequestException) as exc:
        raise AuthenticationError(f"Could not verify the access token: {exc}") from exc
    return True


def _capture_request_token(redirect_url: str, login_url: str, timeout_seconds: int) -> str:
    callback = urlparse(redirect_url)
    if callback.scheme != "http" or callback.hostname not in ("127.0.0.1", "localhost"):
        raise AuthenticationError("KITE_REDIRECT_URL must look like http://127.0.0.1:5000/")

    try:
        server = _CallbackServer(callback.hostname, callback.port or 80, callback.path or "/")
    except OSError as exc:
        raise AuthenticationError(
            f"Cannot listen on {redirect_url} ({exc}). Is another program using that port?"
        ) from exc

    server.timeout = 1.0
    logger.info("Opening Kite login page. Sign in and enter your MPIN/TOTP when prompted.")
    logger.info("If the browser does not open, visit: {}", login_url)
    logger.info("Waiting up to {} seconds for the redirect to {}", timeout_seconds, redirect_url)
    webbrowser.open(login_url)

    deadline = time.monotonic() + timeout_seconds
    try:
        while not server.received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not server.received:
        raise AuthenticationError("Timed out waiting for the Kite login redirect.")
    if server.status != "success" or not server.request_token:
        raise AuthenticationError(f"Kite login did not succeed (status: {server.status}).")
    return server.request_token


def _save_token(path: Path, token: str) -> None:
    path.write_text(token, encoding="utf-8")
    logger.info("Access token saved to {}", path.name)


def interactive_login(config: AppConfig) -> str:
    """Run the browser login and return a fresh access token."""
    creds = config.credentials
    if not creds.api_secret:
        raise AuthenticationError("KITE_API_SECRET must be set in .env to log in.")

    kite = KiteConnect(api_key=creds.api_key)
    request_token = _capture_request_token(
        config.redirect_url, kite.login_url(), config.login_timeout_seconds
    )
    try:
        session = kite.generate_session(request_token, api_secret=creds.api_secret)
    except (TokenException, InputException) as exc:
        raise AuthenticationError(f"Kite rejected the login: {exc}") from exc
    except (KiteException, requests.RequestException) as exc:
        raise AuthenticationError(f"Could not complete the login: {exc}") from exc

    logger.info("Login successful. Connected as {} ({})", session.get("user_name"), session.get("user_id"))
    return str(session["access_token"])


def ensure_authenticated(config: AppConfig, force_login: bool = False) -> AppConfig:
    """Return a config holding a valid access token, logging in if needed."""
    creds = config.credentials
    if not force_login and is_token_valid(creds.api_key, creds.access_token):
        logger.info("Existing access token is valid; skipping login")
        return config

    logger.info("No valid access token; starting login")
    token = interactive_login(config)
    _save_token(config.token_file, token)
    return replace(config, credentials=replace(creds, access_token=token))