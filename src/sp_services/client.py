"""Async client for SP Services household energy and water usage data."""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests

from .exceptions import ApiError, AuthenticationError, SessionExpiredError
from .models import LoginChallenge, TokenSet, UsageData, UsagePoint

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

_SP_TIMEZONE = ZoneInfo("Asia/Singapore")

_KNOWN_CSV_SECTIONS = frozenset({"electricity", "water", "gas"})

_PERIOD_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m",
    "%Y/%m",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%Y",
    "%m-%Y",
    "%b %Y",
    "%B %Y",
    "%b-%Y",
    "%b-%y",
    "%Y%m%d",
    "%Y%m",
)

_CSRF_RE = re.compile(r'name=["\']_csrf["\'][^>]*value=["\']([^"\']+)["\']')
_INLINE_CSRF_RE = re.compile(r'"_csrf"\s*:\s*"([^"]+)"')
_CSRF_TOKEN_RE = re.compile(r'"csrf(?:_token|Token)?"\s*:\s*"([^"]+)"')
_CONFIG_BLOB_RE = re.compile(r"@@config@@([^\"'<\s]+)")
_SET_COOKIE_CSRF_RE = re.compile(r"(?:^|;\s*|,?\s*)_csrf=([^;,\s]+)")
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


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
        self._session: requests.Session = requests.Session()
        self._auth_context: _AuthContext | None = None
        self._token_set: TokenSet | None = None
        self._debug = debug

    @property
    def token_set(self) -> TokenSet | None:
        """Tokens from the most recent exchange or refresh, if any."""
        return self._token_set

    async def __aenter__(self) -> "SpServicesClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def login(self, username: str, password: str) -> LoginChallenge:
        """Submit credentials and trigger the SMS OTP flow."""
        context = await self._bootstrap_auth()
        await self._post_json(_AUTH0_CHALLENGE_URL, {"state": context.login_state})

        resp = await self._request(
            "POST",
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

        send_sms_resp = await self._request(
            "POST",
            _AUTH0_MFA_SEND_SMS_URL,
            json={},
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
            raise AuthenticationError(
                "callback URL did not contain a code matching the expected state; "
                "paste the full callback URL including both code= and state="
            )

        return (await self._exchange_code(code, context.code_verifier)).access_token

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

        resp = await self._request(
            "POST",
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

        return (await self._exchange_code(code, context.code_verifier)).access_token

    async def refresh_access_token(self, refresh_token: str | None = None) -> TokenSet:
        """Exchange a refresh token for a fresh access token.

        Falls back to the refresh token captured by the most recent exchange
        when ``refresh_token`` is omitted. This is the only non-interactive way
        to stay authenticated; SP/Auth0 only issues one when it honours the
        ``offline_access`` scope.
        """
        token = refresh_token or (
            self._token_set.refresh_token if self._token_set else None
        )
        if not token:
            raise AuthenticationError("no refresh token available; restart login")

        token_body = await self._post_json(
            _AUTH0_TOKEN_URL,
            {
                "client_id": _AUTH0_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": token,
            },
        )
        # Auth0 omits refresh_token on refresh unless rotation is enabled, so
        # keep the existing one rather than dropping it.
        return self._store_token_set(token_body, fallback_refresh_token=token)

    async def _exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        token_body = await self._post_json(
            _AUTH0_TOKEN_URL,
            {
                "client_id": _AUTH0_CLIENT_ID,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _AUTH0_REDIRECT_URI,
            },
        )
        return self._store_token_set(token_body)

    def _store_token_set(
        self, token_body: dict[str, Any], fallback_refresh_token: str | None = None
    ) -> TokenSet:
        access_token = token_body.get("access_token")
        if not access_token:
            raise ApiError("SP Services token exchange returned no access token")

        expires_in = _coerce_float(token_body.get("expires_in"))
        refresh_token = token_body.get("refresh_token") or fallback_refresh_token
        self._token_set = TokenSet(
            access_token=str(access_token),
            refresh_token=str(refresh_token) if refresh_token else None,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                if expires_in is not None
                else None
            ),
            token_type=str(token_body.get("token_type") or "Bearer"),
            scope=str(token_body["scope"]) if token_body.get("scope") else None,
        )
        return self._token_set

    async def fetch_usage(self, token: str, account_no: str | None = None) -> UsageData:
        """Fetch today's and this month's electricity and water usage.

        ``account_no`` selects one of several linked accounts; the first
        account SP returns is used when it is omitted.
        """
        accounts_payload = await self._skalbox_call(
            {"user:getAccounts": {"token": token}},
            token=token,
        )
        accounts = _extract_accounts(accounts_payload)
        account = _select_account(accounts, account_no)
        if account is None:
            if account_no:
                raise ApiError(
                    f"SP Services returned no utility account matching {account_no}"
                )
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

        now = _now_sp()
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
        electricity_today = _parse_daily_usage(hourly_payload, now)

        monthly_csv = await self._post_text_optional(
            _SKALBOX_MONTHLY_CSV_URL,
            {
                "ebsPremiseNo": ebs_premise_no,
                "msslPremiseNo": mssl_premise_no,
                "accountNo": account_no,
            },
            token=token,
        )
        monthly_sections = _parse_titled_csv_sections(monthly_csv)
        (
            csv_elec_month,
            csv_water_month,
            electricity_last_month_kwh,
            water_last_month_m3,
        ) = _monthly_summary_from_sections(monthly_sections, now)
        electricity_monthly_history = _section_history_points(
            monthly_sections.get("electricity", [])
        )
        water_monthly_history = _section_history_points(
            monthly_sections.get("water", [])
        )
        if electricity_month is None:
            electricity_month = csv_elec_month
        if water_month is None:
            water_month = csv_water_month

        hourly_csv = await self._post_text_optional(
            _SKALBOX_DAILY_CSV_URL,
            {"accountNos": [account_no], "consumptionBy": "hourCSV"},
            token=token,
        )
        electricity_hourly_history = _parse_hourly_history(hourly_csv)
        if electricity_today is None:
            electricity_today = _parse_daily_csv(hourly_csv, now)

        daily_summary_csv = await self._post_text_optional(
            _SKALBOX_DAILY_CSV_URL,
            {"accountNos": [account_no], "consumptionBy": "dayCSV"},
            token=token,
        )
        electricity_daily_history = _parse_daily_history_csv(daily_summary_csv, now)
        if electricity_today is None and electricity_daily_history:
            electricity_today = _latest_value_for_day(electricity_daily_history, now)

        return UsageData(
            electricity_today_kwh=electricity_today,
            electricity_month_kwh=electricity_month,
            # SP does not publish same-day water usage in any observed export.
            water_today_m3=None,
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
        await asyncio.to_thread(self._session.close)

    async def _bootstrap_auth(self) -> _AuthContext:
        oauth_state = _random_token(32)
        nonce = _random_token(32)
        code_verifier = _random_token(48)
        params = self._authorize_params(
            oauth_state=oauth_state,
            nonce=nonce,
            code_verifier=code_verifier,
        )

        resp = await self._request(
            "GET",
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

    async def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Issue a request without blocking the caller's event loop.

        ``requests`` is synchronous, so each call is dispatched to a worker
        thread. This keeps the public API awaitable and safe to use from hosts
        that forbid blocking I/O in the loop, such as Home Assistant.
        """
        try:
            return await asyncio.to_thread(self._session.request, method, url, **kwargs)
        except requests.RequestException as err:
            raise ApiError(f"SP Services request to {url} failed: {err}") from err

    async def _post_json(
        self, url: str, payload: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
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
        resp = await self._request(
            "POST",
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

    async def _post_text_optional(
        self, url: str, payload: dict[str, Any], token: str | None = None
    ) -> str:
        """Fetch a CSV export, degrading to empty text when it is unavailable.

        Expired sessions still propagate; only upstream export failures are
        treated as missing data so one flaky endpoint cannot fail the whole
        usage fetch.
        """
        try:
            return await self._post_text(url, payload, token=token)
        except ApiError:
            return ""

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

        for candidate in urls:
            code = _code_from_url(candidate, state)
            if code:
                return code

        # Fall back to URLs embedded in the response body, but still require the
        # state to match so an injected code cannot be picked up.
        body = getattr(resp, "text", "") or ""
        for match in _URL_RE.finditer(body):
            code = _code_from_url(match.group(0), state)
            if code:
                return code
        return None


def _now_sp() -> datetime:
    """Current time in SP Services' reporting timezone.

    Every 'today' decision depends on this, so it must not follow the host
    clock: a UTC host would otherwise report yesterday's usage between
    midnight and 08:00 Singapore time.
    """
    return datetime.now(_SP_TIMEZONE)


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
    """Extract an authorization code, requiring the OAuth state to match.

    A missing state is rejected: accepting it would discard the CSRF binding
    that state exists to provide.
    """
    if not url or not expected_state:
        return None
    query = parse_qs(urlparse(url).query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    if code and state and secrets.compare_digest(state, expected_state):
        return code
    return None


def _extract_accounts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every linked utility account, in the order SP returned them."""
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _walk_nodes(payload):
        if not isinstance(item, dict):
            continue
        account_no = item.get("accountNo") or item.get("accountNumber")
        if not account_no or str(account_no) in seen:
            continue
        seen.add(str(account_no))
        accounts.append(
            {
                "accountNo": account_no,
                "premiseNo": item.get("premiseNo") or item.get("premiseNumber"),
                "ebsPremiseNo": item.get("ebsPremiseNo"),
                "msslPremiseNo": item.get("msslPremiseNo"),
                "premises_id": item.get("premises_id"),
            }
        )
    return accounts


def _select_account(
    accounts: list[dict[str, Any]], account_no: str | None
) -> dict[str, Any] | None:
    if account_no is None:
        return accounts[0] if accounts else None
    for account in accounts:
        if str(account.get("accountNo")) == str(account_no):
            return account
    return None


def _extract_primary_account(payload: dict[str, Any]) -> dict[str, Any] | None:
    accounts = _extract_accounts(payload)
    return accounts[0] if accounts else None


def _parse_monthly_usage(
    payload: dict[str, Any], now: datetime
) -> tuple[float | None, float | None]:
    electricity: float | None = None
    water: float | None = None

    for path, item in _walk_with_paths(payload):
        if not isinstance(item, dict):
            continue
        if _item_period_matches(item, _MONTH_MARKER_KEYS, _month_matcher(now)) is False:
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


def _parse_daily_usage(payload: dict[str, Any], now: datetime) -> float | None:
    if payload.get("status") == 150:
        return None

    matcher = _day_matcher(now)
    for path, item in _walk_with_paths(payload):
        if not isinstance(item, dict):
            continue
        if _item_period_matches(item, _DAY_MARKER_KEYS, matcher) is False:
            continue
        electricity = _extract_metric_value(item, path, "electricity")
        if electricity is not None:
            return electricity

    return None


_MONTH_MARKER_KEYS = ("month", "label", "date", "period")
_DAY_MARKER_KEYS = ("date", "day", "label", "period")


def _month_matcher(now: datetime):
    def matches(value: str) -> bool:
        return _period_matches_month(value, now.year, now.month)

    return matches


def _day_matcher(now: datetime):
    target = now.date()

    def matches(value: str) -> bool:
        return _period_matches_day(value, target)

    return matches


def _item_period_matches(
    item: dict[str, Any], marker_keys: tuple[str, ...], matcher: Any
) -> bool | None:
    """Whether an item's period labels match the wanted period.

    Returns ``None`` when the item carries no period-ish field at all, so
    callers can still consider payload shapes that omit period labels.
    """
    values = [str(item.get(key, "")).strip() for key in marker_keys]
    values = [value for value in values if value]
    if not values:
        return None
    return any(matcher(value) for value in values)


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


def _monthly_summary_from_sections(
    sections: dict[str, list[dict[str, str]]], now: datetime
) -> tuple[float | None, float | None, float | None, float | None]:
    previous_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    electricity_rows = sections.get("electricity", [])
    water_rows = sections.get("water", [])

    return (
        _section_value_for_month(electricity_rows, now.year, now.month),
        _section_value_for_month(water_rows, now.year, now.month),
        _section_value_for_month(
            electricity_rows, previous_month.year, previous_month.month
        ),
        _section_value_for_month(water_rows, previous_month.year, previous_month.month),
    )


def _parse_monthly_csv(
    csv_text: str, now: datetime
) -> tuple[float | None, float | None, float | None, float | None]:
    return _monthly_summary_from_sections(_parse_titled_csv_sections(csv_text), now)


def _parse_monthly_history(csv_text: str) -> tuple[list[UsagePoint], list[UsagePoint]]:
    sections = _parse_titled_csv_sections(csv_text)
    electricity = _section_history_points(sections.get("electricity", []))
    water = _section_history_points(sections.get("water", []))
    return electricity, water


def _parse_hourly_history(csv_text: str) -> list[UsagePoint]:
    points: list[UsagePoint] = []
    for row in _csv_rows(csv_text):
        period = _row_period(row)
        value = _coerce_float(row.get("Current"))
        if period and value is not None:
            points.append(UsagePoint(period=period, value=value))
    return points


def _parse_daily_history_csv(
    csv_text: str, now: datetime | None = None
) -> list[UsagePoint]:
    points: list[UsagePoint] = []
    max_day = now.date().isoformat() if now else None
    for row in _csv_rows(csv_text):
        period = _row_period(row)
        value = _coerce_float(row.get("Current"))
        if max_day and period and len(period) == 10 and period > max_day:
            continue
        if period and value is not None:
            points.append(UsagePoint(period=period, value=value))
    return points


def _parse_daily_csv(csv_text: str, now: datetime) -> float | None:
    target_day = now.date().isoformat()
    electricity_values: list[float] = []
    for row in _csv_rows(csv_text):
        if not _row_period(row).startswith(target_day):
            continue
        value = _coerce_float(row.get("Current"))
        if value is not None:
            electricity_values.append(value)

    return sum(electricity_values) if electricity_values else None


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
            title = parsed[0].strip().lower()
            if title in _KNOWN_CSV_SECTIONS:
                current_section = title
                sections.setdefault(current_section, [])
                current_headers = None
            elif current_headers is None:
                # An unrecognized heading starts a section we cannot classify.
                # Leave the previous section rather than silently appending the
                # new section's rows to it.
                current_section = None
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


def _row_period(row: dict[str, str]) -> str:
    return str(row.get("Period", "")).strip().strip('"')


def _section_value_for_period(rows: list[dict[str, str]], period: str) -> float | None:
    for row in rows:
        if _row_period(row) == period:
            return _coerce_float(row.get("Current"))
    return None


def _section_value_for_month(
    rows: list[dict[str, str]], year: int, month: int
) -> float | None:
    exact = _section_value_for_period(
        rows, datetime(year, month, 1).date().isoformat()
    )
    if exact is not None:
        return exact

    # Periods are compared as dates, not as strings: a mixed export containing
    # both '2026-04-28' and 'Apr 2026' would otherwise sort by ASCII and pick
    # the text label over the more recent row.
    dated: list[tuple[date, float]] = []
    undated: list[float] = []
    for row in rows:
        period = _row_period(row)
        if not _period_matches_month(period, year, month):
            continue
        value = _coerce_float(row.get("Current"))
        if value is None:
            continue
        parsed = _parse_period_date(period)
        if parsed is not None:
            dated.append((parsed, value))
        else:
            undated.append(value)

    if dated:
        dated.sort(key=lambda item: item[0])
        return dated[-1][1]
    return undated[-1] if undated else None


def _section_history_points(rows: list[dict[str, str]]) -> list[UsagePoint]:
    points: list[UsagePoint] = []
    for row in rows:
        period = _row_period(row)
        value = _coerce_float(row.get("Current"))
        status = str(row.get("Status", "")).strip().strip('"') or None
        # Keep periods SP published without a figure as long as they carry a
        # status, so an unpublished month is visible rather than missing.
        if period and (value is not None or status is not None):
            points.append(UsagePoint(period=period, value=value, status=status))
    return points


def _latest_value_for_day(points: list[UsagePoint], now: datetime) -> float | None:
    target_day = now.date().isoformat()
    for point in reversed(points):
        if point.period == target_day:
            return point.value
    return None


def _parse_period_date(period: str) -> date | None:
    """Parse an SP period label into a date, or ``None`` if the shape is new."""
    text = period.strip().strip('"')
    if not text:
        return None
    for fmt in _PERIOD_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _period_matches_day(period: str, target: date) -> bool:
    normalized = period.strip().strip('"').lower()
    if not normalized:
        return False

    patterns = (
        target.isoformat(),
        target.strftime("%d/%m/%Y"),
        target.strftime("%d-%m-%Y"),
        target.strftime("%Y%m%d"),
    )
    return any(pattern in normalized for pattern in patterns)


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


def _walk_nodes(node: Any) -> Iterator[Any]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_nodes(item)


def _walk_with_paths(
    node: Any, path: list[str] | None = None
) -> Iterator[tuple[list[str], Any]]:
    path = path or []
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk_with_paths(value, [*path, str(key)])
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            yield from _walk_with_paths(value, [*path, str(idx)])
