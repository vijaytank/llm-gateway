"""
gateway/env_security.py — .env permission check (Phase 5 security review)

Per plan Phase 5 deliverable 6 / test_env_permissions_check:
    Start gateway with .env having mode 0644 → startup logs a warning.
    Mode 0600 → no warning.

POSIX-only enforcement: Windows filesystems don't carry POSIX modes; there
the check is skipped entirely (documented limitation per Issue 12).
"""

import os
import stat
import sys


def check_env_permissions(env_path: str = ".env") -> bool:
    """Warn if the .env file is readable by group/other.

    Returns True if permissions are OK (or the platform can't enforce them),
    False if a warning was raised. Never raises — a permission problem must
    not block gateway boot.
    """
    try:
        if not os.path.exists(env_path):
            return True  # nothing to protect here
        if sys.platform == "win32":
            # POSIX modes are meaningless on Windows (Issue 12 documented
            # limitation); DPAPI-encrypted storage is post-v1 backlog.
            return True

        mode = os.stat(env_path).st_mode
        world_or_group_readable = bool(mode & (stat.S_IRGRP | stat.S_IROTH))
        if world_or_group_readable:
            print(
                f"[security] WARNING: {env_path} is readable by group/other "
                f"(mode {stat.filemode(mode)}). API keys could be exposed to "
                f"local users. Fix with: chmod 600 {env_path}"
            )
            return False
        return True
    except Exception as e:
        print(f"[security] WARNING: could not check {env_path} permissions ({e})")
        return True  # fail-open: never block boot on a checker error
