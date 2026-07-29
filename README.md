# sp-services

[![CI](https://github.com/kianhean/sp_api/actions/workflows/ci.yml/badge.svg)](https://github.com/kianhean/sp_api/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

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
- daily electricity history from the CSV export endpoint
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

To also save the fetched payload to disk:

```bash
uv run sp-services-e2e --save-json usage.json
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

### How To Capture The Callback URL

After you complete login, captcha, and OTP in the browser, SP Services may
redirect immediately away from the Auth0 callback page. In many cases the final
callback URL is visible only briefly, so the most reliable method is to capture
it from the browser network log.

Expected callback shape:

```text
https://services.spservices.sg/callback?fromLogin=true&code=...&state=...
```

You need the full URL, including both:

- `code=...`
- `state=...`

#### Fast Path: Copy From Address Bar

If the browser pauses on the callback URL long enough:

1. complete the login flow
2. when the page lands on `services.spservices.sg/callback?...`
3. copy the full URL from the address bar
4. paste it into the CLI at `Callback URL:`

#### Reliable Path: Use Browser DevTools Network Tab

If the page redirects too quickly, use the network tab instead.

Chrome or Edge:

1. open DevTools with `Cmd+Option+I` on macOS or `Ctrl+Shift+I` on Windows/Linux
2. open the `Network` tab
3. enable `Preserve log`
4. start the browser login flow from the URL printed by the CLI
5. complete captcha, login, and OTP
6. in the network request list, look for a request to:

```text
https://services.spservices.sg/callback?fromLogin=true&code=...&state=...
```

7. click that request
8. copy the full Request URL from the headers panel
9. paste it into the CLI

Safari:

1. enable the Develop menu if needed
2. open Web Inspector
3. go to the `Network` tab
4. enable persistent logging if available
5. complete the login flow
6. find the request to `services.spservices.sg/callback?...`
7. copy the full request URL
8. paste it into the CLI

Firefox:

1. open Developer Tools
2. open `Network`
3. enable persistent logs
4. complete the login flow
5. locate the `services.spservices.sg/callback?...` request
6. copy the full URL from the request details
7. paste it into the CLI

#### Important

- copy the callback URL, not the later redirected portal URL
- the callback URL must include the `code` query parameter
- the `state` value must match the login attempt currently running in the CLI
- if you restart the CLI, use the new callback URL from that new run

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
- `SpServicesClient.refresh_access_token(refresh_token=None)`
- `SpServicesClient.fetch_usage(token, account_no=None)`
- `SpServicesClient.token_set`

`fetch_usage` uses the first linked account unless `account_no` is given.

### Models

- `LoginChallenge`
- `TokenSet`
- `UsageData`
- `UsagePoint`

### Tokens And Re-Authentication

Every successful exchange stores a `TokenSet` on the client:

```python
token = await client.exchange_callback_url(callback_url)
tokens = client.token_set

if tokens.refresh_token and tokens.is_expired():
    tokens = await client.refresh_access_token(tokens.refresh_token)
```

The library requests the `offline_access` scope, so Auth0 usually returns a
refresh token. Persist it if you need unattended re-authentication; without one
the only way back is another interactive browser login. `refresh_access_token`
keeps the existing refresh token when Auth0 does not rotate it.

### Timezone

All "today" and "this month" decisions are made in `Asia/Singapore`, regardless
of the host clock, and `last_updated` is timezone-aware. Period strings in the
history series are returned exactly as SP publishes them and are Singapore local
time.

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
- `electricity_daily_history`
- `electricity_hourly_history`

Historical series are returned as lists of
`UsagePoint(period, value, status=None)`.

### What The Fields Mean

- `electricity_today_kwh`
  Best available electricity usage total for the current day. This may come
  from the live hourly payload or from the daily CSV fallback.
- `electricity_month_kwh`
  Best available electricity total for the current month. This can be `null`
  when SP has not yet published the current month in its monthly dataset.
- `water_today_m3`
  Current-day water usage. This is often unavailable in the current SP data
  sources and may be `null`.
- `water_month_m3`
  Best available water total for the current month. This can be `null` when SP
  has not yet published the current month in its monthly dataset.
- `account_no`
  The SP account number selected from the linked household accounts.
- `last_updated`
  Timezone-aware (`Asia/Singapore`) timestamp for when the library fetched and
  assembled the payload.
- `electricity_last_month_kwh`
  Last published monthly electricity total from the monthly CSV history.
- `water_last_month_m3`
  Last published monthly water total from the monthly CSV history.
- `electricity_monthly_history`
  Monthly electricity series from the SP monthly CSV export.
- `water_monthly_history`
  Monthly water series from the SP monthly CSV export.
- `electricity_daily_history`
  Daily electricity series from the SP daily CSV export.
- `electricity_hourly_history`
  Half-hourly electricity series from the SP hourly CSV export.

### UsagePoint Shape

Each history item is a `UsagePoint` with:

- `period`
  The time bucket label exactly as returned by SP, for example
  `2026-03-01`, `2026-04-12`, or `2026-04-12 00:30:00`.
- `value`
  The numeric consumption value for that period, or `null` when SP published
  the period without a figure.
- `status`
  Optional metadata from SP. This is mainly present in monthly history and can
  be values such as `Actual` or `Estimated`.

### Example Output

```json
{
  "electricity_today_kwh": 19.967,
  "electricity_month_kwh": null,
  "water_today_m3": null,
  "water_month_m3": null,
  "account_no": "8949049293",
  "last_updated": "2026-04-12 16:42:41.245935+08:00",
  "electricity_last_month_kwh": 572.0,
  "water_last_month_m3": 34.1,
  "electricity_monthly_history": [
    {"period": "2026-03-01", "value": 572.0, "status": "Actual"}
  ],
  "water_monthly_history": [
    {"period": "2026-03-01", "value": 34.1, "status": "Estimated"}
  ],
  "electricity_daily_history": [
    {"period": "2026-04-11", "value": 15.265, "status": null},
    {"period": "2026-04-12", "value": 19.967, "status": null}
  ],
  "electricity_hourly_history": [
    {"period": "2026-04-12 00:00:00", "value": 1.452, "status": null},
    {"period": "2026-04-12 00:30:00", "value": 1.356, "status": null}
  ]
}
```

### Why Some Values Can Be `null`

`null` does not always mean the parser failed. In practice it can mean:

- SP has not published the current month yet in the monthly export
- the current endpoint does not expose that utility at that granularity
- the source dataset exists for electricity but not for water
- the live JSON payload is missing the field and the CSV fallback also does not
  contain a usable value

Notes on real SP data:

- monthly files may not contain the current in-progress month yet
- electricity monthly rows can exist while water monthly rows lag, or vice versa
- monthly rows include `Status` such as `Actual` or `Estimated`
- daily exports are separate from half-hourly exports
- hourly exports appear to be electricity-only in the current SP format
- daily exports appear to be electricity-only in the current SP format

## Using This In Home Assistant

The library is usable from a custom integration. Read this section before
building one — the login flow, not the data parsing, is what makes or breaks it.

### Check This First: Does SP Return A Refresh Token?

This is the one thing worth confirming before writing any integration code.

The login flow is browser + captcha + OTP. That is fine once, during config
flow, but Home Assistant polls for months and Auth0 access tokens typically last
hours. Without a refresh token every expiry drops the integration into a broken
state that only a human pasting a callback URL can fix.

The library requests the `offline_access` scope, so Auth0 should return a
refresh token — but this is a private SP deployment and that is not guaranteed.
Verify against your own account:

```bash
uv run sp-services-e2e
```

The CLI prints whether a refresh token was returned and when the access token
expires. If it reports one, the unattended path below works. If it does not, no
amount of integration code fixes it: plan for a reauth flow that walks the user
through the browser login again whenever the token dies.

### Token Handling

Persist the refresh token in the config entry and refresh before expiry:

```python
tokens = client.token_set          # after exchange_callback_url / verify_otp
entry.data["refresh_token"]        # persist tokens.refresh_token here

# later, before each update
if tokens.is_expired():
    tokens = await client.refresh_access_token(stored_refresh_token)
```

`refresh_access_token` keeps the existing refresh token when Auth0 does not
rotate it, so a stored value stays valid across refreshes. `is_expired()`
applies a 60-second leeway by default.

### Coordinator And Error Mapping

`fetch_usage` is a single async call returning one immutable snapshot, which
maps cleanly onto a `DataUpdateCoordinator`. The exception hierarchy lines up
with Home Assistant's:

```python
try:
    return await client.fetch_usage(token, account_no=entry.data["account_no"])
except SessionExpiredError as err:
    raise ConfigEntryAuthFailed from err
except SpServicesError as err:
    raise UpdateFailed(str(err)) from err
```

A CSV export that fails upstream degrades to `None` fields rather than failing
the whole update, so one flaky endpoint will not red-line every entity.

### Poll Interval

One `fetch_usage` makes up to six upstream requests against private endpoints,
and SP refreshes this data slowly. Poll every 15–30 minutes — not the
coordinator default.

### Entities Worth Creating

- `water_today_m3` is always `None`. SP does not publish same-day water usage in
  any observed export, so do not create that sensor.
- `electricity_month_kwh` and `water_month_m3` are `null` until SP publishes the
  in-progress month. Model them as unavailable, not zero.
- `UsagePoint.value` is optional; a period SP published with a status but no
  figure comes back with `value=None`.

### Energy Dashboard Statistics

The history series are consumption per bucket, not monotonic meter readings.
Convert to running sums before feeding `async_add_external_statistics`, and
localize the period strings first — they are naive strings in Singapore local
time, exactly as SP publishes them.

### HTTP Client

This library uses `requests` on a worker thread (via `asyncio.to_thread`), so it
never blocks the event loop, but it manages its own session and will not share
Home Assistant's `aiohttp` session. That is fine for a custom or HACS
integration. Home Assistant Core review prefers a natively async client such as
`aiohttp` or `httpx`, so expect pushback if you submit upstream.

### Upstream Stability

These are private endpoints with no compatibility guarantee. Treat a parsing gap
as `None` rather than an entity error, and expect to update the library when SP
changes its export shapes.

## Testing

The automated test suite focuses on deterministic parsing and auth helpers, not
live portal access.

Run it with:

```bash
uv run pytest
```

Current coverage includes:

- Auth0 callback and CSRF extraction helpers
- monthly CSV parsing
- monthly history parsing with `Status`
- daily electricity history parsing
- half-hourly electricity history parsing
- current-day fallback selection from daily history

## Development

Common commands:

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv run sp-services-e2e
uv run sp-services-e2e --save-json usage.json
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
