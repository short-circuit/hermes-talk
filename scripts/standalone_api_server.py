"""Start the Hermes API server adapter standalone, for probing without a gateway restart.

Usage: HERMES_HOME=<profile home> API_SERVER_KEY=<key> <hermes venv python> scripts/standalone_api_server.py [port]

This runs the same ``APIServerAdapter`` the gateway hosts, minus the gateway's session
authority, so canonical-mode-only behaviour (pending_controls, ownership refusals) is not
exercised. It exists to smoke-test the probe script, not to replace the real listener.
"""
import asyncio
import os
import sys

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


async def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8642
    cfg = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": port, "key": os.environ.get("API_SERVER_KEY", "")},
    )
    adapter = APIServerAdapter(cfg)
    if not await adapter.connect():
        raise SystemExit("adapter failed to connect")
    print(f"listening on 127.0.0.1:{port}", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
