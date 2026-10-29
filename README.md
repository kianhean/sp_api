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
- automatic browser login: complete captcha and OTP in your own Chrome/Edge
  window, with no callback URL to copy
- silent renewal from the saved Auth0 session, so later runs and long-running
  clients get new access tokens without captcha or OTP
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
# optional: browser used for automatic login (Chrome, Edge, Chromium, Brave)
SP_SERVICES_BROWSER=
```

Notes:

- `.env` is gitignored.
- `SP_SERVICES_DEBUG=1` enables more verbose auth failure output.
- username and password are optional. Without them the CLI goes straight to
  the browser login, because SP enforces captcha on the direct login anyway.
- `SP_SERVICES_BROWSER` is only needed if your browser is installed somewhere
  unusual.

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

- reuse the access token saved by a previous run in `.sp_services_tokens.json`
  (gitignored, owner-only permissions) while it is still valid
- once it has expired, renew silently from the Auth0 session saved in the same
  file, with no browser, captcha or OTP
- otherwise try the direct login flow if credentials are in `.env`
- fall back to the automatic browser login when captcha is required
- exchange the resulting authorization code and save the new tokens and session
- fetch and print usage data as JSON

Use `--token-cache PATH` to keep the tokens elsewhere, or `--no-token-cache` to
neither read nor write them.

Two more modes skip the usage fetch:

- `--renew` forces one silent renewal and prints the new access-token expiry
  (never the token).
- `--export-session` logs in if needed and prints the session string for
  `SessionState.from_string()`. It is a credential: treat it like a password.

### Browser-Assisted Login

SP Services currently enforces captcha on the direct Auth0
`usernamepassword/login` endpoint, so headless login is not always usable.

When that happens, the CLI will:

1. open a Chrome, Edge, Chromium or Brave window with a throwaway profile
2. let you complete captcha, login, and OTP in that window
3. capture the callback automatically and show "Login complete"
4. exchange that callback for an access token
5. continue fetching usage data

Nothing needs to be copied or pasted. The window is driven over the Chrome
DevTools Protocol using only the Python standard library, so no extra packages
are required. The callback request is intercepted inside the browser, so the
authorization code never reaches SP's own page. The temporary profile is
deleted afterwards.

If no supported browser is found, the CLI falls back to the manual flow below.

### Manual Fallback: Capturing The Callback URL

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

### Automatic Browser Flow

```python
import asyncio

from sp_services import SpServicesClient


async def main() -> None:
    async with SpServicesClient() as client:
        token = await client.login_with_browser()
        usage = await client.fetch_usage(token)
        print(usage)


asyncio.run(main())
```

`login_with_browser()` raises `BrowserNotFoundError` when no supported browser
is installed; use the manual flow below in that case.

### Silent Renewal From A Saved Session

SP's Auth0 tenant returns no refresh token, so the Auth0 session cookie is what
keeps a client logged in. Export it after a login, store it, and renew from it
later with no browser, captcha or OTP:

```python
from sp_services import SessionState, SessionExpiredError, SpServicesClient

async with SpServicesClient() as client:
    await client.login_with_browser()
    saved = client.export_session().to_string()   # store this like a password

# later, in another process
async with SpServicesClient(session_state=SessionState.from_string(saved)) as client:
    try:
        tokens = await client.renew_silently()
    except SessionExpiredError:
        ...  # the Auth0 session ended; a person has to log in again
    saved = client.export_session().to_string()   # persist again: the session slides
    usage = await client.fetch_usage(tokens.access_token)
```

`renew_silently()` calls `/authorize` with `prompt=none` and follows the
redirects itself until the SP callback, checks the `state`, and exchanges the
code. It raises `SessionExpiredError` when Auth0 answers `login_required`,
`consent_required` or `interaction_required`, or when no session cookies are
loaded, and `ApiError` for any other unexpected answer.

### Manual Browser Flow

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
- `SpServicesClient.login_with_browser(browser_path=None, timeout=600)`
- `SpServicesClient.begin_browser_login()`
- `SpServicesClient.exchange_callback_url(callback_url)`
- `SpServicesClient.refresh_access_token(refresh_token=None)`
- `SpServicesClient.renew_silently()`
- `SpServicesClient.export_session()`
- `SpServicesClient.fetch_usage(token, account_no=None)`
- `SpServicesClient.token_set`

`SpServicesClient(debug=False, session_state=None)` loads a saved
`SessionState` so `renew_silently()` can use it.

`fetch_usage` uses the first linked account unless `account_no` is given.

### Models

- `LoginChallenge`
- `SessionCookie`
- `SessionState` (`to_string()` / `from_string()`)
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

The library requests the `offline_access` scope, but SP's Auth0 tenant does not
return a refresh token (checked 2026-10-29), so `refresh_access_token` only
helps if SP enables refresh tokens later. Use the saved session and
`renew_silently()` instead; see "Silent Renewal From A Saved Session" above.

Measured lifetimes (2026-10-29, against a real account):

- access token: 1 hour (`expires_in` 3600)
- Auth0 session (`auth0` / `auth0_compat` cookies): expires 3 days after it
  was last used. Every login and every silent renewal sets the cookies'
  expiry 3 days ahead again, so a client that renews at least once every
  3 days keeps the session alive. Auth0 may also have an absolute maximum
  lifetime; that has not been measured yet.

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

### Staying Logged In

The login is browser + captcha + OTP, which is fine once during the config flow.
SP returns no refresh token, so store the **session string** instead:

```python
# config flow, after the login
entry_data["session"] = client.export_session().to_string()

# setup / each update, when the access token is missing or expired
client = SpServicesClient(session_state=SessionState.from_string(entry.data["session"]))
tokens = await client.renew_silently()
hass.config_entries.async_update_entry(
    entry, data={**entry.data, "session": client.export_session().to_string()}
)
```

- Access tokens last 1 hour, so renew when `client.token_set.is_expired()`
  is true (60-second leeway by default).
- The Auth0 session expires 3 days after its last use. Each renewal pushes
  that back, so an integration polling every 15–30 minutes never gets near
  the limit. Write the session string back after each renewal anyway, so the
  stored cookie expiry stays current.
- `renew_silently()` raises `SessionExpiredError` when the session has ended.
  Map it to `ConfigEntryAuthFailed`, so Home Assistant starts a reauth flow,
  then backfill the missed history once the user is logged in again.
- The session string is a credential, so keep it in the config entry, never in
  logs or diagnostics.

`uv run sp-services-e2e --export-session` prints the session string after a
login, for pasting into an integration while you develop it.

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
