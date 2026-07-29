# Changelog

## Unreleased

### Added

- `TokenSet` model plus `SpServicesClient.token_set` and
  `refresh_access_token()` for unattended re-authentication
- `fetch_usage(token, account_no=...)` to select a specific linked account
- Guidance for building a Home Assistant integration on top of the library

### Changed

- HTTP transport moved from `niquests` to `requests`, dispatched through
  `asyncio.to_thread` so the async API stays non-blocking
- All date logic is pinned to `Asia/Singapore` instead of the host clock, and
  `UsageData.last_updated` is now timezone-aware
- `UsagePoint.value` is now optional, so a period SP published with a status but
  no figure is preserved instead of dropped
- Every CSV export fetch now degrades to empty on upstream failure; expired
  sessions still propagate
- Transport failures now raise `ApiError` instead of escaping as library errors

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
