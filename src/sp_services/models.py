"""Data models for the SP Services client."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(slots=True)
class UsagePoint:
    """Single historical usage data point.

    ``value`` is ``None`` when SP published the period (usually with a
    ``status`` such as ``Estimated``) but no consumption figure for it.
    """

    period: str
    value: float | None
    status: str | None = None


@dataclass(slots=True)
class UsageData:
    """Household utility consumption data fetched from SP Services."""

    electricity_today_kwh: float | None
    electricity_month_kwh: float | None
    water_today_m3: float | None
    water_month_m3: float | None
    account_no: str | None
    last_updated: datetime
    electricity_last_month_kwh: float | None = None
    water_last_month_m3: float | None = None
    electricity_monthly_history: list[UsagePoint] | None = None
    water_monthly_history: list[UsagePoint] | None = None
    electricity_daily_history: list[UsagePoint] | None = None
    electricity_hourly_history: list[UsagePoint] | None = None


@dataclass(slots=True)
class TokenSet:
    """OAuth tokens returned by the Auth0 token endpoint.

    ``refresh_token`` is only present when SP/Auth0 honours the
    ``offline_access`` scope, which SP's tenant currently does not. For
    unattended re-authentication (for example a long-running Home Assistant
    integration) persist a :class:`SessionState` and call
    :meth:`SpServicesClient.renew_silently` instead.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    token_type: str = "Bearer"
    scope: str | None = None

    def is_expired(self, *, leeway_seconds: int = 60) -> bool:
        """Return whether the access token is expired or about to expire."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(
            seconds=leeway_seconds
        )


@dataclass(slots=True)
class SessionCookie:
    """One Auth0 session cookie. ``expires`` is a Unix timestamp, or ``None``
    for a browser-session cookie."""

    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    secure: bool = True
    http_only: bool = True


@dataclass(slots=True)
class SessionState:
    """The Auth0 login session, as cookies for ``identity.spdigital.auth0.com``.

    SP's Auth0 tenant issues no refresh token, so this is what lets a
    long-running client get new access tokens without captcha or OTP: pass it
    back to :class:`SpServicesClient` and call
    :meth:`SpServicesClient.renew_silently`. Anyone holding it can act as the
    logged-in user, so store it like a password.
    """

    cookies: list[SessionCookie] = field(default_factory=list)

    def to_string(self) -> str:
        """Serialize to compact base64url JSON for storage in a config entry."""
        payload = json.dumps(
            [asdict(cookie) for cookie in self.cookies], separators=(",", ":")
        )
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")

    @classmethod
    def from_string(cls, value: str) -> SessionState:
        """Parse a string made by :meth:`to_string`; raises ``ValueError``."""
        try:
            raw = base64.urlsafe_b64decode(value.strip().encode("ascii"))
            items: Any = json.loads(raw.decode("utf-8"))
        except (binascii.Error, UnicodeError, json.JSONDecodeError) as err:
            raise ValueError("not a valid SP Services session string") from err
        if not isinstance(items, list):
            raise ValueError("not a valid SP Services session string")
        try:
            cookies = [SessionCookie(**item) for item in items]
        except TypeError as err:
            raise ValueError("not a valid SP Services session string") from err
        return cls(cookies=cookies)


@dataclass(slots=True)
class LoginChallenge:
    """Ephemeral data returned after a successful username/password login step."""

    oauth_state: str
    login_state: str
    code_verifier: str
    csrf: str
    phone_number: str | None = None
    transaction_id: str | None = None
