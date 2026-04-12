"""Data models for the SP Services client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class UsagePoint:
    """Single historical usage data point."""

    period: str
    value: float
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
    electricity_hourly_history: list[UsagePoint] | None = None


@dataclass(slots=True)
class LoginChallenge:
    """Ephemeral data returned after a successful username/password login step."""

    oauth_state: str
    login_state: str
    code_verifier: str
    csrf: str
    phone_number: str | None = None
    transaction_id: str | None = None
