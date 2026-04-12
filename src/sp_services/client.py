"""Async client for SP Services household energy and water usage data."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import niquests

from .exceptions import ApiError, AuthenticationError, SessionExpiredError
from .models import LoginChallenge, UsageData, UsagePoint

_BASE_URL = "https://services.spservices.sg"
_AUTH0_BASE_URL = "https://identity.spdigital.auth0.com"
_SKALBOX_BASE_URL = "https://c-api-gateway.tkg.spdigital.io/skalbox"

_AUTH0_AUTHORIZE_URL = f"{_AUTH0_BASE_URL}/authorize"
_AUTH0_CHALLENGE_URL = f"{_AUTH0_BASE_URL}/usernamepassword/challenge"
_AUTH0_LOGIN_URL = f"{_AUTH0_BASE_URL}/usernamepassword/login"
_AUTH0_MFA_START_URL = f"{_AUTH0_BASE_URL}/appliance-mfa/api/start-flow"
_AUTH0_MFA_STATE_URL = f"{_AUTH0_BASE_URL}/appliance-mfa/api/transaction-state"
_AUTH0_MFA_SEND_SMS_URL = f"{_AUTH0_BASE_URL}/appliance-mfa/api/send-sms"
_AUTH0_MFA_VERIFY_URL = f"{_AUTH0_BASE_URL}/appliance-mfa/api/verify-otp"
_AUTH0_TOKEN_URL = f"{_AUTH0_BASE_URL}/oauth/token"

_SKALBOX_API_URL = f"{_SKALBOX_BASE_URL}/api"
_SKALBOX_DAILY_CSV_URL = f"{_SKALBOX_BASE_URL}/private/charts/csv/dailyHourly"
_SKALBOX_MONTHLY_CSV_URL = f"{_SKALBOX_BASE_URL}/private/charts/csv/monthly"

_AUTH0_CLIENT_ID = "0I6XpXThehIU3SgaSbzraCgekkHg2rJH"
_AUTH0_REDIRECT_URI = f"{_BASE_URL}/callback?fromLogin=true"
_AUTH0_SCOPE = "openid profile email offline_access"
_AUTH0_AUDIENCE = "https://profile.up.spdigital.sg/"
_AUTH0_CONNECTION = "Username-Password-Authentication"
_AUTH0_TENANT = "identity"

_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-SG,en;q=0.9",
    "Origin": _BASE_URL,
    "Referer": f"{_BASE_URL}/",
}

_AUTH_HEADERS = {**_HEADERS, "Content-Type": "application/json"}

_CSRF_RE = re.compile(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']')
_INLINE_CSRF_RE = re.compile(r'"_csrf"\s*:\s*"([^"]+)"')
_CSRF_TOKEN_RE = re.compile(r'"csrf(?:_token|Token)?"\s*:\s*"([^"]+)"')
_CONFIG_BLOB_RE = re.compile(r"@@config@@([^\"'<\s]+)")
_SET_COOKIE_CSRF_RE = re.compile(r"(?:^|;\s*|,?\s*)_csrf=([^;,\s]+)")


@dataclass(slots=True)
class _AuthContext:
    """Ephemeral Auth0 state carried across the login and OTP steps."""

    oauth_state: str
    login_state: str
    nonce: str
    code_verifier: str
    csrf: str
    phone_number: str | None = None
    transaction_id: str | None = None


class SpServicesClient:
    """Low-level async HTTP client for the SP Services portal."""

    def __init__(self, *, debug: bool = False) -> None:
        self._session: niquests.AsyncSession = niquests.AsyncSession()
        self._auth_context: _AuthContext | None = None
        self._debug = debug

    async def __aenter__(self) -> "SpServicesClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def login(self, username: str, password: str) -> LoginChallenge:
        """Submit credentials and trigger the SMS OTP flow."""
        context = await self._bootstrap_auth()
        await self._post_json(_AUTH0_CHALLENGE_URL, {"state": context.login_state})

        resp = await self._session.post(
            _AUTH0_LOGIN_URL,
            json={
                "client_id": _AUTH0_CLIENT_ID,
                "redirect_uri": _AUTH0_REDIRECT_URI,
                "tenant": _AUTH0_TENANT,
                "response_type": "code",
                "scope": _AUTH0_SCOPE,
                "audience": _AUTH0_AUDIENCE,
                "_csrf": context.csrf,
                "state": context.login_state,
                "_intstate": "deprecated",
                "nonce": context.nonce,
                "username": username,
                "password": password,
                "force_mfa": False,
                "connection": _AUTH0_CONNECTION,
            },
            headers=_AUTH_HEADERS,
            timeout=30,
        )
        if resp.status_code == 401:
            raise AuthenticationError(self._format_login_error(resp))
        if not resp.ok:
            raise ApiError(f"SP Services login returned HTTP {resp.status_code}")

        await self._post_json(_AUTH0_MFA_START_URL, {"state_transport": "polling"})
        txn = await self._post_json(_AUTH0_MFA_STATE_URL, {})
        context.transaction_id = txn.get("id")
        enrollment = txn.get("enrollment", {})
        context.phone_number = enrollment.get("phone_number")

        send_sms_resp = await self._session.post(
            _AUTH0_MFA_SEND_SMS_URL,
            headers=_AUTH_HEADERS,
            timeout=30,
        )
        if not send_sms_resp.ok:
            raise ApiError(
                f"SP Services SMS OTP trigger returned HTTP {send_sms_resp.status_code}"
            )

        self._auth_context = context
        return LoginChallenge(
            oauth_state=context.oauth_state,
            login_state=context.login_state,
            code_verifier=context.code_verifier,
            csrf=context.csrf,
            phone_number=context.phone_number,
            transaction_id=context.transaction_id,
        )

    async def begin_browser_login(self) -> str:
        """Prepare a browser-based login flow and return the authorize URL."""
        context = await self._bootstrap_auth()
        self._auth_context = context
        return self._authorize_url(
            oauth_state=context.oauth_state,
            nonce=context.nonce,
            code_verifier=context.code_verifier,
        )

    async def exchange_callback_url(self, callback_url: str) -> str:
        """Exchange an Auth0 callback URL for an access token."""
        context = self._auth_context
        if context is None or not context.oauth_state or not context.code_verifier:
            raise AuthenticationError("login session is missing; restart login")

        code = _code_from_url(callback_url.strip(), context.oauth_state)
        if not code:
            raise AuthenticationError("callback URL did not contain a valid code")

        token_body = await self._post_json(
            _AUTH0_TOKEN_URL,
            {
                "client_id": _AUTH0_CLIENT_ID,
                "code_verifier": context.code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _AUTH0_REDIRECT_URI,
            },
        )
        token = token_body.get("access_token")
        if not token:
            raise ApiError("SP Services token exchange returned no access token")
        return str(token)

    async def verify_otp(
        self, otp: str, login_challenge: LoginChallenge | None = None
    ) -> str:
        """Submit OTP and exchange it for an access token."""
        context = self._auth_context
        if context is None and login_challenge:
            context = _AuthContext(
                oauth_state=login_challenge.oauth_state,
                login_state=login_challenge.login_state,
                nonce="",
                code_verifier=login_challenge.code_verifier,
                csrf=login_challenge.csrf,
                phone_number=login_challenge.phone_number,
                transaction_id=login_challenge.transaction_id,
            )
        if context is None or not context.oauth_state or not context.code_verifier:
            raise AuthenticationError("login session is missing; restart login")

        resp = await self._session.post(
            _AUTH0_MFA_VERIFY_URL,
            json={"type": "manual_input", "code": otp.strip()},
            headers=_AUTH_HEADERS,
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code == 401:
            raise AuthenticationError("invalid otp")
        if not resp.ok:
            raise ApiError(f"SP Services OTP verify returned HTTP {resp.status_code}")

        code = self._extract_authorization_code(resp, context.oauth_state)
        if not code:
            raise ApiError("SP Services OTP flow returned no authorization code")

        token_body = await self._post_json(
            _AUTH0_TOKEN_URL,
            {
                "client_id": _AUTH0_CLIENT_ID,
                "code_verifier": context.code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _AUTH0_REDIRECT_URI,
            },
        )
        token = token_body.get("access_token")
        if not token:
            raise ApiError("SP Services token exchange returned no access token")
        return str(token)

    async def fetch_usage(self, token: str) -> UsageData:
        """Fetch today's and this month's electricity and water usage."""
        accounts_payload = await self._skalbox_call(
            {"user:getAccounts": {"token": token}},
            token=token,
        )
        account = _extract_primary_account(accounts_payload)
        if account is None:
            raise ApiError("SP Services returned no linked utility accounts")

        account_no = str(account.get("accountNo") or "")
        ebs_premise_no = str(
            account.get("premiseNo")
            or account.get("ebsPremiseNo")
            or account.get("premises_id")
            or ""
        )
        mssl_premise_no = str(account.get("msslPremiseNo") or "")
        if not account_no or not ebs_premise_no:
            raise ApiError("SP Services account payload is missing premise details")

        now = datetime.now()
        monthly_payload = await self._skalbox_call(
            {
                "charts:monthly": {
                    "ebsPremiseNo": ebs_premise_no,
                    "msslPremiseNo": mssl_premise_no,
                    "consumptionValue": str(now.year),
                }
            },
            token=token,
        )
        hourly_payload = await self._skalbox_call(
            {
                "charts:hourly": {
                    "accountNos": [account_no],
                    "date": now.date().isoformat(),
                }
            },
            token=token,
            allow_business_no_data=True,
        )

        electricity_month, water_month = _parse_monthly_usage(monthly_payload, now)
        electricity_today, _ = _parse_daily_usage(hourly_payload, now)
        water_today: float | None = None
        electricity_last_month_kwh: float | None = None
        water_last_month_m3: float | None = None
        electricity_daily_history: list[UsagePoint] = []
        electricity_hourly_history: list[UsagePoint] = []

        monthly_csv = await self._post_text(
            _SKALBOX_MONTHLY_CSV_URL,
            {
                "ebsPremiseNo": ebs_premise_no,
                "msslPremiseNo": mssl_premise_no,
                "accountNo": account_no,
            },
            token=token,
        )
        (
            csv_elec_month,
            csv_water_month,
            electricity_last_month_kwh,
            water_last_month_m3,
        ) = _parse_monthly_csv(monthly_csv, now)
        electricity_monthly_history, water_monthly_history = _parse_monthly_history(
            monthly_csv
        )
        if electricity_month is None or water_month is None:
            electricity_month = (
                electricity_month if electricity_month is not None else csv_elec_month
            )
            water_month = water_month if water_month is not None else csv_water_month

        daily_csv = await self._post_text(
            _SKALBOX_DAILY_CSV_URL,
            {"accountNos": [account_no], "consumptionBy": "hourCSV"},
            token=token,
        )
        csv_elec_today, _ = _parse_daily_csv(daily_csv, now)
        electricity_hourly_history = _parse_hourly_history(daily_csv)
        if electricity_today is None:
            electricity_today = (
                electricity_today if electricity_today is not None else csv_elec_today
            )

        try:
            daily_summary_csv = await self._post_text(
                _SKALBOX_DAILY_CSV_URL,
                {"accountNos": [account_no], "consumptionBy": "dayCSV"},
                token=token,
            )
        except ApiError:
            daily_summary_csv = ""
        electricity_daily_history = _parse_daily_history_csv(daily_summary_csv)
        if electricity_today is None and electricity_daily_history:
            electricity_today = _latest_value_for_day(electricity_daily_history, now)

        return UsageData(
            electricity_today_kwh=electricity_today,
            electricity_month_kwh=electricity_month,
            water_today_m3=water_today,
            water_month_m3=water_month,
            account_no=account_no,
            last_updated=now,
            electricity_last_month_kwh=electricity_last_month_kwh,
            water_last_month_m3=water_last_month_m3,
            electricity_monthly_history=electricity_monthly_history,
            water_monthly_history=water_monthly_history,
            electricity_daily_history=electricity_daily_history,
            electricity_hourly_history=electricity_hourly_history,
        )

    async def close(self) -> None:
        """Release the underlying HTTP session."""
        await self._session.close()

    async def _bootstrap_auth(self) -> _AuthContext:
        oauth_state = _random_token(32)
        nonce = _random_token(32)
        code_verifier = _random_token(48)
        params = self._authorize_params(
            oauth_state=oauth_state,
            nonce=nonce,
            code_verifier=code_verifier,
        )

        resp = await self._session.get(
            f"{_AUTH0_AUTHORIZE_URL}?{urlencode(params)}",
            headers=_HEADERS,
            timeout=30,
        )
        if not resp.ok:
            raise ApiError(
                f"SP Services authorize bootstrap returned HTTP {resp.status_code}"
            )

        html = getattr(resp, "text", "") or ""
        csrf = _extract_csrf_from_response(resp, html)
        if not csrf:
            raise ApiError(
                "SP Services authorize bootstrap returned no CSRF token in HTML, "
                "cookies, or headers"
            )
        login_state = _extract_login_state(str(getattr(resp, "url", ""))) or oauth_state

        return _AuthContext(
            oauth_state=oauth_state,
            login_state=login_state,
            nonce=nonce,
            code_verifier=code_verifier,
            csrf=csrf,
        )

    def _authorize_params(
        self, *, oauth_state: str, nonce: str, code_verifier: str
    ) -> dict[str, str]:
        challenge = _pkce_challenge(code_verifier)
        return {
            "client_id": _AUTH0_CLIENT_ID,
            "redirect_uri": _AUTH0_REDIRECT_URI,
            "response_type": "code",
            "scope": _AUTH0_SCOPE,
            "audience": _AUTH0_AUDIENCE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
            "state": oauth_state,
        }

    def _authorize_url(
        self, *, oauth_state: str, nonce: str, code_verifier: str
    ) -> str:
        params = self._authorize_params(
            oauth_state=oauth_state,
            nonce=nonce,
            code_verifier=code_verifier,
        )
        return f"{_AUTH0_AUTHORIZE_URL}?{urlencode(params)}"

    async def _post_json(
        self, url: str, payload: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        resp = await self._session.post(
            url,
            json=payload,
            headers=self._api_headers(token),
            timeout=30,
        )
        if resp.status_code == 401:
            raise SessionExpiredError("SP Services token expired")
        if not resp.ok:
            raise ApiError(f"SP Services request returned HTTP {resp.status_code}")
        body = resp.json()
        if isinstance(body, dict):
            return body
        raise ApiError("SP Services returned a non-JSON response")

    async def _skalbox_call(
        self,
        payload: dict[str, Any],
        token: str,
        allow_business_no_data: bool = False,
    ) -> dict[str, Any]:
        body = await self._post_json(_SKALBOX_API_URL, payload, token=token)
        status = body.get("status")
        if status == 100 or status is None:
            return body
        if allow_business_no_data and status == 150:
            return body
        raise ApiError(f"SP Services API returned unexpected status {status}")

    async def _post_text(
        self, url: str, payload: dict[str, Any], token: str | None = None
    ) -> str:
        resp = await self._session.post(
            url,
            json=payload,
            headers=self._api_headers(token),
            timeout=30,
        )
        if resp.status_code == 401:
            raise SessionExpiredError("SP Services token expired")
        if not resp.ok:
            raise ApiError(f"SP Services request returned HTTP {resp.status_code}")
        return str(getattr(resp, "text", "") or "")

    def _api_headers(self, token: str | None) -> dict[str, str]:
        headers = dict(_AUTH_HEADERS)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _format_login_error(self, resp: Any) -> str:
        message = "invalid username or password"
        details: list[str] = []

        try:
            body = resp.json()
        except Exception:
            body = None

        if isinstance(body, dict):
            for key in ("error", "error_description", "description", "message", "name"):
                value = body.get(key)
                if value:
                    details.append(f"{key}={value}")
        else:
            text = str(getattr(resp, "text", "") or "").strip()
            if text and self._debug:
                details.append(f"body={text[:500]}")

        if self._debug:
            details.append(f"status={resp.status_code}")
            details.append(f"url={getattr(resp, 'url', '')}")
            for prev in getattr(resp, "history", []) or []:
                details.append(f"history_url={getattr(prev, 'url', '')}")

        return f"{message} ({', '.join(details)})" if details else message

    @staticmethod
    def _extract_authorization_code(resp: Any, state: str) -> str | None:
        urls = [str(getattr(resp, "url", ""))]
        for prev in getattr(resp, "history", []) or []:
            urls.append(str(getattr(prev, "url", "")))
            headers = getattr(prev, "headers", {}) or {}
            location = headers.get("location") or headers.get("Location")
            if location:
                urls.append(str(location))

        body = getattr(resp, "text", "") or ""
        for candidate in urls:
            code = _code_from_url(candidate, state)
            if code:
                return code

        match = re.search(r"[?&]code=([^&\"' >]+)", body)
        return match.group(1) if match else None


def _random_token(length: int) -> str:
    return secrets.token_urlsafe(length)


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _extract_csrf(html: str) -> str | None:
    match = (
        _CSRF_RE.search(html)
        or _INLINE_CSRF_RE.search(html)
        or _CSRF_TOKEN_RE.search(html)
    )
    if match:
        return match.group(1)

    config_blob = _CONFIG_BLOB_RE.search(html)
    if not config_blob:
        return None

    try:
        decoded = base64.b64decode(config_blob.group(1)).decode("utf-8")
        config = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None

    for key in ("_csrf", "csrf", "csrfToken", "csrf_token"):
        value = config.get(key)
        if value:
            return str(value)
    return None


def _extract_csrf_from_response(resp: Any, html: str) -> str | None:
    csrf = _extract_csrf(html)
    if csrf:
        return csrf

    cookies = getattr(resp, "cookies", None)
    if cookies is not None:
        for key in ("_csrf", "csrf", "csrfToken", "csrf_token"):
            try:
                value = cookies.get(key)
            except Exception:
                value = None
            if value:
                return str(value)

    headers = getattr(resp, "headers", {}) or {}
    set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
    match = _SET_COOKIE_CSRF_RE.search(str(set_cookie))
    if match:
        return match.group(1)

    return None


def _extract_login_state(url: str) -> str | None:
    if not url:
        return None
    return parse_qs(urlparse(url).query).get("state", [None])[0]


def _code_from_url(url: str, expected_state: str) -> str | None:
    if not url:
        return None
    query = parse_qs(urlparse(url).query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    if code and (not state or state == expected_state):
        return code
    return None


def _extract_primary_account(payload: dict[str, Any]) -> dict[str, Any] | None:
    for item in _walk_nodes(payload):
        if isinstance(item, dict) and (
            item.get("accountNo") or item.get("accountNumber")
        ):
            return {
                "accountNo": item.get("accountNo") or item.get("accountNumber"),
                "premiseNo": item.get("premiseNo") or item.get("premiseNumber"),
                "ebsPremiseNo": item.get("ebsPremiseNo"),
                "msslPremiseNo": item.get("msslPremiseNo"),
                "premises_id": item.get("premises_id"),
            }
    return None


def _parse_monthly_usage(
    payload: dict[str, Any], now: datetime
) -> tuple[float | None, float | None]:
    month_keys = {
        now.strftime("%Y-%m"),
        now.strftime("%Y-%m-01"),
        now.strftime("%b").lower(),
        now.strftime("%B").lower(),
        str(now.month),
    }
    electricity: float | None = None
    water: float | None = None

    for path, item in _walk_with_paths(payload):
        if not isinstance(item, dict):
            continue
        marker = " ".join(
            str(item.get(k, "")).lower() for k in ("month", "label", "date", "period")
        )
        if month_keys and marker and not any(key in marker for key in month_keys):
            continue
        electricity = (
            electricity
            if electricity is not None
            else _extract_metric_value(item, path, "electricity")
        )
        water = (
            water if water is not None else _extract_metric_value(item, path, "water")
        )
        if electricity is not None and water is not None:
            break

    return electricity, water


def _parse_daily_usage(
    payload: dict[str, Any], now: datetime
) -> tuple[float | None, float | None]:
    if payload.get("status") == 150:
        return None, None

    day_keys = {
        now.date().isoformat(),
        now.strftime("%d/%m/%Y"),
        now.strftime("%d-%m-%Y"),
        str(now.day),
    }
    electricity: float | None = None

    for path, item in _walk_with_paths(payload):
        if not isinstance(item, dict):
            continue
        marker = " ".join(
            str(item.get(k, "")).lower() for k in ("date", "day", "label", "period")
        )
        if marker and not any(key.lower() in marker for key in day_keys):
            continue
        electricity = (
            electricity
            if electricity is not None
            else _extract_metric_value(item, path, "electricity")
        )
        if electricity is not None:
            break

    return electricity, None


def _extract_metric_value(
    item: dict[str, Any], path: list[str], utility: str
) -> float | None:
    utility_tokens = (
        ("electric", "energy", "kwh", "ebs")
        if utility == "electricity"
        else ("water", "m3", "mssl")
    )
    metric_tokens = ("total", "usage", "consumption", "value", "amount")

    for key, value in item.items():
        if not isinstance(value, (str, int, float)):
            continue
        normalized_key = key.lower()
        if any(token in normalized_key for token in utility_tokens) and any(
            token in normalized_key for token in metric_tokens
        ):
            return _coerce_float(value)

    joined_path = " ".join(path).lower()
    if any(token in joined_path for token in utility_tokens):
        for key in ("total", "usage", "consumption", "value", "amount"):
            if key in item:
                return _coerce_float(item[key])

    return None


def _parse_monthly_csv(
    csv_text: str, now: datetime
) -> tuple[float | None, float | None, float | None, float | None]:
    sections = _parse_titled_csv_sections(csv_text)
    previous_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1)

    electricity = _section_value_for_month(
        sections.get("electricity", []), now.year, now.month
    )
    water = _section_value_for_month(sections.get("water", []), now.year, now.month)
    electricity_last_month = _section_value_for_month(
        sections.get("electricity", []), previous_month.year, previous_month.month
    )
    water_last_month = _section_value_for_month(
        sections.get("water", []), previous_month.year, previous_month.month
    )
    return electricity, water, electricity_last_month, water_last_month


def _parse_monthly_history(csv_text: str) -> tuple[list[UsagePoint], list[UsagePoint]]:
    sections = _parse_titled_csv_sections(csv_text)
    electricity = _section_history_points(sections.get("electricity", []))
    water = _section_history_points(sections.get("water", []))
    return electricity, water


def _parse_hourly_history(csv_text: str) -> list[UsagePoint]:
    points: list[UsagePoint] = []
    for row in _csv_rows(csv_text):
        period = str(row.get("Period", "")).strip().strip('"')
        value = _coerce_float(row.get("Current"))
        if period and value is not None:
            points.append(UsagePoint(period=period, value=value))
    return points


def _parse_daily_history_csv(csv_text: str) -> list[UsagePoint]:
    points: list[UsagePoint] = []
    for row in _csv_rows(csv_text):
        period = str(row.get("Period", "")).strip().strip('"')
        value = _coerce_float(row.get("Current"))
        if period and value is not None:
            points.append(UsagePoint(period=period, value=value))
    return points


def _parse_daily_csv(csv_text: str, now: datetime) -> tuple[float | None, float | None]:
    rows = _csv_rows(csv_text)
    if not rows:
        return None, None

    target_day = now.date().isoformat()
    electricity_values: list[float] = []
    for row in rows:
        period = str(row.get("Period", "")).strip().strip('"')
        if not period.startswith(target_day):
            continue
        value = _coerce_float(row.get("Current"))
        if value is not None:
            electricity_values.append(value)

    return (sum(electricity_values) if electricity_values else None), None


def _csv_rows(csv_text: str) -> list[dict[str, str]]:
    if not csv_text.strip():
        return []
    return list(csv.DictReader(io.StringIO(csv_text)))


def _parse_titled_csv_sections(csv_text: str) -> dict[str, list[dict[str, str]]]:
    sections: dict[str, list[dict[str, str]]] = {}
    current_section: str | None = None
    current_headers: list[str] | None = None

    for raw_line in csv_text.splitlines():
        line = raw_line.strip()
        if not line:
            current_headers = None
            continue

        parsed = next(csv.reader([line]))
        if len(parsed) == 1:
            title = parsed[0].strip()
            if title.lower() in {"electricity", "water", "gas"}:
                current_section = title.lower()
                sections.setdefault(current_section, [])
                current_headers = None
            continue

        if current_section is None:
            continue

        if current_headers is None:
            current_headers = [header.strip() for header in parsed]
            continue

        if len(parsed) != len(current_headers):
            continue
        sections[current_section].append(
            dict(zip(current_headers, parsed, strict=False))
        )

    return sections


def _section_value_for_period(rows: list[dict[str, str]], period: str) -> float | None:
    for row in rows:
        row_period = str(row.get("Period", "")).strip().strip('"')
        if row_period == period:
            return _coerce_float(row.get("Current"))
    return None


def _section_value_for_month(
    rows: list[dict[str, str]], year: int, month: int
) -> float | None:
    matched_values: list[tuple[str, float]] = []
    fallback_exact = _section_value_for_period(
        rows, datetime(year, month, 1).date().isoformat()
    )
    if fallback_exact is not None:
        return fallback_exact

    for row in rows:
        row_period = str(row.get("Period", "")).strip().strip('"')
        if not _period_matches_month(row_period, year, month):
            continue
        value = _coerce_float(row.get("Current"))
        if value is not None:
            matched_values.append((row_period, value))

    if not matched_values:
        return None

    matched_values.sort(key=lambda item: item[0])
    return matched_values[-1][1]


def _section_history_points(rows: list[dict[str, str]]) -> list[UsagePoint]:
    points: list[UsagePoint] = []
    for row in rows:
        period = str(row.get("Period", "")).strip().strip('"')
        value = _coerce_float(row.get("Current"))
        status = str(row.get("Status", "")).strip().strip('"') or None
        if period and value is not None:
            points.append(UsagePoint(period=period, value=value, status=status))
    return points


def _latest_value_for_day(points: list[UsagePoint], now: datetime) -> float | None:
    target_day = now.date().isoformat()
    for point in reversed(points):
        if point.period == target_day:
            return point.value
    return None


def _period_matches_month(period: str, year: int, month: int) -> bool:
    normalized = period.strip().strip('"')
    month_num = f"{month:02d}"
    month_short = datetime(year, month, 1).strftime("%b").lower()
    month_long = datetime(year, month, 1).strftime("%B").lower()
    year_short = str(year)[-2:]
    lowered = normalized.lower()

    patterns = (
        f"{year}-{month_num}",
        f"{year}/{month_num}",
        f"{month_num}/{year}",
        f"{month_num}-{year}",
        f"{year}{month_num}",
        f"{month_short}-{year_short}",
        f"{month_short}-{year}",
        f"{month_short} {year}",
        f"{month_long} {year}",
    )
    return any(pattern in lowered for pattern in patterns)


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _walk_nodes(node: Any) -> list[Any]:
    if isinstance(node, dict):
        values: list[Any] = [node]
        for value in node.values():
            values.extend(_walk_nodes(value))
        return values
    if isinstance(node, list):
        values: list[Any] = []
        for item in node:
            values.extend(_walk_nodes(item))
        return values
    return []


def _walk_with_paths(
    node: Any, path: list[str] | None = None
) -> list[tuple[list[str], Any]]:
    path = path or []
    results = [(path, node)]
    if isinstance(node, dict):
        for key, value in node.items():
            results.extend(_walk_with_paths(value, [*path, str(key)]))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            results.extend(_walk_with_paths(value, [*path, str(idx)]))
    return results
