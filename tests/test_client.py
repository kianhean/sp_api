from __future__ import annotations

import unittest
from datetime import datetime

from sp_services.client import (
    _code_from_url,
    _extract_csrf,
    _extract_csrf_from_response,
    _extract_primary_account,
    _parse_daily_csv,
    _parse_monthly_csv,
    _parse_monthly_usage,
)


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

    def test_code_from_url_without_state_still_uses_code(self) -> None:
        url = "https://services.spservices.sg/callback?code=auth-code"
        self.assertEqual(_code_from_url(url, "expected"), "auth-code")

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

    def test_parse_daily_csv(self) -> None:
        now = datetime(2026, 4, 12)
        csv_text = """Period,Current
2026-04-12T00:00:00,1.2
2026-04-12T01:00:00,0.8
2026-04-11T23:00:00,9.9
"""
        electricity_today, water_today = _parse_daily_csv(csv_text, now)
        self.assertEqual(electricity_today, 2.0)
        self.assertIsNone(water_today)


if __name__ == "__main__":
    unittest.main()
