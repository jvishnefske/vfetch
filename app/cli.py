"""Console entry point: `xfetch` runs the ASGI app under uvicorn.

Honours $XFETCH_HOST / $XFETCH_PORT (same knobs the systemd units use).
"""
import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "app.main:app",
        # Bind all interfaces by default so it's reachable on the LAN. NOTE:
        # /api/proxy is an open relay — add auth before exposing publicly.
        host=os.environ.get("XFETCH_HOST", "0.0.0.0"),
        port=int(os.environ.get("XFETCH_PORT", "8000")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
