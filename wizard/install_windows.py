"""
wizard/install_windows.py — Windows guidance (Phase 4 deliverable 4, MVP scope).

Per plan Issue 13 fix: native Windows Services are out of scope for MVP.
Docker Desktop is the supported path on Windows because it handles process
management. This module prints instructions; it installs nothing.
"""

from __future__ import annotations

DOCS_URL = "https://docs.docker.com/desktop/install/windows-install/"


def show_instructions() -> None:
    print("""
=== Windows Deployment (Docker Desktop recommended) ===

Native Windows services are out of scope for the MVP (plan Issue 13).
Docker Desktop handles process management and is the supported path:

1. Install Docker Desktop with WSL2 backend:
   https://docs.docker.com/desktop/install/windows-install/

2. From this project directory (PowerShell):
       docker compose --profile full up -d

3. Verify:
       Gateway : http://localhost:4000/health/liveliness
       Adapter : http://localhost:4001/health
       UI      : http://localhost:4002

The compose stack starts Postgres, Redis, db-init (migrations), the gateway,
the Anthropic adapter, and the web UI in dependency order.
""")


if __name__ == "__main__":
    show_instructions()
