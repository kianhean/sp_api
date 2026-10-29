"""Capture the Auth0 callback from the user's own browser, without pasting.

SP Services enforces captcha on Auth0, so a person still has to complete the
login in a real browser. This module launches an installed Chromium-family
browser (Chrome, Edge, Chromium, Brave) with a throwaway profile and talks to it
over the Chrome DevTools Protocol, using only the standard library. The request
to the SP callback URL is intercepted before it leaves the browser, so the
authorization code is captured for the client and never reaches SP's own page.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .exceptions import AuthenticationError, BrowserNotFoundError

_BROWSER_ENV_VAR = "SP_SERVICES_BROWSER"

_MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
)
_WINDOWS_BROWSERS = (
    r"Google\Chrome\Application\chrome.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
)

_DONE_PAGE = (
    "<!doctype html><title>SP Services login complete</title>"
    "<body style='font-family:sans-serif;padding:2em'>"
    "<h1>Login complete</h1><p>You can close this window.</p></body>"
)


def find_browser() -> str:
    """Return the path of an installed Chromium-family browser.

    ``SP_SERVICES_BROWSER`` overrides the search.
    """
    override = os.environ.get(_BROWSER_ENV_VAR, "").strip()
    if override:
        return override

    if sys.platform == "darwin":
        for path in _MAC_BROWSERS:
            if Path(path).exists():
                return path
    elif sys.platform == "win32":
        roots = [
            os.environ.get(name)
            for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        ]
        for root in filter(None, roots):
            for relative in _WINDOWS_BROWSERS:
                path = Path(root) / relative
                if path.exists():
                    return str(path)
    else:
        for name in _LINUX_BROWSERS:
            path = shutil.which(name)
            if path:
                return path

    raise BrowserNotFoundError(
        "no Chrome, Edge, Chromium or Brave installation found; "
        f"set {_BROWSER_ENV_VAR} to the browser executable"
    )


def is_callback_url(url: str, callback_prefix: str) -> bool:
    """Whether ``url`` is the OAuth callback carrying both ``code`` and ``state``."""
    if not url.startswith(callback_prefix):
        return False
    query = parse_qs(urlparse(url).query)
    return bool(query.get("code")) and bool(query.get("state"))


def capture_callback_url(
    authorize_url: str,
    callback_prefix: str,
    *,
    browser_path: str | None = None,
    timeout: float = 600.0,
    headless: bool = False,
) -> str:
    """Open ``authorize_url`` in a browser and return the callback URL it reaches.

    Blocks until the login finishes, the browser window is closed, or
    ``timeout`` seconds pass. Run it in a worker thread from async code.
    """
    executable = browser_path or find_browser()
    profile_dir = tempfile.mkdtemp(prefix="sp-services-login-")
    args = [
        executable,
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    if headless:
        args[1:1] = ["--headless=new", "--no-sandbox"]

    try:
        process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as err:
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise BrowserNotFoundError(f"could not start {executable}: {err}") from err

    deadline = time.monotonic() + timeout
    connection: _CdpConnection | None = None
    try:
        port, ws_path = _wait_for_devtools(Path(profile_dir), process, deadline)
        connection = _CdpConnection("127.0.0.1", port, ws_path)
        return _intercept_callback(connection, authorize_url, callback_prefix, deadline)
    finally:
        if connection is not None:
            try:
                connection.send("Browser.close")
            except OSError:
                pass
            connection.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        shutil.rmtree(profile_dir, ignore_errors=True)


def _wait_for_devtools(
    profile_dir: Path, process: subprocess.Popen[bytes], deadline: float
) -> tuple[int, str]:
    """Read the DevTools port the browser picked from its profile directory."""
    port_file = profile_dir / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AuthenticationError(
                "browser exited before login started; if it was already open, "
                "it may have handed the window to an existing instance"
            )
        try:
            lines = port_file.read_text().splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2 and lines[0].strip().isdigit():
            return int(lines[0]), lines[1].strip()
        time.sleep(0.1)
    raise AuthenticationError("timed out waiting for the browser to start")


def _intercept_callback(
    connection: _CdpConnection,
    authorize_url: str,
    callback_prefix: str,
    deadline: float,
) -> str:
    targets = connection.call("Target.getTargets", deadline=deadline)
    page = next(
        (t for t in targets.get("targetInfos", []) if t.get("type") == "page"), None
    )
    if page is None:
        raise AuthenticationError("browser opened no page to log in with")
    attached = connection.call(
        "Target.attachToTarget",
        {"targetId": page["targetId"], "flatten": True},
        deadline=deadline,
    )
    session_id = attached["sessionId"]

    # Interception must be active before navigating, or a fast redirect could
    # deliver the code to SP's page first.
    connection.call(
        "Fetch.enable",
        {
            "patterns": [
                {"urlPattern": f"{callback_prefix}*", "requestStage": "Request"}
            ]
        },
        session_id=session_id,
        deadline=deadline,
    )
    # Not awaited: Page.navigate only replies once the navigation commits, and
    # the callback request it leads to stays paused until handled below.
    connection.send("Page.navigate", {"url": authorize_url}, session_id)

    while True:
        event = connection.next_event(deadline)
        method = event.get("method")
        params = event.get("params", {})
        if method == "Target.detachedFromTarget" and (
            params.get("sessionId") == session_id
        ):
            raise AuthenticationError("browser window closed before login completed")
        if method != "Fetch.requestPaused" or event.get("sessionId") != session_id:
            continue

        url = str(params.get("request", {}).get("url", ""))
        request_id = params["requestId"]
        if not is_callback_url(url, callback_prefix):
            connection.send(
                "Fetch.continueRequest", {"requestId": request_id}, session_id
            )
            continue

        connection.send(
            "Fetch.fulfillRequest",
            {
                "requestId": request_id,
                "responseCode": 200,
                "responseHeaders": [
                    {"name": "Content-Type", "value": "text/html; charset=utf-8"}
                ],
                "body": base64.b64encode(_DONE_PAGE.encode("utf-8")).decode("ascii"),
            },
            session_id,
        )
        return url


class _CdpConnection:
    """Minimal blocking WebSocket client for the Chrome DevTools Protocol."""

    def __init__(self, host: str, port: int, path: str) -> None:
        self._sock = socket.create_connection((host, port), timeout=10)
        self._next_id = 0
        self._events: deque[dict[str, Any]] = deque()
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise AuthenticationError("browser closed the DevTools connection")
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise AuthenticationError(
                f"browser refused the DevTools connection: {status_line!r}"
            )

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> int:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        self._send_frame(0x1, json.dumps(message).encode("utf-8"))
        return self._next_id

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        deadline: float,
    ) -> dict[str, Any]:
        message_id = self.send(method, params, session_id)
        while True:
            message = self._receive(deadline)
            if message.get("id") != message_id:
                if "method" in message:
                    self._events.append(message)
                continue
            if "error" in message:
                raise AuthenticationError(
                    f"browser rejected {method}: {message['error']}"
                )
            return message.get("result", {})

    def next_event(self, deadline: float) -> dict[str, Any]:
        if self._events:
            return self._events.popleft()
        while True:
            message = self._receive(deadline)
            if "method" in message:
                return message

    def _receive(self, deadline: float) -> dict[str, Any]:
        payload = bytearray()
        while True:
            opcode, fin, data = self._read_frame(deadline)
            if opcode == 0x8:
                raise AuthenticationError("browser closed before login completed")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            payload += data
            if fin:
                return json.loads(payload.decode("utf-8"))

    def _read_frame(self, deadline: float) -> tuple[int, bool, bytes]:
        header = self._read_exact(2, deadline)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2, deadline), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8, deadline), "big")
        mask = self._read_exact(4, deadline) if header[1] & 0x80 else None
        data = self._read_exact(length, deadline)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, fin, data

    def _read_exact(self, size: int, deadline: float) -> bytes:
        buffer = bytearray()
        while len(buffer) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthenticationError("timed out waiting for the browser login")
            self._sock.settimeout(min(remaining, 1.0))
            try:
                chunk = self._sock.recv(size - len(buffer))
            except TimeoutError:
                continue
            except OSError as err:
                raise AuthenticationError(
                    "browser closed before login completed"
                ) from err
            if not chunk:
                raise AuthenticationError("browser closed before login completed")
            buffer += chunk
        return bytes(buffer)

    def _send_frame(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        self._sock.sendall(
            bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        )
