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
- HTTP client: `niquests`
- lint: `ruff`
- tests: `pytest`

## Repository Layout

- `src/sp_services/client.py`
  Core auth, token exchange, API requests, and parsing helpers.
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
```

Notes:

- `SP_SERVICES_USERNAME` and `SP_SERVICES_PASSWORD` are optional if you use the
  browser-assisted flow.
- `SP_SERVICES_DEBUG=1` enables more verbose auth error output.

## Authentication Notes

SP Services currently enforces captcha on the direct Auth0 username/password
login endpoint. Because of that, pure headless login is not reliable.

The supported fallback is:

1. start the CLI
2. let it print an Auth0 authorize URL
3. complete captcha, login, and OTP in a browser
4. paste the final callback URL back into the CLI

The library then exchanges the authorization code for an access token and
continues with API calls.

## Public API

Primary entrypoint:

- `sp_services.SpServicesClient`

Main methods:

- `login(username, password)`
- `verify_otp(otp, login_challenge=None)`
- `begin_browser_login()`
- `exchange_callback_url(callback_url)`
- `fetch_usage(token)`
- `close()`

Primary models:

- `UsageData`
- `UsagePoint`
- `LoginChallenge`

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

## Future Work

Reasonable next improvements:

- persist refresh tokens if SP/Auth0 reliably returns them
- expose water daily history if SP exposes it through a separate endpoint
- add typed raw-response capture hooks for debugging upstream changes
- add optional token caching for local CLI reuse
