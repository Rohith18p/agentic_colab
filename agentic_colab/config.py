"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "sqlite:///./data/workspace.db")
    )
    # How long a claim survives without a heartbeat or task update.
    lease_seconds: float = field(default_factory=lambda: _env_float("LEASE_SECONDS", 90))
    # How often the background job looks for expired leases.
    lease_sweep_seconds: float = field(default_factory=lambda: _env_float("LEASE_SWEEP_SECONDS", 5))
    # An agent with no heartbeat for this long is shown as offline.
    offline_after_seconds: float = field(default_factory=lambda: _env_float("OFFLINE_AFTER_SECONDS", 60))
    # Comma-separated Host headers the MCP endpoint accepts (e.g. "myhost.tailnet.ts.net:*").
    # Empty means DNS-rebinding protection is off and any Host is accepted.
    mcp_allowed_hosts: list[str] = field(
        default_factory=lambda: [
            h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
        ]
    )
