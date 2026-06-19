"""
Isolated smoke test for the MS Teams MCP connector — runs OUTSIDE the gateway so it
disrupts nothing. It imports the connector's helpers and exercises read-only calls.

Usage (set the app-registration creds in your shell first; do NOT hardcode them):
    export TEAMS_TENANT_ID=...     TEAMS_CLIENT_ID=...     TEAMS_CLIENT_SECRET=...
    python scripts/test_msteams.py

It will:
  1) acquire an app-only token and print the granted Application roles (from the token),
  2) try a cheap chats read (covered by the Zycus app's current grants),
  3) try a channel read (expected to 403 until app-only/RSC channel scope is added).
No writes are attempted.
"""
import asyncio
import json
import sys

import msteams_mcp_server as t  # noqa: E402  (imported after env is set)


async def main():
    print("== teams_health ==")
    print(await t.teams_health())

    print("\n== teams_list_chats (top 3) ==")
    print(await t.teams_list_chats(top=3))

    print("\n== teams_list_teams (top 3) — may 403 until app-only Group/Team scope added ==")
    print(await t.teams_list_teams(top=3))


if __name__ == "__main__":
    if not (t.TENANT_ID and t.CLIENT_ID and t.CLIENT_SECRET):
        print("Set TEAMS_TENANT_ID / TEAMS_CLIENT_ID / TEAMS_CLIENT_SECRET first.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main())
