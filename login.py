"""Authenticate with Kite Connect and save today's access token."""

import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(name.strip(), value)


class RequestTokenHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        callback = self.server.callback
        parsed = urlparse(self.path)
        if parsed.path != callback.path:
            self.send_error(404)
            return

        params = parse_qs(parsed.query)
        self.server.request_token = params.get("request_token", [None])[0]
        self.server.login_status = params.get("status", ["unknown"])[0]
        self.server.callback_received = True
        message = (
            "Login complete. You can close this tab."
            if self.server.request_token and self.server.login_status == "success"
            else "Login was not completed. Return to the terminal for details."
        )
        self.send_response(200 if self.server.request_token else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode("utf-8"))

    def log_message(self, format, *args):
        return


def main() -> None:
    load_env_file(Path(__file__).with_name(".env"))
    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")
    redirect_url = os.environ.get("KITE_REDIRECT_URL", "http://127.0.0.1:8765/")

    if not api_key or not api_secret:
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET in the .env file.")

    callback = urlparse(redirect_url)
    if callback.scheme != "http" or callback.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("KITE_REDIRECT_URL must be an HTTP localhost URL.")
    port = callback.port or 80

    kite = KiteConnect(api_key=api_key)
    server = HTTPServer((callback.hostname, port), RequestTokenHandler)
    server.callback = callback
    server.callback_received = False
    server.request_token = None
    server.login_status = None
    server.timeout = 300

    login_url = kite.login_url()
    print("Opening Kite's official login page. Sign in there and enter your MPIN when prompted.")
    print("If it does not open automatically, visit:", login_url)
    print("Waiting up to five minutes for the configured localhost redirect...")
    webbrowser.open(login_url)

    try:
        server.handle_request()
    finally:
        server.server_close()

    if not server.callback_received:
        raise SystemExit("Timed out waiting for the login redirect.")
    if server.login_status != "success" or not server.request_token:
        raise SystemExit(f"Kite login did not succeed (status: {server.login_status}).")

    session = kite.generate_session(server.request_token, api_secret=api_secret)
    access_token = session["access_token"]
    kite.set_access_token(access_token)
    profile = kite.profile()

    os.environ["ACCESS_TOKEN"] = access_token
    print(f"Login successful. Connected as {profile['user_name']} ({profile['user_id']}).")
    print(f"Access token saved to ACCESS_TOKEN env.")


if __name__ == "__main__":
    main()