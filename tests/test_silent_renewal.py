from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

from sp_services.browser import _cookie_matches_host
from sp_services.cli import _load_session, _load_tokens, _save_tokens
from sp_services.client import SpServicesClient, _session_cookie_from_cdp
from sp_services.exceptions import ApiError, SessionExpiredError
from sp_services.models import SessionCookie, SessionState, TokenSet

_AUTH0 = "identity.spdigital.auth0.com"
_CALLBACK = "https://services.spservices.sg/callback"


def _session() -> SessionState:
    return SessionState(
        cookies=[
            SessionCookie(
                name="auth0",
                value="session-value",
                domain=_AUTH0,
                expires=time.time() + 3 * 86400,
            ),
            SessionCookie(
                name="did",
                value="device-value",
                domain=_AUTH0,
                expires=time.time() + 365 * 86400,
            ),
        ]
    )


def _redirect(location: str) -> Mock:
    return Mock(status_code=302, headers={"Location": location})


def _state_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


class TestSessionState(unittest.TestCase):
    def test_round_trip(self) -> None:
        state = _session()
        restored = SessionState.from_string(state.to_string())
        self.assertEqual(restored, state)

    def test_string_is_url_safe(self) -> None:
        text = _session().to_string()
        self.assertRegex(text, r"^[A-Za-z0-9_\-=]+$")

    def test_rejects_garbage(self) -> None:
        for bad in ("not base64!", "e30=", "W3sibm9wZSI6IDF9XQ=="):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SessionState.from_string(bad)

    def test_cdp_cookie_conversion(self) -> None:
        cookie = _session_cookie_from_cdp(
            {
                "name": "auth0",
                "value": "v",
                "domain": _AUTH0,
                "path": "/",
                "expires": -1,
                "secure": True,
                "httpOnly": True,
            }
        )
        self.assertIsNone(cookie.expires)
        self.assertTrue(cookie.http_only)

    def test_cookie_host_matching(self) -> None:
        self.assertTrue(_cookie_matches_host(_AUTH0, _AUTH0))
        self.assertTrue(_cookie_matches_host(".spdigital.auth0.com", _AUTH0))
        self.assertFalse(_cookie_matches_host("services.spservices.sg", _AUTH0))
        self.assertFalse(
            _cookie_matches_host("evil-identity.spdigital.auth0.com", _AUTH0)
        )


class TestExportSession(unittest.IsolatedAsyncioTestCase):
    async def test_export_returns_loaded_cookies(self) -> None:
        client = SpServicesClient(session_state=_session())
        exported = client.export_session()
        self.assertEqual(sorted(c.name for c in exported.cookies), ["auth0", "did"])
        await client.close()

    async def test_export_ignores_other_domains_and_expired(self) -> None:
        state = _session()
        state.cookies.append(
            SessionCookie(name="other", value="x", domain="services.spservices.sg")
        )
        state.cookies.append(
            SessionCookie(
                name="stale", value="x", domain=_AUTH0, expires=time.time() - 60
            )
        )
        client = SpServicesClient(session_state=state)
        self.assertEqual(
            sorted(c.name for c in client.export_session().cookies), ["auth0", "did"]
        )
        await client.close()

    async def test_export_reflects_rolled_cookie(self) -> None:
        client = SpServicesClient(session_state=_session())
        client._session.cookies.set("auth0", "rolled-value", domain=_AUTH0, path="/")
        values = {c.name: c.value for c in client.export_session().cookies}
        self.assertEqual(values["auth0"], "rolled-value")
        await client.close()


class TestRenewSilently(unittest.IsolatedAsyncioTestCase):
    async def _client_with(self, responder) -> SpServicesClient:
        client = SpServicesClient(session_state=_session())
        client._request = AsyncMock(side_effect=responder)  # type: ignore[method-assign]
        client._post_json = AsyncMock(  # type: ignore[method-assign]
            return_value={"access_token": "new-token", "expires_in": 3600}
        )
        return client

    async def test_builds_prompt_none_url_and_exchanges_code(self) -> None:
        seen: list[tuple[str, dict]] = []

        async def responder(method, url, **kwargs):
            seen.append((url, kwargs))
            return _redirect(f"{_CALLBACK}?code=abc&state={_state_of(url)}")

        client = await self._client_with(responder)
        tokens = await client.renew_silently()

        self.assertEqual(tokens.access_token, "new-token")
        url, kwargs = seen[0]
        query = parse_qs(urlparse(url).query)
        self.assertTrue(url.startswith(f"https://{_AUTH0}/authorize?"))
        self.assertEqual(query["prompt"], ["none"])
        self.assertEqual(query["response_mode"], ["query"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        for key in ("state", "nonce", "code_challenge", "client_id", "redirect_uri"):
            self.assertTrue(query[key][0], key)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertNotIn("Origin", kwargs["headers"])

        exchange = client._post_json.await_args.args[1]
        self.assertEqual(exchange["code"], "abc")
        self.assertEqual(exchange["grant_type"], "authorization_code")
        await client.close()

    async def test_each_renewal_uses_fresh_state(self) -> None:
        states: list[str] = []

        async def responder(method, url, **kwargs):
            states.append(_state_of(url))
            return _redirect(f"{_CALLBACK}?code=abc&state={states[-1]}")

        client = await self._client_with(responder)
        await client.renew_silently()
        await client.renew_silently()
        self.assertNotEqual(states[0], states[1])
        await client.close()

    async def test_login_required_raises_session_expired(self) -> None:
        for error in ("login_required", "consent_required", "interaction_required"):

            async def responder(method, url, error=error, **kwargs):
                return _redirect(
                    f"{_CALLBACK}?error={error}&error_description=x"
                    f"&state={_state_of(url)}"
                )

            client = await self._client_with(responder)
            with self.subTest(error=error), self.assertRaises(SessionExpiredError):
                await client.renew_silently()
            client._post_json.assert_not_awaited()
            await client.close()

    async def test_other_error_raises_api_error(self) -> None:
        async def responder(method, url, **kwargs):
            return _redirect(f"{_CALLBACK}?error=access_denied&state={_state_of(url)}")

        client = await self._client_with(responder)
        with self.assertRaises(ApiError) as caught:
            await client.renew_silently()
        self.assertNotIsInstance(caught.exception, SessionExpiredError)
        await client.close()

    async def test_code_only_accepted_with_matching_state(self) -> None:
        for callback in (
            f"{_CALLBACK}?code=abc&state=wrong-state",
            f"{_CALLBACK}?code=abc",
        ):

            async def responder(method, url, callback=callback, **kwargs):
                return _redirect(callback)

            client = await self._client_with(responder)
            with self.subTest(callback=callback), self.assertRaises(ApiError):
                await client.renew_silently()
            client._post_json.assert_not_awaited()
            await client.close()

    async def test_follows_multiple_redirects_without_requesting_callback(
        self,
    ) -> None:
        requested: list[str] = []
        oauth_state: list[str] = []

        async def responder(method, url, **kwargs):
            requested.append(url)
            if len(requested) == 1:
                oauth_state.append(_state_of(url))
                return _redirect("/authorize/resume?state=internal")
            if len(requested) == 2:
                return _redirect(f"https://{_AUTH0}/continue?x=1")
            return _redirect(
                f"{_CALLBACK}?fromLogin=true&code=abc&state={oauth_state[0]}"
            )

        client = await self._client_with(responder)
        tokens = await client.renew_silently()

        self.assertEqual(tokens.access_token, "new-token")
        self.assertEqual(len(requested), 3)
        self.assertEqual(
            requested[1], f"https://{_AUTH0}/authorize/resume?state=internal"
        )
        self.assertFalse(any(u.startswith(_CALLBACK) for u in requested))
        await client.close()

    async def test_page_instead_of_redirect_raises_api_error(self) -> None:
        async def responder(method, url, **kwargs):
            return Mock(status_code=200, headers={})

        client = await self._client_with(responder)
        with self.assertRaises(ApiError):
            await client.renew_silently()
        await client.close()

    async def test_redirect_loop_raises_api_error(self) -> None:
        async def responder(method, url, **kwargs):
            return _redirect(f"https://{_AUTH0}/loop")

        client = await self._client_with(responder)
        with self.assertRaises(ApiError):
            await client.renew_silently()
        await client.close()

    async def test_without_cookies_raises_before_any_request(self) -> None:
        client = SpServicesClient()
        client._request = AsyncMock()  # type: ignore[method-assign]
        with self.assertRaises(SessionExpiredError):
            await client.renew_silently()
        client._request.assert_not_awaited()
        await client.close()


class TestCliCache(unittest.TestCase):
    def test_cache_round_trip_with_session_is_owner_only(self) -> None:
        tokens = TokenSet(
            access_token="tok",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session = _session()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            _save_tokens(path, tokens, session)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            loaded_tokens = _load_tokens(path)
            loaded_session = _load_session(path)

        assert loaded_tokens is not None
        self.assertEqual(loaded_tokens.access_token, "tok")
        self.assertEqual(loaded_session, session)

    def test_cache_without_session_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text('{"access_token": "tok", "session": "garbage!"}')
            tokens = _load_tokens(path)
            session = _load_session(path)
        assert tokens is not None
        self.assertEqual(tokens.access_token, "tok")
        self.assertIsNone(session)


if __name__ == "__main__":
    unittest.main()
