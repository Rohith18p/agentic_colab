"""Run the server: `python -m agentic_colab [--host H] [--port P]`.

Same as `uvicorn agentic_colab.main:create_app --factory`, except that on
Ctrl-C open live-update streams are closed right away instead of holding up
the graceful shutdown.
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from .main import create_app


class _Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, app) -> None:
        super().__init__(config)
        self._app = app

    def handle_exit(self, sig, frame) -> None:
        self._app.state.workspace.notifier.close()
        super().handle_exit(sig, frame)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m agentic_colab")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    app = create_app()
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level, timeout_graceful_shutdown=5)
    try:
        _Server(config, app).run()
    except KeyboardInterrupt:  # uvicorn re-raises the captured Ctrl-C after a clean shutdown
        pass


if __name__ == "__main__":
    main()
