# CLAUDE.md

## Project

`sp-services` is an unofficial async Python library for accessing SP Services
household electricity and water usage data.

The library currently supports:

- browser-assisted Auth0 login with OTP
- access token exchange via callback URL
- household usage fetches from SP private APIs
- current usage summary fields
- monthly historical electricity and water series when available
- daily electricity history from CSV exports
- half-hourly electricity history from CSV exports

## Stack

- Python `>=3.11`
- package manager: `uv`
- build backend: `setuptools`
- HTTP client: `requests`, dispatched via `asyncio.to_thread` so the public
  async API never blocks the caller's event loop
- lint: `ruff`
- tests: `pytest`

## Repository Layout

- `src/sp_services/client.py`
  Core auth, token exchange, API requests, and parsing helpers.
- `src/sp_services/browser.py`
  Stdlib-only Chrome DevTools Protocol helper that captures the Auth0 callback
  from the user's own browser.
- `src/sp_services/models.py`
  Public data models returned by the client.
- `src/sp_services/exceptions.py`
  Package exception hierarchy.
- `src/sp_services/cli.py`
  Manual E2E CLI for real-account verification.
- `tests/test_client.py`
  Parser and helper tests.
- `.github/workflows/ci.yml`
  CI for lint and tests.

## Local Setup

Install runtime dependencies:

```bash
uv sync
```

Install runtime + dev dependencies:

```bash
uv sync --group dev
```

## Common Commands

Run the interactive E2E CLI:

```bash
uv run sp-services-e2e
```

Run lint:

```bash
uv run ruff check .
```

Run tests:

```bash
uv run pytest
```

## Environment Variables

The CLI reads `.env` from the repository root.

Supported values:

```dotenv
SP_SERVICES_USERNAME=
SP_SERVICES_PASSWORD=
SP_SERVICES_DEBUG=0
SP_SERVICES_BROWSER=
```

Notes:

- `SP_SERVICES_USERNAME` and `SP_SERVICES_PASSWORD` are optional if you use the
  browser-assisted flow.
- `SP_SERVICES_DEBUG=1` enables more verbose auth error output.
- `SP_SERVICES_BROWSER` overrides browser discovery for automatic login.

## Authentication Notes

SP Services currently enforces captcha on the direct Auth0 username/password
login endpoint. Because of that, pure headless login is not reliable.

The supported flow is `login_with_browser()`:

1. it opens an installed Chrome/Edge/Chromium/Brave with a throwaway profile
2. the user completes captcha, login, and OTP in that window
3. the callback request is intercepted over the DevTools Protocol, so nothing
   is pasted and SP's page never receives the code

The library then exchanges the authorization code for an access token and
continues with API calls. If no browser is found, the CLI falls back to
printing the authorize URL and asking for the callback URL to be pasted.

The CLI caches tokens in `.sp_services_tokens.json` and reuses the refresh
token, so later runs skip the login. Never print or log refresh tokens.

Keep `browser.py` dependency-free (no Playwright/Selenium): it relies on the
user's own browser so captcha is not tripped by automation fingerprints.

## Public API

Primary entrypoint:

- `sp_services.SpServicesClient`

Main methods:

- `login(username, password)`
- `verify_otp(otp, login_challenge=None)`
- `login_with_browser(browser_path=None, timeout=600)`
- `begin_browser_login()`
- `exchange_callback_url(callback_url)`
- `refresh_access_token(refresh_token=None)`
- `fetch_usage(token, account_no=None)`
- `close()`

Primary properties:

- `token_set`

Primary models:

- `UsageData`
- `UsagePoint`
- `LoginChallenge`
- `TokenSet`

## UsageData Shape

`UsageData` currently exposes:

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

`UsagePoint` currently exposes:

- `period`
- `value`
- `status`

## Testing Guidance

Automated tests should stay deterministic and avoid live SP network calls.

Good test targets:

- CSV parsers
- daily CSV shape handling
- hourly CSV shape handling
- monthly CSV status preservation
- auth helper parsing
- URL/code extraction
- response-shape normalization

Avoid adding CI tests that require:

- real SP credentials
- OTP input
- captcha solving
- live upstream endpoint stability

## E2E Verification

Use the CLI for real verification:

```bash
uv run sp-services-e2e
```

If captcha is required, the CLI will switch to browser-assisted login
automatically.

## Implementation Constraints

- Prefer extending CSV-based parsing when SP JSON payloads are unstable.
- Do not assume Auth0 page structure is stable; verify against live responses.
- Keep public models backward-compatible when possible.
- Add tests for any parsing change before considering the change complete.
- Monthly exports may lag the in-progress month; do not assume the current month
  is present.
- Daily and half-hourly exports are different shapes and should stay modeled
  separately.
- Monthly rows can contain `Status` values such as `Actual` or `Estimated`;
  preserve them in public history output.
- All date logic must use `_now_sp()` (`Asia/Singapore`), never `datetime.now()`
  on the host clock.
- Period labels must be matched with `_period_matches_month` /
  `_period_matches_day`, never with bare digit substrings.
- The OAuth `state` must always be verified when extracting an authorization
  code; never accept a code from a callback that omits it.
- `UsagePoint.value` is optional; periods SP publishes with a status but no
  figure are preserved with `value=None`.

## Future Work

Reasonable next improvements:

- persist refresh tokens if SP/Auth0 reliably returns them
- expose water daily history if SP exposes it through a separate endpoint
- add typed raw-response capture hooks for debugging upstream changes
