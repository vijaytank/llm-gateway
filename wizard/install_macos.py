"""
wizard/install_macos.py — LaunchAgent plist installer (Phase 4 deliverable 4).

Writes ~/Library/LaunchAgents/com.llmgateway.plist, loads it with
launchctl, and prints status. No hardcoded paths.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.llmgateway"
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"


def _render_plist(project_root: Path, python: str) -> bytes:
    root = str(project_root)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            python, "-c",
            "import subprocess, sys, os; "
            "procs=[subprocess.Popen([sys.executable, '-u', '-m', m], "
            f"cwd={root!r}, env=os.environ) for m in ['gateway.main', 'adapter.server', 'ui.app']]; "
            "raise SystemExit(int(any(p.wait() != 0 for p in procs)))",
        ],
        "WorkingDirectory": root,
        "EnvironmentVariables": {},  # launchctl reads .env via the app itself
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / "llm-gateway.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / "llm-gateway.err.log"),
    }
    return plistlib.dumps(plist)


def install(project_root: Path | None = None) -> None:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    python = sys.executable

    plist_dir = PLIST_DIR
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{LABEL}.plist"
    plist_path.write_bytes(_render_plist(root, python))

    subprocess.run(["launchctl", "unload", str(plist_path)], check=False,
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", str(plist_path)], check=False,
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr}")
        return
    status()
    print(f"\nLaunchAgent written to {plist_path}")
    print("Logs: ~/Library/Logs/llm-gateway.log")


def uninstall() -> None:
    plist_path = PLIST_DIR / f"{LABEL}.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], check=False,
                       capture_output=True)
        plist_path.unlink()
        print("LaunchAgent removed.")


def status() -> None:
    result = subprocess.run(["launchctl", "list", LABEL], check=False,
                            capture_output=True, text=True)
    print(result.stdout or result.stderr)


if __name__ == "__main__":
    install()
