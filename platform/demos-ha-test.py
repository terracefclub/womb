"""Guest-only test client: credentials never enter the VM driver's command log."""

import asyncio
import json
import os
from pathlib import Path
import secrets
import sys

import aiohttp

ORIGIN = "http://127.0.0.1:8123"
TOKEN = Path("/run/demos-test-token")


async def main():
    os.umask(0o077)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15), raise_for_status=True
    ) as session:
        action = sys.argv[1]
        if action == "onboard":
            async with session.post(
                ORIGIN + "/api/onboarding/users",
                json={
                    "client_id": ORIGIN + "/",
                    "name": "Test owner",
                    "username": "testowner",
                    "password": secrets.token_hex(32),
                    "language": "en",
                },
            ) as response:
                code = (await response.json())["auth_code"]
            async with session.post(
                ORIGIN + "/auth/token",
                data={
                    "client_id": ORIGIN + "/",
                    "code": code,
                    "grant_type": "authorization_code",
                },
            ) as response:
                TOKEN.write_text((await response.json())["access_token"])
            return

        token = TOKEN.read_text()
        if action in ("analytics", "identity"):
            async with session.ws_connect(ORIGIN + "/api/websocket") as socket:
                assert (await socket.receive_json())["type"] == "auth_required"
                await socket.send_json({"type": "auth", "access_token": token})
                assert (await socket.receive_json())["type"] == "auth_ok"
                await socket.send_json({"id": 1, "type": "analytics" if action == "analytics" else "auth/current_user"})
                result = await socket.receive_json()
                assert result["success"]
                value = result["result"]
                if action == "identity":
                    value = {key: value[key] for key in ["id", "name", "is_owner", "is_admin"]}
                    assert value["is_owner"] and value["is_admin"]
                print(json.dumps(value, sort_keys=True))
        elif action in ("states", "config"):
            async with session.get(
                ORIGIN + "/api/" + action,
                headers={"Authorization": "Bearer " + token},
            ) as response:
                print(json.dumps(await response.json()))
        else:
            raise ValueError("unknown action")


try:
    asyncio.run(main())
except Exception as error:
    # Library errors can echo request data; only the class is safe in build logs.
    print("Home Assistant test request failed: " + type(error).__name__, file=sys.stderr)
    sys.exit(1)
