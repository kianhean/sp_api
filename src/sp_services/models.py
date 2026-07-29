"""Data models for the SP Services client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


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
    ``offline_access`` scope. Callers that need unattended re-authentication
    (for example a long-running Home Assistant integration) should persist it
    and pass it to :meth:`SpServicesClient.refresh_access_token`.
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
class LoginChallenge:
    """Ephemeral data returned after a successful username/password login step."""

    oauth_state: str
    login_state: str
    code_verifier: str
    csrf: str
    phone_number: str | None = None
    transaction_id: str | None = None
