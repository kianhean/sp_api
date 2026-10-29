from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from sp_services import browser
from sp_services.cli import _load_tokens, _save_tokens, _token_from_cache
from sp_services.client import (
    SpServicesClient,
    _AuthContext,
    _code_from_url,
    _extract_csrf_from_response,
    _latest_value_for_day,
    _parse_daily_csv,
    _parse_titled_csv_sections,
    _section_history_points,
)
from sp_services.exceptions import ApiError, BrowserNotFoundError
from sp_services.models import TokenSet, UsagePoint

_SG = ZoneInfo("Asia/Singapore")
_CALLBACK = "https://services.spservices.sg/callback"


class TestAuthFixes(unittest.TestCase):
    def test_code_from_url_rejects_non_ascii_state_without_raising(self) -> None:
        url = f"{_CALLBACK}?code=abc&state=%C3%A9"
        self.assertIsNone(_code_from_url(url, "expected"))

    def test_set_cookie_csrf_requires_cookie_boundary(self) -> None:
        def response(set_cookie: str) -> Mock:
            return Mock(cookies=None, headers={"set-cookie": set_cookie})

        self.assertIsNone(
            _extract_csrf_from_response(response("foo_csrf=wrong; Path=/"), "")
        )
        self.assertEqual(
            _extract_csrf_from_response(response("a=1; Path=/, _csrf=right"), ""),
            "right",
        )
        self.assertEqual(
            _extract_csrf_from_response(response("_csrf=first; Path=/"), ""),
            "first",
        )


class TestPostJson(unittest.IsolatedAsyncioTestCase):
    async def test_non_json_body_raises_api_error(self) -> None:
        client = SpServicesClient()
        resp = Mock(status_code=200, ok=True)
        resp.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
        client._request = AsyncMock(return_value=resp)  # type: ignore[method-assign]

        with self.assertRaises(ApiError):
            await client._post_json("https://example.invalid", {})
        await client.close()


class TestParsingFixes(unittest.TestCase):
    def test_unknown_heading_without_blank_line_ends_section(self) -> None:
        csv_text = (
            "Electricity\n"
            "Period,Current,Status\n"
            "2026-01,5,Actual\n"
            "Gas Summary\n"
            "Period,Current,Status\n"
            "2026-01,999,Actual\n"
        )
        points = _section_history_points(
            _parse_titled_csv_sections(csv_text)["electricity"]
        )
        self.assertEqual(points, [UsagePoint("2026-01", 5.0, "Actual")])

    def test_parse_daily_csv_matches_day_first_periods(self) -> None:
        now = datetime(2026, 10, 29, 12, tzinfo=_SG)
        csv_text = (
            "Period,Current\n"
            "29/10/2026 00:30,1.5\n"
            "29/10/2026 01:00,0.5\n"
            "28/10/2026 23:30,9\n"
        )
        self.assertEqual(_parse_daily_csv(csv_text, now), 2.0)

    def test_latest_value_for_day_matches_day_first_periods(self) -> None:
        now = datetime(2026, 10, 29, 12, tzinfo=_SG)
        points = [UsagePoint("28/10/2026", 1.0), UsagePoint("29/10/2026", 3.0)]
        self.assertEqual(_latest_value_for_day(points, now), 3.0)


class TestBrowserHelpers(unittest.TestCase):
    def test_is_callback_url_needs_code_and_state(self) -> None:
        self.assertTrue(
            browser.is_callback_url(
                f"{_CALLBACK}?fromLogin=true&code=a&state=b", _CALLBACK
            )
        )
        self.assertFalse(browser.is_callback_url(f"{_CALLBACK}?code=a", _CALLBACK))
        self.assertFalse(
            browser.is_callback_url(
                "https://evil.example/callback?code=a&state=b", _CALLBACK
            )
        )

    def test_find_browser_env_override(self) -> None:
        with patch.dict(os.environ, {"SP_SERVICES_BROWSER": "/opt/my-chrome"}):
            self.assertEqual(browser.find_browser(), "/opt/my-chrome")

    def test_find_browser_raises_when_none_installed(self) -> None:
        with (
            patch.dict(os.environ, {"SP_SERVICES_BROWSER": ""}),
            patch.object(browser.sys, "platform", "linux"),
            patch.object(browser.shutil, "which", return_value=None),
        ):
            with self.assertRaises(BrowserNotFoundError):
                browser.find_browser()


class TestLoginWithBrowser(unittest.IsolatedAsyncioTestCase):
    async def test_captured_callback_is_exchanged(self) -> None:
        client = SpServicesClient()
        client._bootstrap_auth = AsyncMock(  # type: ignore[method-assign]
            return_value=_AuthContext(
                oauth_state="expected-state",
                login_state="login-state",
                nonce="nonce",
                code_verifier="verifier",
                csrf="csrf",
            )
        )
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"access_token": "token-123", "refresh_token": "r-1"}
        )
        capture = Mock(
            return_value=(
                f"{_CALLBACK}?code=abc&state=expected-state",
                [
                    {
                        "name": "auth0",
                        "value": "session-value",
                        "domain": "identity.spdigital.auth0.com",
                        "path": "/",
                        "expires": -1,
                        "secure": True,
                        "httpOnly": True,
                    }
                ],
            )
        )

        with patch("sp_services.client.capture_login", capture):
            token = await client.login_with_browser(timeout=5)

        self.assertEqual(token, "token-123")
        self.assertIn("state=expected-state", capture.call_args.args[0])
        self.assertEqual(capture.call_args.args[1], _CALLBACK)
        self.assertEqual(
            capture.call_args.kwargs["cookie_host"], "identity.spdigital.auth0.com"
        )
        self.assertEqual(
            [c.name for c in client.export_session().cookies], ["auth0"]
        )
        sent = client._post_json.await_args.args[1]
        self.assertEqual(sent["code"], "abc")
        self.assertEqual(sent["code_verifier"], "verifier")
        await client.close()


class TestTokenCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "tokens.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_round_trip_is_owner_only(self) -> None:
        tokens = TokenSet(
            access_token="a",
            refresh_token="r",
            expires_at=datetime(2026, 10, 29, tzinfo=timezone.utc),
        )
        _save_tokens(self.path, tokens)

        self.assertEqual(_load_tokens(self.path), tokens)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_corrupt_cache_is_ignored(self) -> None:
        self.path.write_text("{not json")
        self.assertIsNone(_load_tokens(self.path))

    async def test_cached_refresh_token_skips_login(self) -> None:
        _save_tokens(self.path, TokenSet(access_token="old", refresh_token="r-1"))
        client = SpServicesClient()
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"access_token": "new"}
        )

        self.assertEqual(await _token_from_cache(client, self.path), "new")
        self.assertEqual(client._post_json.await_args.args[1]["refresh_token"], "r-1")
        await client.close()

    async def test_expired_access_token_without_refresh_is_not_reused(self) -> None:
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        _save_tokens(self.path, TokenSet(access_token="old", expires_at=expired))
        client = SpServicesClient()

        self.assertIsNone(await _token_from_cache(client, self.path))
        await client.close()


if __name__ == "__main__":
    unittest.main()
