"""Small demo CLI for manual testing with uv run."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from getpass import getpass

from .client import SpServicesClient


async def _run() -> None:
    username = input("Username: ").strip()
    password = getpass("Password: ")

    async with SpServicesClient() as client:
        challenge = await client.login(username, password)
        if challenge.phone_number:
            print(f"OTP sent to {challenge.phone_number}")
        else:
            print("OTP requested")

        otp = input("OTP: ").strip()
        token = await client.verify_otp(otp, challenge)
        usage = await client.fetch_usage(token)
        print(json.dumps(asdict(usage), indent=2, default=str))


def main() -> None:
    asyncio.run(_run())
