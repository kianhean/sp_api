# Changelog

## Unreleased

### Added

- `SpServicesClient.login_with_browser()`: opens the user's own Chrome, Edge,
  Chromium or Brave and captures the Auth0 callback automatically, so no URL
  has to be pasted. Uses only the standard library (Chrome DevTools Protocol).
- `BrowserNotFoundError` when no supported browser is installed
- The E2E CLI saves tokens to `.sp_services_tokens.json` (owner-only) and reuses
  the refresh token on later runs; `--token-cache` / `--no-token-cache`

### Changed

- The E2E CLI no longer prompts for a username and password; it uses them only
  when they are set in `.env`, and otherwise goes straight to the browser login

### Fixed

- A callback `state` containing non-ASCII characters raised `TypeError` instead
  of being rejected
- A non-JSON response body raised `requests.JSONDecodeError` instead of
  `ApiError`
- The `Set-Cookie` CSRF fallback matched any cookie ending in `_csrf`
- An unrecognized CSV section heading not preceded by a blank line had its rows
  appended to the previous section
- Today's electricity from the half-hourly CSV and the daily history only
  matched ISO dates; day-first periods such as `29/10/2026` are now matched

## 2.0.0 - 2026-07-29

### Breaking

- The runtime dependency is now `requests` instead of `niquests`. The public API
  is unchanged and still async; calls are dispatched through
  `asyncio.to_thread` so they do not block the caller's event loop.
- `UsageData.last_updated` is now timezone-aware (`Asia/Singapore`). Comparing
  it against a naive `datetime` will raise.
- `UsagePoint.value` is now `float | None`. Consumers doing arithmetic on it
  directly must handle `None`.
- An authorization code is only accepted when the OAuth `state` is present and
  matches. Callback URLs without a `state` parameter are now rejected.
- Summary values may differ from 1.0.0 where period matching was previously
  wrong; see Fixed below.

### Added

- `TokenSet` model plus `SpServicesClient.token_set` and
  `refresh_access_token()` for unattended re-authentication
- `fetch_usage(token, account_no=...)` to select a specific linked account
- Guidance for building a Home Assistant integration on top of the library

### Changed

- All "today" and "this month" decisions are made in `Asia/Singapore` rather
  than on the host clock, so a UTC-clocked host no longer reports the previous
  day's usage between midnight and 08:00 SGT
- A period SP published with a status but no figure is preserved with
  `value=None` instead of being dropped from history
- Every CSV export fetch degrades to empty on upstream failure; expired
  sessions still propagate
- Transport failures raise `ApiError` instead of escaping as library errors

### Fixed

- Monthly and daily period matching no longer matches on a bare digit
  substring, which could return a different month's or day's value
- An unrecognized CSV section heading no longer merges its rows into the
  previously parsed section
- Monthly values for a month are now chosen by parsed date rather than string
  sort, so mixed `2026-04-28` and `Apr 2026` labels resolve correctly
- The OAuth `state` is now required and verified when extracting an
  authorization code, including in the response-body fallback
- The MFA send-SMS request now sends a JSON body matching its content type
- `tests/test_client.py` no longer skips the async flow tests when run directly

### Removed

- Unused `pytest-asyncio` development dependency
- Committed `.coverage` artifact, now gitignored

## 1.0.0

First stable public release of `sp-services`.

### Added

- Async Python client for SP Services household usage data
- Auth0 authorization-code flow support
- OTP verification support for SP login flows
- Browser-assisted login fallback for captcha-protected Auth0 login
- Interactive E2E CLI for real-account verification
- Optional JSON export from the CLI with `--save-json`
- Electricity usage summary fields for current day and latest available month
- Water usage summary fields for latest available month where published
- Monthly electricity history with SP-provided status metadata
- Monthly water history with SP-provided status metadata
- Daily electricity history from SP CSV exports
- Half-hourly electricity history from SP CSV exports
- Structured public data models for summary and historical usage points
- MIT license
- CI with `ruff` and `pytest`
- GitHub project scaffolding:
  issue templates, PR template, contributing guide, security policy, and code of conduct

### Changed

- Auth bootstrap updated to read CSRF tokens from current Auth0 cookie/header behavior
- Browser login workflow documented in detail, including how to capture callback URLs from the network tab
- README expanded with setup, E2E flow, output field explanations, and testing guidance
- Package metadata updated for MIT licensing and GitHub project URLs

### Fixed

- Daily history no longer includes future placeholder `0.0` rows from SP exports
- Packaging metadata corrected for modern setuptools license handling
- Parsing aligned with real SP monthly, daily, and half-hourly CSV export shapes

### Notes

- SP Services upstream endpoints are private and may change without notice
- Current-month monthly totals may be unavailable until SP publishes them in the monthly export
- Water daily history is not currently exposed by the library because it was not available in the observed SP export format
