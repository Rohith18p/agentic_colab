"""Run the server: `python -m agentic_colab [--host H] [--port P] [--reload]`.

Same as `uvicorn agentic_colab.main:create_app --factory`, except that on
Ctrl-C open live-update streams are closed right away instead of holding up
the graceful shutdown.

--reload (or RELOAD=1) restarts the server whenever a file in the package
changes (Python, templates, JS, CSS). State lives in the database, agents'
MCP calls are stateless and retry, and open dashboards reload themselves
when they reconnect to a new server process, so a reload loses nothing.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

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
    parser.add_argument("--reload", action="store_true", default=os.environ.get("RELOAD") == "1",
                        help="restart on code, template and static-file changes")
    args = parser.parse_args()
    if args.reload:
        # Open live-update streams would hold up each restart, so cut them off after 1 s.
        uvicorn.run(
            "agentic_colab.main:create_app", factory=True, host=args.host, port=args.port,
            log_level=args.log_level, reload=True, reload_dirs=[str(Path(__file__).parent)],
            reload_includes=["*.py", "*.html", "*.js", "*.css"], timeout_graceful_shutdown=1,
        )
        return
    app = create_app()
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level, timeout_graceful_shutdown=5)
    try:
        _Server(config, app).run()
    except KeyboardInterrupt:  # uvicorn re-raises the captured Ctrl-C after a clean shutdown
        pass


if __name__ == "__main__":
    main()
