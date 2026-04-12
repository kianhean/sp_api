# sp-services

Async Python client for SP Services household electricity and water usage data.

## Install

```bash
uv sync
```

## Real E2E Check

This project includes a manual end-to-end runner for verifying the real SP login, SMS OTP flow, and usage download against your account.

Create a local `.env` file first:

```dotenv
SP_SERVICES_USERNAME=your-email@example.com
SP_SERVICES_PASSWORD=your-password
```

Run it with:

```bash
uv run sp-services-e2e
```

What it does:

- prompts for your SP username
- prompts for your password without echoing it
- starts the OTP flow
- waits for you to type the SMS OTP
- exchanges the OTP for a token
- fetches and prints the current usage payload

This is intentionally manual and interactive. It should not run in CI because it depends on real credentials, a live OTP, and private upstream endpoints.

## Usage

```bash
uv run sp-services-demo
```

Or call the library directly:

```python
import asyncio

from sp_services import SpServicesClient


async def main() -> None:
    async with SpServicesClient() as client:
        challenge = await client.login("username", "password")
        print(challenge.phone_number)

        otp = input("OTP: ")
        token = await client.verify_otp(otp)
        usage = await client.fetch_usage(token)
        print(usage)


asyncio.run(main())
```

## Notes

- `login()` starts the SMS OTP flow.
- `verify_otp()` exchanges the OTP result for an access token.
- `fetch_usage()` fetches electricity and water usage for the linked household account.
- Common `uv` workflow:

```bash
uv sync
uv sync --group dev
uv run sp-services-demo
uv run ruff check .
uv run pytest
```

- This package is unofficial and depends on SP Services private web endpoints, so the flow can break if the upstream site changes.
