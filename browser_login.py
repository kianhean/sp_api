"""Browser-assisted login for SP Services — paste this into a Python session or run it."""

import asyncio
import json
import webbrowser
from dataclasses import asdict

from sp_services import SpServicesClient


async def main():
    async with SpServicesClient() as client:
        # Step 1 — get the Auth0 authorize URL
        auth_url = await client.begin_browser_login()
        print("Opening browser for SP Services login…")
        print(f"URL: {auth_url}\n")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        # Step 2 — complete login + OTP in your browser, then paste the
        # callback URL (the page you land on after OTP verification)
        callback_url = input("Paste the full callback URL: ").strip()

        # Step 3 — exchange the callback for an access token
        token = await client.exchange_callback_url(callback_url)

        # Step 4 — check if you got a refresh token (save it for next time!)
        ts = client.token_set
        if ts:
            print(f"\nAccess token expires: {ts.expires_at}")
            if ts.refresh_token:
                print(f"Refresh token: {ts.refresh_token}")
                print(
                    "\nSave this refresh token! You can use it later with:\n"
                    "  new_tokens = await client.refresh_access_token(refresh_token='...')\n"
                )
            else:
                print(
                    "\nNo refresh token returned — you'll need to re-login "
                    "interactively each time the access token expires.\n"
                )

        # Step 5 — fetch usage
        usage = await client.fetch_usage(token)
        print(json.dumps(asdict(usage), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
