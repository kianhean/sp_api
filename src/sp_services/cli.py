"""Small demo CLI for manual testing with uv run."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from dataclasses import asdict
from getpass import getpass
from pathlib import Path

from .client import SpServicesClient
from .exceptions import SpServicesError


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


async def _run() -> None:
    _load_env_file()
    debug = os.environ.get("SP_SERVICES_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    username = os.environ.get("SP_SERVICES_USERNAME", "").strip() or input(
        "Username: "
    ).strip()
    password = os.environ.get("SP_SERVICES_PASSWORD") or getpass("Password: ")

    try:
        async with SpServicesClient(debug=debug) as client:
            challenge = await client.login(username, password)
            if challenge.phone_number:
                print(f"OTP sent to {challenge.phone_number}")
            else:
                print("OTP requested")

            otp = input("OTP: ").strip()
            token = await client.verify_otp(otp, challenge)
            usage = await client.fetch_usage(token)
            print(json.dumps(asdict(usage), indent=2, default=str))
    except SpServicesError as err:
        print(f"SP Services error: {err}")
        if debug:
            traceback.print_exc()
        raise SystemExit(1) from err


def main() -> None:
    asyncio.run(_run())
