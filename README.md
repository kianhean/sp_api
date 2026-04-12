# sp-services

Unofficial async Python client for SP Services household electricity and water
usage data.

It is designed for real-world SP portal access rather than public documented
APIs, so the library targets resilience and practical debugging over strict
upstream stability guarantees.

## Features

- Auth0-based login support for SP Services
- OTP verification flow
- browser-assisted fallback when captcha blocks headless login
- current electricity and water summary fields
- monthly historical electricity and water data where available
- hourly electricity history from the CSV export endpoint
- interactive CLI for real account verification
- CI with `ruff` and `pytest`

## Requirements

- Python `3.11+`
- `uv` as the package and environment manager

## Install

Runtime only:

```bash
uv sync
```

Runtime + development tools:

```bash
uv sync --group dev
```

## Configuration

The CLI reads a local `.env` file from the repository root.

Example:

```dotenv
SP_SERVICES_USERNAME=your-email@example.com
SP_SERVICES_PASSWORD=your-password
SP_SERVICES_DEBUG=0
```

Notes:

- `.env` is gitignored.
- `SP_SERVICES_DEBUG=1` enables more verbose auth failure output.
- username and password are optional if you use the browser-assisted flow.

## Real E2E Check

Run the interactive end-to-end verification:

```bash
uv run sp-services-e2e
```

The CLI will:

- read credentials from `.env` if present
- try the direct login flow first
- fall back to browser-assisted login if captcha is required
- exchange the resulting authorization code
- fetch and print usage data as JSON

### Browser-Assisted Login

SP Services currently enforces captcha on the direct Auth0
`usernamepassword/login` endpoint, so headless login is not always usable.

When that happens, the CLI will:

1. print an Auth0 authorize URL
2. optionally try to open it in your default browser
3. let you complete captcha, login, and OTP in the browser
4. ask you to paste the final callback URL
5. exchange that callback for an access token
6. continue fetching usage data

This is the intended manual verification path for real accounts.

## Library Usage

### Browser-Assisted Flow

```python
import asyncio

from sp_services import SpServicesClient


async def main() -> None:
    async with SpServicesClient() as client:
        authorize_url = await client.begin_browser_login()
        print(authorize_url)

        callback_url = input("Callback URL: ").strip()
        token = await client.exchange_callback_url(callback_url)
        usage = await client.fetch_usage(token)
        print(usage)


asyncio.run(main())
```

### Direct Login + OTP Flow

```python
import asyncio

from sp_services import SpServicesClient


async def main() -> None:
    async with SpServicesClient() as client:
        challenge = await client.login("username", "password")
        print(challenge.phone_number)

        otp = input("OTP: ").strip()
        token = await client.verify_otp(otp, challenge)
        usage = await client.fetch_usage(token)
        print(usage)


asyncio.run(main())
```

## Public API

### Client

- `SpServicesClient.login(username, password)`
- `SpServicesClient.verify_otp(otp, login_challenge=None)`
- `SpServicesClient.begin_browser_login()`
- `SpServicesClient.exchange_callback_url(callback_url)`
- `SpServicesClient.fetch_usage(token)`

### Models

- `LoginChallenge`
- `UsageData`
- `UsagePoint`

## Returned Data

`UsageData` currently includes:

- `electricity_today_kwh`
- `electricity_month_kwh`
- `water_today_m3`
- `water_month_m3`
- `account_no`
- `last_updated`
- `electricity_last_month_kwh`
- `water_last_month_m3`
- `electricity_monthly_history`
- `water_monthly_history`
- `electricity_hourly_history`

Historical series are returned as lists of
`UsagePoint(period, value, status=None)`.

Notes on real SP data:

- monthly files may not contain the current in-progress month yet
- electricity monthly rows can exist while water monthly rows lag, or vice versa
- monthly rows include `Status` such as `Actual` or `Estimated`
- hourly exports appear to be electricity-only in the current SP format

## Development

Common commands:

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv run sp-services-e2e
```

## CI

GitHub Actions runs:

- `ruff`
- `pytest`

across Python `3.11`, `3.12`, and `3.13`.

## Project Notes

- This package is unofficial.
- It relies on SP Services private web endpoints and Auth0 flows.
- Upstream changes can break auth or parsing without notice.
- CSV endpoints are currently the most reliable source for historical usage.
- Live E2E verification should stay manual because it depends on credentials,
  captcha, OTP, and live upstream behavior.
