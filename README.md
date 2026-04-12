# sp-services

Async Python client for SP Services household electricity and water usage data.

## Install

```bash
uv sync
```

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
uv run python
uv run sp-services-demo
uv run python -m unittest discover -s tests -v
```

- This package is unofficial and depends on SP Services private web endpoints, so the flow can break if the upstream site changes.
