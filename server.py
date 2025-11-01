"""Convenience entry point for the AIroad dispatcher."""
from __future__ import annotations

import os

from project.server import app, bootstrap, socketio


def main() -> None:
    """Bootstrap the dispatcher and start the Socket.IO server."""
    bootstrap()
    socketio.run(
        app,
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", "5000")),
    )


if __name__ == "__main__":
    main()
