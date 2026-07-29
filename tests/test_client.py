from __future__ import annotations

import threading
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import requests

from sp_services.client import (
    SpServicesClient,
    _AuthContext,
    _code_from_url,
    _extract_accounts,
    _extract_csrf,
    _extract_csrf_from_response,
    _extract_primary_account,
    _latest_value_for_day,
    _parse_daily_csv,
    _parse_daily_history_csv,
    _parse_daily_usage,
    _parse_hourly_history,
    _parse_monthly_csv,
    _parse_monthly_history,
    _parse_monthly_usage,
    _parse_period_date,
    _parse_titled_csv_sections,
    _period_matches_day,
    _period_matches_month,
    _section_value_for_month,
    _select_account,
)
from sp_services.exceptions import ApiError, AuthenticationError, SessionExpiredError


class TestClientHelpers(unittest.TestCase):
    def test_extract_csrf_from_response_cookie(self) -> None:
        class FakeResponse:
            cookies = {"_csrf": "cookie-token"}
            headers = {}

        self.assertEqual(
            _extract_csrf_from_response(FakeResponse(), ""),
            "cookie-token",
        )

    def test_extract_csrf_from_input_field(self) -> None:
        html = '<input type="hidden" name="_csrf" value="csrf-token-123">'
        self.assertEqual(_extract_csrf(html), "csrf-token-123")

    def test_extract_csrf_from_config_blob(self) -> None:
        html = 'window.config="@@config@@eyJjc3JmVG9rZW4iOiAiYWJjMTIzIn0="'
        self.assertEqual(_extract_csrf(html), "abc123")

    def test_code_from_url_matches_expected_state(self) -> None:
        url = "https://services.spservices.sg/callback?code=auth-code&state=expected"
        self.assertEqual(_code_from_url(url, "expected"), "auth-code")
        self.assertIsNone(_code_from_url(url, "other"))

    def test_code_from_url_requires_state(self) -> None:
        # A missing state discards the CSRF binding the OAuth state provides,
        # so the code must be rejected rather than accepted.
        url = "https://services.spservices.sg/callback?code=auth-code"
        self.assertIsNone(_code_from_url(url, "expected"))
        self.assertIsNone(
            _code_from_url(
                "https://services.spservices.sg/callback?code=auth-code&state=",
                "expected",
            )
        )
        self.assertIsNone(
            _code_from_url(
                "https://services.spservices.sg/callback?code=auth-code&state=x",
                "",
            )
        )

    def test_extract_primary_account(self) -> None:
        payload = {
            "data": {
                "accounts": [
                    {
                        "accountNumber": "1234567",
                        "premiseNumber": "P-001",
                        "msslPremiseNo": "W-001",
                    }
                ]
            }
        }
        self.assertEqual(
            _extract_primary_account(payload),
            {
                "accountNo": "1234567",
                "premiseNo": "P-001",
                "ebsPremiseNo": None,
                "msslPremiseNo": "W-001",
                "premises_id": None,
            },
        )

    def test_parse_monthly_usage_from_payload(self) -> None:
        now = datetime(2026, 4, 12)
        payload = {
            "results": [
                {
                    "month": "2026-04",
                    "electricityConsumption": "321.5",
                    "waterConsumption": "12.75",
                }
            ]
        }
        self.assertEqual(_parse_monthly_usage(payload, now), (321.5, 12.75))

    def test_parse_monthly_csv(self) -> None:
        now = datetime(2026, 4, 12)
        csv_text = """Electricity
Period,Current
2026-04-01,321.5
2026-03-01,300.0

Water
Period,Current
2026-04-01,12.75
2026-03-01,11.25
"""
        self.assertEqual(
            _parse_monthly_csv(csv_text, now),
            (321.5, 12.75, 300.0, 11.25),
        )

    def test_parse_monthly_csv_accepts_current_month_partial_dates(self) -> None:
        now = datetime(2026, 4, 12)
        csv_text = """Electricity
Period,Current
2026-04-12,321.5
2026-03-31,300.0

Water
Period,Current
Apr 2026,12.75
Mar 2026,11.25
"""
        self.assertEqual(
            _parse_monthly_csv(csv_text, now),
            (321.5, 12.75, 300.0, 11.25),
        )

    def test_period_matches_month(self) -> None:
        self.assertTrue(_period_matches_month("2026-04-12", 2026, 4))
        self.assertTrue(_period_matches_month("Apr 2026", 2026, 4))
        self.assertTrue(_period_matches_month("04/2026", 2026, 4))
        self.assertFalse(_period_matches_month("2026-03-31", 2026, 4))

    def test_parse_monthly_history(self) -> None:
        csv_text = """Electricity
Period,Status,Current
2026-04-01,Actual,321.5
2026-03-01,Estimated,300.0

Water
Period,Status,Current
2026-04-01,Actual,12.75
2026-03-01,Estimated,11.25
"""
        electricity, water = _parse_monthly_history(csv_text)
        self.assertEqual(
            [(point.period, point.status, point.value) for point in electricity],
            [
                ("2026-04-01", "Actual", 321.5),
                ("2026-03-01", "Estimated", 300.0),
            ],
        )
        self.assertEqual(
            [(point.period, point.status, point.value) for point in water],
            [
                ("2026-04-01", "Actual", 12.75),
                ("2026-03-01", "Estimated", 11.25),
            ],
        )

    def test_parse_hourly_history(self) -> None:
        csv_text = """Period,Current
2026-04-12 00:00:00,1.2
2026-04-12 00:30:00,0.8
"""
        points = _parse_hourly_history(csv_text)
        self.assertEqual(
            [(point.period, point.value) for point in points],
            [("2026-04-12 00:00:00", 1.2), ("2026-04-12 00:30:00", 0.8)],
        )

    def test_parse_daily_history_csv(self) -> None:
        csv_text = """Period,Current
2026-04-11,15.265
2026-04-12,19.967
2026-04-13,0.0
"""
        points = _parse_daily_history_csv(csv_text, datetime(2026, 4, 12))
        self.assertEqual(
            [(point.period, point.value) for point in points],
            [("2026-04-11", 15.265), ("2026-04-12", 19.967)],
        )

    def test_latest_value_for_day(self) -> None:
        now = datetime(2026, 4, 12, 12, 0, 0)
        points = _parse_daily_history_csv(
            """Period,Current
2026-04-11,15.265
2026-04-12,19.967
2026-04-13,0.0
""",
            now,
        )
        self.assertEqual(_latest_value_for_day(points, now), 19.967)

    def test_parse_daily_csv(self) -> None:
        now = datetime(2026, 4, 12)
        csv_text = """Period,Current
2026-04-12T00:00:00,1.2
2026-04-12T01:00:00,0.8
2026-04-11T23:00:00,9.9
"""
        self.assertEqual(_parse_daily_csv(csv_text, now), 2.0)

    def test_parse_daily_csv_without_rows(self) -> None:
        self.assertIsNone(_parse_daily_csv("", datetime(2026, 4, 12)))


class TestPeriodMatchingRegressions(unittest.TestCase):
    """Period labels must not match on a bare digit substring."""

    def test_monthly_usage_ignores_digit_collision_in_year(self) -> None:
        now = datetime(2026, 4, 12)
        payload = {
            "results": [
                {"month": "2024-01", "electricityConsumption": "111"},
                {"month": "2026-04", "electricityConsumption": "321.5"},
            ]
        }
        electricity, _ = _parse_monthly_usage(payload, now)
        self.assertEqual(electricity, 321.5)

    def test_monthly_usage_ignores_single_digit_month_collision(self) -> None:
        now = datetime(2026, 1, 15)
        payload = {
            "results": [
                {"month": "2025-11", "electricityConsumption": "111"},
                {"month": "2026-01", "electricityConsumption": "222"},
            ]
        }
        electricity, _ = _parse_monthly_usage(payload, now)
        self.assertEqual(electricity, 222.0)

    def test_monthly_usage_without_period_fields_still_matches(self) -> None:
        now = datetime(2026, 4, 12)
        payload = {"electricity": {"total": "50"}}
        electricity, _ = _parse_monthly_usage(payload, now)
        self.assertEqual(electricity, 50.0)

    def test_daily_usage_ignores_day_digit_collision(self) -> None:
        now = datetime(2026, 4, 12)
        payload = {
            "status": 100,
            "results": [
                {"date": "2026-04-01", "electricityUsage": "99"},
                {"date": "2026-04-12", "electricityUsage": "5"},
            ],
        }
        self.assertEqual(_parse_daily_usage(payload, now), 5.0)

    def test_daily_usage_returns_none_for_business_no_data(self) -> None:
        self.assertIsNone(_parse_daily_usage({"status": 150}, datetime(2026, 4, 12)))

    def test_period_matches_day(self) -> None:
        target = date(2026, 4, 12)
        self.assertTrue(_period_matches_day("2026-04-12", target))
        self.assertTrue(_period_matches_day("2026-04-12 00:30:00", target))
        self.assertTrue(_period_matches_day("12/04/2026", target))
        self.assertFalse(_period_matches_day("2026-04-01", target))
        self.assertFalse(_period_matches_day("", target))

    def test_parse_period_date(self) -> None:
        self.assertEqual(_parse_period_date("2026-04-12"), date(2026, 4, 12))
        self.assertEqual(_parse_period_date("Apr 2026"), date(2026, 4, 1))
        self.assertEqual(_parse_period_date('"04/2026"'), date(2026, 4, 1))
        self.assertEqual(_parse_period_date("12/04/2026"), date(2026, 4, 12))
        self.assertIsNone(_parse_period_date("not-a-period"))
        self.assertIsNone(_parse_period_date(""))


class TestCsvSectionRegressions(unittest.TestCase):
    def test_unknown_section_rows_do_not_leak_into_previous_section(self) -> None:
        csv_text = '''"Electricity"
"Period","Status","Current"
"2026-03-01","Actual","572"

"Water"
"Period","Status","Current"
"2026-03-01","Estimated","34.1"

"Solar"
"Period","Status","Current"
"2026-03-01","Actual","999"
'''
        sections = _parse_titled_csv_sections(csv_text)
        self.assertEqual(len(sections["electricity"]), 1)
        self.assertEqual(
            [row["Current"] for row in sections["water"]],
            ["34.1"],
        )

    def test_single_column_filler_inside_section_is_skipped(self) -> None:
        csv_text = '''"Electricity"
"Period","Current"
"2026-03-01","572"
"No data available"
"2026-02-01","500"
'''
        sections = _parse_titled_csv_sections(csv_text)
        self.assertEqual(
            [row["Current"] for row in sections["electricity"]],
            ["572", "500"],
        )

    def test_section_value_for_month_prefers_latest_real_date(self) -> None:
        rows = [
            {"Period": "2026-04-05", "Current": "10"},
            {"Period": "2026-04-28", "Current": "40"},
            {"Period": "Apr 2026", "Current": "99"},
        ]
        self.assertEqual(_section_value_for_month(rows, 2026, 4), 40.0)

    def test_section_value_for_month_falls_back_to_text_periods(self) -> None:
        rows = [{"Period": "Apr 2026", "Current": "99"}]
        self.assertEqual(_section_value_for_month(rows, 2026, 4), 99.0)
        self.assertIsNone(_section_value_for_month(rows, 2026, 3))

    def test_history_preserves_published_period_without_value(self) -> None:
        csv_text = """Electricity
Period,Status,Current
2026-04-01,Estimated,
2026-03-01,Actual,572
"""
        electricity, _ = _parse_monthly_history(csv_text)
        self.assertEqual(
            [(point.period, point.status, point.value) for point in electricity],
            [("2026-04-01", "Estimated", None), ("2026-03-01", "Actual", 572.0)],
        )


class TestAccountSelection(unittest.TestCase):
    def test_extract_accounts_dedupes_and_preserves_order(self) -> None:
        payload = {
            "data": {
                "accounts": [
                    {"accountNo": "111", "premiseNo": "P-1"},
                    {"accountNo": "222", "premiseNo": "P-2"},
                    {"accountNo": "111", "premiseNo": "P-1"},
                ]
            }
        }
        accounts = _extract_accounts(payload)
        self.assertEqual([item["accountNo"] for item in accounts], ["111", "222"])

    def test_select_account_by_number(self) -> None:
        accounts = [{"accountNo": "111"}, {"accountNo": "222"}]
        self.assertEqual(_select_account(accounts, "222"), {"accountNo": "222"})
        self.assertEqual(_select_account(accounts, None), {"accountNo": "111"})
        self.assertIsNone(_select_account(accounts, "999"))
        self.assertIsNone(_select_account([], None))


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 4, 12, 12, 0, 0)


class TestClientFlows(unittest.IsolatedAsyncioTestCase):
    async def test_begin_browser_login_returns_authorize_url(self) -> None:
        client = SpServicesClient()
        context = _AuthContext(
            oauth_state="oauth-state",
            login_state="login-state",
            nonce="nonce-value",
            code_verifier="verifier-value",
            csrf="csrf-value",
        )
        client._bootstrap_auth = AsyncMock(return_value=context)  # type: ignore[method-assign]

        url = await client.begin_browser_login()

        self.assertIn("state=oauth-state", url)
        self.assertIn("nonce=nonce-value", url)
        self.assertIs(client._auth_context, context)
        await client.close()

    async def test_exchange_callback_url_uses_authorization_code(self) -> None:
        client = SpServicesClient()
        client._auth_context = _AuthContext(
            oauth_state="expected-state",
            login_state="login-state",
            nonce="nonce-value",
            code_verifier="verifier-value",
            csrf="csrf-value",
        )
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"access_token": "token-123"}
        )

        token = await client.exchange_callback_url(
            "https://services.spservices.sg/callback?code=abc&state=expected-state"
        )

        self.assertEqual(token, "token-123")
        client._post_json.assert_awaited_once()
        await client.close()

    async def test_exchange_callback_url_rejects_invalid_callback(self) -> None:
        client = SpServicesClient()
        client._auth_context = _AuthContext(
            oauth_state="expected-state",
            login_state="login-state",
            nonce="nonce-value",
            code_verifier="verifier-value",
            csrf="csrf-value",
        )

        with self.assertRaises(AuthenticationError):
            await client.exchange_callback_url(
                "https://services.spservices.sg/callback?state=expected-state"
            )

        await client.close()

    async def test_fetch_usage_builds_usage_from_mocked_responses(self) -> None:
        client = SpServicesClient()
        client._skalbox_call = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "data": {
                        "accounts": [
                            {
                                "accountNo": "8949049293",
                                "premiseNo": "EBS-1",
                                "msslPremiseNo": "MSSL-1",
                            }
                        ]
                    }
                },
                {"status": 100, "results": []},
                {"status": 150},
            ]
        )
        client._post_text = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                """"Electricity"
"Period","Status","Current"
"2026-03-01","Actual","572"

"Gas"
"No data available"

"Water"
"Period","Status","Current"
"2026-03-01","Estimated","34.1"
""",
                """"Period","Current"
"2026-04-12 00:00:00","1.2"
"2026-04-12 00:30:00","0.8"
""",
                """"Period","Current"
"2026-04-11","15.265"
"2026-04-12","19.967"
"2026-04-13","0.0"
""",
            ]
        )

        with patch("sp_services.client.datetime", FixedDateTime):
            usage = await client.fetch_usage("token-123")

        self.assertEqual(usage.account_no, "8949049293")
        self.assertEqual(usage.electricity_today_kwh, 2.0)
        self.assertIsNone(usage.electricity_month_kwh)
        self.assertEqual(usage.electricity_last_month_kwh, 572.0)
        self.assertEqual(usage.water_last_month_m3, 34.1)
        self.assertEqual(len(usage.electricity_hourly_history or []), 2)
        self.assertEqual(len(usage.electricity_daily_history or []), 2)
        self.assertEqual(len(usage.electricity_monthly_history or []), 1)
        self.assertEqual(len(usage.water_monthly_history or []), 1)
        await client.close()

    async def test_format_login_error_uses_json_error_fields(self) -> None:
        client = SpServicesClient(debug=True)
        resp = Mock()
        resp.status_code = 401
        resp.url = "https://identity.spdigital.auth0.com/usernamepassword/login"
        resp.history = []
        resp.json.return_value = {
            "description": "Invalid captcha value",
            "name": "invalid_captcha",
        }

        message = client._format_login_error(resp)

        self.assertIn("invalid_captcha", message)
        self.assertIn("Invalid captcha value", message)
        self.assertIn("status=401", message)
        await client.close()

    async def test_request_runs_off_the_event_loop(self) -> None:
        """Blocking requests calls must not run on the caller's loop thread."""
        client = SpServicesClient()
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}

        def fake_request(method, url, **kwargs):
            seen["thread"] = threading.get_ident()
            seen["method"] = method
            return Mock(status_code=200, ok=True)

        client._session.request = fake_request  # type: ignore[method-assign]

        await client._request("POST", "https://example.invalid/x", timeout=30)

        self.assertEqual(seen["method"], "POST")
        self.assertNotEqual(seen["thread"], loop_thread)
        await client.close()

    async def test_request_maps_transport_errors_to_api_error(self) -> None:
        client = SpServicesClient()

        def fake_request(method, url, **kwargs):
            raise requests.ConnectionError("boom")

        client._session.request = fake_request  # type: ignore[method-assign]

        with self.assertRaises(ApiError):
            await client._request("GET", "https://example.invalid/x")
        await client.close()

    async def test_exchange_callback_url_rejects_missing_state(self) -> None:
        client = SpServicesClient()
        client._auth_context = _AuthContext(
            oauth_state="expected-state",
            login_state="login-state",
            nonce="nonce-value",
            code_verifier="verifier-value",
            csrf="csrf-value",
        )
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"access_token": "token-123"}
        )

        with self.assertRaises(AuthenticationError):
            await client.exchange_callback_url(
                "https://services.spservices.sg/callback?code=abc"
            )

        client._post_json.assert_not_awaited()
        await client.close()

    async def test_exchange_callback_url_captures_refresh_token(self) -> None:
        client = SpServicesClient()
        client._auth_context = _AuthContext(
            oauth_state="expected-state",
            login_state="login-state",
            nonce="nonce-value",
            code_verifier="verifier-value",
            csrf="csrf-value",
        )
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "access_token": "token-123",
                "refresh_token": "refresh-123",
                "expires_in": 3600,
                "scope": "openid offline_access",
            }
        )

        await client.exchange_callback_url(
            "https://services.spservices.sg/callback?code=abc&state=expected-state"
        )

        token_set = client.token_set
        assert token_set is not None
        self.assertEqual(token_set.refresh_token, "refresh-123")
        self.assertEqual(token_set.scope, "openid offline_access")
        self.assertIsNotNone(token_set.expires_at)
        self.assertFalse(token_set.is_expired())
        await client.close()

    async def test_refresh_access_token_keeps_existing_refresh_token(self) -> None:
        client = SpServicesClient()
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"access_token": "token-456", "expires_in": 3600}
        )

        token_set = await client.refresh_access_token("refresh-123")

        self.assertEqual(token_set.access_token, "token-456")
        self.assertEqual(token_set.refresh_token, "refresh-123")
        await client.close()

    async def test_refresh_access_token_without_token_raises(self) -> None:
        client = SpServicesClient()
        with self.assertRaises(AuthenticationError):
            await client.refresh_access_token()
        await client.close()

    async def test_extract_authorization_code_requires_matching_state(self) -> None:
        resp = Mock()
        resp.url = "https://identity.spdigital.auth0.com/mfa"
        resp.history = []
        resp.text = (
            "<a href='https://services.spservices.sg/callback"
            "?code=injected&state=attacker'>go</a>"
        )

        self.assertIsNone(
            SpServicesClient._extract_authorization_code(resp, "expected-state")
        )

        resp.text = (
            "<a href='https://services.spservices.sg/callback"
            "?code=good&state=expected-state'>go</a>"
        )
        self.assertEqual(
            SpServicesClient._extract_authorization_code(resp, "expected-state"),
            "good",
        )

    async def test_fetch_usage_survives_missing_csv_exports(self) -> None:
        client = SpServicesClient()
        client._skalbox_call = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"data": {"accounts": [{"accountNo": "1", "premiseNo": "EBS-1"}]}},
                {"status": 100, "results": []},
                {"status": 150},
            ]
        )
        client._post_text = AsyncMock(  # type: ignore[method-assign]
            side_effect=ApiError("SP Services request returned HTTP 500")
        )

        with patch("sp_services.client.datetime", FixedDateTime):
            usage = await client.fetch_usage("token-123")

        self.assertEqual(usage.account_no, "1")
        self.assertIsNone(usage.electricity_today_kwh)
        self.assertEqual(usage.electricity_monthly_history, [])
        await client.close()

    async def test_fetch_usage_propagates_expired_session(self) -> None:
        client = SpServicesClient()
        client._skalbox_call = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"data": {"accounts": [{"accountNo": "1", "premiseNo": "EBS-1"}]}},
                {"status": 100, "results": []},
                {"status": 150},
            ]
        )
        client._post_text = AsyncMock(  # type: ignore[method-assign]
            side_effect=SessionExpiredError("SP Services token expired")
        )

        with patch("sp_services.client.datetime", FixedDateTime):
            with self.assertRaises(SessionExpiredError):
                await client.fetch_usage("token-123")

        await client.close()

    async def test_fetch_usage_selects_requested_account(self) -> None:
        client = SpServicesClient()
        client._skalbox_call = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "data": {
                        "accounts": [
                            {"accountNo": "111", "premiseNo": "EBS-1"},
                            {"accountNo": "222", "premiseNo": "EBS-2"},
                        ]
                    }
                },
                {"status": 100, "results": []},
                {"status": 150},
            ]
        )
        client._post_text = AsyncMock(return_value="")  # type: ignore[method-assign]

        with patch("sp_services.client.datetime", FixedDateTime):
            usage = await client.fetch_usage("token-123", account_no="222")

        self.assertEqual(usage.account_no, "222")
        await client.close()

    async def test_fetch_usage_rejects_unknown_account(self) -> None:
        client = SpServicesClient()
        client._skalbox_call = AsyncMock(  # type: ignore[method-assign]
            return_value={"data": {"accounts": [{"accountNo": "111"}]}}
        )

        with self.assertRaises(ApiError):
            await client.fetch_usage("token-123", account_no="999")

        await client.close()

    async def test_fetch_usage_uses_singapore_time(self) -> None:
        """'Today' must follow SP's timezone, not the host clock."""
        client = SpServicesClient()
        client._skalbox_call = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"data": {"accounts": [{"accountNo": "1", "premiseNo": "EBS-1"}]}},
                {"status": 100, "results": []},
                {"status": 150},
            ]
        )
        client._post_text = AsyncMock(return_value="")  # type: ignore[method-assign]

        usage = await client.fetch_usage("token-123")

        assert usage.last_updated.tzinfo is not None
        self.assertEqual(
            usage.last_updated.utcoffset(), timedelta(hours=8)
        )
        hourly_request = client._skalbox_call.await_args_list[2].args[0]
        self.assertEqual(
            hourly_request["charts:hourly"]["date"],
            datetime.now(ZoneInfo("Asia/Singapore")).date().isoformat(),
        )
        await client.close()


if __name__ == "__main__":
    unittest.main()
