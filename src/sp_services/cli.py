"""Small demo CLI for manual testing with uv run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import traceback
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .client import SpServicesClient
from .exceptions import (
    AuthenticationError,
    BrowserNotFoundError,
    SessionExpiredError,
    SpServicesError,
)
from .models import TokenSet

_DEFAULT_TOKEN_CACHE = ".sp_services_tokens.json"


def _load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactive E2E CLI for SP Services login and usage fetch."
    )
    parser.add_argument(
        "--save-json",
        dest="save_json",
        help="Write the fetched usage payload to a JSON file.",
    )
    parser.add_argument(
        "--token-cache",
        default=_DEFAULT_TOKEN_CACHE,
        help=(
            "File that stores tokens between runs so the login can be skipped "
            f"(default: {_DEFAULT_TOKEN_CACHE})."
        ),
    )
    parser.add_argument(
        "--no-token-cache",
        action="store_true",
        help="Neither read nor write the token cache.",
    )
    return parser


def _load_tokens(path: Path) -> TokenSet | None:
    try:
        data = json.loads(path.read_text())
        expires_at = data.get("expires_at")
        return TokenSet(
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            token_type=str(data.get("token_type") or "Bearer"),
            scope=data.get("scope"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_tokens(path: Path, tokens: TokenSet) -> None:
    data = asdict(tokens)
    data["expires_at"] = tokens.expires_at.isoformat() if tokens.expires_at else None
    # Owner-only: the file holds a long-lived refresh token.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{json.dumps(data, indent=2)}\n")


async def _token_from_cache(client: SpServicesClient, path: Path) -> str | None:
    cached = _load_tokens(path)
    if cached is None:
        return None
    if cached.refresh_token:
        try:
            tokens = await client.refresh_access_token(cached.refresh_token)
        except SpServicesError as err:
            print(f"Saved refresh token was rejected ({err}); logging in again.")
            return None
        print("Reused saved login.")
        return tokens.access_token
    if not cached.is_expired():
        print("Reused saved access token.")
        return cached.access_token
    return None


async def _browser_login(client: SpServicesClient) -> str:
    try:
        print(
            "Opening a browser window. Complete captcha, login and OTP there; "
            "it closes by itself when done."
        )
        return await client.login_with_browser()
    except BrowserNotFoundError as err:
        print(f"{err}\nFalling back to manual callback paste.")

    auth_url = await client.begin_browser_login()
    print(
        "Open this URL in your browser, complete login and OTP, "
        "then paste the final callback URL here:"
    )
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    callback_url = input("Callback URL: ").strip()
    return await client.exchange_callback_url(callback_url)


async def _interactive_login(client: SpServicesClient) -> str:
    username = os.environ.get("SP_SERVICES_USERNAME", "").strip()
    password = os.environ.get("SP_SERVICES_PASSWORD", "")
    if not username or not password:
        return await _browser_login(client)

    try:
        challenge = await client.login(username, password)
    except AuthenticationError as err:
        if "invalid_captcha" not in str(err):
            raise
        print("Captcha is required. Switching to browser-assisted login.")
        return await _browser_login(client)

    if challenge.phone_number:
        print(f"OTP sent to {challenge.phone_number}")
    else:
        print("OTP requested")
    otp = input("OTP: ").strip()
    return await client.verify_otp(otp, challenge)


async def _run(
    save_json: str | None = None, token_cache: str | None = _DEFAULT_TOKEN_CACHE
) -> None:
    _load_env_file()
    debug = os.environ.get("SP_SERVICES_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    cache_path = Path(token_cache) if token_cache else None

    try:
        async with SpServicesClient(debug=debug) as client:
            token = await _token_from_cache(client, cache_path) if cache_path else None
            from_cache = token is not None
            if token is None:
                token = await _interactive_login(client)

            try:
                usage = await client.fetch_usage(token)
            except SessionExpiredError:
                if not from_cache:
                    raise
                print("Saved login has expired; logging in again.")
                token = await _interactive_login(client)
                usage = await client.fetch_usage(token)

            token_set = client.token_set
            if token_set is not None:
                if token_set.refresh_token:
                    print("Refresh token obtained; later runs can skip the login.")
                else:
                    print(
                        "No refresh token returned; re-authentication will "
                        "require another interactive login."
                    )
                if token_set.expires_at:
                    print(f"Access token expires at {token_set.expires_at.isoformat()}")
                if cache_path:
                    _save_tokens(cache_path, token_set)
                    print(f"Saved tokens to {cache_path}")

            payload = json.dumps(asdict(usage), indent=2, default=str)
            print(payload)
            if save_json:
                output_path = Path(save_json)
                output_path.write_text(f"{payload}\n")
                print(f"Saved JSON to {output_path}")
    except SpServicesError as err:
        print(f"SP Services error: {err}")
        if debug:
            traceback.print_exc()
        raise SystemExit(1) from err


def main() -> None:
    args = _build_parser().parse_args()
    token_cache = None if args.no_token_cache else args.token_cache
    asyncio.run(_run(save_json=args.save_json, token_cache=token_cache))
