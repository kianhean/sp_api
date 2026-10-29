"""Log in to SP Services through your own browser and print usage.

A Chrome/Edge/Chromium/Brave window opens; complete captcha, login and OTP
there. The callback is captured automatically, so nothing needs pasting.
"""

import asyncio
import json
from dataclasses import asdict

from sp_services import SpServicesClient


async def main() -> None:
    async with SpServicesClient() as client:
        token = await client.login_with_browser()

        tokens = client.token_set
        if tokens and tokens.refresh_token:
            # Persist tokens.refresh_token somewhere private and pass it to
            # client.refresh_access_token() next time to skip the browser.
            print("Refresh token obtained (not printed).")
        else:
            print("No refresh token returned; the next run needs a browser login.")

        usage = await client.fetch_usage(token)
        print(json.dumps(asdict(usage), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
